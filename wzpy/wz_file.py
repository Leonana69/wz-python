"""WzFile and WzDirectory parsing.

Parses the legacy 32-bit WZ container format (PKG1 header + nested directories
+ embedded .img files). 64-bit ``Data/`` layouts and ``.ms`` Snowcrypt packs
are out of scope for this minimal implementation but the directory model is
the same once unwrapped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from .crypto import WzKey, WZ_IV, compute_version_hash, derive_version_check
from .reader import WzBinaryReader
from .wz_image import WzImage


# ── header ─────────────────────────────────────────────────────────────
@dataclass
class WzHeader:
    ident: str
    fsize: int
    fstart: int
    copyright: str


# ── tree nodes ─────────────────────────────────────────────────────────
class WzNode:
    """Common base for directories and images.

    Held weakly in mind: every node knows its name, parent, and full path so
    the web UI can navigate without holding a separate index.
    """

    def __init__(self, name: str, parent: Optional["WzNode"] = None):
        self.name = name
        self.parent = parent

    @property
    def path(self) -> str:
        parts: List[str] = []
        node: Optional[WzNode] = self
        while node is not None and node.parent is not None:
            parts.append(node.name)
            node = node.parent
        return "/".join(reversed(parts))


class WzDirectory(WzNode):
    def __init__(self, name: str, parent: Optional[WzNode] = None):
        super().__init__(name, parent)
        self.subdirs: Dict[str, "WzDirectory"] = {}
        self.images: Dict[str, WzImage] = {}

    # ── lookup ──────────────────────────────────────────────────────
    def child(self, name: str) -> Optional[WzNode]:
        if name in self.subdirs:
            return self.subdirs[name]
        if name in self.images:
            return self.images[name]
        return None

    def get(self, path: str) -> Optional[WzNode]:
        if not path:
            return self
        node: Optional[WzNode] = self
        for part in path.split("/"):
            if part == "":
                continue
            if not isinstance(node, WzDirectory):
                return None
            node = node.child(part)
            if node is None:
                return None
        return node

    def walk_images(self, prefix: str = "") -> Iterator[Tuple[str, WzImage]]:
        """Yield ``(relative_path, image)`` for every ``.img`` in this
        directory and its subdirectories. Subdirs are walked first in
        insertion order (matching the order they appear in the WZ
        directory listing), then images at the current level."""
        for name, sub in self.subdirs.items():
            yield from sub.walk_images(f"{prefix}/{name}" if prefix else name)
        for name, img in self.images.items():
            yield (f"{prefix}/{name}" if prefix else name), img


# ── parser ─────────────────────────────────────────────────────────────
class WzFile:
    """A parsed WZ container.

    Use :py:meth:`open` to load a file; the directory tree is parsed eagerly
    (it's small) but image properties are parsed lazily on access.
    """

    def __init__(self, path: str, region: str = "GMS", version: Optional[int] = None):
        self.path = path
        self.region = region
        self.version = version  # may be None until detected
        self.header: Optional[WzHeader] = None
        self.root = WzDirectory(name="")
        self._fp = None
        self._reader: Optional[WzBinaryReader] = None

    # ── lifecycle ───────────────────────────────────────────────────
    @classmethod
    def open(cls, path: str, region: str = "GMS", version: Optional[int] = None) -> "WzFile":
        wz = cls(path, region=region, version=version)
        wz._load()
        return wz

    def close(self) -> None:
        if getattr(self, "_mmap", None) is not None:
            self._mmap.close()
            self._mmap = None
        if self._fp is not None:
            self._fp.close()
            self._fp = None
            self._reader = None

    def __enter__(self) -> "WzFile":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ── reader access (used by lazy WzImage parsing) ────────────────
    @property
    def reader(self) -> WzBinaryReader:
        assert self._reader is not None
        return self._reader

    # ── parsing ─────────────────────────────────────────────────────
    def _load(self) -> None:
        import mmap
        self._fp = open(self.path, "rb")
        # Memory-map the whole file so seeks are O(1). Critical for tree
        # browsing of multi-GB _Canvas files where each .img parse does
        # thousands of small seek+read calls.
        self._mmap = mmap.mmap(self._fp.fileno(), 0, access=mmap.ACCESS_READ)
        key = WzKey.for_region(self.region)
        r = WzBinaryReader(self._mmap, key)
        self._reader = r

        # header
        ident = r.read(4).decode("ascii", errors="replace")
        fsize = r.read_u64()
        fstart = r.read_u32()
        copyright_bytes = bytearray()
        while True:
            b = r.read_byte()
            if b == 0:
                break
            copyright_bytes.append(b)
            if len(copyright_bytes) > 1024:
                raise ValueError("copyright string too long; not a WZ file?")
        self.header = WzHeader(
            ident=ident,
            fsize=fsize,
            fstart=fstart,
            copyright=copyright_bytes.decode("latin-1", errors="replace"),
        )
        r.header_fstart = fstart

        # encrypted version
        encrypted_version = r.read_u16()
        body_start = r.position

        # detect version + region
        version, version_hash = self._detect_version(encrypted_version, body_start, key)
        self.version = version
        r.version_hash = version_hash

        # parse root directory
        r.seek(body_start)
        self._parse_directory(self.root)

    def _detect_version(
        self,
        encrypted_version: int,
        body_start: int,
        key: WzKey,
    ) -> Tuple[int, int]:
        # If a specific version was requested, just verify and use it.
        if self.version is not None:
            h = compute_version_hash(self.version)
            if derive_version_check(h) != encrypted_version:
                # don't fail hard — sometimes the user knows better
                pass
            return self.version, h

        # Search for any version whose check byte matches the header. For each
        # candidate, briefly try parsing a few directory entries and score by
        # how many strings decode to printable ASCII.
        candidates: List[Tuple[int, int]] = []
        for ver in range(1, 1000):
            h = compute_version_hash(ver)
            if derive_version_check(h) == encrypted_version:
                candidates.append((ver, h))

        if not candidates:
            raise ValueError("could not match WZ version (encrypted=0x%X)" % encrypted_version)

        best: Optional[Tuple[int, int, float]] = None
        for ver, h in candidates:
            score = self._score_version(body_start, h, key)
            if best is None or score > best[2]:
                best = (ver, h, score)
        assert best is not None
        return best[0], best[1]

    def _score_version(self, body_start: int, version_hash: int, key: WzKey) -> float:
        """Speculatively decode the root directory; score by printable ratio."""
        r = self._reader
        assert r is not None
        keep = r.position
        r.version_hash = version_hash
        r.seek(body_start)
        printable = 0
        total = 0
        try:
            count = r.read_compressed_int()
            count = min(count, 50)
            for _ in range(count):
                kind = r.read_byte()
                if kind == 1:
                    r.skip(10)
                    continue
                if kind == 2:
                    string_offset = r.read_i32()
                    name = r.read_string_at(body_start - 1 + string_offset)
                    new_kind = ord("?")
                    # placeholder: name decoded above
                elif kind in (3, 4):
                    name = r.read_string()
                else:
                    return -1.0
                r.read_compressed_int()  # size
                r.read_compressed_int()  # checksum
                r.read_offset()
                for ch in name:
                    total += 1
                    if 0x20 <= ord(ch) < 0x7F:
                        printable += 1
        except Exception:
            return -1.0
        finally:
            r.seek(keep)
        if total == 0:
            return 0.0
        return printable / total

    def _parse_directory(self, directory: WzDirectory) -> None:
        r = self._reader
        assert r is not None
        count = r.read_compressed_int()

        # Pass 1: collect entries (name, kind, size, offset). The entries don't
        # store nested data inline — they all point elsewhere in the file.
        entries: List[Tuple[str, int, int, int]] = []
        for _ in range(count):
            kind = r.read_byte()
            name = ""
            if kind == 1:
                # unknown, 10 bytes follow then continue
                r.skip(10)
                continue
            if kind == 2:
                string_offset = r.read_i32()
                # absolute name location: header.fstart + 1 + string_offset
                name_pos = (self.header.fstart + 1 + string_offset) & 0xFFFFFFFF
                name_kind_pos = name_pos
                # peek into the indirected entry header to get the real kind
                keep = r.position
                r.seek(name_kind_pos)
                real_kind = r.read_byte()
                name = r.read_string()
                r.seek(keep)
                kind = real_kind
            elif kind in (3, 4):
                name = r.read_string()
            else:
                # unknown — skip the rest of the directory rather than crashing
                continue

            size = r.read_compressed_int()
            checksum = r.read_compressed_int()
            offset = r.read_offset()
            entries.append((name, kind, size, offset))

        # Pass 2: recurse / register
        for name, kind, size, offset in entries:
            if kind == 3:
                sub = WzDirectory(name, parent=directory)
                directory.subdirs[name] = sub
                keep = r.position
                r.seek(offset)
                self._parse_directory(sub)
                r.seek(keep)
            elif kind == 4:
                img = WzImage(name=name, parent=directory, offset=offset, size=size, wz_file=self)
                directory.images[name] = img
