"""MapleStory map loading and raster composition.

This module ports the data-loading and draw-order portion of HaCreator's
``MapLoader``/``BoardItemsManager`` to Pillow.  It intentionally models the
legacy (pre-64-bit) map schema first: eight tile/object layers, backgrounds,
life, reactors, portal editor markers, footholds, and ropes.

The renderer accepts WZ-like sources (``WzFile`` or ``WzPackage``) rather than
opening paths internally.  Keeping source discovery separate is important for
the next phase: modern clients split the same logical components across many
files, while the scene compositor should not need to care where an image came
from.
"""

from __future__ import annotations

import io
import math
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

from .canvas import decode_canvas
from .properties import (
    WzCanvasProperty,
    WzProperty,
    WzSubProperty,
    WzUolProperty,
    WzVectorProperty,
)
from .wz_package import resolve_canvas_link


LEGACY_LAYER_COUNT = 8
MAX_RENDER_PIXELS = 64_000_000
MAX_CANVAS_CACHE_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class MapBounds:
    """A half-open rectangle in MapleStory world coordinates."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def as_dict(self) -> Dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
            "width": self.width,
            "height": self.height,
        }


def _value(node: Optional[WzProperty], default: Any = None) -> Any:
    if node is None:
        return default
    value = getattr(node, "value", default)
    return default if value is None else value


def _int(node: Optional[WzProperty], default: int = 0) -> int:
    try:
        return int(_value(node, default))
    except (TypeError, ValueError):
        return default


def _str(node: Optional[WzProperty], default: str = "") -> str:
    value = _value(node, default)
    return default if value is None else str(value)


def _vector(node: Optional[WzProperty]) -> Tuple[int, int]:
    if isinstance(node, WzVectorProperty):
        return int(node.x), int(node.y)
    value = _value(node)
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return int(value[0]), int(value[1])
    return 0, 0


def _numeric_children(node: Optional[WzProperty]) -> List[WzProperty]:
    if node is None:
        return []
    children = [child for child in node.children() if child.name.isdigit()]
    children.sort(key=lambda child: int(child.name))
    return children


def _normalize_map_id(map_id: Any) -> str:
    text = str(map_id).strip()
    if not text or not text.isdigit():
        raise ValueError(f"invalid map id {map_id!r}")
    return str(int(text))


class MapRenderer:
    """Load and compose maps from logical MapleStory WZ components.

    ``map_source`` is required.  The other sources are optional: omitting
    String removes display-name search, while omitting Mob/Npc/Reactor simply
    skips those sprites.  This makes a lone ``Map.wz`` useful and lets the web
    app report partial bundles without failing the whole builder.
    """

    def __init__(
        self,
        map_source: Any,
        *,
        string_source: Any = None,
        mob_source: Any = None,
        npc_source: Any = None,
        reactor_source: Any = None,
        region: Optional[str] = None,
        owned_sources: Sequence[Any] = (),
    ) -> None:
        self.map = map_source
        self.string = string_source
        self.mob = mob_source
        self.npc = npc_source
        self.reactor = reactor_source
        self.region = region or getattr(map_source, "region", "GMS")
        self._owned_sources = list(owned_sources)
        self._lock = threading.RLock()
        self._name_index: Optional[List[Tuple[str, str, str]]] = None
        self._names_by_id: Dict[str, Tuple[str, str]] = {}
        self._map_cache: Dict[str, Tuple[WzSubProperty, str]] = {}
        self._canvas_cache: "OrderedDict[int, Image.Image]" = OrderedDict()
        self._canvas_cache_bytes = 0
        self._asset_cache: Dict[Tuple[int, str], Optional[WzSubProperty]] = {}

    def close(self) -> None:
        """Close only auxiliary sources explicitly entrusted to this renderer."""
        for source in self._owned_sources:
            try:
                source.close()
            except Exception:
                pass
        self._owned_sources.clear()

    # -- map and string lookup -------------------------------------------------

    def _image(self, source: Any, path: str):
        if source is None:
            return None
        return source.root.get(path)

    def _map_image(self, map_id: str):
        padded = map_id.zfill(9)
        image = self._image(self.map, f"Map/Map{padded[0]}/{padded}.img")
        if image is not None:
            return image

        # Modern packages and hand-built fixtures sometimes put a map in a
        # different Map<N> shard.  HaCreator asks its file manager for the
        # logical map image; scanning the handful of map directories is the
        # source-agnostic equivalent.
        map_dir = self.map.root.get("Map")
        if map_dir is not None:
            for sub_name in map_dir.subdirs:
                candidate = map_dir.child(sub_name).get(f"{padded}.img")
                if candidate is not None:
                    return candidate
        return None

    def _map_root(self, map_id: Any) -> Tuple[WzSubProperty, str]:
        requested = _normalize_map_id(map_id)
        cached = self._map_cache.get(requested)
        if cached is not None:
            return cached

        current = requested
        seen = set()
        for _ in range(16):
            if current in seen:
                raise ValueError(f"linked-map cycle while loading {requested}")
            seen.add(current)
            image = self._map_image(current)
            if image is None:
                raise KeyError(f"map {requested} not found")
            root = image.parse()
            link = root.get("info/link")
            if link is None:
                result = (root, current)
                self._map_cache[requested] = result
                return result
            current = _normalize_map_id(_value(link))
        raise ValueError(f"too many linked-map hops while loading {requested}")

    def has_map(self, map_id: Any) -> bool:
        try:
            self._map_root(map_id)
            return True
        except (KeyError, TypeError, ValueError):
            return False

    def _build_name_index(self) -> None:
        if self._name_index is not None:
            return
        entries: List[Tuple[str, str, str]] = []
        image = self._image(self.string, "Map.img")
        if image is not None:
            for category in image.parse().children():
                if not isinstance(category, WzSubProperty):
                    continue
                for entry in category.children():
                    if not entry.name.isdigit():
                        continue
                    name = _str(entry.get("mapName"))
                    if not name:
                        continue
                    map_id = _normalize_map_id(entry.name)
                    street = _str(entry.get("streetName"))
                    entries.append((map_id, name, street))
                    self._names_by_id[map_id] = (name, street)
        entries.sort(key=lambda item: (item[1].lower(), int(item[0])))
        self._name_index = entries

    def search(self, query: str = "", limit: int = 50) -> List[Dict[str, str]]:
        with self._lock:
            self._build_name_index()
            query = (query or "").strip().lower()
            entries = self._name_index or []
            if query.isdigit():
                matches = [item for item in entries if item[0].startswith(query)]
            elif query:
                matches = [
                    item for item in entries
                    if query in item[1].lower() or query in item[2].lower()
                ]
            else:
                matches = entries

            def rank(item: Tuple[str, str, str]):
                map_id, name, street = item
                low_name = name.lower()
                if query and (map_id == query or low_name == query):
                    group = 0
                elif query and (map_id.startswith(query) or low_name.startswith(query)):
                    group = 1
                else:
                    group = 2
                return group, len(name), low_name, map_id

            matches.sort(key=rank)
            return [
                {"id": map_id, "name": name, "street": street}
                for map_id, name, street in matches[: max(1, min(limit, 200))]
            ]

    def first_map_id(self) -> Optional[str]:
        if self.has_map("100000000"):
            return "100000000"
        with self._lock:
            self._build_name_index()
            for map_id, _name, _street in self._name_index or []:
                if self.has_map(map_id):
                    return map_id
        map_dir = self.map.root.get("Map")
        if map_dir is not None:
            for sub_name in sorted(map_dir.subdirs):
                sub = map_dir.child(sub_name)
                for image_name in sorted(sub.images):
                    stem = image_name[:-4] if image_name.lower().endswith(".img") else image_name
                    if stem.isdigit():
                        return _normalize_map_id(stem)
        return None

    # -- scene description -----------------------------------------------------

    def _footholds(self, root: WzSubProperty) -> List[Dict[str, int]]:
        result: List[Dict[str, int]] = []
        parent = root.get("foothold")
        if parent is None:
            return result
        for layer in parent.children():
            for platform in layer.children():
                for foothold in platform.children():
                    if foothold.get("x1") is None:
                        continue
                    result.append({
                        "layer": _int_name(layer.name),
                        "platform": _int_name(platform.name),
                        "id": _int_name(foothold.name),
                        "x1": _int(foothold.get("x1")),
                        "y1": _int(foothold.get("y1")),
                        "x2": _int(foothold.get("x2")),
                        "y2": _int(foothold.get("y2")),
                        "prev": _int(foothold.get("prev")),
                        "next": _int(foothold.get("next")),
                    })
        return result

    def _ropes(self, root: WzSubProperty) -> List[Dict[str, Any]]:
        parent = root.get("ladderRope")
        if parent is None:
            return []
        return [{
            "id": _int_name(item.name),
            "x": _int(item.get("x")),
            "y1": _int(item.get("y1")),
            "y2": _int(item.get("y2")),
            "ladder": bool(_int(item.get("l"))),
            "uf": bool(_int(item.get("uf"))),
            "page": _int(item.get("page")),
        } for item in parent.children() if item.get("x") is not None]

    def _life(self, root: WzSubProperty) -> List[Dict[str, Any]]:
        parent = root.get("life")
        if parent is None:
            return []
        result = []
        for item in parent.children():
            life_type = _str(item.get("type"))
            if life_type not in ("m", "n"):
                continue
            result.append({
                "index": _int_name(item.name),
                "type": "mob" if life_type == "m" else "npc",
                "id": _str(item.get("id")),
                "x": _int(item.get("x")),
                "y": _int(item.get("y")),
                "cy": _int(item.get("cy"), _int(item.get("y"))),
                "rx0": _int(item.get("rx0")),
                "rx1": _int(item.get("rx1")),
                "flip": bool(_int(item.get("f"))),
                "hide": bool(_int(item.get("hide"))),
                "mobTime": _value(item.get("mobTime")),
            })
        return result

    def _portals(self, root: WzSubProperty) -> List[Dict[str, Any]]:
        parent = root.get("portal")
        if parent is None:
            return []
        result = []
        for item in parent.children():
            if item.get("pn") is None:
                continue
            result.append({
                "index": _int_name(item.name),
                "name": _str(item.get("pn")),
                "type": _int(item.get("pt")),
                "x": _int(item.get("x")),
                "y": _int(item.get("y")),
                "targetMap": _int(item.get("tm"), 999999999),
                "targetName": _str(item.get("tn")),
                "script": _str(item.get("script")),
            })
        return result

    def _reactors(self, root: WzSubProperty) -> List[Dict[str, Any]]:
        parent = root.get("reactor")
        if parent is None:
            return []
        return [{
            "index": _int_name(item.name),
            "id": _str(item.get("id")),
            "x": _int(item.get("x")),
            "y": _int(item.get("y")),
            "flip": bool(_int(item.get("f"))),
            "name": _str(item.get("name")),
            "reactorTime": _int(item.get("reactorTime")),
        } for item in parent.children() if item.get("id") is not None]

    def _bounds(self, root: WzSubProperty) -> MapBounds:
        info = root.get("info")
        if info is not None and all(info.get(name) is not None for name in (
            "VRLeft", "VRTop", "VRRight", "VRBottom",
        )):
            left = _int(info.get("VRLeft"))
            top = _int(info.get("VRTop"))
            right = _int(info.get("VRRight"))
            bottom = _int(info.get("VRBottom"))
            if right > left and bottom > top:
                return MapBounds(left, top, right, bottom)

        minimap = root.get("miniMap")
        if minimap is not None:
            width = _int(minimap.get("width"))
            height = _int(minimap.get("height"))
            center_x = _int(minimap.get("centerX"))
            center_y = _int(minimap.get("centerY"))
            if width > 0 and height > 0:
                return MapBounds(-center_x, -center_y, width - center_x, height - center_y)

        footholds = self._footholds(root)
        if footholds:
            xs = [value for fh in footholds for value in (fh["x1"], fh["x2"])]
            ys = [value for fh in footholds for value in (fh["y1"], fh["y2"])]
            return MapBounds(min(xs) - 10, min(ys) - 360,
                             max(xs) + 10, max(ys) + 110)
        raise ValueError("map has no VR, miniMap, or footholds from which to derive bounds")

    def describe(self, map_id: Any) -> Dict[str, Any]:
        with self._lock:
            requested = _normalize_map_id(map_id)
            root, source_id = self._map_root(requested)
            self._build_name_index()
            name, street = self._names_by_id.get(requested, (None, None))
            layers = []
            tile_total = object_total = 0
            for layer_number in range(LEGACY_LAYER_COUNT):
                layer = root.get(str(layer_number))
                tiles = len(layer.get("tile").children()) if layer and layer.get("tile") else 0
                objects = len(layer.get("obj").children()) if layer and layer.get("obj") else 0
                tile_total += tiles
                object_total += objects
                layers.append({
                    "number": layer_number,
                    "tileSet": _str(layer.get("info/tS")) if layer else "",
                    "tiles": tiles,
                    "objects": objects,
                })
            footholds = self._footholds(root)
            ropes = self._ropes(root)
            life = self._life(root)
            portals = self._portals(root)
            reactors = self._reactors(root)
            backgrounds = len(root.get("back").children()) if root.get("back") else 0
            minimap = root.get("miniMap")
            return {
                "id": requested,
                "sourceId": source_id,
                "linked": source_id != requested,
                "name": name,
                "street": street,
                "bounds": self._bounds(root).as_dict(),
                "layers": layers,
                "counts": {
                    "tiles": tile_total,
                    "objects": object_total,
                    "backgrounds": backgrounds,
                    "mobs": sum(1 for item in life if item["type"] == "mob"),
                    "npcs": sum(1 for item in life if item["type"] == "npc"),
                    "reactors": len(reactors),
                    "portals": len(portals),
                    "footholds": len(footholds),
                    "ropes": len(ropes),
                },
                "life": life,
                "reactors": reactors,
                "portals": portals,
                "footholds": footholds,
                "ropes": ropes,
                "minimap": ({
                    "width": _int(minimap.get("width")),
                    "height": _int(minimap.get("height")),
                    "centerX": _int(minimap.get("centerX")),
                    "centerY": _int(minimap.get("centerY")),
                    "mag": _int(minimap.get("mag")),
                } if minimap is not None else None),
            }

    # -- asset and frame resolution -------------------------------------------

    def _asset_root(self, source: Any, path: str) -> Optional[WzSubProperty]:
        if source is None:
            return None
        key = (id(source), path)
        if key in self._asset_cache:
            return self._asset_cache[key]
        image = self._image(source, path)
        root = image.parse() if image is not None else None
        self._asset_cache[key] = root
        return root

    def _id_asset(self, source: Any, component: str, asset_id: str) -> Optional[WzSubProperty]:
        padded = str(asset_id).zfill(7)
        for path in (f"{padded}.img", f"{component}/{padded}.img"):
            root = self._asset_root(source, path)
            if root is not None:
                return root
        return None

    @staticmethod
    def _resolve_uol(prop: Optional[WzProperty], max_depth: int = 16) -> Optional[WzProperty]:
        current = prop
        seen = set()
        for _ in range(max_depth):
            if not isinstance(current, WzUolProperty):
                return current
            if id(current) in seen or current.parent is None:
                return None
            seen.add(id(current))
            current = current.parent.get(str(current.value))
        return None

    def _frames(self, source: Optional[WzProperty]) -> List[WzCanvasProperty]:
        source = self._resolve_uol(source)
        if isinstance(source, WzCanvasProperty):
            return [source]
        if not isinstance(source, WzSubProperty):
            return []
        result: List[WzCanvasProperty] = []
        for child in _numeric_children(source):
            real = self._resolve_uol(child)
            if isinstance(real, WzCanvasProperty):
                result.append(real)
            elif isinstance(real, WzSubProperty):
                result.extend(self._frames(real))
        # Some assets put a lone canvas under a non-numeric wrapper.
        if not result:
            canvases = [child for child in source.children()
                        if isinstance(self._resolve_uol(child), WzCanvasProperty)]
            result.extend(self._resolve_uol(child) for child in canvases)
        return result

    def _frame_at(self, source: Optional[WzProperty], time_ms: int) -> Optional[WzCanvasProperty]:
        frames = self._frames(source)
        if not frames:
            return None
        if len(frames) == 1:
            return frames[0]
        delays = [max(1, _int(frame.get("delay"), 100)) for frame in frames]
        cursor = max(0, int(time_ms)) % sum(delays)
        for frame, delay in zip(frames, delays):
            if cursor < delay:
                return frame
            cursor -= delay
        return frames[-1]

    def _pixels(self, canvas: WzCanvasProperty, source: Any) -> Optional[Image.Image]:
        logical = self._resolve_uol(canvas)
        if not isinstance(logical, WzCanvasProperty):
            return None
        pixel_canvas = logical
        if not logical.has_pixels():
            linked = resolve_canvas_link(logical, source.root)
            if linked is None:
                return None
            pixel_canvas = linked
        key = id(pixel_canvas)
        cached = self._canvas_cache.get(key)
        if cached is not None:
            self._canvas_cache.move_to_end(key)
            return cached
        try:
            image = decode_canvas(pixel_canvas, region=self.region).convert("RGBA")
        except (EOFError, OSError, ValueError, zlib.error):
            # A few legacy/private-server archives retain dead canvas entries
            # whose metadata points at removed or repurposed bytes. HaCreator
            # logs and skips missing assets while loading a board; do the same
            # so one bad tile cannot make an otherwise valid map unrenderable.
            return None
        self._canvas_cache[key] = image
        self._canvas_cache_bytes += image.width * image.height * 4
        while (self._canvas_cache_bytes > MAX_CANVAS_CACHE_BYTES
               and len(self._canvas_cache) > 1):
            _old_key, old_image = self._canvas_cache.popitem(last=False)
            self._canvas_cache_bytes -= old_image.width * old_image.height * 4
        return image

    @staticmethod
    def _origin(canvas: WzCanvasProperty) -> Tuple[int, int]:
        return _vector(canvas.get("origin"))

    # -- composition -----------------------------------------------------------

    def compose(
        self,
        map_id: Any,
        *,
        scale: float = 1.0,
        time_ms: int = 0,
        layers: Optional[Iterable[int]] = None,
        backgrounds: bool = True,
        life: bool = True,
        reactors: bool = True,
        portals: bool = False,
        footholds: bool = False,
        ropes: bool = False,
        viewport: Optional[MapBounds] = None,
    ) -> Image.Image:
        """Compose one editor-style map frame.

        Coordinates, z ordering, bitmap origins, and flip shifts follow
        HaCreator.  Background tiling/motion follows its MapSimulator so a
        full-map export is useful even though game backgrounds are parallaxed
        against a moving camera.
        """
        if not math.isfinite(scale) or scale <= 0 or scale > 4:
            raise ValueError("scale must be greater than 0 and at most 4")
        selected_layers = set(range(LEGACY_LAYER_COUNT) if layers is None else layers)
        if any(layer < 0 or layer >= LEGACY_LAYER_COUNT for layer in selected_layers):
            raise ValueError("layers must be between 0 and 7")

        with self._lock:
            root, _source_id = self._map_root(map_id)
            bounds = viewport or self._bounds(root)
            if bounds.width <= 0 or bounds.height <= 0:
                raise ValueError("render bounds are empty")
            out_width = max(1, int(math.ceil(bounds.width * scale)))
            out_height = max(1, int(math.ceil(bounds.height * scale)))
            if out_width * out_height > MAX_RENDER_PIXELS:
                raise ValueError(
                    f"render would create {out_width}x{out_height} pixels; "
                    "lower scale or request a viewport"
                )
            output = Image.new("RGBA", (out_width, out_height), (0, 0, 0, 0))

            back = root.get("back")
            back_items = back.children() if back is not None else []
            if backgrounds:
                for item in back_items:
                    if not bool(_int(item.get("front"))):
                        self._draw_background(output, bounds, scale, item, time_ms)

            placements: List[Tuple[Tuple[int, int, int, int], WzCanvasProperty,
                                   int, int, bool, Any]] = []
            for layer_number in range(LEGACY_LAYER_COUNT):
                if layer_number not in selected_layers:
                    continue
                layer = root.get(str(layer_number))
                if layer is None:
                    continue
                tile_set = _str(layer.get("info/tS"))
                objects = layer.get("obj")
                for order, item in enumerate(objects.children() if objects else []):
                    object_set = _str(item.get("oS"))
                    asset = self._asset_root(self.map, f"Obj/{object_set}.img")
                    source = asset.get("/".join((
                        _str(item.get("l0")), _str(item.get("l1")), _str(item.get("l2")),
                    ))) if asset is not None else None
                    frame = self._frame_at(source, time_ms)
                    if frame is None:
                        continue
                    key = (layer_number, 0, _int(item.get("z")), order)
                    placements.append((key, frame, _int(item.get("x")),
                                       _int(item.get("y")), bool(_int(item.get("f"))), self.map))

                tiles = layer.get("tile")
                tile_asset = self._asset_root(self.map, f"Tile/{tile_set}.img") if tile_set else None
                for order, item in enumerate(tiles.children() if tiles else []):
                    source = tile_asset.get(
                        f"{_str(item.get('u'))}/{_int(item.get('no'))}"
                    ) if tile_asset is not None else None
                    frame = self._frame_at(source, time_ms)
                    if frame is None:
                        continue
                    key = (layer_number, 1, _int(frame.get("z")), _int_name(item.name, order))
                    placements.append((key, frame, _int(item.get("x")),
                                       _int(item.get("y")), False, self.map))

            for _key, frame, x, y, flip, source in sorted(placements, key=lambda item: item[0]):
                self._draw_sprite(output, bounds, scale, frame, source, x, y, flip=flip)

            if life:
                life_items = root.get("life")
                raw_life = life_items.children() if life_items is not None else []
                # HaCreator stores mobs and NPCs in separate board lists and
                # renders all mobs before all NPCs, irrespective of source order.
                for wanted_type, component, source in (
                    ("m", "Mob", self.mob), ("n", "Npc", self.npc),
                ):
                    for item in raw_life:
                        if _str(item.get("type")) != wanted_type:
                            continue
                        asset = self._linked_life_asset(source, component, _str(item.get("id")))
                        if asset is None:
                            continue
                        action = (asset.get("stand") or asset.get("fly") or asset.get("move"))
                        frame = self._frame_at(action, time_ms)
                        if frame is None:
                            continue
                        y = _int(item.get("cy"), _int(item.get("y")))
                        self._draw_sprite(output, bounds, scale, frame, source,
                                          _int(item.get("x")), y,
                                          flip=bool(_int(item.get("f"))))

            if reactors:
                reactor_parent = root.get("reactor")
                for item in reactor_parent.children() if reactor_parent else []:
                    asset = self._linked_life_asset(
                        self.reactor, "Reactor", _str(item.get("id")),
                    )
                    frame = self._frame_at(asset.get("0") if asset else None, time_ms)
                    if frame is not None:
                        self._draw_sprite(output, bounds, scale, frame, self.reactor,
                                          _int(item.get("x")), _int(item.get("y")),
                                          flip=bool(_int(item.get("f"))))

            if portals:
                self._draw_portals(output, bounds, scale, root)

            if backgrounds:
                for item in back_items:
                    if bool(_int(item.get("front"))):
                        self._draw_background(output, bounds, scale, item, time_ms)

            # Geometry overlays stay last so they remain legible over front
            # backgrounds.  They are builder aids, not part of the game frame.
            draw = ImageDraw.Draw(output, "RGBA")
            if footholds:
                line_width = max(1, round(scale * 2))
                for fh in self._footholds(root):
                    draw.line((
                        self._screen_xy(bounds, scale, fh["x1"], fh["y1"]),
                        self._screen_xy(bounds, scale, fh["x2"], fh["y2"]),
                    ), fill=(255, 64, 72, 230), width=line_width)
            if ropes:
                line_width = max(1, round(scale * 2))
                for rope in self._ropes(root):
                    color = (249, 197, 61, 230) if rope["ladder"] else (67, 209, 255, 230)
                    draw.line((
                        self._screen_xy(bounds, scale, rope["x"], rope["y1"]),
                        self._screen_xy(bounds, scale, rope["x"], rope["y2"]),
                    ), fill=color, width=line_width)
            return output

    def compose_png(self, map_id: Any, **options: Any) -> bytes:
        image = self.compose(map_id, **options)
        buffer = io.BytesIO()
        image.save(buffer, "PNG", optimize=False)
        return buffer.getvalue()

    def minimap_png(self, map_id: Any) -> Optional[bytes]:
        with self._lock:
            root, _source_id = self._map_root(map_id)
            canvas = self._resolve_uol(root.get("miniMap/canvas"))
            if not isinstance(canvas, WzCanvasProperty):
                return None
            image = self._pixels(canvas, self.map)
            if image is None:
                return None
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
            return buffer.getvalue()

    def _linked_life_asset(
        self, source: Any, component: str, asset_id: str,
    ) -> Optional[WzSubProperty]:
        asset = self._id_asset(source, component, asset_id)
        seen = set()
        for _ in range(8):
            if asset is None or id(asset) in seen:
                return asset
            seen.add(id(asset))
            link = asset.get("info/link")
            if link is None:
                return asset
            asset = self._id_asset(source, component, _str(link))
        return asset

    def _draw_sprite(
        self,
        output: Image.Image,
        bounds: MapBounds,
        scale: float,
        canvas: WzCanvasProperty,
        source: Any,
        anchor_x: int,
        anchor_y: int,
        *,
        flip: bool = False,
        alpha: int = 255,
    ) -> None:
        pixels = self._pixels(canvas, source)
        if pixels is None:
            return
        origin_x, origin_y = self._origin(canvas)
        left = anchor_x - origin_x
        if flip:
            left = anchor_x - pixels.width + origin_x
            pixels = pixels.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        top = anchor_y - origin_y
        self._paste_scaled(output, bounds, scale, pixels, left, top, alpha)

    def _draw_background(
        self,
        output: Image.Image,
        bounds: MapBounds,
        scale: float,
        item: WzProperty,
        time_ms: int,
    ) -> None:
        background_set = _str(item.get("bS"))
        asset = self._asset_root(self.map, f"Back/{background_set}.img")
        if asset is None:
            return
        number = str(_int(item.get("no")))
        source = asset.get(f"ani/{number}") if bool(_int(item.get("ani"))) else asset.get(f"back/{number}")
        frame = self._frame_at(source, time_ms)
        if frame is None:
            return
        pixels = self._pixels(frame, self.map)
        if pixels is None or pixels.width <= 1 or pixels.height <= 1:
            return
        origin_x, origin_y = self._origin(frame)
        anchor_x = _int(item.get("x"))
        anchor_y = _int(item.get("y"))
        flip = bool(_int(item.get("f")))
        alpha = max(0, min(255, _int(item.get("a"), 255)))
        bg_type = _int(item.get("type"))
        step_x = _int(item.get("cx")) or pixels.width
        step_y = _int(item.get("cy")) or pixels.height

        if bg_type in (4, 6) and step_x > 0:
            anchor_x += int((_int(item.get("rx")) * max(0, time_ms) / 200.0) % step_x)
        if bg_type in (5, 7) and step_y > 0:
            anchor_y += int((_int(item.get("ry")) * max(0, time_ms) / 200.0) % step_y)

        horizontal = bg_type in (1, 3, 4, 6, 7)
        vertical = bg_type in (2, 3, 5, 6, 7)
        x_offsets = self._copy_offsets(
            anchor_x - (pixels.width - origin_x if flip else origin_x),
            pixels.width, step_x, bounds.left, bounds.right,
        ) if horizontal else [0]
        y_offsets = self._copy_offsets(
            anchor_y - origin_y, pixels.height, step_y, bounds.top, bounds.bottom,
        ) if vertical else [0]
        for dx in x_offsets:
            for dy in y_offsets:
                self._draw_sprite(output, bounds, scale, frame, self.map,
                                  anchor_x + dx, anchor_y + dy,
                                  flip=flip, alpha=alpha)

    @staticmethod
    def _copy_offsets(
        first_left: int, size: int, step: int, view_start: int, view_end: int,
    ) -> List[int]:
        if step <= 0:
            return [0]
        first = math.floor((view_start - first_left - size) / step) + 1
        last = math.ceil((view_end - first_left) / step) - 1
        if last < first:
            return [0]
        # Malformed maps occasionally contain cx/cy=1 for a huge bitmap.  A
        # hard copy cap prevents an accidental multi-million-paste request.
        if last - first > 10_000:
            last = first + 10_000
        return [index * step for index in range(first, last + 1)]

    def _draw_portals(
        self, output: Image.Image, bounds: MapBounds, scale: float, root: WzSubProperty,
    ) -> None:
        helper = self._asset_root(self.map, "MapHelper.img")
        editor = helper.get("portal/editor") if helper is not None else None
        icons = editor.children() if editor is not None else []
        for portal in root.get("portal").children() if root.get("portal") else []:
            portal_type = _int(portal.get("pt"))
            frame = icons[portal_type] if 0 <= portal_type < len(icons) else None
            frame = self._resolve_uol(frame)
            if isinstance(frame, WzCanvasProperty):
                self._draw_sprite(output, bounds, scale, frame, self.map,
                                  _int(portal.get("x")), _int(portal.get("y")))
            else:
                draw = ImageDraw.Draw(output, "RGBA")
                x, y = self._screen_xy(bounds, scale, _int(portal.get("x")), _int(portal.get("y")))
                radius = max(2, round(5 * scale))
                draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                             fill=(80, 190, 255, 220))

    @staticmethod
    def _screen_xy(bounds: MapBounds, scale: float, x: int, y: int) -> Tuple[int, int]:
        return round((x - bounds.left) * scale), round((y - bounds.top) * scale)

    @staticmethod
    def _paste_scaled(
        output: Image.Image,
        bounds: MapBounds,
        scale: float,
        pixels: Image.Image,
        left: int,
        top: int,
        alpha: int,
    ) -> None:
        x = round((left - bounds.left) * scale)
        y = round((top - bounds.top) * scale)
        if scale != 1.0:
            # Scale both world-space edges so snapped tiles share the exact
            # same output boundary at fractional render scales.
            right = round((left + pixels.width - bounds.left) * scale)
            bottom = round((top + pixels.height - bounds.top) * scale)
            width = max(1, right - x)
            height = max(1, bottom - y)
            pixels = pixels.resize((width, height), Image.Resampling.NEAREST)
        if alpha < 255:
            pixels = pixels.copy()
            channel = pixels.getchannel("A").point(lambda value: value * alpha // 255)
            pixels.putalpha(channel)
        _alpha_composite_clipped(output, pixels, x, y)


def _alpha_composite_clipped(output: Image.Image, sprite: Image.Image, x: int, y: int) -> None:
    """Alpha-composite a sprite while accepting negative/off-canvas positions."""
    left = max(0, x)
    top = max(0, y)
    right = min(output.width, x + sprite.width)
    bottom = min(output.height, y + sprite.height)
    if right <= left or bottom <= top:
        return
    source = sprite.crop((left - x, top - y, right - x, bottom - y))
    output.alpha_composite(source, (left, top))


def _int_name(name: str, default: int = 0) -> int:
    try:
        return int(name)
    except (TypeError, ValueError):
        return default


__all__ = [
    "LEGACY_LAYER_COUNT", "MAX_CANVAS_CACHE_BYTES", "MAX_RENDER_PIXELS",
    "MapBounds", "MapRenderer",
]
