"""Reading MapleStory ``.ms`` packs (Snow2-encrypted WZ image archives).

A ``.ms`` file is a custom encrypted archive that stores a set of WZ ``.img``
images. It is the modern packaging used by some 64-bit clients for large data
sets (``Mob``, ``Skill``, ...), where a single logical category is split across
several numbered files (``Mob_00000.ms`` … ``Mob_00003.ms``). A sibling
``Packs.ini`` lists ``Category|maxIndex`` lines.

Format (port of ``MapleLib/WzLib/MSFile``; credits Elem8100 / WzComparerR2):

  1. An unencrypted prefix: ``randByteCount`` random bytes (count derived from
     the lowercased filename), a 4-byte obfuscated salt length, then the salt
     bytes XOR-masked with the random bytes. The recovered salt + filename seed
     every key below.
  2. A SNOW 2.0-encrypted header (``hash`` i32, ``version`` byte == 2,
     ``entryCount`` i32) keyed by ``DeriveSnowKey(name+salt, header)``.
  3. After ``9 + padAmount`` bytes, a SNOW 2.0-encrypted entry table keyed by
     ``DeriveSnowKey(name+salt, entry)``; each record carries the image name
     (``Category/xxxxx.img``), size, aligned size, a block index, and a random
     16-byte per-entry key.
  4. Page-aligned (1024) image data. Each image is SNOW 2.0-encrypted with a
     per-image key derived from the salt hash, image name and entry key; the
     first 1024 bytes are encrypted twice.

The decrypted payload of each entry is an ordinary legacy 32-bit ``.img`` body
parsed with the BMS cipher (zero IV) — :class:`wzpy.wz_image.WzImage` reads it
unchanged. Images are decrypted lazily on first parse so opening a pack only
touches headers + entry tables.

:class:`MsPackage` stitches every ``.ms`` in a folder into one virtual
:class:`~wzpy.wz_file.WzDirectory` tree, exposing the same read surface as
:class:`~wzpy.wz_package.WzPackage` (``root``, ``get``, ``region``, ``close``,
context manager) so the server and renderer consume it unchanged.
"""

from __future__ import annotations

import io
import os
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

from .crypto import WzKey
from .reader import WzBinaryReader
from .snow2 import Snow2Decryptor, snow_decrypt
from .wz_file import WzDirectory
from .wz_image import WzImage


# ── format constants (WzMsConstants.cs) ─────────────────────────────────
_SUPPORTED_VERSION = 2
_SNOW_KEY_LEN = 16
_BLOCK_ALIGNMENT = 1024
_PAGE_MASK = 0x3FF
_RAND_BYTE_MOD = 312
_RAND_BYTE_OFFSET = 30
_HEADER_PAD_MOD = 212
_HEADER_PAD_OFFSET = 33
_INITIAL_KEY_HASH = 0x811C9DC5
_KEY_HASH_MULTIPLIER = 0x1000193
_DOUBLE_ENCRYPT_INITIAL_BYTES = 1024
_MASK = 0xFFFFFFFF


def _align_page(pos: int) -> int:
    return (pos + _PAGE_MASK) & ~_PAGE_MASK


# ── entry metadata ──────────────────────────────────────────────────────
@dataclass
class MsEntry:
    name: str          # e.g. "Mob/0100100.img"
    check_sum: int
    flags: int
    block_index: int   # block offset within the data region
    size: int          # decrypted (== on-disk) payload byte count
    size_aligned: int  # payload footprint rounded up to 1024
    unk1: int
    unk2: int
    entry_key: bytes   # 16-byte per-entry key
    start_pos: int = 0  # absolute file offset of the payload (filled in later)


# ── key derivation (WzMsFile.cs) ────────────────────────────────────────
def _derive_snow_key(name_with_salt: str, *, entry_key: bool) -> bytes:
    n = len(name_with_salt)
    key = bytearray(_SNOW_KEY_LEN)
    if not entry_key:
        for i in range(_SNOW_KEY_LEN):
            key[i] = (ord(name_with_salt[i % n]) + i) & 0xFF
    else:
        for i in range(_SNOW_KEY_LEN):
            ch = ord(name_with_salt[n - 1 - i % n])
            key[i] = (i + (i % 3 + 2) * ch) & 0xFF
    return bytes(key)


def _derive_img_key(salt: str, entry: MsEntry) -> bytes:
    key_hash = _INITIAL_KEY_HASH
    for ch in salt:
        key_hash = ((key_hash ^ ord(ch)) * _KEY_HASH_MULTIPLIER) & _MASK
    digits = [ord(c) - ord("0") for c in str(key_hash)]
    nd = len(digits)
    name = entry.name
    nn = len(name)
    ek = entry.entry_key
    ne = len(ek)

    img_key = bytearray(_SNOW_KEY_LEN)
    for i in range(_SNOW_KEY_LEN):
        digit_idx = i % nd
        entry_key_idx = (digits[(i + 2) % nd] + i) % ne
        img_key[i] = (
            i + ord(name[i % nn]) * (
                (digits[digit_idx] % 2)
                + ek[entry_key_idx]
                + ((digits[(i + 1) % nd] + i) % 5)
            )
        ) & 0xFF
    return bytes(img_key)


# ── single .ms file ─────────────────────────────────────────────────────
class MsFile:
    """A single parsed ``.ms`` archive.

    Reads the header and entry table eagerly; image payloads are decrypted on
    demand via :meth:`read_entry_data`. ``region`` is fixed to ``BMS`` (zero
    IV) since ``.ms`` payloads are always BMS-encrypted WZ images.
    """

    def __init__(self, path: str):
        self.path = path
        self.file_name = os.path.basename(path)
        self.salt: str = ""
        self.name_with_salt: str = ""
        self.entry_count: int = 0
        self.header_hash: int = 0
        self.data_start: int = 0
        self.entries: List[MsEntry] = []
        self._mmap = None
        self._fp = None
        self._lock = threading.RLock()

    # ── lifecycle ───────────────────────────────────────────────────────
    @classmethod
    def open(cls, path: str) -> "MsFile":
        ms = cls(path)
        ms._load()
        return ms

    def close(self) -> None:
        if self._mmap is not None:
            try:
                self._mmap.close()
            except Exception:
                pass
            self._mmap = None
        if self._fp is not None:
            self._fp.close()
            self._fp = None

    def __enter__(self) -> "MsFile":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ── header + entry parsing ──────────────────────────────────────────
    def _load(self) -> None:
        import mmap
        self._fp = open(self.path, "rb")
        self._mmap = mmap.mmap(self._fp.fileno(), 0, access=mmap.ACCESS_READ)
        self._read_header()
        self._read_entries()

    def _read_header(self) -> None:
        mm = self._mmap
        file_name = self.file_name.lower()

        rand_count = sum(ord(c) for c in file_name) % _RAND_BYTE_MOD + _RAND_BYTE_OFFSET
        pos = 0
        rand_bytes = mm[pos:pos + rand_count]
        pos += rand_count

        hashed_salt_len = int.from_bytes(mm[pos:pos + 4], "little", signed=True)
        pos += 4
        salt_len = (hashed_salt_len & 0xFF) ^ rand_bytes[0]
        if salt_len <= 0 or salt_len > 4096:
            raise ValueError(
                f"{self.file_name}: implausible salt length {salt_len} "
                "(not a .ms file or wrong filename?)"
            )
        salt_bytes = mm[pos:pos + salt_len * 2]
        pos += salt_len * 2
        salt = "".join(
            chr(rand_bytes[i] ^ salt_bytes[i * 2]) for i in range(salt_len)
        )
        self.salt = salt
        self.name_with_salt = file_name + salt

        header_start = pos

        # Encrypted header: hash(i32), version(byte), entryCount(i32).
        header_key = _derive_snow_key(self.name_with_salt, entry_key=False)
        dec = Snow2Decryptor(mm[header_start:header_start + 16], header_key)
        header_hash = dec.read_i32()
        version = dec.read_byte()
        entry_count = dec.read_i32()
        if version != _SUPPORTED_VERSION:
            raise ValueError(
                f"{self.file_name}: decrypted .ms header version is {version}, "
                f"expected {_SUPPORTED_VERSION}. This file is not in the "
                f"Snow2 .ms format that MapleLib/Elem8100 (and this reader) "
                f"implement — either the filename differs from the one used to "
                f"key it, or it is a different .ms variant entirely."
            )

        # Validate the header hash (cheap integrity / wrong-key guard).
        actual_hash = hashed_salt_len + version + entry_count
        for i in range(salt_len):
            actual_hash += salt_bytes[i * 2] | (salt_bytes[i * 2 + 1] << 8)
        actual_hash &= _MASK
        if (header_hash & _MASK) != actual_hash:
            raise ValueError(
                f"{self.file_name}: header hash mismatch "
                f"(got {header_hash & _MASK}, expected {actual_hash})"
            )

        self.header_hash = header_hash
        self.entry_count = entry_count

        pad_amount = (
            sum(ord(c) * 3 for c in file_name) % _HEADER_PAD_MOD
            + _HEADER_PAD_OFFSET
        )
        self._entry_start = header_start + 9 + pad_amount

    def _read_entries(self) -> None:
        if self.entry_count == 0:
            self.data_start = _align_page(self._entry_start)
            return
        mm = self._mmap
        entry_key = _derive_snow_key(self.name_with_salt, entry_key=True)
        # The entry table is < entry_count * (48 + 2*name) bytes; read a
        # generous slice and let the sequential decryptor stop where it likes.
        # Image names are short (~16 chars) so 256 bytes/entry is ample.
        approx = self.entry_count * 512 + 4096
        raw = mm[self._entry_start:self._entry_start + approx]
        dec = Snow2Decryptor(raw, entry_key)

        entries: List[MsEntry] = []
        table_bytes = 0
        for _ in range(self.entry_count):
            name_len = dec.read_i32()
            if name_len < 0 or name_len > 4096:
                raise ValueError(
                    f"{self.file_name}: implausible entry name length {name_len}"
                )
            name = dec.read(name_len * 2).decode("utf-16-le")
            check_sum = dec.read_i32()
            flags = dec.read_i32()
            block_index = dec.read_i32()
            size = dec.read_i32()
            size_aligned = dec.read_i32()
            unk1 = dec.read_i32()
            unk2 = dec.read_i32()
            ek = dec.read(_SNOW_KEY_LEN)
            entries.append(MsEntry(
                name=name, check_sum=check_sum, flags=flags,
                block_index=block_index, size=size, size_aligned=size_aligned,
                unk1=unk1, unk2=unk2, entry_key=ek,
            ))
            table_bytes += 48 + name_len * 2

        # Data starts at the page boundary after the entry table.
        self.data_start = _align_page(self._entry_start + table_bytes)
        for e in entries:
            e.start_pos = self.data_start + e.block_index * _BLOCK_ALIGNMENT
        self.entries = entries

    # ── payload decryption ──────────────────────────────────────────────
    def read_entry_data(self, entry: MsEntry) -> bytes:
        """Decrypt and return the raw ``.img`` bytes for ``entry``.

        The first 1024 bytes are double-decrypted; the rest single-decrypted,
        per the ``.ms`` payload scheme. Result length == ``entry.size``.
        """
        img_key = _derive_img_key(self.salt, entry)
        n_words = (entry.size + 3) // 4
        read_len = n_words * 4
        end = entry.start_pos + read_len
        # Clamp to the mapped length (the final block is page-padded on disk,
        # but guard anyway).
        if end > len(self._mmap):
            read_len = len(self._mmap) - entry.start_pos
        with self._lock:
            cipher = bytes(self._mmap[entry.start_pos:entry.start_pos + read_len])
        plain = snow_decrypt(
            cipher, img_key,
            double_prefix=_DOUBLE_ENCRYPT_INITIAL_BYTES,
        )
        return plain[:entry.size]


# ── lazy image backing ──────────────────────────────────────────────────
class _MsImageFile:
    """Per-image shim presenting the ``wz_file`` surface :class:`WzImage`
    needs (``reader`` + ``reader_lock``), decrypting the payload lazily on the
    first parse and caching the resulting reader."""

    __slots__ = ("_ms", "_entry", "_region", "_reader", "reader_lock")

    def __init__(self, ms: MsFile, entry: MsEntry, region: str):
        self._ms = ms
        self._entry = entry
        self._region = region
        self._reader: Optional[WzBinaryReader] = None
        self.reader_lock = threading.RLock()

    @property
    def reader(self) -> WzBinaryReader:
        r = self._reader
        if r is not None:
            return r
        with self.reader_lock:
            if self._reader is None:
                data = self._ms.read_entry_data(self._entry)
                self._reader = WzBinaryReader(
                    io.BytesIO(data), WzKey.for_region(self._region),
                )
            return self._reader


# ── multi-file pack ─────────────────────────────────────────────────────
class MsPackage:
    """A virtual WZ tree composed of the ``.ms`` files in a folder.

    Drop-in compatible with :class:`wzpy.wz_package.WzPackage` for read-only
    use: exposes ``root`` (a :class:`WzDirectory`), ``get``, ``region``,
    ``version``, ``close`` and the context-manager protocol. Each entry's
    ``Category/name.img`` is placed at ``root / Category / name.img``.
    """

    def __init__(self, path: str, region: str = "BMS",
                 version: Optional[int] = None):
        self.path = path
        self.region = region
        self.version = version
        self.root = WzDirectory(name="")
        self._files: List[MsFile] = []

    @classmethod
    def open(cls, path: str, region: str = "BMS",
             version: Optional[int] = None) -> "MsPackage":
        """Open every ``.ms`` file at ``path``.

        ``path`` may be a folder (load all ``*.ms`` inside) or a single
        ``.ms`` file (load just that one).
        """
        pkg = cls(path=path, region=region, version=version)
        pkg._load()
        return pkg

    def close(self) -> None:
        for f in self._files:
            try:
                f.close()
            except Exception:
                pass
        self._files = []

    def __enter__(self) -> "MsPackage":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def get(self, path: str):
        return self.root.get(path)

    # ── load ────────────────────────────────────────────────────────────
    def _load(self) -> None:
        ms_paths = _list_ms_files(self.path)
        if not ms_paths:
            raise ValueError(f"no .ms files found at {self.path!r}")
        for p in ms_paths:
            ms = MsFile.open(p)
            self._files.append(ms)
            for entry in ms.entries:
                self._add_entry(ms, entry)

    def _add_entry(self, ms: MsFile, entry: MsEntry) -> None:
        parts = entry.name.replace("\\", "/").split("/")
        img_name = parts[-1]
        directory = self.root
        for seg in parts[:-1]:
            sub = directory.subdirs.get(seg)
            if sub is None:
                sub = WzDirectory(seg, parent=directory)
                directory.subdirs[seg] = sub
            directory = sub
        image = WzImage(
            name=img_name, parent=directory, offset=0, size=entry.size,
            wz_file=_MsImageFile(ms, entry, self.region),
        )
        directory.images[img_name] = image


# ── helpers ─────────────────────────────────────────────────────────────
def _list_ms_files(path: str) -> List[str]:
    """Return the ``.ms`` files at ``path`` (a single file or a folder),
    sorted by (category, numeric index) so a category's parts load in order."""
    if os.path.isfile(path):
        return [path]
    if not os.path.isdir(path):
        return []
    names = [n for n in os.listdir(path) if n.lower().endswith(".ms")]

    import re
    idx_re = re.compile(r"^(.*?)_(\d+)\.ms$", re.IGNORECASE)

    def sort_key(n: str):
        m = idx_re.match(n)
        if m:
            return (m.group(1).lower(), int(m.group(2)))
        return (n.lower(), -1)

    names.sort(key=sort_key)
    return [os.path.join(path, n) for n in names]


def is_ms_path(path: str) -> bool:
    """True if ``path`` is a ``.ms`` file or a folder containing ``.ms``
    files (used by :func:`wzpy.wz_package.open_wz` to dispatch)."""
    if os.path.isfile(path):
        return path.lower().endswith(".ms")
    if os.path.isdir(path):
        try:
            return any(n.lower().endswith(".ms") for n in os.listdir(path))
        except OSError:
            return False
    return False
