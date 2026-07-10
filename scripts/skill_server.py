#!/usr/bin/env python3
"""A tiny HTTP server for looking up MapleStory **skill** info by name.

It stitches together the three data sources in this repo:

* ``data/String``  — ``String.wz/Skill.img`` maps a skill **name** to its **id**
  (and holds the localized ``desc`` / ``h`` help text).
* ``data/Skill``   — ``_Canvas/<stem>.img/skill/<id>/{icon,effect}`` holds the
  real **icon** bitmap and the **effect** animation frames.
* ``data/Packs``   — ``Skill_*.ms`` (V2 / ChaCha20 archives) holds the skill
  **stats**, including the **attack-range box** (``lt``/``rb``), decoded by
  :mod:`wzpy.ms_file_v2`.

A skill id groups into an img by dropping its last four digits
(``1121008`` → ``112``), which is the ``<stem>`` used for both ``_Canvas`` and
the ``.ms`` skill tree.

Usage::

    python scripts/skill_server.py                 # serves data/ at :5001
    python scripts/skill_server.py --region GMS --port 8000
    python scripts/skill_server.py --data-root /path/to/data

Then open http://127.0.0.1:5001/ and search a skill by name.

Endpoints
---------
* ``GET /``                          minimal search UI
* ``GET /api/search?q=<substr>``     ``[{id,name}]`` name autocomplete (≤50)
* ``GET /api/skill?name=<name>``     full info for skills matching a name
* ``GET /api/skill?id=<id>``         full info for one skill id
* ``GET /icon/<id>.png``             skill icon PNG
* ``GET /effect/<id>/<frame>.png``   one effect-animation frame PNG
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

# Make ``wzpy`` importable no matter where the script is launched from.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wzpy.canvas import decode_canvas                       # noqa: E402
from wzpy.ms_file_v2 import MsPackageV2                      # noqa: E402
from wzpy.properties import (                                # noqa: E402
    WzCanvasProperty, WzSubProperty, WzVectorProperty,
)
from wzpy.wz_package import WzPackage, resolve_canvas_link   # noqa: E402


# ── small WZ-tree helpers ────────────────────────────────────────────────
def _vec(p: WzVectorProperty) -> List[int]:
    return [p.x, p.y]


def _scalars(node: WzSubProperty) -> Dict[str, object]:
    """Flatten a sub-property's direct scalar/vector leaves to a plain dict
    (skips nested sub-properties and canvases)."""
    out: Dict[str, object] = {}
    for c in node.children():
        if isinstance(c, WzVectorProperty):
            out[c.name] = _vec(c)
        elif isinstance(c, (WzSubProperty, WzCanvasProperty)):
            continue
        else:
            v = getattr(c, "value", None)
            if v is not None:
                out[c.name] = v
    return out


def _find_ranges(skill: WzSubProperty) -> List[Dict[str, object]]:
    """Every ``lt``+``rb`` attack-range box anywhere under a skill node,
    labelled by its path (``common`` / ``PVPcommon`` / ``level/<n>`` / …)."""
    out: List[Dict[str, object]] = []

    def rec(node: WzSubProperty, path: str) -> None:
        kids = {c.name: c for c in node.children()}
        lt, rb = kids.get("lt"), kids.get("rb")
        if isinstance(lt, WzVectorProperty) and isinstance(rb, WzVectorProperty):
            out.append({
                "source": path or "common",
                "lt": _vec(lt), "rb": _vec(rb),
                "width": rb.x - lt.x, "height": rb.y - lt.y,
            })
        for name, child in kids.items():
            if isinstance(child, WzSubProperty) and not isinstance(child, WzCanvasProperty):
                rec(child, f"{path}/{name}" if path else name)

    rec(skill, "")
    return out


def _pick_primary(ranges: List[Dict[str, object]]) -> Optional[Dict[str, object]]:
    """Choose the representative range: ``common`` first, else the lowest
    ``level/<n>``, else the first non-PVP box."""
    if not ranges:
        return None
    for r in ranges:
        if r["source"] == "common":
            return r
    levels = [r for r in ranges if str(r["source"]).startswith("level/")]
    if levels:
        def lvl(r):
            seg = str(r["source"]).split("/")[1]
            return int(seg) if seg.isdigit() else 1 << 30
        return min(levels, key=lvl)
    non_pvp = [r for r in ranges if not str(r["source"]).lower().startswith("pvp")]
    return (non_pvp or ranges)[0]


# ── data access layer ────────────────────────────────────────────────────
class SkillData:
    """Lazily opens the three packs and answers name / info / bitmap queries.

    Thread-safe: WZ image parses and ``.ms`` decrypts take their own locks; the
    name index and PNG cache are guarded here.
    """

    def __init__(self, data_root: Path, region: str = "BMS"):
        self.data_root = Path(data_root)
        self.region = region
        self._lock = threading.RLock()
        self._loaded = False
        self._string: Optional[WzPackage] = None
        self._skill: Optional[WzPackage] = None
        self._packs: Optional[MsPackageV2] = None
        self._string_img = None
        self._name_index: Dict[str, List[str]] = {}
        self._all_names: List[Tuple[str, str]] = []   # (id, name), String order
        self._png_cache: Dict[Tuple[str, str, Optional[str]], Optional[Tuple[bytes, Tuple[int, int]]]] = {}

    # ── lifecycle ────────────────────────────────────────────────────────
    def open(self) -> None:
        """Eagerly open the packs and build the name index (called at start)."""
        self._ensure()

    def close(self) -> None:
        for pack in (self._string, self._skill, self._packs):
            try:
                if pack is not None:
                    pack.close()
            except Exception:
                pass

    def _ensure(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            region = self.region
            self._string = WzPackage.open(str(self.data_root / "String"), region=region)
            self._skill = WzPackage.open(str(self.data_root / "Skill"), region=region)
            self._packs = MsPackageV2.open(str(self.data_root / "Packs"), region=region)
            self._string_img = self._string.get("Skill.img").parse()
            self._build_name_index()
            self._loaded = True

    def _build_name_index(self) -> None:
        index: Dict[str, List[str]] = {}
        alln: List[Tuple[str, str]] = []
        for child in self._string_img.children():
            sid = child.name
            name_leaf = child.get("name") if hasattr(child, "get") else None
            name = getattr(name_leaf, "value", None)
            if not name:
                continue
            alln.append((sid, name))
            index.setdefault(name.strip().lower(), []).append(sid)
        self._name_index = index
        self._all_names = alln

    # ── name lookup ──────────────────────────────────────────────────────
    def search(self, query: str, limit: int = 50) -> List[Dict[str, str]]:
        """Ranked name search: exact-ci, then prefix, then substring."""
        self._ensure()
        q = query.strip().lower()
        if not q:
            return []
        scored: List[Tuple[Tuple[int, int, str], Dict[str, str]]] = []
        for sid, name in self._all_names:
            low = name.lower()
            if q not in low:
                continue
            if low == q:
                rank = 0
            elif low.startswith(q):
                rank = 1
            else:
                rank = 2
            scored.append(((rank, len(name), low), {"id": sid, "name": name}))
        scored.sort(key=lambda t: t[0])
        return [item for _, item in scored[:limit]]

    def ids_for_name(self, name: str, limit: int = 12) -> List[str]:
        """Ids for an exact (case-insensitive) name; falls back to search."""
        self._ensure()
        exact = self._name_index.get(name.strip().lower())
        if exact:
            return exact[:limit]
        return [m["id"] for m in self.search(name, limit=limit)]

    # ── full skill info ──────────────────────────────────────────────────
    def skill_info(self, sid: str) -> Dict[str, object]:
        self._ensure()
        stem = sid[:-4] if len(sid) > 4 else None
        info: Dict[str, object] = {
            "id": sid, "stem": stem, "name": None, "desc": None, "h": None,
            "attackRange": None, "attackRanges": [], "stats": {}, "info": {},
            "icon": None, "iconSize": None, "effectFrames": 0, "effect": [],
        }

        # name / desc / help from String.wz
        snode = self._string_img.get(sid)
        if snode is not None:
            for key in ("name", "desc", "h"):
                leaf = snode.get(key)
                val = getattr(leaf, "value", None)
                if val:
                    info[key] = val

        # stats + attack range from the .ms skill tree
        mnode = self._ms_skill(sid, stem)
        if mnode is not None:
            ranges = _find_ranges(mnode)
            info["attackRanges"] = ranges
            info["attackRange"] = _pick_primary(ranges)
            common = mnode.get("common")
            if isinstance(common, WzSubProperty):
                info["stats"] = _scalars(common)
            inode = mnode.get("info")
            if isinstance(inode, WzSubProperty):
                info["info"] = _scalars(inode)

        # icon + effect frames from _Canvas
        cnode = self._canvas_skill(sid, stem)
        if cnode is not None:
            icon = self._render_png("icon", sid)
            if icon is not None:
                info["icon"] = f"/icon/{sid}.png"
                info["iconSize"] = list(icon[1])
            eff = cnode.get("effect")
            if isinstance(eff, WzSubProperty):
                frames = sorted((c.name for c in eff.children() if c.name.isdigit()),
                                key=int)
                info["effectFrames"] = len(frames)
                info["effect"] = [f"/effect/{sid}/{f}.png" for f in frames]
        return info

    # ── bitmaps ──────────────────────────────────────────────────────────
    def _ms_skill(self, sid: str, stem: Optional[str]):
        if not stem or self._packs is None:
            return None
        img = self._packs.get(f"Skill/{stem}.img")
        if img is None:
            return None
        try:
            skills = img.parse().get("skill")
        except Exception:
            return None
        return skills.get(sid) if isinstance(skills, WzSubProperty) else None

    def _canvas_skill(self, sid: str, stem: Optional[str]):
        if not stem or self._skill is None:
            return None
        img = self._skill.get(f"_Canvas/{stem}.img")
        if img is None:
            return None
        try:
            skills = img.parse().get("skill")
        except Exception:
            return None
        return skills.get(sid) if isinstance(skills, WzSubProperty) else None

    def _decode(self, canvas) -> Optional["object"]:
        if not isinstance(canvas, WzCanvasProperty):
            return None
        target = canvas
        if not canvas.has_pixels():
            resolved = resolve_canvas_link(canvas, self._skill.root)
            if resolved is not None:
                target = resolved
        try:
            return decode_canvas(target, region=self.region)
        except Exception:
            return None

    def _render_png(self, kind: str, sid: str,
                    frame: Optional[str] = None) -> Optional[Tuple[bytes, Tuple[int, int]]]:
        key = (kind, sid, frame)
        with self._lock:
            if key in self._png_cache:
                return self._png_cache[key]
        self._ensure()
        stem = sid[:-4] if len(sid) > 4 else None
        cnode = self._canvas_skill(sid, stem)
        result: Optional[Tuple[bytes, Tuple[int, int]]] = None
        if cnode is not None:
            if kind == "icon":
                canvas = cnode.get("icon")
            else:
                eff = cnode.get("effect")
                canvas = eff.get(frame) if isinstance(eff, WzSubProperty) and frame else None
            pim = self._decode(canvas)
            if pim is not None:
                buf = io.BytesIO()
                pim.save(buf, "PNG")
                result = (buf.getvalue(), pim.size)
        with self._lock:
            self._png_cache[key] = result
        return result

    def icon_png(self, sid: str) -> Optional[bytes]:
        r = self._render_png("icon", sid)
        return r[0] if r else None

    def effect_png(self, sid: str, frame: str) -> Optional[bytes]:
        r = self._render_png("effect", sid, frame)
        return r[0] if r else None


# ── HTTP layer ───────────────────────────────────────────────────────────
class SkillHandler(BaseHTTPRequestHandler):
    server_version = "SkillServer/1.0"
    data: SkillData = None  # set on the handler class in main()

    # quiet, single-line logging
    def log_message(self, fmt, *args):
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    # ── response helpers ─────────────────────────────────────────────────
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _png(self, body: Optional[bytes]) -> None:
        if body is None:
            self._json({"error": "not found"}, 404)
            return
        self._send(200, body, "image/png")

    # ── routing ──────────────────────────────────────────────────────────
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path == "/" or path == "/index.html":
                self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/search":
                q = (qs.get("q") or [""])[0]
                self._json(self.data.search(q))
            elif path == "/api/skill":
                self._json(self._skill_response(qs))
            elif path.startswith("/icon/") and path.endswith(".png"):
                sid = path[len("/icon/"):-len(".png")]
                self._png(self.data.icon_png(sid))
            elif path.startswith("/effect/") and path.endswith(".png"):
                rest = path[len("/effect/"):-len(".png")]
                sid, _, frame = rest.partition("/")
                self._png(self.data.effect_png(sid, frame) if frame else None)
            else:
                self._json({"error": "not found", "path": path}, 404)
        except BrokenPipeError:
            pass
        except Exception as exc:  # never let one bad request kill the thread
            self._json({"error": str(exc)}, 500)

    def _skill_response(self, qs) -> Dict[str, object]:
        if "id" in qs:
            sid = qs["id"][0]
            return {"query": {"id": sid}, "matches": [self.data.skill_info(sid)]}
        name = (qs.get("name") or [""])[0]
        if not name.strip():
            return {"error": "pass ?name=<skill name> or ?id=<skill id>"}
        ids = self.data.ids_for_name(name)
        return {"query": {"name": name},
                "matches": [self.data.skill_info(sid) for sid in ids]}


# ── minimal single-page UI ───────────────────────────────────────────────
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Skill Lookup</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.5 system-ui, "Segoe UI", sans-serif;
         background: #12141a; color: #e6e8ee; }
  header { padding: 20px 24px; border-bottom: 1px solid #262a35;
           position: sticky; top: 0; background: #12141a; z-index: 5; }
  h1 { margin: 0 0 12px; font-size: 20px; font-weight: 650; }
  .searchbar { position: relative; max-width: 520px; }
  #q { width: 100%; padding: 11px 14px; font-size: 15px; border-radius: 9px;
       border: 1px solid #333846; background: #1b1e27; color: #e6e8ee; }
  #q:focus { outline: none; border-color: #5b8cff; }
  #suggest { position: absolute; left: 0; right: 0; top: 46px; z-index: 9;
             background: #1b1e27; border: 1px solid #333846; border-radius: 9px;
             overflow: hidden; display: none; max-height: 340px; overflow-y: auto; }
  #suggest div { padding: 8px 14px; cursor: pointer; display: flex; gap: 10px; }
  #suggest div:hover, #suggest div.active { background: #2a3550; }
  #suggest .sid { color: #8b93a7; font-variant-numeric: tabular-nums; }
  main { padding: 24px; display: grid; gap: 18px;
         grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
         max-width: 1200px; }
  .card { background: #171a22; border: 1px solid #262a35; border-radius: 14px;
          padding: 18px; }
  .head { display: flex; gap: 14px; align-items: center; }
  .icon { width: 56px; height: 56px; image-rendering: pixelated; flex: none;
          background: #0d0f14; border: 1px solid #2b303c; border-radius: 8px;
          display: grid; place-items: center; }
  .icon img { image-rendering: pixelated; }
  .title { font-size: 17px; font-weight: 650; }
  .sub { color: #8b93a7; font-size: 13px; font-variant-numeric: tabular-nums; }
  .desc { color: #b9c0d0; font-size: 13px; margin: 12px 0 0; white-space: pre-wrap; }
  .sec { margin-top: 16px; }
  .sec h3 { margin: 0 0 8px; font-size: 12px; text-transform: uppercase;
            letter-spacing: .06em; color: #7d8494; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  td { padding: 3px 0; vertical-align: top; }
  td.k { color: #8b93a7; width: 42%; }
  td.v { font-variant-numeric: tabular-nums; }
  .range-wrap { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
  svg { background: #0d0f14; border: 1px solid #2b303c; border-radius: 8px; }
  .rnums { font-size: 13px; font-variant-numeric: tabular-nums; }
  .rnums b { color: #ffd479; }
  .effect { display: flex; gap: 12px; align-items: center; }
  .stage { background: #0d0f14; border: 1px solid #2b303c; border-radius: 8px;
           width: 200px; height: 150px; display: grid; place-items: center;
           overflow: hidden; }
  .stage img { max-width: 100%; max-height: 100%; image-rendering: pixelated; }
  button { background: #2a3550; color: #e6e8ee; border: 1px solid #3a4568;
           border-radius: 7px; padding: 6px 12px;
           cursor: pointer; font-size: 13px; }
  button:hover { background: #35426a; }
  .empty { color: #8b93a7; padding: 40px 24px; }
  .pill { display:inline-block; padding:1px 8px; border-radius:20px; font-size:11px;
          background:#22293a; color:#9fb0d6; margin-left:8px; }
</style>
</head>
<body>
<header>
  <h1>MapleStory Skill Lookup</h1>
  <div class="searchbar">
    <input id="q" placeholder="Search a skill by name…  (e.g. Raging Blow)"
           autocomplete="off" spellcheck="false">
    <div id="suggest"></div>
  </div>
</header>
<main id="out"><div class="empty">Type a skill name above to begin.</div></main>

<script>
const q = document.getElementById('q');
const suggest = document.getElementById('suggest');
const out = document.getElementById('out');
let items = [], active = -1, timer = null;

function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

q.addEventListener('input', () => {
  clearTimeout(timer);
  timer = setTimeout(runSuggest, 140);
});
q.addEventListener('keydown', e => {
  if (e.key === 'ArrowDown') { active = Math.min(active+1, items.length-1); paintSuggest(); e.preventDefault(); }
  else if (e.key === 'ArrowUp') { active = Math.max(active-1, 0); paintSuggest(); e.preventDefault(); }
  else if (e.key === 'Enter') {
    if (active >= 0 && items[active]) pickId(items[active].id);
    else loadName(q.value);
    suggest.style.display = 'none';
  } else if (e.key === 'Escape') suggest.style.display = 'none';
});
document.addEventListener('click', e => { if (!suggest.contains(e.target) && e.target !== q) suggest.style.display='none'; });

async function runSuggest(){
  const v = q.value.trim();
  if (!v){ suggest.style.display='none'; return; }
  const r = await fetch('/api/search?q=' + encodeURIComponent(v));
  items = await r.json(); active = -1; paintSuggest();
}
function paintSuggest(){
  if (!items.length){ suggest.style.display='none'; return; }
  suggest.innerHTML = items.map((it,i) =>
    `<div data-i="${i}" class="${i===active?'active':''}">`+
    `<span class="sid">${esc(it.id)}</span><span>${esc(it.name)}</span></div>`).join('');
  suggest.style.display = 'block';
  [...suggest.children].forEach(el =>
    el.onclick = () => pickId(items[+el.dataset.i].id));
}
function pickId(id){ suggest.style.display='none'; loadId('id=' + encodeURIComponent(id)); }
function loadName(name){ loadId('name=' + encodeURIComponent(name)); }

async function loadId(query){
  out.innerHTML = '<div class="empty">Loading…</div>';
  const r = await fetch('/api/skill?' + query);
  const data = await r.json();
  if (!data.matches || !data.matches.length){
    out.innerHTML = '<div class="empty">No skill found.</div>'; return;
  }
  out.innerHTML = '';
  data.matches.forEach(m => out.appendChild(card(m)));
}

function rangeSvg(r){
  // draw the lt/rb box relative to the character origin (0,0)
  const W=220, H=150, pad=10;
  const xs=[r.lt[0], r.rb[0], 0], ys=[r.lt[1], r.rb[1], 0];
  let minX=Math.min(...xs), maxX=Math.max(...xs), minY=Math.min(...ys), maxY=Math.max(...ys);
  const sx=(W-2*pad)/Math.max(1,maxX-minX), sy=(H-2*pad)/Math.max(1,maxY-minY);
  const s=Math.min(sx,sy);
  const px=x=>pad+(x-minX)*s + (W-2*pad-(maxX-minX)*s)/2;
  const py=y=>pad+(y-minY)*s + (H-2*pad-(maxY-minY)*s)/2;
  const bx=px(r.lt[0]), by=py(r.lt[1]), bw=(r.rb[0]-r.lt[0])*s, bh=(r.rb[1]-r.lt[1])*s;
  return `<svg width="${W}" height="${H}">`+
    `<rect x="${bx}" y="${by}" width="${bw}" height="${bh}" fill="#5b8cff33" stroke="#5b8cff" stroke-width="1.5"/>`+
    `<line x1="${px(0)-6}" y1="${py(0)}" x2="${px(0)+6}" y2="${py(0)}" stroke="#ffd479" stroke-width="1.5"/>`+
    `<line x1="${px(0)}" y1="${py(0)-6}" x2="${px(0)}" y2="${py(0)+6}" stroke="#ffd479" stroke-width="1.5"/>`+
    `<circle cx="${px(0)}" cy="${py(0)}" r="2.5" fill="#ffd479"/></svg>`;
}

function statsRows(obj){
  const keys = Object.keys(obj||{});
  if (!keys.length) return '';
  return '<table>' + keys.map(k =>
    `<tr><td class="k">${esc(k)}</td><td class="v">${esc(
      Array.isArray(obj[k]) ? '['+obj[k].join(', ')+']' : obj[k])}</td></tr>`).join('') + '</table>';
}

function card(m){
  const el = document.createElement('div');
  el.className = 'card';
  const iconImg = m.icon ? `<img src="${m.icon}" alt="">` : '<span class="sub">—</span>';
  let html = `<div class="head">
      <div class="icon">${iconImg}</div>
      <div><div class="title">${esc(m.name || '(unnamed)')}</div>
           <div class="sub">ID ${esc(m.id)} · img ${esc(m.stem||'?')}.img</div></div>
    </div>`;
  if (m.desc) html += `<div class="desc">${esc(m.desc)}</div>`;

  const r = m.attackRange;
  html += `<div class="sec"><h3>Attack Range`+
          (r ? ` <span class="pill">${esc(r.source)}</span>` : '') + `</h3>`;
  if (r){
    html += `<div class="range-wrap">${rangeSvg(r)}
      <div class="rnums">
        lt <b>[${r.lt.join(', ')}]</b><br>rb <b>[${r.rb.join(', ')}]</b><br>
        ${r.width} × ${r.height} px</div></div>`;
    if (m.attackRanges && m.attackRanges.length > 1)
      html += `<div class="sub" style="margin-top:8px">${m.attackRanges.length} range blocks: `+
              m.attackRanges.map(x=>esc(x.source)).join(', ') + `</div>`;
  } else {
    html += `<div class="sub">No attack-range box (buff / passive / not in packs).</div>`;
  }
  html += `</div>`;

  if (m.stats && Object.keys(m.stats).length)
    html += `<div class="sec"><h3>Stats (common)</h3>${statsRows(m.stats)}</div>`;

  if (m.effectFrames > 0){
    html += `<div class="sec"><h3>Effect · ${m.effectFrames} frames</h3>
      <div class="effect"><div class="stage"><img id="fx_${m.id}"></div>
        <button data-id="${m.id}">▶ / ⏸</button></div></div>`;
  }
  el.innerHTML = html;

  if (m.effectFrames > 0) setupEffect(el, m);
  return el;
}

function setupEffect(el, m){
  const img = el.querySelector('#fx_' + CSS.escape(m.id));
  const btn = el.querySelector('button[data-id]');
  let i = 0, playing = true, tid = null;
  const show = () => { img.src = m.effect[i]; };
  const tick = () => { i = (i + 1) % m.effect.length; show(); };
  const start = () => { if (!tid) tid = setInterval(tick, 110); };
  const stop = () => { clearInterval(tid); tid = null; };
  show(); start();
  btn.onclick = () => { playing = !playing; playing ? start() : stop(); };
}
</script>
</body>
</html>
"""


# ── entry point ──────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-root", default=str(REPO_ROOT / "data"),
                        help="folder containing String/, Skill/, Packs/ (default: ./data)")
    parser.add_argument("--region", default="BMS",
                        help="WZ region key for decoding (default: BMS)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args(argv)

    data = SkillData(Path(args.data_root), region=args.region)
    print(f"Loading packs from {args.data_root} (region={args.region}) …")
    data.open()
    print(f"  indexed {len(data._all_names)} named skills.")

    SkillHandler.data = data
    httpd = ThreadingHTTPServer((args.host, args.port), SkillHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Skill server ready at {url}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        httpd.server_close()
        data.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
