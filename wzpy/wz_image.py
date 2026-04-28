"""WzImage — a single .img inside a WZ container.

The image header byte tells us whether the body is a SubProperty (the common
case) or some other container. The property tree is parsed lazily on first
access since some WZ files contain thousands of images.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, List, Optional

from .properties import (
    WzProperty,
    WzSubProperty,
    parse_property_list,
)

if TYPE_CHECKING:
    from .crypto import WzKey
    from .wz_file import WzDirectory, WzFile


class _StandaloneWzFile:
    """Minimal stand-in for :class:`WzFile` used by :meth:`WzImage.from_bytes`.

    The .img parser only ever reaches for ``self._wz_file.reader`` while
    parsing, so a tiny shim suffices and we avoid pretending to be a full
    parsed WZ container.
    """

    __slots__ = ("reader",)

    def __init__(self, reader):
        self.reader = reader


class WzImage:
    def __init__(
        self,
        name: str,
        parent: "WzDirectory",
        offset: int,
        size: int,
        wz_file: "WzFile",
    ):
        self.name = name
        self.parent = parent
        self.offset = offset
        self.size = size
        self._wz_file = wz_file
        self._parsed = False
        self._root: Optional[WzSubProperty] = None
        # Set by parse_property_list when it hits EOF mid-property — tells the
        # caller the parse returned partial results and the file is truncated.
        self.truncated: bool = False

    @classmethod
    def from_bytes(cls, data: bytes, *, key: "WzKey",
                   name: str = "image.img") -> "WzImage":
        """Create a standalone :class:`WzImage` from raw bytes.

        Useful for ``.img`` files extracted from a WZ container by tools
        like HaRepacker. Inside an ``.img`` the property-list offsets are
        local to the image's start (``base_offset = 0``) and the WZ-level
        ``version_hash``/``header_fstart`` are unused, so only the cipher
        ``key`` matters. Pick one with :meth:`WzKey.for_region`,
        :class:`StaticWzKey`, or :func:`detect_region_from_img`.
        """
        from .reader import WzBinaryReader
        reader = WzBinaryReader(io.BytesIO(data), key)
        return cls(name=name, parent=None, offset=0, size=len(data),
                   wz_file=_StandaloneWzFile(reader))

    @property
    def wz_file(self) -> "WzFile":
        return self._wz_file

    @property
    def path(self) -> str:
        if self.parent is None:
            return self.name
        parent_path = self.parent.path
        return f"{parent_path}/{self.name}" if parent_path else self.name

    # ── lazy parse ──────────────────────────────────────────────────
    def parse(self) -> WzSubProperty:
        if self._parsed and self._root is not None:
            return self._root
        r = self._wz_file.reader
        r.seek(self.offset)
        tag = r.read_byte()
        # Identifier byte: 0x73 (Property/SubProperty) is by far the most
        # common. Read the type name to confirm.
        if tag != 0x73:
            # Some images don't follow this layout; fall back to an empty root
            self._root = WzSubProperty(self.name)
            self._parsed = True
            return self._root
        type_name = r.read_string()
        r.skip(2)  # reserved (matches Property block)
        if type_name != "Property":
            self._root = WzSubProperty(self.name)
            self._parsed = True
            return self._root
        root = WzSubProperty(self.name)
        for child in parse_property_list(r, self.offset, root, self):
            root.add(child)
        self._root = root
        self._parsed = True
        return root

    # ── access ──────────────────────────────────────────────────────
    @property
    def root(self) -> WzSubProperty:
        return self.parse()

    def children(self) -> List[WzProperty]:
        return self.root.children()

    def get(self, path: str) -> Optional[WzProperty]:
        return self.root.get(path)
