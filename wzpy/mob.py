"""Legacy MapleStory mob search, animation, and Monster Book drops.

The v83 client keeps these pieces in separate archives:

* ``Mob.wz`` contains sprites and stats;
* ``String.wz/Mob.img`` contains names;
* ``String.wz/MonsterBook.img`` contains the client-authored reward list;
* ``String.wz`` plus ``Item.wz`` / ``Character.wz`` provide item names and
  icons.

``MobRenderer`` joins those sources without claiming drop rates, which are
server-side data and are not present in the v83 WZ files.
"""

from __future__ import annotations

import io
import threading
import zlib
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image

from .canvas import decode_canvas
from .properties import (
    WzCanvasProperty,
    WzProperty,
    WzStringProperty,
    WzSubProperty,
    WzUolProperty,
)
from .wz_package import resolve_canvas_link


MAX_ANIMATION_PIXELS = 100_000_000
MAX_ANIMATION_CACHE = 24


def _value(prop: Optional[WzProperty], default: Any = None) -> Any:
    if prop is None:
        return default
    try:
        return prop.value
    except Exception:
        return default


def _int(prop: Optional[WzProperty], default: int = 0) -> int:
    try:
        return int(_value(prop, default))
    except (TypeError, ValueError):
        return default


def _str(prop: Optional[WzProperty], default: str = "") -> str:
    value = _value(prop, default)
    return default if value is None else str(value)


def _normalize_id(value: Any, label: str = "mob") -> str:
    text = str(value).strip()
    if not text or not text.isdigit():
        raise ValueError(f"invalid {label} id {value!r}")
    return str(int(text))


def _numeric_children(node: Optional[WzProperty]) -> List[WzProperty]:
    if not isinstance(node, WzSubProperty):
        return []
    result = [child for child in node.children() if child.name.isdigit()]
    result.sort(key=lambda child: int(child.name))
    return result


class MobRenderer:
    """Join the legacy v83 Mob/String/Item/Character archives."""

    _ACTION_PRIORITY = (
        "stand", "move", "fly", "jump", "attack", "skill", "hit", "die",
    )
    _STAT_FIELDS = (
        ("level", "level"),
        ("maxHP", "hp"),
        ("maxMP", "mp"),
        ("exp", "exp"),
        ("PADamage", "weaponAttack"),
        ("MADamage", "magicAttack"),
        ("PDDamage", "weaponDefense"),
        ("MDDamage", "magicDefense"),
        ("acc", "accuracy"),
        ("eva", "evasion"),
        ("speed", "speed"),
    )

    def __init__(
        self,
        mob_source: Any,
        *,
        string_source: Any = None,
        item_source: Any = None,
        character_source: Any = None,
        region: Optional[str] = None,
        owned_sources: Sequence[Any] = (),
    ) -> None:
        self.mob = mob_source
        self.string = string_source
        self.item = item_source
        self.character = character_source
        self.region = region or getattr(mob_source, "region", "GMS")
        self._owned_sources = list(owned_sources)
        self._lock = threading.RLock()
        self._mob_names: Optional[Dict[str, str]] = None
        self._search_index: Optional[List[Tuple[str, str]]] = None
        self._item_names: Optional[Dict[str, str]] = None
        self._monster_book: Optional[WzSubProperty] = None
        self._monster_book_loaded = False
        self._mob_cache: Dict[str, Tuple[WzSubProperty, str]] = {}
        self._pixel_cache: "OrderedDict[Tuple[int, int], Image.Image]" = OrderedDict()
        self._animation_cache: "OrderedDict[Tuple[str, str, bool], bytes]" = OrderedDict()

    def close(self) -> None:
        for source in self._owned_sources:
            try:
                source.close()
            except Exception:
                pass
        self._owned_sources.clear()

    # -- source and index lookup ---------------------------------------------

    @staticmethod
    def _image(source: Any, path: str):
        return source.root.get(path) if source is not None else None

    def _mob_image(self, mob_id: str):
        padded = mob_id.zfill(7)
        for path in (f"{padded}.img", f"Mob/{padded}.img"):
            image = self._image(self.mob, path)
            if image is not None:
                return image
        return None

    def _mob_root(self, mob_id: Any) -> Tuple[WzSubProperty, str]:
        requested = _normalize_id(mob_id)
        cached = self._mob_cache.get(requested)
        if cached is not None:
            return cached
        current = requested
        seen = set()
        for _ in range(16):
            if current in seen:
                raise ValueError(f"linked-mob cycle while loading {requested}")
            seen.add(current)
            image = self._mob_image(current)
            if image is None:
                raise KeyError(f"mob {requested} not found")
            root = image.parse()
            link = root.get("info/link")
            if link is None:
                result = (root, current)
                self._mob_cache[requested] = result
                return result
            current = _normalize_id(_value(link))
        raise ValueError(f"too many linked-mob hops while loading {requested}")

    def has_mob(self, mob_id: Any) -> bool:
        try:
            self._mob_root(mob_id)
            return True
        except (KeyError, TypeError, ValueError):
            return False

    def _load_mob_names(self) -> None:
        if self._mob_names is not None:
            return
        names: Dict[str, str] = {}
        image = self._image(self.string, "Mob.img")
        if image is not None:
            for entry in image.parse().children():
                if not entry.name.isdigit():
                    continue
                name = _str(entry.get("name"))
                if name:
                    names[_normalize_id(entry.name)] = name
        self._mob_names = names

    def _build_search_index(self) -> None:
        if self._search_index is not None:
            return
        self._load_mob_names()
        ids = set()
        for image_name in self.mob.root.images:
            stem = image_name[:-4] if image_name.lower().endswith(".img") else image_name
            if stem.isdigit():
                ids.add(_normalize_id(stem))
        mob_dir = self.mob.root.get("Mob")
        if mob_dir is not None:
            for image_name in mob_dir.images:
                stem = image_name[:-4] if image_name.lower().endswith(".img") else image_name
                if stem.isdigit():
                    ids.add(_normalize_id(stem))
        names = self._mob_names or {}
        self._search_index = sorted(
            ((mob_id, names.get(mob_id, f"Mob {mob_id}")) for mob_id in ids),
            key=lambda item: (item[1].lower(), int(item[0])),
        )

    def search(self, query: str = "", limit: int = 50) -> List[Dict[str, str]]:
        with self._lock:
            self._build_search_index()
            query = (query or "").strip().lower()
            entries = self._search_index or []
            if query.isdigit():
                matches = [item for item in entries if item[0].startswith(query)]
            elif query:
                matches = [item for item in entries if query in item[1].lower()]
            else:
                matches = list(entries)

            def rank(item: Tuple[str, str]):
                mob_id, name = item
                low_name = name.lower()
                if query and (mob_id == query or low_name == query):
                    group = 0
                elif query and (mob_id.startswith(query) or low_name.startswith(query)):
                    group = 1
                else:
                    group = 2
                return group, len(name), low_name, int(mob_id)

            matches.sort(key=rank)
            return [
                {"id": mob_id, "name": name}
                for mob_id, name in matches[:max(1, min(int(limit), 200))]
            ]

    def first_mob_id(self) -> Optional[str]:
        if self.has_mob("100100"):
            return "100100"
        with self._lock:
            self._build_search_index()
            return self._search_index[0][0] if self._search_index else None

    # -- actions and animation ------------------------------------------------

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
        frames: List[WzCanvasProperty] = []
        for child in _numeric_children(source):
            real = self._resolve_uol(child)
            if isinstance(real, WzCanvasProperty):
                frames.append(real)
            elif isinstance(real, WzSubProperty):
                frames.extend(self._frames(real))
        return frames

    def _actions(self, root: WzSubProperty) -> List[Dict[str, Any]]:
        actions = []
        for child in root.children():
            if child.name == "info" or child.name.isdigit():
                continue
            frames = self._frames(child)
            if not frames:
                continue
            delays = [max(10, _int(frame.get("delay"), 100)) for frame in frames]
            actions.append({
                "name": child.name,
                "frames": len(frames),
                "durationMs": sum(delays),
            })

        def action_key(action: Dict[str, Any]):
            name = action["name"].lower()
            for index, prefix in enumerate(self._ACTION_PRIORITY):
                if name.startswith(prefix):
                    return index, name
            return len(self._ACTION_PRIORITY), name

        actions.sort(key=action_key)
        return actions

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
        key = (id(source), id(pixel_canvas))
        cached = self._pixel_cache.get(key)
        if cached is not None:
            self._pixel_cache.move_to_end(key)
            return cached
        try:
            image = decode_canvas(pixel_canvas, region=self.region).convert("RGBA")
        except (EOFError, OSError, ValueError, zlib.error):
            return None
        self._pixel_cache[key] = image
        while len(self._pixel_cache) > 256:
            self._pixel_cache.popitem(last=False)
        return image

    @staticmethod
    def _origin(canvas: WzCanvasProperty) -> Tuple[int, int]:
        origin = canvas.get("origin")
        value = _value(origin, (0, 0))
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError, IndexError):
            return 0, 0

    def animation_png(self, mob_id: Any, action: str, flip: bool = False) -> bytes:
        requested = _normalize_id(mob_id)
        action = str(action or "").strip()
        if not action or "/" in action or "\\" in action:
            raise ValueError("invalid mob action")
        cache_key = (requested, action, bool(flip))
        with self._lock:
            cached = self._animation_cache.get(cache_key)
            if cached is not None:
                self._animation_cache.move_to_end(cache_key)
                return cached
            root, _source_id = self._mob_root(requested)
            frames = self._frames(root.get(action))
            if not frames:
                raise KeyError(f"mob {requested} has no action {action!r}")

            decoded: List[Tuple[Image.Image, int, int, int]] = []
            left = top = 0
            right = bottom = 0
            for frame in frames:
                pixels = self._pixels(frame, self.mob)
                if pixels is None:
                    continue
                origin_x, origin_y = self._origin(frame)
                frame_left = -origin_x
                if flip:
                    pixels = pixels.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                    frame_left = -pixels.width + origin_x
                frame_top = -origin_y
                decoded.append((pixels, frame_left, frame_top,
                                max(10, _int(frame.get("delay"), 100))))
                left = min(left, frame_left)
                top = min(top, frame_top)
                right = max(right, frame_left + pixels.width)
                bottom = max(bottom, frame_top + pixels.height)
            if not decoded:
                raise ValueError(f"mob {requested} action {action!r} has no decodable frames")

            padding = 8
            width = max(1, right - left + padding * 2)
            height = max(1, bottom - top + padding * 2)
            if width * height * len(decoded) > MAX_ANIMATION_PIXELS:
                raise ValueError(
                    f"animation would create {width}x{height}x{len(decoded)} pixels"
                )
            images: List[Image.Image] = []
            delays: List[int] = []
            for pixels, frame_left, frame_top, delay in decoded:
                image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                image.alpha_composite(
                    pixels, (frame_left - left + padding, frame_top - top + padding),
                )
                images.append(image)
                delays.append(delay)

            buffer = io.BytesIO()
            if len(images) == 1:
                images[0].save(buffer, "PNG")
            else:
                images[0].save(
                    buffer, "PNG", save_all=True, append_images=images[1:],
                    duration=delays, loop=0, disposal=1, blend=0,
                )
            payload = buffer.getvalue()
            self._animation_cache[cache_key] = payload
            while len(self._animation_cache) > MAX_ANIMATION_CACHE:
                self._animation_cache.popitem(last=False)
            return payload

    # -- Monster Book drops and item assets ----------------------------------

    def _monster_book_root(self) -> Optional[WzSubProperty]:
        if not self._monster_book_loaded:
            image = self._image(self.string, "MonsterBook.img")
            self._monster_book = image.parse() if image is not None else None
            self._monster_book_loaded = True
        return self._monster_book

    def _load_item_names(self) -> None:
        if self._item_names is not None:
            return
        names: Dict[str, str] = {}

        def visit(node: WzProperty) -> None:
            if not isinstance(node, WzSubProperty):
                return
            if node.name.isdigit():
                name_prop = node.get("name")
                if isinstance(name_prop, WzStringProperty) and name_prop.value:
                    names[_normalize_id(node.name, "item")] = str(name_prop.value)
            for child in node.children():
                if isinstance(child, WzSubProperty):
                    visit(child)

        for image_name in ("Eqp.img", "Consume.img", "Ins.img", "Etc.img", "Cash.img", "Pet.img"):
            image = self._image(self.string, image_name)
            if image is not None:
                visit(image.parse())
        self._item_names = names

    def _drops(self, mob_id: str) -> Tuple[bool, List[Dict[str, str]]]:
        book = self._monster_book_root()
        entry = book.get(mob_id) if book is not None else None
        if entry is None:
            return False, []
        rewards = entry.get("reward")
        if rewards is None:
            return True, []
        self._load_item_names()
        names = self._item_names or {}
        result = []
        seen = set()
        for reward in _numeric_children(rewards):
            raw_id = _value(reward)
            try:
                item_id = _normalize_id(raw_id, "item")
            except ValueError:
                continue
            if item_id in seen:
                continue
            seen.add(item_id)
            result.append({
                "id": item_id,
                "name": names.get(item_id, f"Item {item_id}"),
                "iconUrl": f"/api/mob/item/{item_id}/icon.png",
            })
        return True, result

    def describe(self, mob_id: Any) -> Dict[str, Any]:
        requested = _normalize_id(mob_id)
        with self._lock:
            root, source_id = self._mob_root(requested)
            self._load_mob_names()
            info = root.get("info")
            stats: Dict[str, Any] = {}
            for source_name, output_name in self._STAT_FIELDS:
                prop = info.get(source_name) if info is not None else None
                if prop is not None:
                    stats[output_name] = _value(prop)
            for source_name, output_name in (
                ("boss", "boss"), ("undead", "undead"),
                ("bodyAttack", "bodyAttack"), ("firstAttack", "firstAttack"),
            ):
                prop = info.get(source_name) if info is not None else None
                if prop is not None:
                    stats[output_name] = bool(_int(prop))
            actions = self._actions(root)
            has_drop_data, drops = self._drops(requested)
            animated = next((action for action in actions if action["frames"] > 1), None)
            return {
                "id": requested,
                "sourceId": source_id,
                "linked": source_id != requested,
                "name": (self._mob_names or {}).get(requested, f"Mob {requested}"),
                "stats": stats,
                "actions": actions,
                "defaultAction": (animated or (actions[0] if actions else None))["name"]
                                 if actions else None,
                "drops": drops,
                "hasDropData": has_drop_data,
                "dropSource": "String.wz/MonsterBook.img" if has_drop_data else None,
            }

    def _item_icon_canvas(self, item_id: str) -> Tuple[Optional[WzCanvasProperty], Any]:
        raw = _normalize_id(item_id, "item")
        padded = raw.zfill(8)
        if padded.startswith("01") and self.character is not None:
            from .character import CATEGORY_DIR, category_for_id
            category = category_for_id(padded)
            folder = CATEGORY_DIR.get(category or "")
            if category and folder is not None:
                path = f"{folder}/{padded}.img" if folder else f"{padded}.img"
                image = self._image(self.character, path)
                if image is not None:
                    icon = image.parse().get("info/icon")
                    if isinstance(self._resolve_uol(icon), WzCanvasProperty):
                        return self._resolve_uol(icon), self.character

        if self.item is None:
            return None, None
        group = padded[:4]
        kind = padded[1]
        folder = {"2": "Consume", "3": "Install", "4": "Etc", "5": "Cash"}.get(kind)
        if folder:
            image = self._image(self.item, f"{folder}/{group}.img")
            if image is not None:
                entry = image.parse().get(padded)
                icon = entry.get("info/icon") if entry is not None else None
                if isinstance(self._resolve_uol(icon), WzCanvasProperty):
                    return self._resolve_uol(icon), self.item
        if kind == "5":
            image = self._image(self.item, f"Pet/{raw}.img")
            if image is not None:
                icon = image.parse().get("info/icon")
                if isinstance(self._resolve_uol(icon), WzCanvasProperty):
                    return self._resolve_uol(icon), self.item
        return None, None

    def item_icon_png(self, item_id: Any) -> Optional[bytes]:
        with self._lock:
            canvas, source = self._item_icon_canvas(_normalize_id(item_id, "item"))
            if canvas is None:
                return None
            pixels = self._pixels(canvas, source)
            if pixels is None:
                return None
            buffer = io.BytesIO()
            pixels.save(buffer, "PNG")
            return buffer.getvalue()
