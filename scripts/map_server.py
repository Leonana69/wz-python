#!/usr/bin/env python3
"""A tiny HTTP server that renders a MapleStory map's footholds / ropes / portals.

The coordinate pipeline is a **verbatim port of MapleSimulator's
``scripts/map_define_v2.py``** (``merge_platforms`` / ``merge_ropes`` /
``process_portals`` + the scaling & re-merge in ``process_map``), so the x/y
values it emits are **identical** to that script — verified byte-for-byte,
including platform list order, on map ``541000300``.

Coordinates are minimap-pixel space: ``(raw + centerX) // 2**mag`` for x (and
likewise y), with horizontals merged, ropes' ``y1`` bumped by 1, and ``sp``
(spawn) portals dropped. That space lines up 1:1 with the WZ ``miniMap/canvas``
bitmap, which is rendered underneath the overlay (no game-capture / draw-offset
step is needed — those offsets in the original only aligned to a screenshot).

Data sources — under ``--wz-folder`` (default ``data/v83``), each resolved as a
``<Name>/`` hierarchical pack folder or a ``<Name>.wz`` single file:
* ``Map``          — foothold / ladderRope / portal / miniMap for any map.
* ``String``       — ``Map.img`` map-name <-> code lookup.
* ``data/<code>.img`` (``--override-dir``) — if present, a standalone
                     (e.g. "new_classic") export is used in preference to the
                     Map source for that code, so maps revised since v83 match
                     their newer foothold set.

Skill ranges (optional overlay) come from a **separate** folder/region
(``--skill-folder``, default ``data/``; ``--skill-region``, default ``BMS``) so
the map's WZ region need not match the skill packs'. This is the same
``String`` + ``Skill`` + ``Packs`` trio the sibling ``skill_server`` reads: a
skill's ``lt``/``rb`` box is in raw world pixels, so dividing it by the map's
``2**mag`` scale places it in the very same minimap-pixel space as the
platforms and mobs — i.e. the range is drawn to the map's scale.

Usage::

    python scripts/map_server.py                       # serves data/v83 at :5002
    python scripts/map_server.py --wz-folder /path/to/wz --region GMS
    python scripts/map_server.py --map-wz path/to/Map.wz --string-wz path/to/String.wz
    python scripts/map_server.py --skill-folder data --skill-region BMS
    python scripts/map_server.py --no-skills            # map only, no overlay

Endpoints
---------
* ``GET /``                        the viewer
* ``GET /api/search?q=<name>``     ``[{code,name,street}]`` map-name autocomplete
* ``GET /api/map?code=<code>``     footholds / ropes / portals + minimap metadata
* ``GET /minimap/<code>.png``      the WZ minimap bitmap
* ``GET /api/skill_search?q=<s>``  ``{available,results:[{id,name}]}`` skill search
* ``GET /api/skill?id=<id>``       ``{matches:[{id,name,icon,ranges,range}]}``
* ``GET /api/skill?name=<name>``   same, for every skill matching a name
* ``GET /skill_icon/<id>.png``     the skill icon bitmap
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
for _p in (REPO_ROOT, SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from wzpy.canvas import decode_canvas                       # noqa: E402
from wzpy.crypto import WzKey                                # noqa: E402
from wzpy.properties import WzCanvasProperty, WzSubProperty  # noqa: E402
from wzpy.wz_file import WzFile                              # noqa: E402
from wzpy.wz_image import WzImage                            # noqa: E402
from wzpy.wz_package import WzPackage                        # noqa: E402

# Skill lookup (icons + lt/rb attack-range boxes) is reused verbatim from the
# sibling ``skill_server`` so a single map server can overlay skill ranges on
# the map. It is optional: if the skill data (String/Skill/Packs) is missing the
# map still renders and the skill endpoints simply report themselves unavailable.
from skill_server import SkillData                           # noqa: E402


def open_wz_component(folder: Path, name: str, region: str):
    """Open a WZ component named ``name`` under ``folder``.

    Prefers a ``<name>/`` **hierarchical pack** (opened as a :class:`WzPackage`),
    else falls back to a ``<name>.wz`` **single file** (a :class:`WzFile`). Both
    expose ``.root`` / ``.close``. Returns ``None`` when neither exists.
    """
    d = folder / name
    f = folder / f"{name}.wz"
    if d.is_dir():
        return WzPackage.open(str(d), region=region)
    if f.is_file():
        return WzFile.open(str(f), region=region)
    return None


# ── coordinate transform (verbatim from MapleSimulator/scripts/map_define_v2.py) ─
def merge_platforms(platforms, center_x, center_y):
    groups = defaultdict(list)
    for plat in platforms:
        x1 = plat['x1'] + center_x
        y1 = plat['y1'] + center_y
        x2 = plat['x2'] + center_x
        y2 = plat['y2'] + center_y
        if x1 == x2:
            continue
        if y1 == y2:
            groups[y1].append((min(x1, x2), max(x1, x2)))
        else:
            groups[(y1, y2)].append((x1, y1, x2, y2))
    merged = []
    for y, segs in groups.items():
        if isinstance(y, tuple):
            for s in segs:
                merged.append(s)
            continue
        segs.sort()
        cur_x1, cur_x2 = segs[0]
        for x1, x2 in segs[1:]:
            if x1 <= cur_x2 + 1:
                cur_x2 = max(cur_x2, x2)
            else:
                merged.append((cur_x1, y, cur_x2, y))
                cur_x1, cur_x2 = x1, x2
        merged.append((cur_x1, y, cur_x2, y))
    return merged


def merge_ropes(ropes, center_x, center_y):
    merged = []
    for rope in ropes:
        x1 = rope['x'] + center_x
        y1 = rope['y1'] + center_y
        x2 = rope['x'] + center_x
        y2 = rope['y2'] + center_y
        merged.append((x1, y1, x2, y2))
    return merged


def process_portals(portals, center_x, center_y):
    processed = []
    for portal in portals:
        x = portal['x'] + center_x
        y = portal['y'] + center_y
        if portal['pn'] == 'sp':
            continue
        processed.append({"x": x, "y": y, "name": portal['pn'],
                          "target": portal['tn'], "type": portal['tm']})
    return processed


def process_coords(minimap, raw_plats, raw_ropes, raw_portals):
    """The coordinate half of ``map_define_v2.process_map`` (no game capture /
    draw offsets). Returns the same ``platforms`` / ``ropes`` / ``portals``
    structures that script's ``/load`` endpoint returns."""
    plats = merge_platforms(raw_plats, minimap['centerX'], minimap['centerY'])
    ropes = merge_ropes(raw_ropes, minimap['centerX'], minimap['centerY'])
    portals = process_portals(raw_portals, minimap['centerX'], minimap['centerY'])

    scale = 2 ** minimap['mag']
    map_width = minimap['width'] // scale
    map_height = minimap['height'] // scale

    scaled_plats = [(p[0] // scale, p[1] // scale, p[2] // scale, p[3] // scale)
                    for p in plats]

    def _merge_scaled_once(ps):
        groups = defaultdict(list)
        others = []
        for x1, y1, x2, y2 in ps:
            if y1 == y2:
                groups[y1].append((min(x1, x2), max(x1, x2)))
            else:
                others.append((x1, y1, x2, y2))
        out = list(others)
        for y, segs in groups.items():
            segs.sort()
            cx1, cx2 = segs[0]
            for x1, x2 in segs[1:]:
                if x1 <= cx2 + 1:
                    cx2 = max(cx2, x2)
                else:
                    out.append((cx1, y, cx2, y))
                    cx1, cx2 = x1, x2
            out.append((cx1, y, cx2, y))
        return out

    prev_len = -1
    while len(scaled_plats) != prev_len:
        prev_len = len(scaled_plats)
        scaled_plats = _merge_scaled_once(scaled_plats)

    platforms_data = []
    for x1, y1, x2, y2 in scaled_plats:
        if x1 == x2:
            continue
        if x1 > x2:
            x1, x2 = x2, x1
            y1, y2 = y2, y1
        platforms_data.append({'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2})

    ropes_data = [{'x1': r[0] // scale, 'y1': r[1] // scale + 1,
                   'x2': r[2] // scale, 'y2': r[3] // scale} for r in ropes]
    portals_data = [{'x': p['x'] // scale, 'y': p['y'] // scale, 'name': p['name'],
                     'target': p['target'], 'type': int(p['type'])} for p in portals]
    return {
        'width': map_width, 'height': map_height,
        'platforms': platforms_data, 'ropes': ropes_data, 'portals': portals_data,
    }


# ── WZ property tree → plain nested dict (mirrors _children_to_dict) ──────────
def wz_to_dict(node) -> dict:
    out: dict = {}
    for c in node.children():
        if isinstance(c, WzCanvasProperty):
            continue                      # bitmaps handled separately
        if isinstance(c, WzSubProperty):
            out[c.name] = wz_to_dict(c)
        else:
            v = getattr(c, "value", None)
            if v is None and hasattr(c, "x"):
                v = {"x": c.x, "y": c.y}
            out[c.name] = v
    return out


def _flatten_map(top: dict):
    """foothold/ladderRope/portal of a map img → the flat lists the transform
    expects. ``foothold`` is layer → group → id → {x1,y1,x2,y2}."""
    foothold = top.get("foothold", {}) or {}
    platforms = []
    for layer in foothold.values():
        if not isinstance(layer, dict):
            continue
        for group in layer.values():
            if not isinstance(group, dict):
                continue
            for plat in group.values():
                if isinstance(plat, dict) and "x1" in plat:
                    platforms.append(plat)
    ropes = [r for r in (top.get("ladderRope", {}) or {}).values()
             if isinstance(r, dict) and "x" in r]
    portals = [p for p in (top.get("portal", {}) or {}).values()
               if isinstance(p, dict) and "pn" in p]
    return platforms, ropes, portals


# ── data access layer ────────────────────────────────────────────────────
class MapData:
    """Opens v83 Map.wz + String.wz, answers name search / map info / minimap."""

    def __init__(self, wz_folder: Path, region: str = "GMS",
                 override_dir: Optional[Path] = None,
                 map_path: Optional[str] = None, string_path: Optional[str] = None):
        self.wz_folder = Path(wz_folder)
        self.region = region
        self.override_dir = Path(override_dir) if override_dir else None
        self.map_path = Path(map_path) if map_path else None
        self.string_path = Path(string_path) if string_path else None
        self._lock = threading.RLock()
        self._loaded = False
        self._map = None                                   # WzFile or WzPackage
        self._string = None                                # WzFile or WzPackage
        self._names: List[Tuple[str, str, str]] = []       # (code, name, street)
        self._name_by_code: Dict[str, Tuple[str, str]] = {}
        self._img_cache: Dict[str, object] = {}            # code -> parsed img root
        self._info_cache: Dict[str, dict] = {}
        self._mob_names: Optional[Dict[str, str]] = None   # mob id -> display name

    def open(self) -> None:
        self._ensure()

    def close(self) -> None:
        for f in (self._map, self._string):
            try:
                if f is not None:
                    f.close()
            except Exception:
                pass

    def _open_source(self, name: str, explicit: Optional[Path]):
        """Resolve one component: an explicit path (folder or .wz) wins,
        otherwise ``<wz_folder>/<name>`` folder or ``<wz_folder>/<name>.wz``."""
        if explicit is not None:
            if explicit.is_dir():
                return WzPackage.open(str(explicit), region=self.region)
            return WzFile.open(str(explicit), region=self.region)
        handle = open_wz_component(self.wz_folder, name, self.region)
        if handle is None:
            raise FileNotFoundError(
                f"could not find {name}/ folder or {name}.wz in {self.wz_folder}")
        return handle

    def _ensure(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._map = self._open_source("Map", self.map_path)
            self._string = self._open_source("String", self.string_path)
            self._build_name_index()
            self._loaded = True

    def _build_name_index(self) -> None:
        img = self._string.root.get("Map.img")
        if img is None:
            return
        for cat in img.parse().children():
            if not isinstance(cat, WzSubProperty):
                continue
            for node in cat.children():
                if not node.name.isdigit():
                    continue
                name = getattr(node.get("mapName"), "value", None)
                street = getattr(node.get("streetName"), "value", None) or ""
                if name:
                    self._names.append((node.name, name, street))
                    self._name_by_code[node.name] = (name, street)

    # ── map image sourcing (standalone override wins over v83) ────────────
    def _img_root(self, code: str):
        if code in self._img_cache:
            return self._img_cache[code]
        root = None
        # 1. standalone data/<code>.img (e.g. a new_classic export) if present
        if self.override_dir is not None:
            p = self.override_dir / f"{code}.img"
            if p.is_file():
                img = WzImage.from_bytes(p.read_bytes(),
                                         key=WzKey.for_region(self.region),
                                         name=f"{code}.img")
                root = img.parse()
        # 2. else the map from v83 Map.wz
        if root is None:
            padded = code.zfill(9)
            img = self._map.root.get(f"Map/Map{padded[0]}/{padded}.img")
            if img is None:      # fall back to scanning the Map<N> subdirs
                mapdir = self._map.root.get("Map")
                if mapdir is not None:
                    for sub in mapdir.subdirs:
                        cand = mapdir.child(sub).get(f"{padded}.img")
                        if cand is not None:
                            img = cand
                            break
            root = img.parse() if img is not None else None
        self._img_cache[code] = root
        return root

    # ── name lookup ───────────────────────────────────────────────────────
    def search(self, query: str, limit: int = 50) -> List[Dict[str, str]]:
        self._ensure()
        q = query.strip().lower()
        if not q:
            return []
        # a bare number is a direct code query
        if q.isdigit():
            hits = [(c, n, s) for (c, n, s) in self._names if c.startswith(q)]
        else:
            hits = [(c, n, s) for (c, n, s) in self._names if q in n.lower()]

        def rank(t):
            c, n, s = t
            low = n.lower()
            r = 0 if low == q else (1 if low.startswith(q) else 2)
            return (r, len(n), low)

        hits.sort(key=rank)
        return [{"code": c, "name": n, "street": s} for c, n, s in hits[:limit]]

    # ── full map info ─────────────────────────────────────────────────────
    def map_info(self, code: str) -> dict:
        self._ensure()
        code = str(int(code)) if code.isdigit() else code
        if code in self._info_cache:
            return self._info_cache[code]
        name, street = self._name_by_code.get(code, (None, None))
        info: dict = {"code": code, "name": name, "street": street,
                      "width": 0, "height": 0, "platforms": [], "ropes": [],
                      "portals": [], "mobs": [], "minimap": None, "error": None}
        root = self._img_root(code)
        if root is None:
            info["error"] = f"map {code} not found"
            self._info_cache[code] = info
            return info
        top = wz_to_dict(root)
        minimap = top.get("miniMap")
        if not minimap or "mag" not in minimap:
            info["error"] = f"map {code} has no miniMap block"
            self._info_cache[code] = info
            return info
        plats, ropes, portals = _flatten_map(top)
        coords = process_coords(minimap, plats, ropes, portals)
        info.update(coords)
        info["mobs"] = self._process_mobs(top.get("life", {}), minimap)
        info["minimap"] = f"/minimap/{code}.png" if self._has_minimap(root) else None
        info["center"] = [minimap.get("centerX"), minimap.get("centerY")]
        info["mag"] = minimap.get("mag")
        self._info_cache[code] = info
        return info

    # ── mob spawns (from the map's ``life`` node) ─────────────────────────
    def _process_mobs(self, life: dict, minimap: dict) -> List[dict]:
        """Every ``type == 'm'`` entry in ``life`` -> a spawn point in the same
        minimap-pixel space as the platforms: ``(x + centerX) // 2**mag`` etc.
        Uses ``cy`` (the foothold the mob stands on) for y, falling back to
        ``y``. Carries the mob id, name, respawn time and patrol x-range."""
        scale = 2 ** minimap["mag"]
        cx0, cy0 = minimap["centerX"], minimap["centerY"]
        out: List[dict] = []
        for v in (life or {}).values():
            if not isinstance(v, dict) or v.get("type") != "m":
                continue
            x = v.get("x")
            y = v.get("cy")
            if y is None:
                y = v.get("y")
            if x is None or y is None:
                continue
            mid = str(v.get("id"))
            entry = {
                "x": (x + cx0) // scale,
                "y": (y + cy0) // scale,
                "id": mid,
                "name": self._mob_name(mid),
                "mobTime": v.get("mobTime"),
                "hide": v.get("hide", 0),
            }
            rx0, rx1 = v.get("rx0"), v.get("rx1")
            if rx0 is not None and rx1 is not None:
                entry["rx0"] = (rx0 + cx0) // scale
                entry["rx1"] = (rx1 + cx0) // scale
            out.append(entry)
        return out

    def _mob_name(self, mob_id: str) -> Optional[str]:
        """Mob display name from ``String.wz/Mob.img/<id>/name`` (parsed once)."""
        if self._mob_names is None:
            names: Dict[str, str] = {}
            mob_img = self._string.root.get("Mob.img")
            if mob_img is not None:
                try:
                    for node in mob_img.parse().children():
                        nm = getattr(node.get("name"), "value", None)
                        if nm:
                            names[node.name] = nm
                except Exception:
                    pass
            self._mob_names = names
        return self._mob_names.get(str(mob_id))

    # ── minimap bitmap ────────────────────────────────────────────────────
    def _has_minimap(self, root) -> bool:
        mm = root.get("miniMap")
        cv = mm.get("canvas") if isinstance(mm, WzSubProperty) else None
        if not isinstance(cv, WzCanvasProperty):
            return False
        # a real bitmap *or* an _outlink into a _Canvas pack both count
        return cv.has_pixels() or getattr(cv.get("_outlink"), "value", None)

    def _canvas_pixels(self, cv: WzCanvasProperty):
        """Decode a minimap canvas, following an ``_outlink`` into a ``_Canvas``
        pack when the canvas itself is a 1×1 placeholder.

        In 64-bit packs the map's ``miniMap/canvas`` is a stub whose ``_outlink``
        (e.g. ``Map/Map/Map1/_Canvas/<code>.img/miniMap/canvas``) redundantly
        re-prefixes the ``Map`` WZ name. The shared link resolver won't strip
        that leading ``Map`` because ``Map`` is itself a root subdir, so we walk
        the path here, trying each trailing slice until one resolves.
        """
        # 1. direct pixels (v83 single-file Map.wz / standalone .img export)
        if cv.has_pixels():
            try:
                pim = decode_canvas(cv, region=self.region)
                if pim.size != (1, 1):
                    return pim
            except Exception:
                pass
        # 2. follow the _outlink into the _Canvas image tree
        val = getattr(cv.get("_outlink"), "value", None)
        if val and self._map is not None:
            parts = [p for p in str(val).replace("\\", "/").split("/") if p]
            target = self._walk_outlink(self._map.root, parts)
            if isinstance(target, WzCanvasProperty):
                try:
                    pim = decode_canvas(target, region=self.region)
                    if pim.size != (1, 1):
                        return pim
                except Exception:
                    pass
        return None

    @staticmethod
    def _walk_outlink(root, parts: List[str]):
        """Resolve an ``_outlink`` path to its canvas, entering ``.img`` trees.

        Tries the full path first, then progressively drops leading segments so
        a redundant WZ-name prefix (``Map/Map/…``) still resolves.
        """
        from wzpy.wz_package import _walk_path
        for start in range(len(parts)):
            node = _walk_path(root, parts[start:])
            if isinstance(node, WzCanvasProperty) and node.has_pixels():
                return node
        return None

    def minimap_png(self, code: str) -> Optional[bytes]:
        self._ensure()
        code = str(int(code)) if code.isdigit() else code
        root = self._img_root(code)
        if root is None:
            return None
        mm = root.get("miniMap")
        cv = mm.get("canvas") if isinstance(mm, WzSubProperty) else None
        if not isinstance(cv, WzCanvasProperty):
            return None
        pim = self._canvas_pixels(cv)
        if pim is None:
            return None
        buf = io.BytesIO()
        pim.save(buf, "PNG")
        return buf.getvalue()


# ── skill overlay payload ────────────────────────────────────────────────
def _skill_map_payload(skills: SkillData, sid: str) -> dict:
    """Slim ``SkillData.skill_info`` down to what the map overlay needs: name, an
    icon URL in *this* server's namespace, and the attack-range boxes.

    ``lt``/``rb`` stay in raw world pixels; the viewer divides them by the map's
    ``2**mag`` scale so the box is drawn to the same scale as the platforms.
    """
    info = skills.skill_info(sid)
    return {
        "id": info["id"],
        "name": info["name"],
        "icon": f"/skill_icon/{info['id']}.png" if info.get("icon") else None,
        "iconSize": info.get("iconSize"),
        "ranges": info.get("attackRanges") or [],
        "range": info.get("attackRange"),
    }


# ── HTTP layer ───────────────────────────────────────────────────────────
class MapHandler(BaseHTTPRequestHandler):
    server_version = "MapServer/1.0"
    data: MapData = None
    skills: Optional[SkillData] = None

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                self._send(200, INDEX_HTML.encode("utf-8"),
                           "text/html; charset=utf-8")
            elif path == "/api/search":
                self._json(self.data.search((qs.get("q") or [""])[0]))
            elif path == "/api/map":
                code = (qs.get("code") or [""])[0].strip()
                if not code:
                    self._json({"error": "pass ?code=<map code>"}, 400)
                else:
                    self._json(self.data.map_info(code))
            elif path.startswith("/minimap/") and path.endswith(".png"):
                code = path[len("/minimap/"):-len(".png")]
                png = self.data.minimap_png(code)
                if png is None:
                    self._json({"error": "no minimap"}, 404)
                else:
                    self._send(200, png, "image/png")
            elif path == "/api/skill_search":
                if self.skills is None:
                    self._json({"available": False, "results": []})
                else:
                    q = (qs.get("q") or [""])[0]
                    self._json({"available": True,
                                "results": self.skills.search(q)})
            elif path == "/api/skill":
                if self.skills is None:
                    self._json({"available": False, "matches": []})
                else:
                    self._json(self._skill_lookup(qs))
            elif path.startswith("/skill_icon/") and path.endswith(".png"):
                sid = path[len("/skill_icon/"):-len(".png")]
                png = self.skills.icon_png(sid) if self.skills is not None else None
                if png is None:
                    self._json({"error": "no icon"}, 404)
                else:
                    self._send(200, png, "image/png")
            else:
                self._json({"error": "not found", "path": path}, 404)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    def _skill_lookup(self, qs) -> dict:
        """``/api/skill`` body: one skill by ``id``, or every match for a ``name``."""
        if "id" in qs:
            sid = (qs.get("id") or [""])[0].strip()
            return {"matches": [_skill_map_payload(self.skills, sid)] if sid else []}
        name = (qs.get("name") or [""])[0].strip()
        if not name:
            return {"error": "pass ?id=<skill id> or ?name=<skill name>",
                    "matches": []}
        ids = self.skills.ids_for_name(name)
        return {"matches": [_skill_map_payload(self.skills, sid) for sid in ids]}


# ── viewer ───────────────────────────────────────────────────────────────
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Map Viewer</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 13px/1.5 ui-monospace, Consolas, monospace;
         background: #14161c; color: #dfe3ec; }
  header { padding: 12px 16px; border-bottom: 1px solid #262a35; position: sticky;
           top: 0; background: #14161c; z-index: 6; }
  h1 { margin: 0 0 8px; font: 600 16px system-ui, sans-serif; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .searchbar { position: relative; }
  input { padding: 7px 10px; font: 13px ui-monospace, monospace; border-radius: 7px;
          border: 1px solid #333846; background: #1b1e27; color: #dfe3ec; width: 320px; }
  input:focus { outline: none; border-color: #5b8cff; }
  #suggest { position: absolute; left: 0; right: 0; top: 38px; z-index: 9;
             background: #1b1e27; border: 1px solid #333846; border-radius: 7px;
             overflow: hidden auto; max-height: 320px; display: none; }
  #suggest div { padding: 6px 10px; cursor: pointer; display: flex; gap: 10px; }
  #suggest div:hover, #suggest div.active { background: #2a3550; }
  #suggest .code { color: #8b93a7; }
  #suggest .st { color: #6b7488; margin-left: auto; }
  .hint { color: #7d8494; }
  .legend { padding: 6px 16px; font-size: 12px; color: #9aa3b4;
            border-bottom: 1px solid #262a35; }
  .legend b { font-weight: 600; }
  .plat { color: #5ad16b; } .rope { color: #ff6b6b; }
  .plocal { color: #37e06a; } .pother { color: #46a8ff; }
  .mob { color: #ffb020; }
  .viewport { position: relative; width: 100%; height: calc(100vh - 150px);
              overflow: hidden; background: #0a0b0f; cursor: grab; }
  .viewport.grab { cursor: grabbing; }
  .layer { position: absolute; top: 0; left: 0; transform-origin: 0 0; }
  #bg { position: absolute; top: 0; left: 0; image-rendering: pixelated;
        opacity: 0.85; pointer-events: none; }
  canvas { position: absolute; top: 0; left: 0; image-rendering: pixelated; }
  #tip { position: fixed; padding: 2px 6px; background: #000c; border: 1px solid #4a5578;
         border-radius: 4px; font-size: 12px; pointer-events: none; display: none;
         z-index: 20; color: #fff; white-space: nowrap; }
  #status { color: #8b93a7; }
  .skill { color: #ffd479; }
  /* ── skill-range overlay ── */
  #skillPanel { position: absolute; top: 10px; right: 10px; z-index: 7; display: none;
                width: 266px; background: #171a22ee; border: 1px solid #2b303c;
                border-radius: 10px; padding: 8px 8px 6px;
                box-shadow: 0 6px 24px #0008; backdrop-filter: blur(4px); }
  #skillPanel .sptitle { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
                         color: #7d8494; margin: 2px 2px 7px; }
  .sksearch { position: relative; }
  #skq { width: 100%; padding: 6px 9px; font: 13px ui-monospace, monospace; border-radius: 7px;
         border: 1px solid #333846; background: #1b1e27; color: #dfe3ec; }
  #skq:focus { outline: none; border-color: #5b8cff; }
  #sksuggest { position: absolute; left: 0; right: 0; top: 34px; z-index: 9;
               background: #1b1e27; border: 1px solid #333846; border-radius: 7px;
               overflow: hidden auto; max-height: 260px; display: none; }
  #sksuggest div { padding: 6px 9px; cursor: pointer; display: flex; gap: 8px; }
  #sksuggest div:hover, #sksuggest div.active { background: #2a3550; }
  #sksuggest .sid { color: #8b93a7; font-variant-numeric: tabular-nums; }
  #skillList { margin-top: 7px; max-height: calc(100vh - 330px); overflow: hidden auto; }
  #skillList .empty2 { color: #6b7488; font-size: 12px; padding: 6px 2px; }
  .skchip { display: flex; align-items: center; gap: 6px; padding: 4px 6px; border-radius: 7px;
            background: #0e1016; margin-bottom: 5px; border-left: 3px solid var(--c); }
  .skchip .ci { width: 22px; height: 22px; image-rendering: pixelated; border-radius: 4px;
                background: #0d0f14; flex: none; }
  .skchip .nm { font-size: 12px; color: #e6e8ee; flex: 1 1 auto; min-width: 0;
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .skchip .dm { font-size: 11px; color: #8b93a7; font-variant-numeric: tabular-nums; flex: none; }
  .skchip select { font: 11px ui-monospace, monospace; background: #1b1e27; color: #dfe3ec;
                   border: 1px solid #333846; border-radius: 5px; max-width: 78px; }
  .skchip button { background: #232a3a; color: #dfe3ec; border: 1px solid #33405e;
                   border-radius: 5px; cursor: pointer; font-size: 11px; padding: 2px 6px; flex: none; }
  .skchip button:hover { background: #2f3b57; }
  #skillPins { position: absolute; top: 0; left: 0; pointer-events: none; }
  #skillPins .pin { position: absolute; transform: translate(-50%, -50%); cursor: grab;
                    display: flex; flex-direction: column; align-items: center;
                    pointer-events: auto; z-index: 5; user-select: none; }
  #skillPins .pin.dragging { cursor: grabbing; z-index: 8; }
  #skillPins .pin img { width: 26px; height: 26px; image-rendering: pixelated;
                        border: 1.5px solid var(--c); border-radius: 6px; background: #0d0f14c0; }
  #skillPins .pin .dot { width: 15px; height: 15px; border-radius: 50%; background: var(--c);
                         border: 2px solid #0d0f14; box-shadow: 0 0 0 1.5px var(--c); }
  #skillPins .pin .plabel { margin-top: 2px; font-size: 10px; color: #fff; background: #000a;
                            padding: 0 4px; border-radius: 3px; white-space: nowrap;
                            border: 1px solid var(--c); max-width: 120px; overflow: hidden;
                            text-overflow: ellipsis; }
</style>
</head>
<body>
<header>
  <h1 id="title">MapleStory Map Viewer</h1>
  <div class="row">
    <div class="searchbar">
      <input id="q" placeholder="Map name or code…  (e.g. Mysterious Path 3, or 541000300)"
             autocomplete="off" spellcheck="false">
      <div id="suggest"></div>
    </div>
    <span class="hint">scroll = zoom · drag = pan · hover a line for coords · drag a skill pin to move it</span>
    <span id="status"></span>
  </div>
</header>
<div class="legend">
  <b class="plat">platforms</b> (merged footholds) ·
  <b class="rope">ropes/ladders</b> ·
  <b class="plocal">portals→same map</b> ·
  <b class="pother">portals→other map</b> ·
  <b class="mob">mob spawns</b> ·
  <b class="skill">skill ranges</b>
  &nbsp;— x/y match <code>map_define_v2.py</code>
</div>
<div class="viewport" id="vp">
  <div class="layer" id="layer">
    <img id="bg" onerror="this.style.display='none'">
    <canvas id="cv"></canvas>
    <div id="skillPins"></div>
  </div>
  <div id="skillPanel">
    <div class="sptitle">Skill ranges</div>
    <div class="sksearch">
      <input id="skq" placeholder="Add a skill…  (e.g. Raging Blow)"
             autocomplete="off" spellcheck="false">
      <div id="sksuggest"></div>
    </div>
    <div id="skillList"></div>
  </div>
</div>
<div id="tip"></div>

<script>
let M = null;             // current map info
let panX = 40, panY = 40, zoom = 4;
const cv = document.getElementById('cv'), ctx = cv.getContext('2d');
const bg = document.getElementById('bg'), layer = document.getElementById('layer');
const vp = document.getElementById('vp'), tip = document.getElementById('tip');
let hover = null, items = [], active = -1, timer = null;

const esc = s => String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

// ── skill-range overlay state ──
let SKILLS_OK = false;              // backend has skill data (String/Skill/Packs)
let placed = [];                   // [{uid,id,name,icon,ranges,range,rangeIdx,flip,color,x,y}]
let skUid = 1, pinDrag = null;
let skItems = [], skActive = -1, skTimer = null;
const SK_PALETTE = ['#ffd479','#5b8cff','#ff6bd6','#5ad16b','#ff8a5b','#8b5bff','#22d3ee','#f4788a'];
const skPanel = document.getElementById('skillPanel'), skPins = document.getElementById('skillPins');
const skList = document.getElementById('skillList');
const skq = document.getElementById('skq'), sksuggest = document.getElementById('sksuggest');

// ── search ──
const q = document.getElementById('q'), suggest = document.getElementById('suggest');
q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(runSuggest, 130); });
q.addEventListener('keydown', e => {
  if (e.key === 'ArrowDown') { active = Math.min(active+1, items.length-1); paint(); e.preventDefault(); }
  else if (e.key === 'ArrowUp') { active = Math.max(active-1, 0); paint(); e.preventDefault(); }
  else if (e.key === 'Enter') {
    if (active >= 0 && items[active]) load(items[active].code);
    else if (q.value.trim()) { if (items[0]) load(items[0].code); }
    suggest.style.display = 'none';
  } else if (e.key === 'Escape') suggest.style.display = 'none';
});
document.addEventListener('click', e => { if (!suggest.contains(e.target) && e.target !== q) suggest.style.display = 'none'; });

async function runSuggest() {
  const v = q.value.trim();
  if (!v) { suggest.style.display = 'none'; return; }
  items = await (await fetch('/api/search?q=' + encodeURIComponent(v))).json();
  active = -1; paint();
}
function paint() {
  if (!items.length) { suggest.style.display = 'none'; return; }
  suggest.innerHTML = items.map((it,i) =>
    `<div data-i="${i}" class="${i===active?'active':''}"><span class="code">${esc(it.code)}</span>`+
    `<span>${esc(it.name)}</span><span class="st">${esc(it.street)}</span></div>`).join('');
  suggest.style.display = 'block';
  [...suggest.children].forEach(el => el.onclick = () => load(items[+el.dataset.i].code));
}

async function load(code) {
  suggest.style.display = 'none';
  document.getElementById('status').textContent = 'loading ' + code + '…';
  const m = await (await fetch('/api/map?code=' + encodeURIComponent(code))).json();
  if (m.error) { document.getElementById('status').textContent = 'error: ' + m.error; return; }
  M = m;
  document.getElementById('title').textContent =
    `${m.name || '(map)'} — ${m.code}` + (m.street ? `  ·  ${m.street}` : '');
  document.getElementById('status').textContent =
    `${m.width}×${m.height} · ${m.platforms.length} platforms · ${m.ropes.length} ropes · `+
    `${m.portals.length} portals · ${(m.mobs||[]).length} mob spawns`;
  bg.style.display = m.minimap ? '' : 'none';
  if (m.minimap) bg.src = m.minimap + '?t=' + Date.now();
  // fit view
  const r = vp.getBoundingClientRect();
  zoom = Math.max(2, Math.min(12, Math.min(r.width / (m.width + 20), r.height / (m.height + 20))));
  panX = (r.width - m.width * zoom) / 2;
  panY = (r.height - m.height * zoom) / 2;
  placed = [];                     // ranges are scaled per-map — reset on map change
  renderSkillList(); renderPins(); updateSkillUI();
  apply();
}

// ── render ──
function apply() {
  layer.style.transform = `translate(${panX}px, ${panY}px)`;
  if (M) {
    if (bg.style.display !== 'none') { bg.style.width = M.width*zoom+'px'; bg.style.height = M.height*zoom+'px'; }
    draw();
  }
  positionPins();
}
function draw() {
  if (!M) return;
  const w = Math.ceil(M.width * zoom), h = Math.ceil(M.height * zoom);
  if (cv.width !== w) cv.width = w;
  if (cv.height !== h) cv.height = h;
  cv.style.width = w+'px'; cv.style.height = h+'px';
  ctx.setTransform(1,0,0,1,0,0); ctx.clearRect(0,0,w,h); ctx.scale(zoom, zoom);
  ctx.lineWidth = Math.max(0.4, 1.4/zoom);

  M.platforms.forEach((p,i) => {
    const hv = hover && hover.t==='p' && hover.i===i;
    ctx.strokeStyle = hv ? '#fff' : `hsl(${(p.x1*7+p.y1*3)%360},85%,58%)`;
    ctx.beginPath(); ctx.moveTo(p.x1, p.y1+0.5); ctx.lineTo(p.x2, p.y2+0.5); ctx.stroke();
  });
  M.ropes.forEach((r,i) => {
    const hv = hover && hover.t==='r' && hover.i===i;
    ctx.strokeStyle = hv ? '#fff' : '#ff5a5a';
    ctx.beginPath(); ctx.moveTo(r.x1+0.5, r.y1); ctx.lineTo(r.x2+0.5, r.y2); ctx.stroke();
  });
  (M.mobs||[]).forEach((mb,i) => {
    const hv = hover && hover.t==='m' && hover.i===i;
    const rad = Math.max(1.3, 2.2/zoom);
    ctx.globalAlpha = hv ? 1 : 0.78;
    ctx.fillStyle = hv ? '#fff' : (mb.hide ? '#c98a2e' : '#ffb020');
    ctx.beginPath(); ctx.arc(mb.x, mb.y, rad, 0, 7); ctx.fill();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = 'rgba(0,0,0,0.55)'; ctx.lineWidth = Math.max(0.2, 0.5/zoom);
    ctx.stroke();
    ctx.lineWidth = Math.max(0.4, 1.4/zoom);
  });
  M.portals.forEach(p => {
    const local = (p.type == 999999999 || String(p.type) === M.code);
    ctx.fillStyle = local ? '#37e06a' : '#46a8ff';
    ctx.beginPath(); ctx.arc(p.x, p.y, Math.max(1.4, 2.5/zoom), 0, 7); ctx.fill();
  });
  drawSkills();
}

// ── interaction ──
let drag = null;
vp.addEventListener('wheel', e => {
  e.preventDefault();
  const r = vp.getBoundingClientRect(), mx = e.clientX-r.left, my = e.clientY-r.top;
  const f = e.deltaY < 0 ? 1.15 : 1/1.15, nz = Math.max(0.5, Math.min(40, zoom*f));
  panX = mx - (mx-panX)*(nz/zoom); panY = my - (my-panY)*(nz/zoom); zoom = nz; apply();
}, { passive: false });
vp.addEventListener('mousedown', e => { drag = {x: e.clientX-panX, y: e.clientY-panY}; vp.classList.add('grab'); });
window.addEventListener('mouseup', () => {
  drag = null; vp.classList.remove('grab');
  if (pinDrag) { const el = skPins.querySelector('.pin.dragging'); if (el) el.classList.remove('dragging'); pinDrag = null; }
});
window.addEventListener('mousemove', e => {
  if (pinDrag) {                     // moving a skill pin — origin follows the cursor
    const r = vp.getBoundingClientRect();
    pinDrag.s.x = Math.round((e.clientX-r.left-panX)/zoom);
    pinDrag.s.y = Math.round((e.clientY-r.top-panY)/zoom);
    positionPins(); draw(); return;
  }
  if (drag) { panX = e.clientX-drag.x; panY = e.clientY-drag.y; apply(); return; }
  if (M) hoverTest(e);
});
vp.addEventListener('mouseleave', () => { if (hover) { hover=null; draw(); } tip.style.display='none'; });

function segDist(px,py,x1,y1,x2,y2){const dx=x2-x1,dy=y2-y1,L=dx*dx+dy*dy;let t=L?((px-x1)*dx+(py-y1)*dy)/L:0;t=Math.max(0,Math.min(1,t));return Math.hypot(px-(x1+t*dx),py-(y1+t*dy));}
function hoverTest(e) {
  const r = vp.getBoundingClientRect();
  const mx = (e.clientX-r.left-panX)/zoom, my = (e.clientY-r.top-panY)/zoom;
  const thr = 5/zoom; let best=null, bd=thr;
  M.platforms.forEach((p,i)=>{const d=segDist(mx,my,p.x1,p.y1,p.x2,p.y2); if(d<bd){bd=d;best={t:'p',i};}});
  M.ropes.forEach((p,i)=>{const d=segDist(mx,my,p.x1,p.y1,p.x2,p.y2); if(d<bd){bd=d;best={t:'r',i};}});
  const mthr = Math.max(3/zoom, 2.5);
  (M.mobs||[]).forEach((mb,i)=>{const d=Math.hypot(mx-mb.x,my-mb.y); if(d<mthr && d<=bd){bd=d;best={t:'m',i};}});
  const changed = JSON.stringify(best) !== JSON.stringify(hover);
  hover = best;
  if (changed) draw();
  if (best) {
    if (best.t==='m') {
      const mb = M.mobs[best.i];
      const t = (mb.mobTime!=null && mb.mobTime>0) ? ` · ${mb.mobTime}s` : '';
      tip.innerHTML = `<b style="color:#ffcf6b">${esc(mb.name||('mob '+mb.id))}</b> `+
        `<span style="color:#8b93a7">#${esc(mb.id)}</span> @ (${mb.x}, ${mb.y})${t}`;
    } else {
      const o = best.t==='p' ? M.platforms[best.i] : M.ropes[best.i];
      tip.innerHTML = `${best.t==='p'?'platform':'rope'} <b>(${o.x1}, ${o.y1})</b> → <b>(${o.x2}, ${o.y2})</b>`;
    }
    tip.style.left = (e.clientX+12)+'px'; tip.style.top = (e.clientY+12)+'px'; tip.style.display='block';
  } else tip.style.display = 'none';
}

// ── skill-range overlay ─────────────────────────────────────────────────
function updateSkillUI(){
  skPanel.style.display = (SKILLS_OK && M && M.mag != null) ? 'block' : 'none';
}
function curRange(s){
  if (s.rangeIdx >= 0 && s.ranges[s.rangeIdx]) return s.ranges[s.rangeIdx];
  return s.range || null;
}
function primaryIdx(m){
  if (!m.ranges || !m.ranges.length) return -1;
  if (m.range){ const i = m.ranges.findIndex(r => r.source === m.range.source); if (i >= 0) return i; }
  return 0;
}

// skill search (independent of the map search up top)
skq.addEventListener('input', () => { clearTimeout(skTimer); skTimer = setTimeout(runSkSuggest, 130); });
skq.addEventListener('keydown', e => {
  if (e.key === 'ArrowDown') { skActive = Math.min(skActive+1, skItems.length-1); paintSk(); e.preventDefault(); }
  else if (e.key === 'ArrowUp') { skActive = Math.max(skActive-1, 0); paintSk(); e.preventDefault(); }
  else if (e.key === 'Enter') {
    if (skActive >= 0 && skItems[skActive]) addSkill(skItems[skActive].id);
    else if (skItems[0]) addSkill(skItems[0].id);
    sksuggest.style.display = 'none';
  } else if (e.key === 'Escape') sksuggest.style.display = 'none';
});
document.addEventListener('click', e => { if (!sksuggest.contains(e.target) && e.target !== skq) sksuggest.style.display = 'none'; });

async function runSkSuggest(){
  const v = skq.value.trim();
  if (!v){ sksuggest.style.display = 'none'; return; }
  const d = await (await fetch('/api/skill_search?q=' + encodeURIComponent(v))).json();
  skItems = d.results || []; skActive = -1; paintSk();
}
function paintSk(){
  if (!skItems.length){ sksuggest.style.display = 'none'; return; }
  sksuggest.innerHTML = skItems.map((it,i) =>
    `<div data-i="${i}" class="${i===skActive?'active':''}">`+
    `<span class="sid">${esc(it.id)}</span><span>${esc(it.name)}</span></div>`).join('');
  sksuggest.style.display = 'block';
  [...sksuggest.children].forEach(el => el.onclick = () => addSkill(skItems[+el.dataset.i].id));
}

async function addSkill(id){
  sksuggest.style.display = 'none';
  if (!M || M.mag == null){ document.getElementById('status').textContent = 'load a map first'; return; }
  const d = await (await fetch('/api/skill?id=' + encodeURIComponent(id))).json();
  const m = d.matches && d.matches[0];
  if (!m) return;
  const r = vp.getBoundingClientRect();       // drop it at the centre of the current view
  const cx = Math.round((r.width/2 - panX)/zoom), cy = Math.round((r.height/2 - panY)/zoom);
  placed.push({ uid: skUid++, id: m.id, name: m.name, icon: m.icon,
    ranges: m.ranges || [], range: m.range, rangeIdx: primaryIdx(m),
    flip: false, color: SK_PALETTE[placed.length % SK_PALETTE.length], x: cx, y: cy });
  skq.value = ''; skItems = [];
  renderSkillList(); renderPins(); draw();
}

function renderSkillList(){
  if (!placed.length){
    skList.innerHTML = '<div class="empty2">No skills yet — search above to drop one on the map.</div>';
    return;
  }
  skList.innerHTML = placed.map(s => {
    const rg = curRange(s);
    const dims = rg ? `${rg.rb[0]-rg.lt[0]}×${rg.rb[1]-rg.lt[1]}` : 'no range';
    const sel = (s.ranges.length > 1)
      ? `<select data-uid="${s.uid}">` + s.ranges.map((r,i) =>
          `<option value="${i}" ${i===s.rangeIdx?'selected':''}>${esc(r.source)}</option>`).join('') + `</select>`
      : '';
    const ic = s.icon
      ? `<img class="ci" src="${s.icon}" onerror="this.style.visibility='hidden'">`
      : `<span class="ci" style="background:${s.color}"></span>`;
    return `<div class="skchip" style="--c:${s.color}">${ic}`+
      `<span class="nm" title="${esc(s.name||s.id)} · id ${esc(s.id)}">${esc(s.name||s.id)}</span>`+
      `<span class="dm">${dims}</span>${sel}`+
      `<button data-act="flip" data-uid="${s.uid}" title="flip facing">${s.flip?'▶':'◀'}</button>`+
      `<button data-act="rm" data-uid="${s.uid}" title="remove">✕</button></div>`;
  }).join('');
  skList.querySelectorAll('button[data-act="rm"]').forEach(b => b.onclick = () => {
    placed = placed.filter(p => String(p.uid) !== b.dataset.uid);
    renderSkillList(); renderPins(); draw();
  });
  skList.querySelectorAll('button[data-act="flip"]').forEach(b => b.onclick = () => {
    const s = placed.find(p => String(p.uid) === b.dataset.uid); if (!s) return;
    s.flip = !s.flip; renderSkillList(); draw();
  });
  skList.querySelectorAll('select[data-uid]').forEach(sl => sl.onchange = () => {
    const s = placed.find(p => String(p.uid) === sl.dataset.uid); if (!s) return;
    s.rangeIdx = +sl.value; renderSkillList(); draw();
  });
}

function renderPins(){
  skPins.innerHTML = placed.map(s => {
    const body = s.icon
      ? `<img src="${s.icon}" draggable="false" onerror="this.style.display='none'">`
      : `<span class="dot"></span>`;
    return `<div class="pin" data-uid="${s.uid}" style="--c:${s.color}">${body}`+
           `<span class="plabel">${esc(s.name||s.id)}</span></div>`;
  }).join('');
  [...skPins.children].forEach(el => {
    const s = placed.find(p => String(p.uid) === el.dataset.uid);
    el.addEventListener('mousedown', ev => {   // grab a pin without starting a map pan
      ev.preventDefault(); ev.stopPropagation();
      pinDrag = { s }; el.classList.add('dragging');
    });
  });
  positionPins();
}
function positionPins(){
  [...skPins.children].forEach(el => {
    const s = placed.find(p => String(p.uid) === el.dataset.uid); if (!s) return;
    el.style.left = (s.x*zoom)+'px'; el.style.top = (s.y*zoom)+'px';
  });
}

function drawSkills(){
  if (!M || M.mag == null) return;
  const scale = Math.pow(2, M.mag);            // raw world px → this map's minimap px
  placed.forEach(s => {
    const rg = curRange(s);
    // origin crosshair — the character stands here
    ctx.strokeStyle = s.color; ctx.lineWidth = Math.max(0.4, 1.4/zoom);
    const ch = Math.max(2, 4/zoom);
    ctx.beginPath();
    ctx.moveTo(s.x-ch, s.y); ctx.lineTo(s.x+ch, s.y);
    ctx.moveTo(s.x, s.y-ch); ctx.lineTo(s.x, s.y+ch); ctx.stroke();
    if (!rg) return;
    let lx = rg.lt[0], ly = rg.lt[1], rx = rg.rb[0], ry = rg.rb[1];
    if (s.flip){ const nlx = -rx, nrx = -lx; lx = nlx; rx = nrx; }   // mirror around origin
    const x1 = s.x + lx/scale, y1 = s.y + ly/scale, x2 = s.x + rx/scale, y2 = s.y + ry/scale;
    const bx = Math.min(x1,x2), by = Math.min(y1,y2), bw = Math.abs(x2-x1), bh = Math.abs(y2-y1);
    ctx.fillStyle = s.color + '2e';
    ctx.fillRect(bx, by, bw, bh);
    ctx.strokeStyle = s.color; ctx.lineWidth = Math.max(0.5, 1.6/zoom);
    ctx.strokeRect(bx, by, bw, bh);
  });
}

// keep panel scroll / clicks from reaching the map (pan + zoom)
skPanel.addEventListener('mousedown', e => e.stopPropagation());
skPanel.addEventListener('wheel', e => e.stopPropagation());

// probe skill availability once at startup, then reveal the panel if a map is up
(async () => {
  try { const d = await (await fetch('/api/skill_search?q=')).json(); SKILLS_OK = d.available !== false; }
  catch(e){ SKILLS_OK = false; }
  updateSkillUI();
})();

bg.onload = apply;
window.addEventListener('resize', apply);
</script>
</body>
</html>
"""


# ── entry point ──────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    # Force UTF-8 console output so the docstring / prints (which contain a few
    # non-ASCII glyphs) don't crash on a legacy codepage console (e.g. GBK/cp936)
    # when stdout is redirected.
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--wz-folder", default=str(REPO_ROOT / "data" / "v83"),
                   help="folder holding Map and String — each resolved as a "
                        "'<Name>/' pack folder or a '<Name>.wz' file. "
                        "Default: data/v83")
    p.add_argument("--map-wz", default=None,
                   help="explicit path to Map.wz or a Map/ pack (overrides --wz-folder)")
    p.add_argument("--string-wz", default=None,
                   help="explicit path to String.wz or a String/ pack "
                        "(overrides --wz-folder)")
    p.add_argument("--override-dir", default=str(REPO_ROOT / "data"),
                   help="dir of standalone <code>.img files that override Map "
                        "(default: data/) - lets exports match their own foothold set")
    p.add_argument("--region", default="BMS", help="WZ region key (default: GMS)")
    p.add_argument("--skill-folder", default=str(REPO_ROOT / "data"),
                   help="folder holding String, Skill and Packs for the skill-range "
                        "overlay (icons + lt/rb attack boxes). Default: data/")
    p.add_argument("--skill-region", default="BMS",
                   help="WZ region key for the skill packs (default: BMS) - "
                        "independent of --region since the packs are BMS-only")
    p.add_argument("--no-skills", action="store_true",
                   help="disable the skill-range overlay entirely")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5002)
    args = p.parse_args(argv)

    data = MapData(Path(args.wz_folder), region=args.region,
                   override_dir=Path(args.override_dir) if args.override_dir else None,
                   map_path=args.map_wz, string_path=args.string_wz)
    print(f"Loading WZ data from {args.wz_folder} (region={args.region}) ...")
    data.open()
    print(f"  indexed {len(data._names)} named maps.")

    # Skill-range overlay (optional). Its own folder + region so the map's WZ
    # region need not match the skill packs' (v83 maps are GMS, packs are BMS).
    skills: Optional[SkillData] = None
    if not args.no_skills and args.skill_folder:
        try:
            skills = SkillData(Path(args.skill_folder), region=args.skill_region)
            skills.open()
            print(f"  indexed {len(skills._all_names)} named skills for ranges "
                  f"(from {args.skill_folder}, region={args.skill_region}).")
        except Exception as exc:
            print(f"  (skill-range overlay unavailable: {exc})")
            skills = None

    MapHandler.data = data
    MapHandler.skills = skills
    httpd = ThreadingHTTPServer((args.host, args.port), MapHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Map server ready at {url}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        httpd.server_close()
        data.close()
        if skills is not None:
            skills.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
