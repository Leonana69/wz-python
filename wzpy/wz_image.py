"""WzImage — a single .img inside a WZ container.

The image header byte tells us whether the body is a SubProperty (the common
case) or some other container. The property tree is parsed lazily on first
access since some WZ files contain thousands of images.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from .properties import (
    WzProperty,
    WzSubProperty,
    parse_property_list,
)

if TYPE_CHECKING:
    from .wz_file import WzDirectory, WzFile


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
