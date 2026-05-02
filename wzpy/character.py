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

# Categories for which ``list_parts`` reads ``info/cash`` so the UI can
# split the grid into Cash / Non-Cash sub-tabs. Skipped for Body / Head
# (always non-cash character bases) and Hair / Face (character looks,
# and parsing 16k Hair imgs just to set a flag is a 30+s first-load
# regression nobody wants if the UI isn't filtering on it anyway).
_CASH_FILTERED_CATEGORIES: frozenset = frozenset({
    "Cap", "Coat", "Longcoat", "Pants", "Shoes", "Glove",
    "Cape", "Shield", "FaceAcc", "Glass", "Earring", "Weapon",
})

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
    # Back-of-cap canvases sit BEFORE ``hairBelowBody`` so long hair
    # hangs in front of brims / hood-wraps / back-pieces (e.g.
    # 01002437's ``defaultAc`` z=``capBelowBody``) instead of the
    # cap covering the hair. Body still draws on top so the brim
    # disappears behind the torso.
    #
    # Caps like 01001036 / 01000000 (``defaultAc`` z=``capBelowBody``
    # / ``capAccessoryBelowBody``) and the back-hanging full-helmet
    # parts on 01003934 (``capBelowHead``) / 01003817 (``capBelowHair``)
    # all share this "behind everything except for whatever body /
    # back hair covers them" placement.
    "capBelowHead",
    "capBelowHair",
    "capBelowBody",
    "capAccessoryBelowBody",
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
    "accessoryEyeBelowFace",
    "face",
    "faceOverHair",
    "hairShade",
    "hair",
    # Face / eye / ear accessories whose z slot doesn't end in
    # ``*OverCap``. Per the canonical v83 zmap these sit ABOVE the
    # face / hair canvas but BELOW ``hairOverHead`` so bangs cover
    # them — what the user expects when no cap is equipped (otherwise
    # cap-less glasses, earrings, and most FaceAcc render on top of
    # the bangs).
    "accessoryEar",
    "accessoryFace",
    "accessoryFaceOverFaceBelowCap",
    "accessoryFaceOverFaceAcc",
    "accessoryFaceOverEar",
    "accessoryEyes",
    "accessoryEye",
    # Synthetic slot used by ``_OVER_CAP_REMAP`` as the target for
    # cap canvases (z=``cap`` / ``capOverHair`` / ``capAccessory``)
    # when the cap doesn't actually hide any hair (e.g., a headband
    # with ``vslot=Cp`` / ``CpH5``). Sits just above the face / eye
    # accessory slots and just below ``hairOverHead`` so the cap
    # rides on top of the face but bangs still cover the front of
    # it. Real WZ data never declares this slot directly.
    "capBelowHairOverHead",
    "hairOverHead",
    # Cap layers (above hair, below face accessories that overlap caps).
    # ``capBelowBody`` and ``capAccessoryBelowBody`` are NOT in this
    # block — they live with the back-of-body cluster up above so the
    # body covers them as their slot names promise.
    "capeOverHead",
    "capBelowAccessory",
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
    # Face / eye accessories that explicitly sit ABOVE the cap
    # (named ``*OverCap``). These render at the very top of the
    # stack only when a cap is actually equipped — when no cap is on,
    # ``compose`` remaps each one to its non-OverCap sibling so the
    # bangs (hairOverHead) cover the glasses / face accessory like
    # they should. See ``_OVER_CAP_REMAP``.
    "accessoryFaceOverCap",
    "accessoryFaceUpperOverCap",
    "accessoryEyeOverCap",
    "shieldOverHair",
    "emotionOverBody",
)


# Conditional z-slot remap applied during compose. Each entry is
# ``(target_slot, vslot_token)``: the canvas's declared z-slot is
# rewritten to ``target_slot`` whenever the remap should kick in. The
# remap kicks in if EITHER:
#   1. The equipped cap (if any) doesn't hide any hair, leaving the
#      bangs visible (so ``*OverCap`` accessories drop behind them).
#   2. The equipped cap's vslot lists ``vslot_token``, meaning the
#      cap claims the accessory's slot — e.g., a hat with vslot
#      containing ``Ay`` covers the eye-accessory area, so an
#      ``accessoryEyeOverCap`` glass must render BEHIND that cap
#      regardless of what the slot name says (caps 01004141..48 vs
#      glass 01022032). ``vslot_token=None`` means "only the
#      doesn't-hide-hair arm applies" — used for the bare ``cap``
#      slot which is about bangs visibility, not accessory cover.
# With a hair-hiding cap on AND no matching vslot token, the remap
# is skipped — the canvases stay at their declared slot.
#
# ``capOverHair`` and ``capAccessory`` are deliberately left out:
# their slot names promise "above hair / above the cap canvas",
# which the user wants honored even when the cap doesn't hide hair
# (that's how 01002575 / 01002576 / 01002598 / 01002842 render in
# front of the bangs).
_OVER_CAP_REMAP: Dict[str, Tuple[str, Optional[str]]] = {
    "accessoryEyeOverCap": ("accessoryEye", "Ay"),
    "accessoryFaceOverCap": ("accessoryFace", "Af"),
    "accessoryFaceUpperOverCap": ("accessoryFace", "Af"),
    "cap": ("capBelowHairOverHead", None),
}


def _parse_vslot_tokens(vslot: Optional[str]) -> frozenset:
    """Split a cap's vslot string into its 2-character tokens.

    The vslot is a concatenation of two-char slot names like ``Cp``,
    ``H1``, ``Hd``, ``Af``, ``Ay``, ``Ae``. We don't need to validate
    the alphabet — anything length-2 is a token, and unknown tokens
    just don't match any of the rules that consume them.
    """
    if not vslot:
        return frozenset()
    return frozenset(vslot[i:i + 2] for i in range(0, len(vslot), 2))

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

# Each Head image ships its ``head`` canvas alongside one or more ear
# variants under ``front/`` (e.g. ``humanEar``, ``lefEar``,
# ``highlefEar``). The real client picks one to render based on the
# character's race; we mirror that by filtering Head canvases to keep
# only ``head`` plus the canvas matching the selected ``ear_type``.
DEFAULT_EAR_TYPE = "humanEar"


# Caps occupy "visual slots" listed in ``info/vslot``: a concatenation
# of two-character tokens like ``Cp`` (the cap itself), ``H1``..``H6``
# (hair sub-slots), ``Hd``, ``Hs``, ``Hf``, ``Hb`` (more hair), and
# ``Af``/``Ay``/``As``/``Ae``/``Fc`` (face/eye/ear accessories).
#
# Hair-hiding decision is token-based — the original length-cutoff
# heuristic from MapleNecrocer's ``CapType`` derivation
# (MapleCharacter.cs:301) misses edge cases like 01002470's
# ``CpHdH1H2H3H4`` (length exactly 12, clearly meant to hide hair).
# The token rule below replaces it without changing the answer for
# any of the canonical vslots:
#
#   * ``H2`` token present                          → full helmet
#                                                     (every Hair canvas hidden, returns ``None``)
#   * ``H1`` and ``H3`` tokens present              → hide the four
#                                                     "front-hair" canvases
#   * ``H1`` token present (alone or with H4..H6)   → hide ``hairOverHead`` + ``backHair``
#   * otherwise                                     → no hair hidden
#
# A returned ``None`` means "every Hair canvas is hidden". An empty
# frozenset means "show everything".
_CAP_HIDE_PARTIAL: frozenset = frozenset(
    {"hairOverHead", "backHair", "hairBelowBody", "backHairBelowCap"}
)
_CAP_HIDE_TOP: frozenset = frozenset({"hairOverHead", "backHair"})


def _cap_hidden_hair_canvases(vslot: Optional[str]) -> Optional[frozenset]:
    """Resolve a cap's ``info/vslot`` string to the set of Hair canvas
    names it covers, or ``None`` if it covers every Hair canvas."""
    tokens = _parse_vslot_tokens(vslot)
    if "H2" in tokens:
        return None
    if "H1" in tokens and "H3" in tokens:
        return _CAP_HIDE_PARTIAL
    if "H1" in tokens:
        return _CAP_HIDE_TOP
    return frozenset()


def _frame_paths(category: str, pose: str, frame: int = 0) -> Tuple[str, ...]:
    """Return ordered candidate frame paths for collecting render leaves.

    Pose-aware: ``stand2`` swaps in the two-handed body / coat / weapon
    canvases. ``frame`` selects which sub-frame of the pose to render
    — Body's ``stand1`` ships ``0`` / ``1`` / ``2`` (the standby
    breathing animation), and most equipment imgs follow suit so the
    full character can be cycled 0→1→2→1 in the preview. Hair / cap
    / face / accessories ignore pose AND frame because their static
    canvases live under ``default`` (the per-pose ``stand*`` subtrees
    are just UOLs back to it)."""
    pf = f"{pose}/{frame}"
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


# Color-encoding rules for Hair / Face. Each tuple is
# ``(base_divisor, color_modulus)`` such that ``id // base_divisor``
# is shared across colors and ``(id // (base_divisor // 10)) % 10``
# yields the color index. For Hair the color sits in the ones digit
# (last). For Face the color sits in the hundreds digit, e.g.
# ``00022017``, ``00022117`` … ``00022817`` are the same style.
_HAIR_COLOR_INDEX = (10, 1)     # base = id // 10, color = id % 10
_FACE_COLOR_INDEX = (1000, 100)  # base = (id//1000)*100 + id%100, color = (id//100)%10


def _dedupe_by_color(
    parts: List[Dict[str, Any]],
    rule: Tuple[int, int],
) -> List[Dict[str, Any]]:
    """Collapse color-variant groups to a single canonical entry.

    ``rule`` is ``(base_divisor, color_unit)``:
      * ``base_divisor``: divide the numeric ID by this and keep the
        quotient — this is the "style" key that variants share.
        For Hair (``10``), the last digit is dropped. For Face
        (``1000``), the hundreds digit and below are dropped.
      * ``color_unit``: divide the ID by this and modulo 10 to read
        out the color digit (1 for Hair's last digit, 100 for
        Face's hundreds digit).

    The smallest-numbered variant in each group becomes the canonical
    ``id`` so the icon thumbnail and default render show the leftmost
    color in the swatch row (typically black). The full set of
    available color indices is returned under ``colors`` so the UI
    can disable missing colors.
    """
    base_div, color_unit = rule
    grouped: Dict[int, Dict[str, Any]] = {}
    for entry in parts:
        n = int(entry["id"])
        # Strip the color digit out of the ID so styles that differ
        # only in color collapse to the same key. ``base = n - color
        # * color_unit`` is robust whether the color sits in the
        # ones, hundreds, or any other position.
        color = (n // color_unit) % 10
        base = n - color * color_unit
        existing = grouped.get(base)
        if existing is None:
            new_entry = dict(entry)
            new_entry["colors"] = [color]
            grouped[base] = new_entry
        else:
            existing["colors"].append(color)
            if n < int(existing["id"]):
                existing["id"] = entry["id"]
                existing["icon_paths"] = entry["icon_paths"]
                existing["icon_path"] = entry["icon_path"]
    out = list(grouped.values())
    for e in out:
        e["colors"].sort()
    out.sort(key=lambda r: int(r["id"]))
    return out


_INFO_ONLY: frozenset = frozenset({"info"})


def _read_cash_flag(img: WzImage) -> bool:
    """True when the image's ``info/cash`` is a non-zero int.

    Body / Head don't ship an ``info`` subdir at all, so they always
    return False — which matches reality (the character bases aren't
    cash items). Other categories typically carry ``info/cash = 0``
    for the regular ID range and ``info/cash = 1`` for cash-shop
    variants; the pre-built ID prefix isn't a reliable signal because
    cash and non-cash IDs share the same 4-digit prefix in stock data.

    Uses ``WzImage.parse_partial`` so we read just the ``info``
    subtree — for a Weapon img that's a few small scalars instead of
    the full pose / frame / canvas-metadata walk (~30x faster). The
    full-parse cache is left untouched so ``compose`` / canvas
    requests still trigger and cache a complete parse on demand.
    """
    try:
        info = img.parse_partial(only=_INFO_ONLY).get("info")
    except Exception:
        return False
    if not isinstance(info, WzSubProperty):
        return False
    cash = info.get("cash")
    try:
        return bool(cash and getattr(cash, "value", 0))
    except Exception:
        return False


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
    ear_type: str = DEFAULT_EAR_TYPE,
    hide_hair: frozenset = frozenset(),
    frame: int = 0,
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
    ``_inlink`` resolves.

    For Head, ``front/`` siblings other than ``head`` are treated as ear
    variants and filtered to just the one matching ``ear_type`` —
    matching MapleNecrocer's per-frame visibility filter so we don't
    composite (e.g.) ``humanEar`` and ``lefEar`` on top of each other.

    Frame paths are tried in priority order and treated as
    EXCLUSIVE: the first path that produces any canvases wins and
    later paths are skipped. ``stand1/0`` typically UOLs to the
    canvases that live under ``default`` / ``front`` for Hair /
    Head / standard caps, so UOL resolution + the existing
    ``id()``-based dedup already collapses them to the same
    placements. The exclusive walk fixes caps that ship distinct
    placeholders in both paths — 01003843's ``default/default``
    (z=``capOverHairOverHair``, a static rest frame) and
    ``stand1/0/0`` (z=``capOverHair``, a real action frame) both
    rendered before, doubling the cap. Caps with only a
    ``default/default`` (no ``stand1/0`` body) still render
    because the empty primary path leaves ``out_before == len(out)``
    and the loop falls through to ``default``.

    Within a single path the z-slot dedup still fires as a safety
    net so any same-z duplicates inside one frame collapse cleanly."""
    base = _render_root(img, category, equip_id, pose)
    seen_ids: set = set()
    seen_zslots: set = set()
    out: List[Tuple[str, WzCanvasProperty, WzCanvasProperty]] = []
    for path in _frame_paths(category, pose, frame):
        node = base.get(path)
        node = _resolve_uol(node) if isinstance(node, WzUolProperty) else node
        if not isinstance(node, WzSubProperty):
            continue
        out_before = len(out)
        for child in node.children():
            # For Head, every sibling of ``head`` is treated as an ear
            # variant (matches MapleNecrocer's per-frame visibility
            # filter in ``MapleCharacter.cs:1218``). Filter is name-based
            # rather than path-based because ``stand1/0`` UOLs into
            # ``front`` — without this the UOL'd ears get collected
            # before we ever reach ``front`` and the dedupe table hides
            # them from the explicit ``front`` filter.
            if category == "Head" and child.name not in ("head", ear_type):
                continue
            # For Hair, drop canvases the equipped Cap covers (mirrors
            # MapleCharacter.cs:1189-1212 — when DressCap and ShowHair
            # are both true the cap's vslot decides which hair canvases
            # stay visible). The "hide every Hair" case (full helmet)
            # is short-circuited in ``compose`` so we don't need a
            # special sentinel here.
            if category == "Hair" and child.name in hide_hair:
                continue
            target = _resolve_uol(child) if isinstance(child, WzUolProperty) else child
            if not isinstance(target, WzCanvasProperty):
                continue
            if id(target) in seen_ids:
                continue
            # Skip a canvas whose z slot is already covered by an
            # earlier frame-path. ``z`` is read after the canvas-type
            # check (cheap — just a child lookup) and ``None`` z
            # slots are NOT deduped because the heuristic anchor
            # falls back to per-canvas placement and treating them
            # all as a single slot would drop legitimate layers.
            z_slot = _z_slot(target)
            if z_slot is not None and z_slot in seen_zslots:
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
            if z_slot is not None:
                seen_zslots.add(z_slot)
            out.append((child.name, target, pixels))
        if len(out) > out_before:
            # Primary path produced canvases — don't walk the
            # fallback path. See docstring.
            break
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
            # Read ``info/cash`` only for categories the UI offers a
            # cash / non-cash sub-tab on — parsing the img on others
            # (Hair: 16k imgs, ~30s) is a wasted first-load regression
            # because the response field would never be consumed.
            if category in _CASH_FILTERED_CATEGORIES:
                entry["cash"] = _read_cash_flag(img)
            results.append(entry)

        # Natural sort by ID.
        results.sort(key=lambda r: int(r["id"]))

        # Hair / Face styles encode color in a specific digit position
        # (last digit for Hair, hundreds digit for Face). Most styles
        # ship the full color palette as consecutive IDs (Hair: 8
        # colors, Face: 9), so collapse the listing to one entry per
        # style, keep the smallest variant as ``id``, and surface the
        # available color indices so the client can build a color
        # picker that grays out colors the style doesn't ship.
        if category == "Hair":
            results = _dedupe_by_color(results, _HAIR_COLOR_INDEX)
        elif category == "Face":
            results = _dedupe_by_color(results, _FACE_COLOR_INDEX)
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

    def _cap_hair_filter(
        self, equip_ids: List[str],
    ) -> Tuple[bool, frozenset, frozenset]:
        """Find the equipped Cap's ``info/vslot`` and derive the
        per-canvas filters compose needs.

        Returns ``(hide_hair_full, hide_hair_set, vslot_tokens)``:
        - ``hide_hair_full``: the cap covers every Hair canvas (full
          helmet), so ``compose`` should skip the Hair part entirely.
        - ``hide_hair_set``: the specific Hair canvases the cap covers
          when not a full helmet.
        - ``vslot_tokens``: every 2-character token in the cap's
          vslot string (``Cp``, ``H1``, ``Ay``, ``Af``, …). The
          accessory tokens (``Af``/``Ay``/``Ae``/…) drive the
          "cap covers face accessories" arm of the z-sort remap.
        """
        cap_id = next(
            (e for e in equip_ids if category_for_id(e) == "Cap"), None,
        )
        if cap_id is None:
            return (False, frozenset(), frozenset())
        img = self._open_part(cap_id)
        if img is None:
            return (False, frozenset(), frozenset())
        info = img.parse().get("info")
        vslot: Optional[str] = None
        if isinstance(info, WzSubProperty):
            v = info.get("vslot")
            if isinstance(v, WzStringProperty):
                vslot = v.value
        tokens = _parse_vslot_tokens(vslot)
        hidden = _cap_hidden_hair_canvases(vslot)
        if hidden is None:
            return (True, frozenset(), tokens)
        return (False, hidden, tokens)

    def get_ear_types(self, equip_id: str) -> List[str]:
        """Return the ear-canvas names a Head image ships with.

        Walks ``Head/<id>.img/front/`` and returns every child canvas
        name except ``head`` (which is the face/scalp). Stock GMS v83
        Heads ship a single ear, but custom Heads can carry multiple
        (e.g. ``humanEar``, ``lefEar``, ``highlefEar``) and the client
        picks one to render via ``compose(..., ear_type=...)``."""
        if category_for_id(equip_id) != "Head":
            return []
        img = self._open_part(equip_id)
        if img is None:
            return []
        front = img.parse().get("front")
        if not isinstance(front, WzSubProperty):
            return []
        out: List[str] = []
        for child in front.children():
            if child.name == "head":
                continue
            target = _resolve_uol(child) if isinstance(child, WzUolProperty) else child
            if isinstance(target, WzCanvasProperty):
                out.append(child.name)
        return out

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

    def compose(
        self, equip_ids: List[str], pose: Optional[str] = None,
        ear_type: str = DEFAULT_EAR_TYPE,
        flip: bool = False,
        frame: int = 0,
        bbox: Optional[Tuple[int, int, int, int]] = None,
    ) -> Image.Image:
        """Render the equipped parts as a single RGBA :class:`PIL.Image`.

        ``pose`` is one of ``"stand1"`` / ``"stand2"`` and drives which
        body / coat / weapon canvases get pulled. If ``None`` (or an
        unsupported value) we auto-detect from the equipped weapon.

        ``ear_type`` is the canvas name under ``Head/<id>.img/front/``
        to render alongside ``head`` (e.g. ``humanEar``, ``lefEar``,
        ``highlefEar``). If the Head image doesn't ship a matching
        canvas the ear simply doesn't render — call
        :meth:`get_ear_types` first to enumerate what's available.

        ``flip=True`` mirrors the final composite horizontally, which
        is how MapleStory renders a right-facing character — the
        bitmaps are authored facing left and flipped at draw time.

        ``bbox`` overrides the auto-computed content bounding box
        ``(min_x, min_y, max_x, max_y)`` in world coordinates. Used
        by :meth:`compose_animation` to render every frame at the
        same image size with the navel at the same image-space
        pixel — without this, hair / cap stay put while the body
        bitmap leans left and right between frames (the body's per-
        frame canvas position differs by a few pixels) so the body
        appears to wobble in the cycling preview."""
        pose = self.detect_pose(equip_ids, pose)
        hide_hair_full, hide_hair_set, cap_vslot_tokens = \
            self._cap_hair_filter(equip_ids)
        placements = self._build_placements(
            equip_ids, pose, ear_type,
            hide_hair_full, hide_hair_set, cap_vslot_tokens, frame,
        )
        if not placements:
            return Image.new("RGBA", (1, 1), (0, 0, 0, 0))
        return self._render_placements(placements, flip=flip, bbox=bbox)

    def compose_animation(
        self, equip_ids: List[str], pose: Optional[str] = None,
        ear_type: str = DEFAULT_EAR_TYPE,
        flip: bool = False,
        frames: Tuple[int, ...] = (0, 1, 2),
    ) -> List[Image.Image]:
        """Compose multiple frames at consistent image dimensions and
        with frozen anchor positions across frames.

        The body's per-frame canvas advertises slightly different
        anchor offsets — neck moves zig-zag ``(4,-11) → (3,-12) →
        (2,-11)`` for body 00002000, hand drifts by ~1px each frame
        — so equipment that anchors on neck (head / hair / cap /
        face) or hand (weapon) wobbles in lockstep with the
        breathing animation. To keep the animated preview from
        looking jittery, we render frame 0 normally, capture its
        world-anchor map, and reuse those frozen anchors when
        building placements for frames 1+2. The body bitmap itself
        still gets its per-frame artwork (so the chest visibly
        breathes), only the *positions* of dependent parts stay
        locked. Then the union of every frame's bbox is taken so
        all returned images share canvas dimensions and the navel
        sits at the same image-space pixel.
        """
        pose = self.detect_pose(equip_ids, pose)
        hide_hair_full, hide_hair_set, cap_vslot_tokens = \
            self._cap_hair_filter(equip_ids)

        per_frame: List[List[_Placement]] = []
        frozen_anchors: Optional[Dict[str, Tuple[int, int]]] = None
        # Frame-0 placements drive two things across the rest of the
        # cycle:
        #
        # 1. ``frozen_anchors`` — head / hair / cap / face anchor on
        #    the values frame 0 registered, so they stay still.
        #
        # 2. Per-canvas TRANSLATION COMPENSATION. The body's per-frame
        #    bitmap is a different size each frame (21 → 22 → 23 px
        #    wide for stock body 00002000) and the navel sits at a
        #    different point within each bitmap. Aligning by the
        #    bitmap CENTER (rather than top-left or navel) splits
        #    the breathing expansion symmetrically between the left
        #    and right edges so the body silhouette looks like it's
        #    breathing in place instead of translating right then
        #    snapping back. Coat / longcoat / pants / glove ship
        #    per-frame bitmaps tuned to track the body, so we apply
        #    ``-delta`` to every placement whose pixel canvas isn't
        #    shared with frame 0 (i.e., the per-frame parts). Hair /
        #    cap / face / earring etc. UOL into ``default`` and share
        #    pixel canvases with frame 0, so they stay on their
        #    frozen anchor positions.
        frame0_canvases: Dict[Tuple[str, str], Any] = {}
        body_center_0: Optional[Tuple[int, int]] = None

        def _body_center(pls: List[_Placement]) -> Optional[Tuple[int, int]]:
            for pl in pls:
                if pl.category == "Body" and pl.name == "body" \
                        and pl.top_left is not None:
                    return (
                        pl.top_left[0] + pl.pixel_canvas.width // 2,
                        pl.top_left[1] + pl.pixel_canvas.height // 2,
                    )
            return None

        for f in frames:
            placements, anchors = self._build_placements(
                equip_ids, pose, ear_type,
                hide_hair_full, hide_hair_set, cap_vslot_tokens, f,
                frozen_anchors=frozen_anchors,
                return_anchors=True,
            )
            if frozen_anchors is None:
                frozen_anchors = anchors
                for pl in placements:
                    frame0_canvases[(pl.equip_id, pl.name)] = pl.pixel_canvas
                body_center_0 = _body_center(placements)
            else:
                body_center_now = _body_center(placements)
                if body_center_0 is not None and body_center_now is not None:
                    dx = body_center_0[0] - body_center_now[0]
                    dy = body_center_0[1] - body_center_now[1]
                    if dx or dy:
                        for pl in placements:
                            if pl.top_left is None:
                                continue
                            f0_canvas = frame0_canvases.get(
                                (pl.equip_id, pl.name)
                            )
                            if f0_canvas is pl.pixel_canvas:
                                continue
                            pl.top_left = (
                                pl.top_left[0] + dx,
                                pl.top_left[1] + dy,
                            )
            per_frame.append(placements)

        all_pls = [p for fr in per_frame for p in fr if p.top_left is not None]
        if not all_pls:
            return [Image.new("RGBA", (1, 1), (0, 0, 0, 0)) for _ in frames]
        bbox = (
            min(p.top_left[0] for p in all_pls),
            min(p.top_left[1] for p in all_pls),
            max(p.top_left[0] + p.pixel_canvas.width for p in all_pls),
            max(p.top_left[1] + p.pixel_canvas.height for p in all_pls),
        )
        return [
            self._render_placements(pls, flip=flip, bbox=bbox)
            if pls else Image.new(
                "RGBA",
                (max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])),
                (0, 0, 0, 0),
            )
            for pls in per_frame
        ]

    def _build_placements(
        self,
        equip_ids: List[str], pose: str, ear_type: str,
        hide_hair_full: bool, hide_hair_set: frozenset,
        cap_vslot_tokens: frozenset, frame: int,
        frozen_anchors: Optional[Dict[str, Tuple[int, int]]] = None,
        return_anchors: bool = False,
        body_frame: Optional[int] = None,
    ):
        """Collect, anchor, and z-sort placements for a single frame.

        Shared between :meth:`compose` (one frame) and
        :meth:`compose_animation` (multiple frames at consistent
        bbox). When ``frozen_anchors`` is provided, the body /
        sub-body canvases position themselves normally (their own
        per-frame navel still goes to world (0, 0)) but DO NOT
        register their other map anchors (neck / hand / lHand etc.).
        Instead, the supplied ``frozen_anchors`` are used for every
        downstream placement — keeps head / hair / cap / face /
        weapon at fixed image positions across frames so only the
        body bitmap actually breathes on screen.

        ``body_frame`` overrides the frame index used for the Body
        category only — :meth:`compose_animation` pins it to 0 so
        the body bitmap stays fixed across the cycling preview while
        other equipment (coat sleeves, capes, weapons with their own
        per-frame data) still animate. Without that, the body's
        per-frame bitmap shifts the torso left/up/down each frame —
        breathing motion that's faithful to the WZ data but reads
        as a wobble in the small preview.

        Returns the placements list, or ``(placements, world_anchors)``
        when ``return_anchors=True``.
        """
        cap_covers_hair = hide_hair_full or bool(hide_hair_set)
        placements: List[_Placement] = []
        for eid in equip_ids:
            cat = category_for_id(eid)
            if cat is None:
                continue
            if cat == "Hair" and hide_hair_full:
                continue
            img = self._open_part(eid)
            if img is None:
                continue
            f_for_part = body_frame if (body_frame is not None and cat == "Body") else frame
            for leaf_name, canvas, pixel_canvas in _collect_part_canvases(
                img, cat, eid, pose, pkg_root=self.wz.root,
                ear_type=ear_type, hide_hair=hide_hair_set, frame=f_for_part,
            ):
                placements.append(_Placement(
                    equip_id=eid, category=cat, name=leaf_name,
                    canvas=canvas, pixel_canvas=pixel_canvas,
                    origin=_origin(canvas), map_anchors=_map_anchors(canvas),
                    z_slot=_z_slot(canvas),
                ))

        if not placements:
            return (placements, {}) if return_anchors else placements

        def order_key(pl: _Placement) -> Tuple[int, int]:
            if pl.category == "Body":
                return (0, 0 if pl.name == "body" else 1)
            if pl.category == "Head":
                return (1, 0 if pl.name == "head" else 1)
            return (2, 0)
        placements.sort(key=order_key)

        # When frozen_anchors is set, downstream parts read from those
        # values and the body's anchor contributions are suppressed
        # (its bitmap still positions itself via its own navel
        # offset → world(0,0), but other anchor wobbles don't leak
        # through). Without it, behaviour matches the original
        # single-frame compose path.
        world_anchors: Dict[str, Tuple[int, int]] = (
            dict(frozen_anchors) if frozen_anchors is not None else {}
        )
        body_anchored = False

        for pl in placements:
            if pl.category == "Body" and pl.name == "body" and not body_anchored:
                navel = pl.map_anchors.get("navel", (0, 0))
                pl.top_left = (-pl.origin[0] - navel[0],
                               -pl.origin[1] - navel[1])
                if frozen_anchors is None:
                    self._register_anchors(pl, world_anchors, overwrite=True)
                body_anchored = True
                continue

            anchor_name = _determine_anchor(pl.canvas, pl.category)
            anchor_world = world_anchors.get(anchor_name)
            map_pt = pl.map_anchors.get(anchor_name, (0, 0))
            if anchor_world is None:
                pl.top_left = (-pl.origin[0], -pl.origin[1])
            else:
                pl.top_left = (
                    anchor_world[0] - pl.origin[0] - map_pt[0],
                    anchor_world[1] - pl.origin[1] - map_pt[1],
                )
            if frozen_anchors is None:
                self._register_anchors(pl, world_anchors, overwrite=False)

        def z_for(pl: _Placement) -> int:
            slot = pl.z_slot
            if slot is None:
                return self._z_index(slot)
            rule = _OVER_CAP_REMAP.get(slot)
            if rule is None:
                return self._z_index(slot)
            target, token = rule
            if not cap_covers_hair or (
                token is not None and token in cap_vslot_tokens
            ):
                slot = target
            return self._z_index(slot)
        placements.sort(key=z_for)
        return (placements, world_anchors) if return_anchors else placements

    def _render_placements(
        self, placements: List[_Placement], *, flip: bool = False,
        bbox: Optional[Tuple[int, int, int, int]] = None,
    ) -> Image.Image:
        """Composite the supplied placements into a single image. When
        ``bbox`` is None, the canvas is the tight bounding box of
        ``placements``; otherwise it's the supplied
        ``(min_x, min_y, max_x, max_y)`` so multiple frames can
        share dimensions and a stable navel position."""
        if bbox is None:
            min_x = min(p.top_left[0] for p in placements if p.top_left is not None)
            min_y = min(p.top_left[1] for p in placements if p.top_left is not None)
            max_x = max(p.top_left[0] + p.pixel_canvas.width
                        for p in placements if p.top_left is not None)
            max_y = max(p.top_left[1] + p.pixel_canvas.height
                        for p in placements if p.top_left is not None)
        else:
            min_x, min_y, max_x, max_y = bbox
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
        if flip:
            composite = composite.transpose(Image.FLIP_LEFT_RIGHT)
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
