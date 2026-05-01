"""Character builder: assemble a static MapleStory character from Character.wz.

Given a list of equip IDs (body, head, hair, face, cap, coat, …) this module
walks each part image, resolves the per-canvas anchor points (``origin`` plus
``map/<navel|neck|hand|brow|handMove>``) and chains them so that named anchors
align across parts (head's ``neck`` ↔ body's ``neck``, weapon's ``hand`` ↔
arm's ``hand``, etc.). Parts are then sorted by the ``z`` slot referenced in
each canvas and composited into a single PNG.

The algorithm matches HaCreator/MapleNecrocer's CharacterAssembler:

  body draws so its navel sits at world (0, 0). For every other canvas we
  pick the highest-priority ``map/<anchor>`` it advertises and place the
  canvas so that anchor coincides with the same-named anchor on whichever
  part already populated it. Map values are *relative to ``origin``* so the
  world position of an anchor is ``top_left + origin + map[name]``.

The static composite uses ``stand1/0`` for body-frame parts and
``default``/``backDefault`` for hair/face/cap (so that hat covers, back-hair,
etc. all show up). Animation, expression cycling, dye, and HSL adjustments
from MapleNecrocer's full Avatar form are out of scope for v1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

from .canvas import decode_canvas
from .properties import (
    WzCanvasProperty,
    WzProperty,
    WzStringProperty,
    WzSubProperty,
    WzUolProperty,
    WzVectorProperty,
)
from .wz_file import WzDirectory, WzFile
from .wz_image import WzImage
from .wz_package import resolve_canvas_link


# ── category mapping ──────────────────────────────────────────────────────
# Mirrors MapleNecrocer's ``Equip.GetDir`` / ``GetPart`` for the parts the
# static builder exposes. Keys are the high 4 digits of the equip ID divided
# by 10000 (i.e., ``int(eid)//10000``).

_CATEGORY_BY_ID_PREFIX: Dict[int, str] = {
    0: "Body",
    1: "Head",
    2: "Face", 5: "Face",
    3: "Hair", 4: "Hair", 6: "Hair",
    100: "Cap",
    101: "FaceAcc",
    102: "Glass",
    103: "Earring",
    104: "Coat",
    105: "Longcoat",
    106: "Pants",
    107: "Shoes",
    108: "Glove",
    109: "Shield",
    110: "Cape",
    170: "Weapon",
}
# Weapon range: 121..160 (and 170, included above).
for _n in range(121, 161):
    _CATEGORY_BY_ID_PREFIX[_n] = "Weapon"


CATEGORIES: Tuple[str, ...] = (
    "Body", "Head", "Hair", "Face",
    "Cap", "Coat", "Longcoat", "Pants", "Shoes", "Glove",
    "Cape", "Shield", "FaceAcc", "Glass", "Earring", "Weapon",
)

CATEGORY_DIR: Dict[str, str] = {
    "Body": "",
    "Head": "",
    "Hair": "Hair",
    "Face": "Face",
    "Cap": "Cap",
    "Coat": "Coat",
    "Longcoat": "Longcoat",
    "Pants": "Pants",
    "Shoes": "Shoes",
    "Glove": "Glove",
    "Cape": "Cape",
    "Shield": "Shield",
    "FaceAcc": "Accessory",
    "Glass": "Accessory",
    "Earring": "Accessory",
    "Weapon": "Weapon",
}

# Default anchor when a canvas has no map/* point. Picked so that the
# canvas's ``origin`` point lands at the implicit anchor in world space —
# matches the convention HaCreator's CharacterAssembler uses when
# ``GetMapPoint`` returns ``Point.Zero``.
_DEFAULT_ANCHOR_BY_CATEGORY: Dict[str, str] = {
    "Body": "navel",
    "Head": "neck",
    "Hair": "brow",
    "Face": "brow",
    "Cap": "navel",
    "Coat": "navel",
    "Longcoat": "navel",
    "Pants": "navel",
    "Shoes": "navel",
    "Glove": "hand",
    "Cape": "navel",
    "Shield": "navel",
    "FaceAcc": "brow",
    "Glass": "brow",
    "Earring": "brow",
    "Weapon": "hand",
}

_ANCHOR_PRIORITY: Tuple[str, ...] = ("navel", "neck", "hand", "brow", "handMove")


# Default Z-order, populated by walking every ``z`` string in a stock GMS
# v83 ``Character.wz`` and ordering them semantically. The list is
# back-to-front: index 0 is drawn first (deepest), the last entry sits on
# top. Used when ``Base/zmap.img`` isn't present in the WZ (HaSuite-style
# exports frequently drop ``Base/``).
#
# When a slot name appears that's not in this table, ``_z_index`` falls
# back to a name-pattern heuristic (``back…`` → behind, ``…Below…`` → mid,
# ``…Over…`` → in front) so a previously-unseen variant still composites
# in roughly the right place instead of pinning to mid-stack.
_DEFAULT_ZMAP: Tuple[str, ...] = (
    # Deepest back: shadows, mount + saddle back panels.
    "shadow",
    "characterEnd",
    "backMobEquipUnderSaddle",
    "backSaddle",
    "backSaddleFront",
    "backTamingMobMid",
    "backTamingMobFront",
    "saddleRear",
    "saddleMid",
    "saddleFront",
    "tamingMobRear",
    "tamingMobMid",
    "tamingMobFront",
    "mobEquipMid",
    # Back hair, cape, back accessories.
    "backWing",
    "backHair",
    "backHairBelowCapWide",
    "backHairBelowCapNarrow",
    "backHairBelowCap",
    "backHairOverCape",
    "backCape",
    "cape",
    "capeBelowBody",
    "backShieldBelowBody",
    "backShield",
    "backWeapon",
    "backWeaponOverHead",
    "backWeaponOverShield",
    "backWeaponOverGlove",
    "backCap",
    "backCapOverHair",
    "backCapAccessory",
    "backAccessoryEar",
    "backHead",
    "backBody",
    # Body / torso reference.
    "shieldBelowBody",
    "weaponBelowBody",
    "hairBelowBody",
    "body",
    # Pants / shoes stack (behind body parts that overlap).
    "backPantsBelowShoes",
    "backShoesBelowPants",
    "backShoes",
    "backPants",
    "pantsBelowShoes",
    "shoes",
    "shoesOverPants",
    "shoesTop",
    "pants",
    "pantsOverShoes",
    "backPantsOverShoesBelowMailChest",
    "pantsOverShoesBelowMailChest",
    "backMailChestBelowPants",
    "mailChestBelowPants",
    "backPantsOverMailChest",
    "pantsOverMailChest",
    "backMailChestAccessory",
    "backMailChestOverPants",
    "backMailChest",
    # Mail / coat torso.
    "shieldBelowArm",
    "gloveWristBelowMailArm",
    "gloveBelowMailArm",
    "mailArmBelowHeadOverMailChest",
    "mailArmBelowHead",
    "armBelowHeadOverMailChest",
    "armBelowHead",
    "shield",
    "shieldOverBody",
    # Coat / mail layers come BEFORE the default weapon slot so that
    # a held one-handed weapon (z="weapon") draws in front of the
    # mailChest plate — otherwise the coat covered the blade going
    # across the torso. The arm / mailArm still draw after the weapon
    # so the grip stays hidden behind the gripping arm.
    "coatBelowArmoverMail",
    "coat",
    "mail",
    "mailChest",
    "mailChestOverPants",
    "mailChestTop",
    # ``mailChestOverHighest`` is used by 184 coats / longcoats in stock
    # GMS v83 (e.g. 01050021). The literal "OverHighest" is misleading
    # — this is just the topmost coat layer in the mail/chest stack,
    # not "above everything". My earlier fallback put it at the very
    # end of the zmap which masked weapons and hands.
    "mailChestOverHighest",
    # Default weapon slot ("weapon" / "Weapon"): tucked between the
    # coat/mail and the arm so a held sword shows in front of the
    # torso garments but the arm + hand sit on top of the grip — what
    # Maple v83 stand1 looks like in-game. ``weaponBelowArm`` (some
    # two-handed weapons) lives here too so its blade likewise shows
    # over the coat while remaining hidden behind the arm proper.
    "weaponBelowArm",
    "weaponOveArm",  # appears in stock data — likely a Maple typo
    "weapon",
    "Weapon",  # case variant seen in stock data
    # Arms / hands / gloves.
    "arm",
    "armOverHair",
    "armOverHairBelowWeapon",
    # Coat / longcoat sleeve goes ON TOP of the bare arm — otherwise
    # the body image's skin-colored arm canvas covers the sleeve and
    # mailArm becomes invisible.
    "mailArm",
    "mailArmOverHair",
    "mailArmOverHairBelowWeapon",
    "weaponOverArmBelowHead",
    "weaponBelowHand",
    "hand",
    "handBelowWeapon",
    "weaponWrist",
    "weaponWristOverGlove",
    "gloveWristBelowWeapon",
    "gloveWristBelowHead",
    "gloveBelowWeapon",
    "gloveBelowHead",
    "backGlove",
    "backGloveWrist",
    "gloveWrist",
    "gloveWristOverBody",
    "glove",
    "gloveOverBody",
    # Head, ear, face.
    "head",
    "ear",
    "accessoryEarBelowFace",
    "accessoryFaceBelowFace",
    "face",
    "faceOverHair",
    "hairShade",
    "hair",
    "hairOverHead",
    # Cap layers (above hair, below face accessories that overlap caps).
    "capeOverHead",
    "capBelowBody",
    "capBelowAccessory",
    "capAccessoryBelowBody",
    "capAccessoryBelowAccFace",
    "cap",
    "capOverHair",
    "capAccessory",
    # Front-of-hand / two-handed weapon variants. These sit ABOVE head
    # / hair / cap so a vertically-held spear or polearm shaft remains
    # visible past the face — matches MapleStory's stand2 in-game look.
    "weaponOverGlove",
    "weaponOverArm",
    "weaponOverHand",
    "weaponOverBody",
    # ``*OverHair`` slots fire AFTER the weapon stack so the body's
    # stand2 ``hand`` canvas (z=handOverHair) and any glove ``*OverHair``
    # variants land on top of a vertically-held spear/polearm — making
    # both gripping hands visible across the shaft. ``handOverHair`` was
    # previously listed before the weapon stack and got covered.
    "handOverHair",
    "gloveOverHair",
    "gloveWristOverHair",
    # Face / eye / ear accessories — top of the stack.
    "accessoryFace",
    "accessoryFaceOverFaceBelowCap",
    "accessoryFaceOverFaceAcc",
    "accessoryFaceOverEar",
    "accessoryFaceOverCap",
    "accessoryFaceUpperOverCap",
    "accessoryEyes",
    "accessoryEye",
    "accessoryEyeOverCap",
    "accessoryEar",
    "shieldOverHair",
    "emotionOverBody",
)

# Per-category candidate frame paths to walk when collecting render leaves.
# We try each path in order, dedupe by canvas identity (UOLs into the same
# target only contribute once), and combine the results.
# NOTE: ``backDefault`` is intentionally excluded. It's the back-facing-pose
# variant Maple swaps in when the character is shown from behind (rope/
# ladder climbing), and contains canvases like ``backHair`` /
# ``backHairBelowCap`` whose only purpose is to fill in the back of the
# head when you can't see the face. Including them in our front-facing
# stand1 composite was rendering back-of-head hair behind the body and
# masquerading as a fallback for missing ``default/hairBelowBody``,
# which is wrong: hair styles without ``hairBelowBody`` simply don't
# show flowing hair behind the torso in front-facing poses.
SUPPORTED_POSES: Tuple[str, ...] = ("stand1", "stand2")
DEFAULT_POSE = "stand1"


def _frame_paths(category: str, pose: str) -> Tuple[str, ...]:
    """Return ordered candidate frame paths for collecting render leaves.

    Pose-aware: ``stand2`` swaps in the two-handed body / coat / weapon
    canvases. Hair / cap / face / accessories ignore pose because their
    static canvases live under ``default`` (and ``stand1/0`` /
    ``stand2/0`` only contain UOLs back to default)."""
    pf = f"{pose}/0"
    if category == "Body":      return (pf,)
    if category == "Head":      return (pf, "front")
    if category == "Hair":      return (pf, "default")
    if category == "Face":      return ("default",)
    if category == "Cap":       return (pf, "default")
    if category == "Cape":      return (pf,)
    if category == "Coat":      return (pf,)
    if category == "Longcoat":  return (pf,)
    if category == "Pants":     return (pf,)
    if category == "Shoes":     return (pf,)
    if category == "Glove":     return (pf,)
    if category == "Shield":    return (pf,)
    if category == "Weapon":    return (pf,)
    if category in ("FaceAcc", "Glass", "Earring"): return ("default",)
    return (pf, "default")


# ── helpers ──────────────────────────────────────────────────────────────

def category_for_id(equip_id: str) -> Optional[str]:
    """Return the category for an equip ID like ``"01302000"``, or None."""
    if not equip_id or not equip_id.isdigit():
        return None
    return _CATEGORY_BY_ID_PREFIX.get(int(equip_id) // 10000)


def _resolve_uol(node: Optional[WzProperty]) -> Optional[WzProperty]:
    """Follow a UOL chain to its non-UOL target. Same idea as the
    server-side helper, but local so the renderer doesn't depend on Flask."""
    seen: set = set()
    cur: Optional[WzProperty] = node
    for _ in range(16):
        if cur is None or not isinstance(cur, WzUolProperty):
            return cur
        if id(cur) in seen:
            return None
        seen.add(id(cur))
        target_str = cur.value
        if not target_str or cur.parent is None:
            return None
        cur = cur.parent.get(target_str)
    return None


def _vec(prop: Optional[WzProperty]) -> Optional[Tuple[int, int]]:
    p = _resolve_uol(prop) if prop is not None else None
    if isinstance(p, WzVectorProperty):
        return (p.x, p.y)
    return None


def _string(prop: Optional[WzProperty]) -> Optional[str]:
    p = _resolve_uol(prop) if prop is not None else None
    if isinstance(p, WzStringProperty):
        return p.value
    return None


def _origin(canvas: WzCanvasProperty) -> Tuple[int, int]:
    return _vec(canvas.child("origin")) or (0, 0)


def _map_point(canvas: WzCanvasProperty, name: str) -> Optional[Tuple[int, int]]:
    map_node = canvas.child("map")
    if isinstance(map_node, WzSubProperty):
        return _vec(map_node.child(name))
    return None


def _map_anchors(canvas: WzCanvasProperty) -> Dict[str, Tuple[int, int]]:
    """All ``map/<name>`` vectors as a dict (UOL-resolved)."""
    out: Dict[str, Tuple[int, int]] = {}
    map_node = canvas.child("map")
    if isinstance(map_node, WzSubProperty):
        for c in map_node.children():
            v = _vec(c)
            if v is not None:
                out[c.name] = v
    return out


def _z_slot(canvas: WzCanvasProperty) -> Optional[str]:
    return _string(canvas.child("z"))


# ── per-part canvas collection ────────────────────────────────────────────

@dataclass
class _Placement:
    equip_id: str
    category: str
    name: str                          # leaf canvas name (e.g. "body", "arm")
    canvas: WzCanvasProperty           # owns the metadata: origin / map / z
    pixel_canvas: WzCanvasProperty     # owns the actual pixels (== canvas, except
                                       # for hierarchical _outlink placeholders)
    origin: Tuple[int, int]
    map_anchors: Dict[str, Tuple[int, int]]
    z_slot: Optional[str]
    top_left: Optional[Tuple[int, int]] = None


def _is_cash_weapon(equip_id: str) -> bool:
    """Cash weapons (170xxxx range) nest their actions one level deeper
    inside numeric weapon-num children like ``30`` / ``31`` / …"""
    return bool(equip_id) and equip_id.startswith("0170")


def _has_action(node: WzSubProperty, action: str) -> bool:
    """True if ``<node>/<action>/0`` resolves to a real SubProperty
    (handles UOLs that redirect e.g. ``41/stand2`` → ``../43/stand2``)."""
    n = node.get(action)
    n = _resolve_uol(n) if isinstance(n, WzUolProperty) else n
    if not isinstance(n, WzSubProperty):
        return False
    fr = n.get("0")
    fr = _resolve_uol(fr) if isinstance(fr, WzUolProperty) else fr
    return isinstance(fr, WzSubProperty)


def _pose_data_home(numeric_root: WzSubProperty, pose: str) -> Optional[WzSubProperty]:
    """Walk ``numeric_root.<pose>`` through any UOLs and return the
    weapon-num SubProperty that *physically* owns the pose data.

    Cash weapons (e.g. ``01702087``) commonly have multiple
    weapon-nums where only one ships the real ``stand2`` tree and the
    rest carry ``stand2`` as a UOL pointing at it (``../43/stand2``).
    Returning the UOL'd numeric child as the render root makes the
    later ``base.get("stand2/0")`` lookup fail (``WzProperty.get``
    doesn't follow intermediate UOLs), so we always resolve to the
    home that owns the canvas tree.
    """
    pose_node = numeric_root.child(pose)
    resolved = _resolve_uol(pose_node) if isinstance(pose_node, WzUolProperty) else pose_node
    if not isinstance(resolved, WzSubProperty):
        return None
    fr0 = resolved.get("0")
    fr0 = _resolve_uol(fr0) if isinstance(fr0, WzUolProperty) else fr0
    if not isinstance(fr0, WzSubProperty):
        return None
    home = resolved.parent
    if isinstance(home, WzSubProperty):
        return home
    return numeric_root


def _render_root(img: WzImage, category: str, equip_id: str, pose: str) -> WzProperty:
    """Return the SubProperty under which the per-action subtrees live.

    For ordinary parts that's the .img root (``stand1`` / ``walk1`` /
    ``default`` etc. live directly underneath). Cash weapons need an
    extra hop: the .img root holds numeric weapon-num children, and the
    pose's action tree lives inside *one* of them — but stand1 and
    stand2 may be owned by *different* numeric children (in
    ``01702087`` stand1 is in ``44``, stand2 in ``43``), and the rest
    of the children just UOL into them. We resolve the UOL chain so the
    render root is the actual data owner, not a UOL stub.
    """
    root = img.parse()
    if category == "Weapon" and _is_cash_weapon(equip_id):
        numeric = sorted(
            (c for c in root.children()
             if c.name.isdigit() and isinstance(c, WzSubProperty)),
            key=lambda c: int(c.name),
        )
        # First pass: smallest weapon-num whose <pose> data home exists.
        for c in numeric:
            home = _pose_data_home(c, pose)
            if home is not None:
                return home
        # Second pass: any supported pose (rare; weapon ships only one).
        for p in SUPPORTED_POSES:
            for c in numeric:
                home = _pose_data_home(c, p)
                if home is not None:
                    return home
        # Last resort: smallest numeric child, even without action data.
        if numeric:
            return numeric[0]
    return root


def _collect_part_canvases(
    img: WzImage, category: str, equip_id: str, pose: str,
    pkg_root: Optional[WzDirectory] = None,
) -> List[Tuple[str, WzCanvasProperty, WzCanvasProperty]]:
    """Return ``(leaf_name, metadata_canvas, pixel_canvas)`` triples to render.

    Walks each frame-path candidate from the appropriate render root,
    follows UOLs, and deduplicates by canvas identity. For hierarchical
    packs the per-frame canvas is a 1×1 placeholder with ``_outlink``
    pointing into a ``_Canvas`` sibling — we resolve that link and
    return the linked canvas as ``pixel_canvas`` while keeping the
    placeholder as the metadata canvas (it owns ``origin`` / ``map`` /
    ``z``). When no link is present, both fields point at the same
    canvas. ``pkg_root`` is the WZ root used for absolute outlink
    navigation; when ``None`` (legacy single-file Character.wz), only
    ``_inlink`` resolves."""
    base = _render_root(img, category, equip_id, pose)
    seen_ids: set = set()
    out: List[Tuple[str, WzCanvasProperty, WzCanvasProperty]] = []
    for path in _frame_paths(category, pose):
        node = base.get(path)
        node = _resolve_uol(node) if isinstance(node, WzUolProperty) else node
        if not isinstance(node, WzSubProperty):
            continue
        for child in node.children():
            target = _resolve_uol(child) if isinstance(child, WzUolProperty) else child
            if not isinstance(target, WzCanvasProperty):
                continue
            if id(target) in seen_ids:
                continue
            # Resolve _outlink/_inlink (a no-op when neither child is
            # present, returns the original canvas). For hierarchical
            # packs the placeholder is 1×1 and the link target carries
            # the real pixels; for legacy single-file Character.wz the
            # placeholder *is* the pixel canvas.
            pixels: WzCanvasProperty = target
            if (target.child("_outlink") is not None
                    or target.child("_inlink") is not None):
                root_for_link = pkg_root if pkg_root is not None \
                    else WzDirectory(name="")
                resolved = resolve_canvas_link(target, root_for_link)
                if isinstance(resolved, WzCanvasProperty):
                    pixels = resolved
            if not pixels.has_pixels():
                continue
            seen_ids.add(id(target))
            out.append((child.name, target, pixels))
    return out


def _determine_anchor(canvas: WzCanvasProperty, category: str) -> str:
    """Pick which ``map/<name>`` anchor to use for placement.

    Highest-priority anchor present on the canvas wins; falls back to the
    category default (e.g. ``brow`` for face, ``hand`` for weapon) so a
    canvas with an empty ``map`` still gets a consistent reference."""
    anchors = _map_anchors(canvas)
    for name in _ANCHOR_PRIORITY:
        if name in anchors:
            return name
    return _DEFAULT_ANCHOR_BY_CATEGORY.get(category, "navel")


# ── renderer ─────────────────────────────────────────────────────────────

class CharacterRenderer:
    """Compose static MapleStory character frames from a Character.wz."""

    def __init__(self, wz: WzFile, region: str = "GMS"):
        self.wz = wz
        self.region = region
        self._zmap: Tuple[str, ...] = self._load_zmap()

    # ── tree traversal helpers ──────────────────────────────────────────
    def _load_zmap(self) -> Tuple[str, ...]:
        """Try to read ``Base/zmap.img``; otherwise use the hardcoded order
        from HaCreator. Most HaSuite-exported Character.wz files don't ship
        Base/, so the fallback is the common case."""
        node = self.wz.root.get("Base/zmap.img")
        if isinstance(node, WzImage):
            try:
                node.parse()
                names = tuple(c.name for c in node.children())
                if names:
                    # zmap.img is ordered front-to-back (lower index = on top
                    # in the Maple client). HaCreator's ZMapReference is
                    # stored back-to-front; flip so our index convention is
                    # uniform: lower = behind.
                    return tuple(reversed(names))
            except Exception:
                pass
        return _DEFAULT_ZMAP

    def _z_index(self, slot: Optional[str]) -> int:
        if not slot:
            return len(self._zmap)  # no slot → drawn in front
        try:
            return self._zmap.index(slot)
        except ValueError:
            # Unknown slot — use the name to guess a reasonable position
            # rather than pinning to mid-stack (which dropped back-hair
            # variants like ``backHairBelowCapWide`` in front of the
            # body). Multiplied indices keep us inside the integer range
            # the regular zmap uses, with deliberate spacing so future
            # additions can slot in without rebalancing.
            anchor = self._heuristic_anchor(slot)
            return anchor

    def _heuristic_anchor(self, slot: str) -> int:
        """Best-effort placement for a previously-unseen z-slot."""
        n = len(self._zmap)
        s = slot.lower()
        # Anything starting with ``back`` is a back-side variant; keep it
        # well behind the body.
        if s.startswith("back"):
            try:
                # If we have a non-back equivalent, sit just behind it.
                base = slot[4].lower() + slot[5:] if len(slot) > 4 else slot
                return max(1, self._zmap.index(base) - 5)
            except (ValueError, IndexError):
                # Generic back layer — between shadow and body.
                return max(1, self._zmap.index("body") - 1) if "body" in self._zmap else 1
        # ``…Below…`` → sits behind the named target.
        for separator in ("Below", "below"):
            if separator in slot:
                target = slot.split(separator)[-1]
                target = target[0].lower() + target[1:] if target else target
                if target in self._zmap:
                    return max(1, self._zmap.index(target) - 1)
        # ``…Over…`` → sits in front of the named target.
        for separator in ("Over", "over"):
            if separator in slot:
                target = slot.split(separator)[-1]
                target = target[0].lower() + target[1:] if target else target
                if target in self._zmap:
                    return min(n - 1, self._zmap.index(target) + 1)
        # Default: front-leaning (small, decorative slots like
        # ``emotionOverBody`` should be visible).
        return n - 1

    def _open_part(self, equip_id: str) -> Optional[WzImage]:
        cat = category_for_id(equip_id)
        if cat is None:
            return None
        sub = CATEGORY_DIR.get(cat, "")
        path = f"{sub}/{equip_id}.img" if sub else f"{equip_id}.img"
        node = self.wz.root.get(path)
        return node if isinstance(node, WzImage) else None

    # ── public API ──────────────────────────────────────────────────────
    def list_parts(self, category: str) -> List[Dict[str, Any]]:
        """Enumerate available equip IDs in the given category.

        Returns
        ``[{"id": "01002000", "icon_paths": ["Cap/01002000.img/info/icon"]}, …]``.
        Each ``icon_paths`` entry is a list of WZ-relative thumbnail paths
        the client should try in order; the first one with pixel data
        wins, the rest are fallbacks. Hair styles in particular don't all
        ship a ``default/hairOverHead`` canvas (some short hair only has
        ``default/hair``), so we hand the client both candidates and let
        it pick — keeps ``list_parts`` from having to parse 1500 imgs to
        probe which canvas actually exists.
        """
        sub = CATEGORY_DIR.get(category)
        if sub is None:
            return []
        if sub == "":
            target = self.wz.root
        else:
            target = self.wz.root.get(sub)
            if not isinstance(target, WzDirectory):
                return []

        results: List[Dict[str, Any]] = []
        for img_name, img in target.images.items():
            # Match the category by ID prefix (so we don't surface non-equip
            # imgs like ``info.img`` if the WZ ever contains them).
            stem = img_name.split(".")[0]
            if not stem.isdigit():
                continue
            cat = category_for_id(stem)
            if cat != category:
                continue

            entry: Dict[str, Any] = {"id": stem}
            # Pick a sensible thumbnail path. Body / Head have no info
            # subdir; use front/head or stand1/0/body. Hair / Face have
            # a default frame canvas. Everything else has info/icon.
            prefix = f"{sub}/{img_name}" if sub else img_name
            if category == "Body":
                paths = [f"{prefix}/stand1/0/body"]
            elif category == "Head":
                paths = [f"{prefix}/front/head"]
            elif category == "Hair":
                # Most hairs have hairOverHead; some short styles
                # (e.g., 00030030) only ship the bare ``hair`` canvas.
                paths = [
                    f"{prefix}/default/hairOverHead",
                    f"{prefix}/default/hair",
                ]
            elif category == "Face":
                paths = [f"{prefix}/default/face"]
            else:
                paths = [f"{prefix}/info/icon"]
            entry["icon_paths"] = paths
            # Back-compat: keep ``icon_path`` set to the first candidate
            # so older clients that expected a single string still work.
            entry["icon_path"] = paths[0]
            results.append(entry)

        # Natural sort by ID.
        results.sort(key=lambda r: int(r["id"]))
        return results

    # ── pose discovery ──────────────────────────────────────────────────
    def get_weapon_poses(self, equip_id: str) -> List[str]:
        """Return the static-pose actions a weapon ships with: some
        subset of ``("stand1", "stand2")``. One-handed weapons usually
        return ``["stand1"]``, two-handed weapons ``["stand2"]``, and
        a few weapons (~96 of 1220 in stock GMS v83) return both — those
        are the ones the UI should expose a pose toggle for."""
        if category_for_id(equip_id) != "Weapon":
            return []
        img = self._open_part(equip_id)
        if img is None:
            return []
        root = img.parse()
        # Cash weapons: scan every numeric weapon-num. Non-cash: just
        # the .img root.
        if _is_cash_weapon(equip_id):
            roots = [c for c in root.children()
                     if c.name.isdigit() and isinstance(c, WzSubProperty)]
        else:
            roots = [root]
        seen: List[str] = []
        for p in SUPPORTED_POSES:
            for r in roots:
                if _has_action(r, p):
                    seen.append(p)
                    break
        return seen

    def detect_pose(self, equip_ids: List[str], requested: Optional[str] = None) -> str:
        """Pick a pose for a composite. Honors ``requested`` if the
        equipped weapon supports it; otherwise falls back to the
        weapon's first available pose, then to ``stand1``."""
        weapon_id = next(
            (e for e in equip_ids if category_for_id(e) == "Weapon"),
            None,
        )
        if weapon_id is None:
            return requested if requested in SUPPORTED_POSES else DEFAULT_POSE
        poses = self.get_weapon_poses(weapon_id) or [DEFAULT_POSE]
        if requested in poses:
            return requested
        return poses[0]

    def compose(self, equip_ids: List[str], pose: Optional[str] = None) -> Image.Image:
        """Render the equipped parts as a single RGBA :class:`PIL.Image`.

        ``pose`` is one of ``"stand1"`` / ``"stand2"`` and drives which
        body / coat / weapon canvases get pulled. If ``None`` (or an
        unsupported value) we auto-detect from the equipped weapon."""
        pose = self.detect_pose(equip_ids, pose)
        # Step 1: Per-part canvas collection.
        placements: List[_Placement] = []
        for eid in equip_ids:
            cat = category_for_id(eid)
            if cat is None:
                continue
            img = self._open_part(eid)
            if img is None:
                continue
            for leaf_name, canvas, pixel_canvas in _collect_part_canvases(
                img, cat, eid, pose, pkg_root=self.wz.root,
            ):
                placements.append(_Placement(
                    equip_id=eid, category=cat, name=leaf_name,
                    canvas=canvas, pixel_canvas=pixel_canvas,
                    origin=_origin(canvas), map_anchors=_map_anchors(canvas),
                    z_slot=_z_slot(canvas),
                ))

        if not placements:
            return Image.new("RGBA", (1, 1), (0, 0, 0, 0))

        # Step 2: Place body's "body" canvas first to anchor world (0, 0)
        # at body's navel. Then process the rest in priority order so body
        # sub-parts (arm, lHand, hand) and head are placed before
        # downstream parts that depend on them.
        def order_key(pl: _Placement) -> Tuple[int, int]:
            if pl.category == "Body":
                return (0, 0 if pl.name == "body" else 1)
            if pl.category == "Head":
                return (1, 0 if pl.name == "head" else 1)
            return (2, 0)
        placements.sort(key=order_key)

        world_anchors: Dict[str, Tuple[int, int]] = {}
        body_anchored = False

        for pl in placements:
            if pl.category == "Body" and pl.name == "body" and not body_anchored:
                # The canonical body. Place its navel at world (0, 0).
                # Map values are origin-relative, so:
                #   navel_world = top_left + origin + map[navel] = (0, 0)
                #   → top_left = -origin - map[navel]
                navel = pl.map_anchors.get("navel", (0, 0))
                pl.top_left = (-pl.origin[0] - navel[0],
                               -pl.origin[1] - navel[1])
                self._register_anchors(pl, world_anchors, overwrite=True)
                body_anchored = True
                continue

            anchor_name = _determine_anchor(pl.canvas, pl.category)
            anchor_world = world_anchors.get(anchor_name)
            map_pt = pl.map_anchors.get(anchor_name, (0, 0))
            if anchor_world is None:
                # No matching world anchor (rare — happens when the user
                # composes only a weapon with no body, etc.). Fall back to
                # placing the canvas's origin at world (0, 0).
                pl.top_left = (-pl.origin[0], -pl.origin[1])
            else:
                pl.top_left = (
                    anchor_world[0] - pl.origin[0] - map_pt[0],
                    anchor_world[1] - pl.origin[1] - map_pt[1],
                )
            self._register_anchors(pl, world_anchors, overwrite=False)

        # Step 3: Sort by z-slot back-to-front and composite.
        placements.sort(key=lambda p: self._z_index(p.z_slot))

        # Bounding box uses the *pixel* canvas dimensions — for
        # hierarchical packs ``pl.canvas`` is a 1×1 placeholder and the
        # real pixels live on ``pl.pixel_canvas`` in a sibling _Canvas
        # WZ. (For legacy single-file Character.wz they're the same
        # object so this is a no-op.)
        min_x = min(p.top_left[0] for p in placements if p.top_left is not None)
        min_y = min(p.top_left[1] for p in placements if p.top_left is not None)
        max_x = max(p.top_left[0] + p.pixel_canvas.width
                    for p in placements if p.top_left is not None)
        max_y = max(p.top_left[1] + p.pixel_canvas.height
                    for p in placements if p.top_left is not None)
        width = max(1, max_x - min_x)
        height = max(1, max_y - min_y)

        composite = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        for pl in placements:
            if pl.top_left is None:
                continue
            try:
                layer = decode_canvas(pl.pixel_canvas, region=self.region)
            except Exception:
                continue
            if layer.mode != "RGBA":
                layer = layer.convert("RGBA")
            composite.alpha_composite(
                layer,
                (pl.top_left[0] - min_x, pl.top_left[1] - min_y),
            )
        return composite

    # ── internals ──────────────────────────────────────────────────────
    def _register_anchors(
        self,
        placement: _Placement,
        world_anchors: Dict[str, Tuple[int, int]],
        *,
        overwrite: bool,
    ) -> None:
        """Add this canvas's map points to the shared world-anchor dict.

        ``overwrite`` is True only for the canonical body canvas (it
        defines the reference frame). Sub-parts (arm, head, lHand…) only
        contribute anchors that haven't been seen yet — that's how arm
        provides ``hand`` for weapons and head provides ``brow`` for face
        without stomping body's navel."""
        if placement.top_left is None:
            return
        ox, oy = placement.origin
        tx, ty = placement.top_left
        for name, vec in placement.map_anchors.items():
            if not overwrite and name in world_anchors:
                continue
            world_anchors[name] = (tx + ox + vec[0], ty + oy + vec[1])
