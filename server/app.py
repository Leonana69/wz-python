"""Flask web UI for browsing a WZ file.

Routes:
  /                                  - tree browser shell (HTML)
  /api/tree/<path>                   - JSON listing of a directory / .img subtree
  /api/property/<p>                  - JSON value for a leaf property
  /api/canvas/<p>.png                - rendered PNG bytes for a Canvas property
  /api/sound/<p>                     - raw audio bytes for a Sound property
  /api/export/json/<p>               - JSON dump of the subtree
  /api/export/xml/<p>                - XML dump of the subtree
  /api/export/images/<p>?layout=...  - ZIP of every Canvas under <p>
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import unquote
from xml.sax.saxutils import escape as xml_escape, quoteattr


# In-memory job tracker for long-running bundle exports. Keyed by job_id.
# Each entry: {status, progress, total, current, label, file_path?, error?, cancel?}.
# A single mutex guards all access — jobs are short-lived and contention is low.
_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


_NUMBER_RE = re.compile(r"(\d+)")


def _natural_key(s: str):
    """Sort key that orders ``"2.img"`` before ``"10.img"`` and ``"0"`` before ``"10"``."""
    return [int(p) if p.isdigit() else p.lower() for p in _NUMBER_RE.split(s)]


# ── recursive serializers used by the export routes ─────────────────────
# Canvas pixel data and Sound bytes are NOT inlined: they'd bloat the dump
# from kilobytes to gigabytes for a typical Mob/Map export. Use the dedicated
# /api/canvas + /api/sound routes (or /api/export/images) for the binaries.

from wzpy.json_export import node_to_dict as _node_to_dict, property_to_dict as _property_to_dict


# Property type names → XML tag names. Mirrors the convention used by the
# C# HaSuite XML exporter so the output is recognizable to MapleStory tooling.
_XML_TAG_BY_TYPE = {
    "Null": "null",
    "Short": "short",
    "Int": "int",
    "Long": "long",
    "Float": "float",
    "Double": "double",
    "String": "string",
    "Vector": "vector",
    "SubProperty": "imgdir",
    "Canvas": "canvas",
    "Sound": "sound",
    "UOL": "uol",
    "Convex": "extended",
}


def _xml_tag(prop) -> str:
    return _XML_TAG_BY_TYPE.get(prop.type_name, "property")


def _property_to_xml(prop, indent: int = 0) -> str:
    from wzpy.properties import (
        WzCanvasProperty, WzConvexProperty, WzNullProperty, WzSoundProperty,
        WzSubProperty, WzUolProperty, WzVectorProperty,
    )
    pad = "  " * indent
    tag = _xml_tag(prop)
    name_attr = f"name={quoteattr(prop.name)}"

    if isinstance(prop, WzNullProperty):
        return f"{pad}<{tag} {name_attr}/>"
    if isinstance(prop, WzVectorProperty):
        return f'{pad}<{tag} {name_attr} x="{prop.x}" y="{prop.y}"/>'
    if isinstance(prop, WzCanvasProperty):
        attrs = (f'{name_attr} width="{prop.width}" height="{prop.height}" '
                 f'format="{prop.format + prop.format2}"')
        if not prop.has_children():
            return f"{pad}<{tag} {attrs}/>"
        body = "\n".join(_property_to_xml(c, indent + 1) for c in prop.children())
        return f"{pad}<{tag} {attrs}>\n{body}\n{pad}</{tag}>"
    if isinstance(prop, WzSoundProperty):
        return (f'{pad}<{tag} {name_attr} length_ms="{prop.length_ms}" '
                f'bytes="{prop.value}"/>')
    if isinstance(prop, WzConvexProperty):
        body = "\n".join(
            f'{pad}  <vector x="{p.x}" y="{p.y}"/>' for p in prop.points
        )
        return f"{pad}<{tag} {name_attr}>\n{body}\n{pad}</{tag}>"
    if isinstance(prop, WzUolProperty):
        return f"{pad}<{tag} {name_attr} target={quoteattr(str(prop.value))}/>"
    if isinstance(prop, WzSubProperty):
        if not prop.has_children():
            return f"{pad}<{tag} {name_attr}/>"
        body = "\n".join(_property_to_xml(c, indent + 1) for c in prop.children())
        return f"{pad}<{tag} {name_attr}>\n{body}\n{pad}</{tag}>"
    # Scalar fallback.
    try:
        v = prop.value
    except Exception:
        v = ""
    return f"{pad}<{tag} {name_attr} value={quoteattr(str(v))}/>"


def _node_to_xml(node) -> str:
    from wzpy.properties import WzProperty
    from wzpy.wz_file import WzDirectory
    from wzpy.wz_image import WzImage
    if isinstance(node, WzDirectory):
        body_parts = []
        for n, d in node.subdirs.items():
            body_parts.append(_node_to_xml(d))
        for n, i in node.images.items():
            body_parts.append(_node_to_xml(i))
        body = "\n".join(body_parts)
        return f"<directory name={quoteattr(node.name or '')}>\n{body}\n</directory>"
    if isinstance(node, WzImage):
        node.parse()
        body = "\n".join(_property_to_xml(c, 1) for c in node.children())
        return f"<imgdir name={quoteattr(node.name)}>\n{body}\n</imgdir>"
    if isinstance(node, WzProperty):
        return _property_to_xml(node)
    return f"<unknown name={quoteattr(getattr(node, 'name', '') or '')}/>"


# ── MP3 header + duration estimator (used by /api/add/sound) ──────────
# WZ Sound properties carry a WAVEFORMATEX + MPEGLAYER3WAVEFORMAT
# header before the actual audio bytes. For added sounds we emit a
# fixed CD-quality stereo MP3 header — compatible with every WZ
# reader and good enough for typical MapleStory sound effects.

_MP3_BITRATES_V1_L3 = (
    0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0,
)
_MP3_SAMPLE_RATES_V1 = (44100, 48000, 32000, 0)


def _is_mp3_bytes(data: bytes) -> bool:
    """Validate that ``data`` looks like an MP3 stream — either an MPEG
    audio sync (``0xFFE``…) or a leading ID3v2 tag (``ID3``)."""
    if len(data) < 3:
        return False
    if data[:3] == b"ID3":
        return True
    return data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def _estimate_mp3_duration_ms(data: bytes) -> int:
    """Sum the duration of every MPEG-1 Layer III frame.

    Falls back to a file-size estimate at 128 kbps if no frames decode
    cleanly (for esoteric MP3 variants — V2/V2.5, layer I/II, free-
    format). Good enough for the in-game sound-length display."""
    duration_ms = 0.0
    pos = 0
    if data[:3] == b"ID3":
        # Skip the ID3v2 tag if present. Length is encoded as 4
        # syncsafe 7-bit ints.
        if len(data) >= 10:
            sz = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) \
                 | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
            pos = 10 + sz
    while pos + 4 <= len(data):
        if data[pos] != 0xFF or (data[pos + 1] & 0xE0) != 0xE0:
            pos += 1
            continue
        h = (data[pos] << 24) | (data[pos + 1] << 16) | (data[pos + 2] << 8) | data[pos + 3]
        version_id = (h >> 19) & 0x3        # 3 = MPEG-1
        layer = (h >> 17) & 0x3             # 1 = Layer III
        bitrate_idx = (h >> 12) & 0xF
        sample_idx = (h >> 10) & 0x3
        padding = (h >> 9) & 0x1
        if version_id != 0x3 or layer != 0x1 or bitrate_idx in (0, 0xF) or sample_idx == 0x3:
            pos += 1
            continue
        bitrate_bps = _MP3_BITRATES_V1_L3[bitrate_idx] * 1000
        sample_rate = _MP3_SAMPLE_RATES_V1[sample_idx]
        frame_size = 144 * bitrate_bps // sample_rate + padding
        if frame_size < 4:
            pos += 1
            continue
        duration_ms += 1152 * 1000 / sample_rate
        pos += frame_size

    if duration_ms <= 0:
        # Conservative file-size fallback — assume 128 kbps.
        duration_ms = (len(data) * 1000) / 16000
    return int(duration_ms)


def _default_mp3_header(
    sample_rate: int = 44100, channels: int = 2, bitrate_bps: int = 128_000,
) -> bytes:
    """Return a 28-byte WAVEFORMATEX + MPEGLAYER3WAVEFORMAT for a
    stereo MP3 stream. Matches the layout MapleLib emits."""
    import struct as _s
    bytes_per_sec = bitrate_bps // 8
    wfx = _s.pack(
        "<HHIIHHH",
        0x0055,           # wFormatTag = WAVE_FORMAT_MPEGLAYER3
        channels,         # nChannels
        sample_rate,      # nSamplesPerSec
        bytes_per_sec,    # nAvgBytesPerSec
        1,                # nBlockAlign
        0,                # wBitsPerSample (0 for variable / MP3)
        12,               # cbSize (size of MPEGLAYER3WAVEFORMAT extension)
    )
    ext = _s.pack(
        "<HIHHH",
        1,                # wID (MPEGLAYER3_ID_MPEG)
        2,                # fdwFlags (MPEGLAYER3_FLAG_PADDING_OFF)
        1,                # nBlockSize
        1,                # nFramesPerBlock
        1393,             # nCodecDelay
    )
    return wfx + ext


def _construct_property(kind: str, name: str, body: Dict[str, Any], parent):
    """Build a fresh property of ``kind`` for the /api/add route.

    The supported simple types are the ones the in-place editor
    already handles. Canvas/Sound/UOL/Convex are intentionally not
    here — they need richer construction (image upload, audio
    upload, etc.) and are deferred to v2.
    """
    from wzpy.properties import (
        WzDoubleProperty, WzFloatProperty, WzIntProperty,
        WzLongProperty, WzNullProperty, WzShortProperty,
        WzStringProperty, WzSubProperty, WzVectorProperty,
    )
    if kind == "Null":
        return WzNullProperty(name, parent)
    if kind == "Short":
        v = int(body.get("value", 0))
        if not (-(1 << 15) <= v < (1 << 15)):
            raise ValueError(f"Short out of range: {v}")
        return WzShortProperty(name, v, parent)
    if kind == "Int":
        v = int(body.get("value", 0))
        if not (-(1 << 31) <= v < (1 << 31)):
            raise ValueError(f"Int out of range: {v}")
        return WzIntProperty(name, v, parent)
    if kind == "Long":
        v = int(body.get("value", 0))
        if not (-(1 << 63) <= v < (1 << 63)):
            raise ValueError(f"Long out of range: {v}")
        return WzLongProperty(name, v, parent)
    if kind == "Float":
        return WzFloatProperty(name, float(body.get("value", 0.0)), parent)
    if kind == "Double":
        return WzDoubleProperty(name, float(body.get("value", 0.0)), parent)
    if kind == "String":
        s = str(body.get("value", ""))
        prop = WzStringProperty(name, s, parent)
        # The serializer auto-picks ASCII vs Unicode based on the
        # string's contents, so we don't have to set ``_encoding`` here
        # — but giving it a value lets future in-place edits compute
        # the right byte budget without re-detecting.
        try:
            s.encode("cp1252")
            prop._encoding = "ascii"
        except UnicodeEncodeError:
            prop._encoding = "unicode"
        return prop
    if kind == "Vector":
        x = int(body.get("x", 0))
        y = int(body.get("y", 0))
        return WzVectorProperty(name, x, y, parent)
    if kind == "SubProperty":
        return WzSubProperty(name, parent)
    raise ValueError(
        f"unsupported kind {kind!r} (try Null, Short, Int, Long, Float, "
        f"Double, String, Vector, or SubProperty)"
    )


def _walk_canvases(node, current_path: str = "") -> Iterator[Tuple[str, Any]]:
    """Yield every (path, WzCanvasProperty) with pixels in the subtree."""
    from wzpy.properties import WzCanvasProperty, WzProperty
    from wzpy.wz_file import WzDirectory
    from wzpy.wz_image import WzImage
    if isinstance(node, WzCanvasProperty):
        if node.has_pixels():
            yield current_path, node
        for c in node.children():
            yield from _walk_canvases(c, f"{current_path}/{c.name}")
        return
    if isinstance(node, WzDirectory):
        children = list(node.subdirs.items()) + list(node.images.items())
        for name, child in children:
            yield from _walk_canvases(child, f"{current_path}/{name}" if current_path else name)
        return
    if isinstance(node, WzImage):
        node.parse()
        for c in node.children():
            yield from _walk_canvases(c, f"{current_path}/{c.name}" if current_path else c.name)
        return
    if isinstance(node, WzProperty):
        for c in node.children():
            yield from _walk_canvases(c, f"{current_path}/{c.name}" if current_path else c.name)


def _run_json_bundle_job(job_id: str, target, label: str, reader_lock: threading.Lock):
    """Background worker: serialize each .img under ``target`` into its own
    JSON file inside a temp ZIP, updating the job entry as it progresses."""
    from wzpy.wz_image import WzImage
    images: List[Tuple[str, Any]] = list(target.walk_images(label))
    total = len(images)
    with _JOBS_LOCK:
        _JOBS[job_id]["total"] = total

    fd, zip_path = tempfile.mkstemp(suffix=".zip", prefix="wzpy_export_")
    os.close(fd)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for i, (rel, img) in enumerate(images):
                with _JOBS_LOCK:
                    j = _JOBS[job_id]
                    if j.get("cancel"):
                        j["status"] = "cancelled"
                        return
                    j["progress"] = i
                    j["current"] = rel
                # Reader has shared state (file position + cipher) — serialize
                # one image at a time so we don't fight tree/canvas requests.
                try:
                    with reader_lock:
                        if isinstance(img, WzImage):
                            img.parse()
                        body = json.dumps(_node_to_dict(img), indent=2, ensure_ascii=False)
                except Exception as e:
                    body = json.dumps(
                        {"error": str(e), "name": getattr(img, "name", "")},
                        indent=2,
                    )
                zf.writestr(f"{rel}.json", body)

        with _JOBS_LOCK:
            _JOBS[job_id]["progress"] = total
            _JOBS[job_id]["file_path"] = zip_path
            _JOBS[job_id]["status"] = "done"
    except Exception as e:
        with _JOBS_LOCK:
            _JOBS[job_id]["status"] = "error"
            _JOBS[job_id]["error"] = str(e)
        try:
            os.remove(zip_path)
        except OSError:
            pass


def _walk_sounds(node, current_path: str = "") -> Iterator[Tuple[str, Any]]:
    """Yield every (path, WzSoundProperty) reachable from ``node``."""
    from wzpy.properties import WzSoundProperty, WzSubProperty, WzProperty
    from wzpy.wz_file import WzDirectory
    from wzpy.wz_image import WzImage
    if isinstance(node, WzSoundProperty):
        yield current_path, node
        return
    if isinstance(node, WzDirectory):
        children = list(node.subdirs.items()) + list(node.images.items())
        for name, child in children:
            yield from _walk_sounds(child, f"{current_path}/{name}" if current_path else name)
        return
    if isinstance(node, WzImage):
        node.parse()
        for c in node.children():
            yield from _walk_sounds(c, f"{current_path}/{c.name}" if current_path else c.name)
        return
    if isinstance(node, WzProperty):
        for c in node.children():
            yield from _walk_sounds(c, f"{current_path}/{c.name}" if current_path else c.name)


def _build_sound_zip(node, layout: str) -> bytes:
    """Pack every Sound payload under ``node`` into a ZIP as MP3.

    WZ Sound properties carry MP3 audio bytes after a WAVEFORMATEX
    header (see :func:`_default_mp3_header`); we strip the header
    and emit the raw audio so the result is a real, playable .mp3
    file. Unknown / non-MP3 sounds get the raw payload anyway —
    a media player will still recognize MP3 sync bytes if present
    and ignore garbage prefixes."""
    buf = io.BytesIO()
    seen_names: Dict[str, int] = {}
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path, sound in _walk_sounds(node):
            data = _read_sound_bytes(sound)
            if not data:
                continue
            if layout == "flat":
                name = path.replace("/", "_") + ".mp3"
            else:
                name = f"{path}.mp3"
            if name in seen_names:
                seen_names[name] += 1
                stem, ext = name.rsplit(".", 1)
                name = f"{stem}_{seen_names[name]}.{ext}"
            else:
                seen_names[name] = 0
            zf.writestr(name, data)
            count += 1
    return buf.getvalue() if count else b""


def _read_sound_bytes(sound) -> bytes:
    """Pull the audio payload off either the staged ``_data`` or the
    source mmap. Mirrors the logic in /api/sound."""
    if getattr(sound, "_data", None) is not None:
        return sound._data
    if sound._wz_image is None:
        return b""
    r = sound._wz_image.wz_file.reader
    keep = r.position
    r.seek(sound._data_offset)
    data = r.read(sound._data_length)
    r.seek(keep)
    return data


def _build_image_zip(node, layout: str, region: str) -> bytes:
    """Decode every Canvas under ``node`` and pack into a ZIP."""
    buf = io.BytesIO()
    seen_names: Dict[str, int] = {}
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path, canvas in _walk_canvases(node):
            try:
                img = decode_canvas(canvas, region=region)
            except Exception:
                continue  # skip undecodable canvases (e.g., outlinked)
            png_buf = io.BytesIO()
            img.save(png_buf, format="PNG", optimize=False)
            if layout == "flat":
                # Avoid collisions by suffixing with a counter when we've seen
                # the same final filename before.
                name = path.replace("/", "_") + ".png"
            else:
                name = f"{path}.png"
            # Defensive deduplication (paths *should* be unique but be safe).
            if name in seen_names:
                seen_names[name] += 1
                stem, ext = name.rsplit(".", 1)
                name = f"{stem}_{seen_names[name]}.{ext}"
            else:
                seen_names[name] = 0
            zf.writestr(name, png_buf.getvalue())
            count += 1
    return buf.getvalue() if count else b""

from flask import Flask, Response, abort, jsonify, render_template, request
from PIL import Image, ImageDraw

from wzpy import (
    WzCanvasProperty,
    WzConvexProperty,
    WzFile,
    WzImage,
    WzNullProperty,
    WzPackage,
    WzProperty,
    WzSoundProperty,
    WzSubProperty,
    WzUolProperty,
    WzVectorProperty,
    is_hierarchical_pack,
    open_wz,
    resolve_canvas_link,
)
from wzpy.canvas import decode_canvas
from wzpy.wz_file import WzDirectory


def _character_supported(wz: "WzFile") -> bool:
    """A Character.wz is recognized by the equip-shape root: top-level
    ``Hair``/``Coat``/``Cap`` etc. directories. We probe a couple to avoid
    false positives on look-alike WZs."""
    root = wz.root
    needed = {"Hair", "Coat", "Face"}
    return needed.issubset(set(root.subdirs))


def _get_character_renderer(app: "Flask", region: str):
    """Lazy-build a CharacterRenderer once per Flask app and reuse it.
    Reading zmap + walking dirs is cheap, but calling list_parts() per
    request would otherwise re-do that scan every time."""
    cached = app.config.get("CHARACTER_RENDERER")
    if cached is not None:
        return cached
    from wzpy.character import CharacterRenderer
    wz = app.config["WZ"]
    if not _character_supported(wz):
        return None
    renderer = CharacterRenderer(wz, region=region)
    app.config["CHARACTER_RENDERER"] = renderer
    return renderer


def _score_root_printability(wz: "WzFile") -> float:
    """Fraction of bytes in root directory entry names that look like
    printable ASCII. With the right region key, names like ``"Map"`` /
    ``"Mob_000"`` decode cleanly and the score is ~1.0; with the wrong
    region the same bytes XOR through to high-bit gibberish and the
    score collapses to near zero. Dependable enough as a region oracle."""
    names = list(wz.root.subdirs.keys()) + list(wz.root.images.keys())
    if not names:
        return 0.0
    total = 0
    printable = 0
    for n in names:
        for c in n:
            total += 1
            if 0x20 <= ord(c) < 0x7F:
                printable += 1
    return printable / max(1, total)


def _auto_detect_region(wz_path: str, version: Optional[int]) -> str:
    """Try each known region and return the one that decodes the root
    directory most cleanly. Open + parse-root is cheap for memory-mapped
    files even on multi-GB WZs, so doing it three times is fine.

    For hierarchical packs we score the structure file alone — same
    cipher applies to every sibling, so detection on the entry point
    is enough."""
    # Hierarchical packs derive region from the structure file (the
    # ``<base>.wz`` next to the indexed siblings). Scoring just that
    # one file avoids opening dozens of indexed siblings per region.
    structure_path = wz_path
    if os.path.isdir(wz_path):
        base = os.path.basename(os.path.abspath(wz_path).rstrip(os.sep))
        candidate = os.path.join(wz_path, f"{base}.wz")
        if os.path.isfile(candidate):
            structure_path = candidate
    best: Optional[Tuple[str, float]] = None
    for r in ("BMS", "GMS", "EMS"):
        try:
            wz = WzFile.open(structure_path, region=r, version=version)
        except Exception as e:
            print(f"  {r}: open failed ({e})")
            continue
        score = _score_root_printability(wz)
        wz.close()
        print(f"  {r}: root printability = {score * 100:.1f}%")
        if best is None or score > best[1]:
            best = (r, score)
    # Below this threshold every candidate looks like noise, so the WZ is
    # using a key we don't have built in.
    if best is None or best[1] < 0.5:
        raise SystemExit(
            f"could not auto-detect region for {wz_path}. "
            f"Pass --region GMS/EMS/BMS explicitly."
        )
    return best[0]


def _try_load_character_strings(
    wz_path: str, region: str, version: Optional[int],
):
    """Auto-discover and load ``String.wz`` (or a hierarchical
    ``String/`` folder) sibling of the loaded Character pack.

    The String pack lets the Character Builder show display names
    (``Eqp.img/Eqp/<sub>/<id>/name``) alongside the bare IDs. Returns
    a :class:`StringLookup` on success, or ``None`` when no sibling
    is found or it doesn't load cleanly — callers fall back to IDs.
    """
    from wzpy.string_lookup import StringLookup
    # Recognise the Character pack only — String lookup is gear-only,
    # so loading it for an arbitrary WZ would just waste startup time.
    base = os.path.basename(os.path.abspath(wz_path).rstrip(os.sep)).lower()
    if not (base.startswith("character") or base == "character.wz"):
        return None
    parent = os.path.dirname(os.path.abspath(wz_path).rstrip(os.sep))
    if os.path.isfile(wz_path):
        # If wz_path was a .wz file (e.g. ``data/Character.wz``), the
        # sibling ``data/`` is its parent.
        parent = os.path.dirname(os.path.abspath(wz_path))
    candidates = [
        os.path.join(parent, "String"),
        os.path.join(parent, "String.wz"),
    ]
    for cand in candidates:
        if not os.path.exists(cand):
            continue
        sl = StringLookup.open(cand, region=region, version=version)
        if sl is not None:
            print(f"  loaded String lookup from {cand}", flush=True)
            return sl
        else:
            print(f"  String at {cand} didn't validate; skipping", flush=True)
    return None


def create_app(wz_path: str, region: str = "auto", version: Optional[int] = None) -> Flask:
    if region == "auto":
        print(f"auto-detecting region for {wz_path}:")
        region = _auto_detect_region(wz_path, version)
        print(f"  -> using region: {region}")
    app = Flask(__name__, template_folder="templates", static_folder="static")
    # writable=True mmaps the WZ with ACCESS_WRITE so /api/save can patch
    # scalar values in-place without copying the entire archive. For
    # hierarchical packs this falls back to read-only — split-file
    # save_as is not supported.
    hierarchical = is_hierarchical_pack(wz_path)
    if hierarchical:
        wz = WzPackage.open(wz_path, region=region, version=version)
        print(f"  loaded hierarchical pack with {len(wz._files)} .wz file(s)")
    else:
        wz = WzFile.open(wz_path, region=region, version=version, writable=True)
    app.config["WZ"] = wz
    app.config["WZ_HIERARCHICAL"] = hierarchical
    app.config["WZ_REGION"] = region
    # Background bundle exports parse images on a worker thread; the WZ reader
    # carries shared file-position + cipher state, so we mediate access with
    # this lock. Currently only the bundle worker acquires it.
    app.config["WZ_READER_LOCK"] = threading.Lock()

    # When the loaded WZ is Character, look for a sibling ``String.wz``
    # / ``String/`` so the UI can show real item names instead of bare
    # IDs. Best-effort: any failure (missing file, unreadable, wrong
    # region) leaves ``CHARACTER_STRINGS`` as None and the UI falls
    # back to IDs.
    app.config["CHARACTER_STRINGS"] = _try_load_character_strings(
        wz_path, region, version,
    )

    # ── helpers ──────────────────────────────────────────────────────
    def _resolve(path: str) -> Tuple[Any, str]:
        """Walk ``path`` (slash-separated) from the WZ root.

        Returns ``(node, remaining)`` where ``node`` is the deepest WZ tree
        node (directory or image) we could reach, and ``remaining`` is the
        path inside the .img property tree (may be empty).
        """
        path = path.strip("/")
        if not path:
            return wz.root, ""
        node: Any = wz.root
        parts = path.split("/")
        i = 0
        while i < len(parts):
            part = parts[i]
            if isinstance(node, WzDirectory):
                child = node.child(part)
                if child is None:
                    abort(404, f"no such node: {part}")
                node = child
                i += 1
            elif isinstance(node, WzImage):
                remaining = "/".join(parts[i:])
                return node, remaining
            elif isinstance(node, WzProperty):
                child = node.child(part)
                if child is None:
                    abort(404, f"no such property: {part}")
                node = child
                i += 1
            else:
                abort(404, "cannot descend further")
        return node, ""

    def _children_of(node: Any, image_path: Optional[str] = None) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if isinstance(node, WzDirectory):
            for name in sorted(node.subdirs, key=_natural_key):
                out.append({"name": name, "kind": "directory", "leaf": False})
            for name in sorted(node.images, key=_natural_key):
                out.append({"name": name, "kind": "image", "leaf": False})
            return out
        if isinstance(node, (WzImage, WzProperty)):
            children = sorted(node.children(), key=lambda p: _natural_key(p.name))
            for c in children:
                out.append(_describe_property(c, image_path=image_path))
            return out
        return out

    def _resolve_uol_target(uol_prop: WzUolProperty) -> Optional[WzProperty]:
        """Follow a UOL chain to its non-UOL target. Resolution starts from
        the UOL's parent (matching MapleLib's WzUolProperty.LinkValue) and
        bails on cycles or paths that escape the .img tree."""
        seen = set()
        cur: Optional[WzProperty] = uol_prop
        for _ in range(16):  # depth cap; chains beyond this are pathological
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

    def _in_image_path(p: WzProperty) -> str:
        """Slash-joined path from the .img root to ``p`` (excludes the root
        SubProperty's own name, which mirrors the image filename)."""
        parts: List[str] = []
        cur: Optional[WzProperty] = p
        while cur is not None and cur.parent is not None:
            parts.append(cur.name)
            cur = cur.parent
        return "/".join(reversed(parts))

    def _describe_property(p: WzProperty, image_path: Optional[str] = None) -> Dict[str, Any]:
        d: Dict[str, Any] = {"name": p.name, "kind": p.type_name, "leaf": True}
        if isinstance(p, WzSubProperty):
            # ``has_children`` is O(1); calling ``children()`` would allocate
            # a fresh list of every child purely to check emptiness.
            has = p.has_children()
            d["leaf"] = not has
            if has:
                d["count"] = p.child_count()
        if isinstance(p, WzCanvasProperty):
            d["leaf"] = False
            d["width"] = p.width
            d["height"] = p.height
            d["format"] = p.format + p.format2
            d["renderable"] = p.has_pixels()
            # Surfaced so the canvas viewer can show the slot budget for
            # the "Replace…" button.
            d["slot_total"] = p._png_length
            # Outlink/inlink: most placeholders are 1×1 with the real
            # pixels stored in a sibling _Canvas image. Resolve the link
            # so the UI lays out the canvas at its true size and the
            # viewer fetches the linked PNG.
            if p.child("_outlink") is not None or p.child("_inlink") is not None:
                try:
                    linked = resolve_canvas_link(p, wz.root)
                except Exception:
                    linked = None
                if linked is not None and linked is not p:
                    d["linked"] = True
                    d["width"] = linked.width
                    d["height"] = linked.height
                    d["format"] = linked.format + linked.format2
                    d["renderable"] = linked.has_pixels()
                    d["slot_total"] = linked._png_length
        elif isinstance(p, WzSoundProperty):
            d["length_ms"] = p.length_ms
            d["bytes"] = p.value
        elif isinstance(p, WzVectorProperty):
            d["x"] = p.x
            d["y"] = p.y
        elif isinstance(p, WzNullProperty):
            d["value"] = None
        elif not isinstance(p, WzSubProperty):
            try:
                d["value"] = p.value
            except Exception:
                d["value"] = None
            # For strings, include the editor budget metadata so the
            # client can show a live N / max indicator without a probe
            # request per keystroke.
            from wzpy.properties import WzStringProperty
            if isinstance(p, WzStringProperty) and p._payload_length is not None:
                d["encoding"] = p._encoding
                d["payload_length"] = p._payload_length
                d["indirected"] = p._indirected

        # UOL: resolve the chain so the client can inline the referenced
        # value (image, audio, scalar) instead of just showing the path.
        if isinstance(p, WzUolProperty):
            target = _resolve_uol_target(p)
            if target is not None:
                d["target_kind"] = target.type_name
                if image_path is not None:
                    in_img = _in_image_path(target)
                    d["target_path"] = (
                        f"{image_path}/{in_img}" if in_img else image_path
                    )
                if isinstance(target, WzCanvasProperty):
                    d["target_width"] = target.width
                    d["target_height"] = target.height
                    d["target_format"] = target.format + target.format2
                    d["target_renderable"] = target.has_pixels()
                elif isinstance(target, WzSoundProperty):
                    d["target_length_ms"] = target.length_ms
                    d["target_bytes"] = target.value
                elif isinstance(target, WzVectorProperty):
                    d["target_x"] = target.x
                    d["target_y"] = target.y
                elif not isinstance(target, WzSubProperty):
                    try:
                        d["target_value"] = target.value
                    except Exception:
                        pass
        return d

    # ── routes ───────────────────────────────────────────────────────
    @app.route("/")
    def index() -> str:
        # ``has_character`` toggles a "Character Builder" link in the header
        # — the builder only makes sense when a Character.wz is loaded.
        return render_template(
            "index.html",
            wz_name=wz_path,
            wz_version=wz.version,
            wz_region=region,
            has_character=_character_supported(wz),
        )

    @app.route("/character")
    def character_builder() -> str:
        if not _character_supported(wz):
            abort(404, "Character builder requires a Character.wz")
        return render_template(
            "character.html",
            wz_name=wz_path,
            wz_version=wz.version,
            wz_region=region,
        )

    @app.route("/api/character/parts/<category>")
    def api_character_parts(category: str) -> Response:
        from wzpy.character import CATEGORIES, CharacterRenderer
        if category not in CATEGORIES:
            abort(404, f"unknown category {category!r}")
        renderer = _get_character_renderer(app, region)
        if renderer is None:
            abort(404, "Character.wz not loaded")
        items = renderer.list_parts(category)
        # Decorate each entry with its display name from String.wz
        # when available. Misses (no String pack, no entry for this
        # ID) leave ``name`` absent so the client falls back to the ID.
        strings = app.config.get("CHARACTER_STRINGS")
        if strings is not None:
            for entry in items:
                nm = strings.name(category, entry["id"])
                if nm:
                    entry["name"] = nm
        return jsonify({"category": category, "parts": items})

    @app.route("/api/character/weapon_poses/<equip_id>")
    def api_character_weapon_poses(equip_id: str) -> Response:
        """Return the action subtrees a weapon ships with — a subset
        of :data:`SUPPORTED_POSES` (stand1/stand2/walk1/swing*/…). The
        UI uses it to filter the pose dropdown so the user only sees
        poses with weapon art."""
        renderer = _get_character_renderer(app, region)
        if renderer is None:
            abort(404, "Character.wz not loaded")
        poses = renderer.get_weapon_poses(equip_id)
        return jsonify({"id": equip_id, "poses": poses})

    @app.route("/api/character/poses")
    def api_character_poses() -> Response:
        """List every pose the equipped body actually ships, with the
        per-frame delays in ms. ``body`` query param overrides the
        default ``00002000`` body. The UI uses this to populate the
        pose dropdown and to time the cycling preview animation."""
        from wzpy.character import SUPPORTED_POSES
        renderer = _get_character_renderer(app, region)
        if renderer is None:
            abort(404, "Character.wz not loaded")
        body_id = request.args.get("body", "").strip() or "00002000"
        out = []
        for p in SUPPORTED_POSES:
            delays = renderer.pose_frame_delays(p, body_id)
            if delays:
                out.append({"pose": p, "delays": delays})
        return jsonify({"body": body_id, "poses": out})

    @app.route("/api/character/equip_info/<equip_id>")
    def api_character_equip_info(equip_id: str) -> Response:
        """Return the gameplay-relevant ``info/*`` fields for an equip.

        The Character Builder's hover tooltip shows the same numbers
        the in-game tooltip would: required stats, stat increases,
        weapon type / speed, price. We hand back only the fields the
        client knows how to render so unknown future fields don't
        leak into the UI.
        """
        from wzpy.character import category_for_id
        from wzpy.properties import (
            WzIntProperty, WzShortProperty, WzStringProperty, WzSubProperty,
        )
        renderer = _get_character_renderer(app, region)
        if renderer is None:
            abort(404, "Character.wz not loaded")
        cat = category_for_id(equip_id)
        if cat is None:
            abort(404, "unknown equip id")
        img = renderer._open_part(equip_id)
        if img is None:
            abort(404, "no such equip")
        info = img.parse().get("info")
        out: Dict[str, Any] = {}
        if isinstance(info, WzSubProperty):
            keep = (
                # Requirements
                "reqLevel", "reqJob", "reqSTR", "reqDEX", "reqINT",
                "reqLUK", "reqPOP",
                # Stat / combat increases
                "incSTR", "incDEX", "incINT", "incLUK",
                "incPAD", "incMAD", "incPDD", "incMDD",
                "incACC", "incEVA", "incMHP", "incMMP",
                "incSpeed", "incJump",
                # Weapon-specific
                "attackSpeed", "sfx",
                # Misc
                "price", "tuc", "cash",
            )
            for k in keep:
                v = info.get(k)
                if isinstance(v, (WzIntProperty, WzShortProperty)):
                    out[k] = int(v.value)
                elif isinstance(v, WzStringProperty):
                    out[k] = v.value
        # Display name from String.wz when available.
        name = None
        strings = app.config.get("CHARACTER_STRINGS")
        if strings is not None:
            name = strings.name(cat, equip_id)
        return jsonify(
            {"id": equip_id, "category": cat, "info": out, "name": name},
        )

    @app.route("/api/character/ear_types/<equip_id>")
    def api_character_ear_types(equip_id: str) -> Response:
        """Return the ear-canvas names the given Head image ships with
        (e.g. ``humanEar``, ``lefEar``, ``highlefEar``) so the UI can
        offer a selector when the Head has more than one option."""
        renderer = _get_character_renderer(app, region)
        if renderer is None:
            abort(404, "Character.wz not loaded")
        ears = renderer.get_ear_types(equip_id)
        return jsonify({"id": equip_id, "ear_types": ears})

    @app.route("/api/character/compose")
    def api_character_compose() -> Response:
        from wzpy.character import (
            CharacterRenderer, DEFAULT_EAR_TYPE, SUPPORTED_POSES, category_for_id,
        )
        renderer = _get_character_renderer(app, region)
        if renderer is None:
            abort(404, "Character.wz not loaded")
        ids_param = request.args.get("ids", "").strip()
        ids = [s for s in ids_param.split(",") if s.strip()]
        if not ids:
            abort(400, "missing or empty ?ids=")
        pose = request.args.get("pose", "").strip() or None
        if pose is not None and pose not in SUPPORTED_POSES:
            pose = None  # silently fall through to auto-detect
        ear_type = request.args.get("ear", "").strip() or DEFAULT_EAR_TYPE
        flip = request.args.get("flip", "").lower() in ("1", "true", "yes")
        try:
            frame = int(request.args.get("frame", "0"))
        except ValueError:
            frame = 0
        # Clamp to the actual frame count of the resolved pose's body
        # subtree. Stand1 ships 3 frames, walk1 ships 4, shoot2 ships
        # 5 — without per-pose discovery the old hard cap of 2 cut
        # off any animation longer than the breathing cycle.
        resolved_pose = renderer.detect_pose(ids, pose)
        body_id = next(
            (e for e in ids if category_for_id(e) == "Body"), "00002000",
        )
        n_frames = len(renderer.pose_frame_delays(resolved_pose, body_id))
        if n_frames > 0:
            frame = max(0, min(n_frames - 1, frame))
        else:
            frame = 0
        try:
            img = renderer.compose(
                ids, pose=pose, ear_type=ear_type, flip=flip, frame=frame,
            )
        except Exception as exc:
            print(f"  [compose error] {exc}", flush=True)
            abort(500, f"compose failed: {exc}")
        scale = max(1, min(8, int(request.args.get("scale", "2"))))
        if scale != 1:
            img = img.resize(
                (img.width * scale, img.height * scale), Image.NEAREST,
            )
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(buf.getvalue(), mimetype="image/png", headers={
            "Cache-Control": "no-store",  # interactive — selection changes
            "X-Resolved-Pose": renderer.detect_pose(ids, pose),
        })

    @app.route("/api/character/compose_animation")
    def api_character_compose_animation() -> Response:
        """Compose every frame of the requested pose at a SHARED
        bounding box and return them base64-encoded inside a JSON
        envelope, alongside the per-frame ``delay`` (ms) read from
        the body image so the client can time the cycling preview.

        Each frame's per-part anchor math runs independently (so the
        body's per-frame position is faithful), but the union of all
        frames' content bboxes is taken before rendering — that way
        the navel sits at the same image-space pixel in every frame
        and the cycling preview animation doesn't wobble. For action
        poses (walk, swing, …) the natural per-frame motion is
        preserved; only the rest poses (stand1/stand2) get the
        breathing-anchor stabilization that keeps cap / face frozen
        across the cycle.
        """
        import base64
        from wzpy.character import (
            CharacterRenderer, DEFAULT_EAR_TYPE, SUPPORTED_POSES, category_for_id,
        )
        renderer = _get_character_renderer(app, region)
        if renderer is None:
            abort(404, "Character.wz not loaded")
        ids_param = request.args.get("ids", "").strip()
        ids = [s for s in ids_param.split(",") if s.strip()]
        if not ids:
            abort(400, "missing or empty ?ids=")
        pose = request.args.get("pose", "").strip() or None
        if pose is not None and pose not in SUPPORTED_POSES:
            pose = None
        ear_type = request.args.get("ear", "").strip() or DEFAULT_EAR_TYPE
        flip = request.args.get("flip", "").lower() in ("1", "true", "yes")
        try:
            scale = max(1, min(8, int(request.args.get("scale", "2"))))
        except ValueError:
            scale = 2
        resolved_pose = renderer.detect_pose(ids, pose)
        body_id = next(
            (e for e in ids if category_for_id(e) == "Body"), "00002000",
        )
        delays = renderer.pose_frame_delays(resolved_pose, body_id)
        try:
            # Pass ``None`` so the renderer auto-discovers the frame
            # count from the body's pose subtree — same source of
            # truth as ``delays`` above so the two arrays stay
            # length-matched.
            frames_imgs = renderer.compose_animation(
                ids, pose=pose, ear_type=ear_type, flip=flip,
                frames=None,
            )
        except Exception as exc:
            print(f"  [compose_animation error] {exc}", flush=True)
            abort(500, f"compose_animation failed: {exc}")
        if scale != 1:
            frames_imgs = [
                f.resize((f.width * scale, f.height * scale), Image.NEAREST)
                for f in frames_imgs
            ]
        encoded: List[str] = []
        for f in frames_imgs:
            buf = io.BytesIO()
            f.save(buf, format="PNG")
            encoded.append(base64.b64encode(buf.getvalue()).decode("ascii"))
        return jsonify({
            "frames": encoded,
            "count": len(encoded),
            "delays": delays,
            "width": frames_imgs[0].width if frames_imgs else 0,
            "height": frames_imgs[0].height if frames_imgs else 0,
            "resolved_pose": resolved_pose,
        })

    @app.route("/api/search")
    def api_search() -> Response:
        """Walk the WZ tree and return nodes whose name contains the
        query as a case-insensitive substring.

        By default, matches directories and ``.img`` images only
        (fast — the tree alone is enough to find any equipment ID).
        With ``deep=1`` also descends into each .img's property tree,
        matching property names and stringy values (WzStringProperty
        / WzUolProperty values), so queries like ``swordL`` find
        weapons whose ``info/sfx="swordL"``. Deep search forces a
        parse of every visited img — slow on big WZ packs the first
        time, near-instant after the parse cache warms up.

        Capped at ``limit`` hits (default 200) with early
        termination so a 1-character ``deep`` query doesn't iterate
        the entire archive.
        """
        from wzpy.properties import (
            WzCanvasProperty, WzStringProperty, WzSubProperty,
            WzUolProperty,
        )
        q = request.args.get("q", "").strip().lower()
        try:
            limit = max(1, min(1000, int(request.args.get("limit", "200"))))
        except ValueError:
            limit = 200
        deep = request.args.get("deep", "").lower() in ("1", "true", "yes")
        if not q:
            return jsonify({"results": [], "truncated": False})
        results: List[Dict[str, Any]] = []
        truncated = False
        # Sentinel raised inside the walkers to short-circuit recursion
        # the moment we hit ``limit`` — saves walking the rest of the
        # WZ once the response is already full.
        class _SearchFull(Exception):
            pass

        def append(entry: Dict[str, Any]) -> None:
            results.append(entry)
            if len(results) >= limit:
                raise _SearchFull

        def walk_property(prop_node: Any, path: str) -> None:
            # Walk every descendant property within an img, matching
            # by name and (for stringy properties) value. Skip
            # ``Image``-rooted recursion that's already counted:
            # ``prop_node`` is always the SubProperty representing
            # the .img's own root (or a sub).
            if not hasattr(prop_node, "children"):
                return
            for child in prop_node.children():
                full = f"{path}/{child.name}"
                name_match = q in child.name.lower()
                value_match = False
                if isinstance(child, (WzStringProperty, WzUolProperty)):
                    cv = child.value
                    if cv is not None and q in str(cv).lower():
                        value_match = True
                if name_match or value_match:
                    append({
                        "path": full,
                        "name": child.name,
                        "kind": child.type_name,
                        "match": "value" if (value_match and not name_match) else "name",
                    })
                # Descend into containers. Canvas has metadata children
                # (origin / map / z / _outlink / _inlink) that are
                # legitimate to surface as matches; UOL is a leaf.
                if not isinstance(child, WzUolProperty):
                    walk_property(child, full)

        def walk(node: WzDirectory, prefix: str) -> None:
            # Directories first so the result order roughly mirrors
            # the on-disk layout users expect.
            for name, sub in node.subdirs.items():
                full = f"{prefix}/{name}" if prefix else name
                if q in name.lower():
                    append({
                        "path": full, "name": name, "kind": "Directory",
                    })
                walk(sub, full)
            for name, img in node.images.items():
                full = f"{prefix}/{name}" if prefix else name
                if q in name.lower():
                    append({"path": full, "name": name, "kind": "Image"})
                if deep:
                    try:
                        root = img.parse()
                    except Exception:
                        continue
                    walk_property(root, full)

        try:
            walk(wz.root, "")
        except _SearchFull:
            truncated = True
        return jsonify({"results": results, "truncated": truncated})

    @app.route("/api/tree")
    @app.route("/api/tree/")
    @app.route("/api/tree/<path:subpath>")
    def api_tree(subpath: str = "") -> Response:
        t0 = time.perf_counter()
        node, remaining = _resolve(unquote(subpath))
        if isinstance(node, WzImage) and remaining:
            prop = node.get(remaining)
            if prop is None:
                abort(404)
            children = _children_of(prop, image_path=node.path)
            kind = prop.type_name
        elif isinstance(node, WzImage):
            node.parse()
            children = _children_of(node, image_path=node.path)
            kind = "Image"
        else:
            children = _children_of(node)
            kind = "Directory" if isinstance(node, WzDirectory) else node.type_name
        elapsed_ms = (time.perf_counter() - t0) * 1000
        # Log in the access stream so the user can see exactly where time goes
        # without us having to redirect them to a profiler. ``flush`` matters
        # because Flask's dev server access log goes to stderr; otherwise this
        # line can buffer behind a chunk of access lines.
        print(f"  [tree {elapsed_ms:6.1f} ms, {len(children):5d} children] /{subpath}", flush=True)
        payload: Dict[str, Any] = {"path": subpath, "kind": kind, "children": children}
        # Bubble up partial-parse warnings so the UI can show a hint that
        # the listing isn't complete.
        if isinstance(node, WzImage) and (node.truncated or node.parse_warnings):
            payload["truncated"] = node.truncated
            if node.parse_warnings:
                payload["parse_warnings"] = list(node.parse_warnings)
        resp = jsonify(payload)
        resp.headers["X-Server-Ms"] = f"{elapsed_ms:.1f}"
        resp.headers["X-Children"] = str(len(children))
        return resp

    @app.route("/api/property/<path:subpath>")
    def api_property(subpath: str) -> Response:
        node, remaining = _resolve(unquote(subpath))
        target: Any = node
        image_path: Optional[str] = None
        if isinstance(node, WzImage):
            image_path = node.path
            if remaining:
                target = node.get(remaining)
            else:
                target = node.root
        elif remaining:
            abort(404)
        if target is None:
            abort(404)
        return jsonify(
            _describe_property(target, image_path=image_path)
            if isinstance(target, WzProperty)
            else {"name": getattr(target, "name", ""), "kind": "Directory"}
        )

    @app.route("/api/canvas/<path:subpath>.png")
    def api_canvas(subpath: str) -> Response:
        # Break ``decode_canvas`` into its three measurable phases
        # (raw read → decompress → pixel-decode → PNG encode) so the
        # log line tells us which one is the bottleneck. Heavy hitters
        # are usually pixel-decode for the pure-Python paths
        # (ARGB4444, ARGB1555, RGB565, downsampled 3/517, DXT3/DXT5),
        # and PNG encode for very large bitmaps.
        from wzpy.canvas import (
            _decompress, _decode_pixels, _read_canvas_bytes,
        )
        from wzpy.crypto import WzKey

        t_total = time.perf_counter()
        node, remaining = _resolve(unquote(subpath))
        if not isinstance(node, WzImage) or not remaining:
            abort(404)
        prop = node.get(remaining)
        # Follow a UOL to its target so paths like
        # ``Cap/01005222.img/alert/0/default`` (a UOL pointing back at
        # ``default/default``) resolve transparently. Without this an
        # animation frame URL that lands on a UOL leaf would 404.
        if isinstance(prop, WzUolProperty):
            prop = _resolve_uol_target(prop) or prop
        # Cap-style frames are SubProperties wrapping a UOL'd
        # ``default``. Look one level deeper so the URL can point at
        # the wrapping SubProperty (matches how the animation
        # endpoint addresses each frame).
        if isinstance(prop, WzSubProperty):
            for child_name in ("default", "0"):
                child = prop.get(child_name)
                if isinstance(child, WzUolProperty):
                    child = _resolve_uol_target(child) or child
                if isinstance(child, WzCanvasProperty):
                    prop = child
                    break
        if not isinstance(prop, WzCanvasProperty):
            abort(404)

        # If the canvas has _outlink/_inlink, prefer the linked canvas's
        # pixels — the property here is usually a 1×1 placeholder. We
        # fall back to the placeholder if resolution fails so the
        # original error path still renders something useful.
        link_target: Optional[WzCanvasProperty] = None
        if prop.child("_outlink") is not None or prop.child("_inlink") is not None:
            try:
                link_target = resolve_canvas_link(prop, wz.root)
            except Exception:
                link_target = None
        render_prop = link_target if link_target is not None else prop
        if not render_prop.has_pixels():
            abort(404)

        region = app.config["WZ_REGION"]
        key = WzKey.for_region(region)
        fmt = render_prop.format + render_prop.format2

        t_read = t_decompress = t_decode = t_encode = 0.0
        raw_bytes = decompressed_bytes = png_bytes = 0
        try:
            t0 = time.perf_counter()
            raw = _read_canvas_bytes(render_prop)
            raw_bytes = len(raw)
            t_read = time.perf_counter() - t0

            t0 = time.perf_counter()
            decompressed = _decompress(render_prop, key)
            decompressed_bytes = len(decompressed)
            t_decompress = time.perf_counter() - t0

            t0 = time.perf_counter()
            img = _decode_pixels(
                decompressed, render_prop.width, render_prop.height, fmt
            )
            t_decode = time.perf_counter() - t0
        except Exception as exc:
            try:
                raw = _read_canvas_bytes(render_prop)
            except Exception:
                raw = b""
            outlink = prop.child("_outlink")
            inlink = prop.child("_inlink")
            lines = [
                f"decode error: {exc}",
                f"format={render_prop.format + render_prop.format2}  "
                f"size={render_prop.width}x{render_prop.height}",
                f"data {len(raw)} bytes; first 16: {raw[:16].hex()}",
            ]
            if outlink is not None:
                lines.append(f"_outlink → {outlink.value}")
                if link_target is None:
                    lines.append("(link unresolved — target not in this pack)")
            if inlink is not None:
                lines.append(f"_inlink → {inlink.value}")
            placeholder = Image.new(
                "RGBA",
                (
                    max(render_prop.width, 560),
                    max(render_prop.height, 16 * (len(lines) + 1)),
                ),
                (40, 40, 40, 255),
            )
            draw = ImageDraw.Draw(placeholder)
            for i, line in enumerate(lines):
                color = (220, 120, 120, 255) if i == 0 else (180, 180, 180, 255)
                draw.text((6, 6 + 14 * i), line, fill=color)
            buf = io.BytesIO()
            placeholder.save(buf, format="PNG")
            elapsed_ms = (time.perf_counter() - t_total) * 1000
            print(
                f"  [canvas {elapsed_ms:6.1f} ms FAILED] "
                f"{render_prop.width}x{render_prop.height} fmt={fmt}  "
                f"raw={raw_bytes}b  reason={exc}  /{subpath}",
                flush=True,
            )
            return Response(buf.getvalue(), mimetype="image/png", status=200)

        t0 = time.perf_counter()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.tell()
        t_encode = time.perf_counter() - t0

        elapsed_ms = (time.perf_counter() - t_total) * 1000
        link_tag = " (linked)" if link_target is not None else ""
        # Always log canvases that take meaningful time so the user can
        # see when something is heavy without grepping every request.
        if elapsed_ms > 30 or png_bytes > 200_000:
            print(
                f"  [canvas {elapsed_ms:6.1f} ms]{link_tag} "
                f"{render_prop.width}x{render_prop.height} fmt={fmt}  "
                f"raw={raw_bytes}b decomp={decompressed_bytes}b png={png_bytes}b  "
                f"read={t_read*1000:.1f} decompress={t_decompress*1000:.1f} "
                f"decode={t_decode*1000:.1f} encode={t_encode*1000:.1f}  "
                f"/{subpath}",
                flush=True,
            )
        resp = Response(buf.getvalue(), mimetype="image/png")
        # Server-Timing is what Chrome's Network panel surfaces in the
        # "Timing" tab — neat per-request breakdown without console
        # spam for fast cases.
        resp.headers["Server-Timing"] = ", ".join([
            f"read;dur={t_read*1000:.1f}",
            f"decompress;dur={t_decompress*1000:.1f}",
            f"decode;dur={t_decode*1000:.1f}",
            f"encode;dur={t_encode*1000:.1f}",
            f"total;dur={elapsed_ms:.1f}",
        ])
        resp.headers["X-Canvas-Format"] = str(fmt)
        resp.headers["X-Canvas-Size"] = f"{render_prop.width}x{render_prop.height}"
        if link_target is not None:
            resp.headers["X-Canvas-Linked"] = "1"
        # Save-As filename hint. Without this the browser falls back to
        # the last URL segment, which for thumbnails is just ``icon`` /
        # ``body`` / ``hairOverHead``. Pull the .img stem out of the
        # request path so a Cap thumbnail saves as e.g. ``01000000.png``
        # instead of ``icon.png``. Use ``inline`` so the browser still
        # renders it in the page; the filename only kicks in on Save As.
        resp.headers["Content-Disposition"] = (
            f'inline; filename="{_canvas_save_name(subpath)}"'
        )
        return resp

    @app.route("/api/sound/<path:subpath>")
    def api_sound(subpath: str) -> Response:
        """Serve a Sound property's audio bytes.

        Honours HTTP ``Range`` requests with ``206 Partial Content`` —
        without this the browser's <audio controls> hides the scrubber
        and refuses to seek, because it can't tell whether the source
        supports random access. Even on a single-shot full-file fetch
        we set ``Accept-Ranges: bytes`` so the browser knows the
        response is seekable on subsequent range requests.
        """
        node, remaining = _resolve(unquote(subpath))
        if not isinstance(node, WzImage) or not remaining:
            abort(404)
        prop = node.get(remaining)
        if not isinstance(prop, WzSoundProperty):
            abort(404)
        # Sounds added via /api/add/sound have no source-mmap bytes;
        # their audio lives on ``prop._data``.
        if getattr(prop, "_data", None) is not None:
            data = prop._data
        else:
            r = wz.reader
            keep = r.position
            r.seek(prop._data_offset)
            data = r.read(prop._data_length)
            r.seek(keep)
        total = len(data)

        range_hdr = request.headers.get("Range", "")
        # Match ``bytes=START-END`` with either side optional. Any
        # other form (multipart, suffix-length, etc.) we just fall
        # through to the full-body response, which is still legal.
        m = re.match(r"^bytes=(\d*)-(\d*)$", range_hdr)
        if m and (m.group(1) or m.group(2)):
            start = int(m.group(1)) if m.group(1) else 0
            end = int(m.group(2)) if m.group(2) else total - 1
            if start >= total or start > end:
                resp = Response("", status=416, mimetype="audio/mpeg")
                resp.headers["Content-Range"] = f"bytes */{total}"
                return resp
            end = min(end, total - 1)
            chunk = data[start:end + 1]
            resp = Response(chunk, status=206, mimetype="audio/mpeg")
            resp.headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            resp.headers["Content-Length"] = str(len(chunk))
            resp.headers["Accept-Ranges"] = "bytes"
            return resp

        resp = Response(data, mimetype="audio/mpeg")
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Content-Length"] = str(total)
        return resp

    @app.route("/api/animation/<path:subpath>")
    def api_animation(subpath: str) -> Response:
        """Gather animation frames for a SubProperty whose children are
        numbered (0, 1, 2, ...). Each frame's bitmap is either a
        direct Canvas child or a SubProperty wrapping a UOL'd
        ``default`` canvas — the cap-action shape (``alert``,
        ``walk1``, …) where every frame UOLs back to the cap's
        canonical ``default/default`` canvas. ``delay`` (ms) and
        ``origin`` (Vector) sit on either the wrapper SubProperty or
        the resolved canvas itself, depending on the WZ author.

        Response: ``{path, frame_count, frames: [{index, name, url,
        width, height, delay_ms, origin: {x, y}}]}``.

        Status codes:
          * 200 — frames found.
          * 204 — target IS a SubProperty but doesn't look animation-
            shaped (no numeric children, or none resolve to a
            canvas). Lets the client's speculative
            ``maybeOfferAnimation`` probe drop quietly without
            painting the access log red on every SubProperty click.
          * 404 — target isn't a SubProperty at all (or the path
            doesn't resolve).
        """
        from wzpy.properties import (
            WzCanvasProperty, WzIntProperty, WzShortProperty,
            WzSubProperty, WzVectorProperty,
        )
        from wzpy.wz_package import resolve_canvas_link
        from urllib.parse import quote as _quote

        target = _resolve_target(subpath)
        if not isinstance(target, WzSubProperty):
            abort(404, "target is not a SubProperty")

        def _frame_dimensions(canvas):
            """Return ``(width, height)``. Resolves _outlink/_inlink so
            cap-style 1×1 placeholders report the real pixel size of
            the linked canvas instead of the placeholder's dimensions
            (api_canvas does the same at render time)."""
            if canvas.child("_outlink") is None and canvas.child("_inlink") is None:
                return canvas.width, canvas.height
            try:
                linked = resolve_canvas_link(canvas, wz.root)
            except Exception:
                linked = None
            if isinstance(linked, WzCanvasProperty):
                return linked.width, linked.height
            return canvas.width, canvas.height

        def _resolve_frame_canvas(child):
            """Return ``(canvas, sub_path_extra)`` where ``canvas`` is
            the WzCanvasProperty for this frame's bitmap and
            ``sub_path_extra`` is the additional path segments past
            ``child.name`` that the canvas URL needs (so api_canvas
            lands on either the SubProperty wrapper or the deeper
            UOL leaf — both follow UOLs after the recent fix).
            ``(None, None)`` when no bitmap is found.

            Walks every leaf inside the wrapping SubProperty rather
            than checking a hardcoded name list — different equipment
            uses different leaf names (Cap: ``default``/``defaultAc``,
            Longcoat: ``mail``/``mailArm``, Pants: ``pants``,
            Glove: ``lGlove``/``rGlove``, Weapon: ``weapon``, …) and
            we want the inline ▶ Play animation button to work for
            all of them. Picks the first leaf with pixels in WZ
            order, which matches how the in-game animation looks
            since the primary canvas (``mail``, ``default``,
            ``weapon``…) always comes first in stock data."""
            if isinstance(child, WzCanvasProperty) and child.has_pixels():
                return child, ""
            if isinstance(child, WzSubProperty):
                for sub in child.children():
                    target_node = sub
                    if isinstance(target_node, WzUolProperty):
                        target_node = _resolve_uol_target(target_node)
                    if isinstance(target_node, WzCanvasProperty) and target_node.has_pixels():
                        return target_node, f"/{sub.name}"
            return None, None

        frames: List[Dict[str, Any]] = []
        for child in target.children():
            try:
                idx = int(child.name)
            except (TypeError, ValueError):
                continue
            canvas, extra = _resolve_frame_canvas(child)
            if canvas is None:
                continue
            delay_ms = 100  # MapleStory's typical default when no delay is set
            origin = {"x": 0, "y": 0}
            # Per-frame metadata (delay / origin) can sit on either the
            # wrapping SubProperty (cap-style) or the resolved canvas
            # (legacy direct-canvas frames). Sweep both, last writer
            # wins so author-explicit values on the wrapper override
            # any defaults on the shared underlying canvas.
            for source in (canvas, child):
                if not isinstance(source, WzSubProperty) and not isinstance(source, WzCanvasProperty):
                    continue
                for sub in source.children():
                    if sub.name == "delay" and isinstance(sub, (WzIntProperty, WzShortProperty)):
                        try:
                            delay_ms = int(sub.value)
                        except Exception:
                            pass
                    elif sub.name == "origin" and isinstance(sub, WzVectorProperty):
                        origin = {"x": int(sub.x), "y": int(sub.y)}
            width, height = _frame_dimensions(canvas)
            frames.append({
                "index": idx,
                "name": child.name,
                "url": f"/api/canvas/{_quote(subpath, safe='/')}/{_quote(child.name)}{extra}.png",
                "width": width,
                "height": height,
                "delay_ms": max(1, delay_ms),
                "origin": origin,
            })

        if not frames:
            # Target is a SubProperty but not animation-shaped; let
            # the client probe complete quietly.
            return Response(status=204)
        frames.sort(key=lambda f: f["index"])
        return jsonify({
            "path": subpath,
            "frame_count": len(frames),
            "frames": frames,
        })

    # ── export endpoints ─────────────────────────────────────────────
    def _resolve_target(subpath: str):
        """Like ``_resolve`` but returns whatever node the caller asked for —
        for an .img mid-path it descends into the property tree."""
        node, remaining = _resolve(unquote(subpath))
        if isinstance(node, WzImage):
            node.parse()
            if remaining:
                prop = node.get(remaining)
                if prop is None:
                    abort(404)
                return prop
        return node

    def _safe_filename(subpath: str, ext: str) -> str:
        base = subpath.replace("/", "_").replace("\\", "_") or "wz_root"
        # Strip characters problematic on Windows.
        base = re.sub(r'[<>:"|?*]', "_", base)
        return f"{base}.{ext}"

    def _canvas_save_name(subpath: str) -> str:
        """Default Save As name for ``/api/canvas/<subpath>.png``.

        Picks the .img stem so a thumbnail at ``Cap/01000000.img/info/icon``
        saves as ``01000000.png``. If the path doesn't contain an .img
        segment (rare — direct property paths under the root), fall back
        to the underscore-joined subpath."""
        for part in unquote(subpath).split("/"):
            if part.endswith(".img"):
                stem = part[: -len(".img")]
                if stem:
                    return re.sub(r'[<>:"|?*\\/]', "_", stem) + ".png"
        return _safe_filename(subpath, "png")

    @app.route("/api/export/json/", defaults={"subpath": ""})
    @app.route("/api/export/json/<path:subpath>")
    def api_export_json(subpath: str) -> Response:
        target = _resolve_target(subpath)
        body = json.dumps(_node_to_dict(target), indent=2, ensure_ascii=False)
        return Response(
            body,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{_safe_filename(subpath, "json")}"'},
        )

    @app.route("/api/export/json_bundle/start/", defaults={"subpath": ""}, methods=["POST"])
    @app.route("/api/export/json_bundle/start/<path:subpath>", methods=["POST"])
    def api_export_json_bundle_start(subpath: str) -> Response:
        target = _resolve_target(unquote(subpath))
        if not isinstance(target, WzDirectory):
            abort(400, "json_bundle requires a directory target")
        job_id = uuid.uuid4().hex
        label = unquote(subpath).strip("/") or "wz_root"
        with _JOBS_LOCK:
            _JOBS[job_id] = {
                "status": "running",
                "progress": 0,
                "total": 0,
                "current": "",
                "label": label,
            }
        t = threading.Thread(
            target=_run_json_bundle_job,
            args=(job_id, target, label, app.config["WZ_READER_LOCK"]),
            daemon=True,
        )
        t.start()
        return jsonify({"job_id": job_id})

    @app.route("/api/export/json_bundle/status/<job_id>")
    def api_export_json_bundle_status(job_id: str) -> Response:
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if not j:
                abort(404)
            # Strip server-only fields before returning to the client.
            return jsonify({k: v for k, v in j.items() if k not in ("file_path",)})

    @app.route("/api/export/json_bundle/cancel/<job_id>", methods=["POST"])
    def api_export_json_bundle_cancel(job_id: str) -> Response:
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if not j:
                abort(404)
            j["cancel"] = True
        return jsonify({"ok": True})

    @app.route("/api/export/json_bundle/download/<job_id>")
    def api_export_json_bundle_download(job_id: str) -> Response:
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if not j or j.get("status") != "done":
                abort(404)
            zip_path = j["file_path"]
            label = j["label"]

        def stream_and_cleanup():
            try:
                with open(zip_path, "rb") as f:
                    while True:
                        chunk = f.read(64 * 1024)
                        if not chunk:
                            break
                        yield chunk
            finally:
                try:
                    os.remove(zip_path)
                except OSError:
                    pass
                with _JOBS_LOCK:
                    _JOBS.pop(job_id, None)

        return Response(
            stream_and_cleanup(),
            mimetype="application/zip",
            headers={
                "Content-Disposition":
                    f'attachment; filename="{_safe_filename(label, "json_bundle.zip")}"',
            },
        )

    @app.route("/api/export/xml/", defaults={"subpath": ""})
    @app.route("/api/export/xml/<path:subpath>")
    def api_export_xml(subpath: str) -> Response:
        target = _resolve_target(subpath)
        body = '<?xml version="1.0" encoding="UTF-8"?>\n' + _node_to_xml(target)
        return Response(
            body,
            mimetype="application/xml",
            headers={"Content-Disposition": f'attachment; filename="{_safe_filename(subpath, "xml")}"'},
        )

    @app.route("/api/export/img/", defaults={"subpath": ""})
    @app.route("/api/export/img/<path:subpath>")
    def api_export_img(subpath: str) -> Response:
        """Raw .img bytes (the on-disk WZ slice for this image, or a ZIP
        of every image under a directory). Useful for round-tripping into
        HaRepacker, which can open a loose .img directly."""
        target = _resolve_target(subpath)

        def _read_img_bytes(img: WzImage) -> bytes:
            r = wz.reader
            with app.config["WZ_READER_LOCK"]:
                keep = r.position
                r.seek(img.offset)
                data = r.read(img.size)
                r.seek(keep)
            return data

        if isinstance(target, WzImage):
            return Response(
                _read_img_bytes(target),
                mimetype="application/octet-stream",
                headers={"Content-Disposition":
                    f'attachment; filename="{target.name}"'},
            )

        if isinstance(target, WzDirectory):
            buf = io.BytesIO()
            # ZIP_STORED — the bytes are XOR-encrypted and won't compress
            # any further; storing skips a CPU-heavy deflate pass.
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
                for rel, img in target.walk_images():
                    zf.writestr(rel, _read_img_bytes(img))
            return Response(
                buf.getvalue(),
                mimetype="application/zip",
                headers={"Content-Disposition":
                    f'attachment; filename="{_safe_filename(subpath, "img.zip")}"'},
            )

        abort(400, "img export only supports image or directory targets")

    @app.route("/api/export/images/", defaults={"subpath": ""})
    @app.route("/api/export/images/<path:subpath>")
    def api_export_images(subpath: str) -> Response:
        target = _resolve_target(subpath)
        layout = request.args.get("layout", "nested")
        if layout not in ("nested", "flat"):
            abort(400, "layout must be 'nested' or 'flat'")
        zip_bytes = _build_image_zip(target, layout=layout, region=app.config["WZ_REGION"])
        if not zip_bytes:
            abort(404, "no decodable images in this subtree")
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition":
                    f'attachment; filename="{_safe_filename(subpath, "images_" + layout + ".zip")}"',
            },
        )

    @app.route("/api/export/sounds/", defaults={"subpath": ""})
    @app.route("/api/export/sounds/<path:subpath>")
    def api_export_sounds(subpath: str) -> Response:
        """Bundle every Sound under ``subpath`` into a ZIP of .mp3
        files. ``layout`` mirrors /api/export/images: ``nested``
        keeps the WZ tree as folder structure, ``flat`` collapses
        path separators to underscores."""
        target = _resolve_target(subpath)
        layout = request.args.get("layout", "nested")
        if layout not in ("nested", "flat"):
            abort(400, "layout must be 'nested' or 'flat'")
        zip_bytes = _build_sound_zip(target, layout=layout)
        if not zip_bytes:
            abort(404, "no sounds in this subtree")
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition":
                    f'attachment; filename="{_safe_filename(subpath, "sounds_" + layout + ".zip")}"',
            },
        )

    # ── save (in-place value patching) ───────────────────────────────
    # In-place strategy: encode the new value, accept the edit only if
    # the resulting bytes have the same length as the original. Anything
    # that would shift downstream offsets (compressed-int crossing the
    # 1↔5 byte boundary, float zero↔non-zero, string length change) is
    # rejected with a clear reason. A real WZ rewriter is out of scope.
    _SAVE_LOCK = threading.Lock()

    def _encode_value_for(prop, new_value):
        """Encode ``new_value`` in the on-wire form for ``prop``'s type.
        Returns ``(bytes, normalized_value)`` or raises ``ValueError``."""
        from wzpy.properties import (
            WzShortProperty, WzIntProperty, WzLongProperty,
            WzFloatProperty, WzDoubleProperty, WzStringProperty,
        )
        from wzpy import writer as _w
        if isinstance(prop, WzShortProperty):
            v = int(new_value)
            if not (-(1 << 15) <= v < (1 << 15)):
                raise ValueError(f"Short out of range: {v}")
            return _w.encode_short(v), v
        if isinstance(prop, WzIntProperty):
            v = int(new_value)
            if not (-(1 << 31) <= v < (1 << 31)):
                raise ValueError(f"Int out of range: {v}")
            return _w.encode_compressed_int(v), v
        if isinstance(prop, WzLongProperty):
            v = int(new_value)
            if not (-(1 << 63) <= v < (1 << 63)):
                raise ValueError(f"Long out of range: {v}")
            return _w.encode_compressed_long(v), v
        if isinstance(prop, WzFloatProperty):
            v = float(new_value)
            return _w.encode_float(v), v
        if isinstance(prop, WzDoubleProperty):
            v = float(new_value)
            return _w.encode_double(v), v
        if isinstance(prop, WzStringProperty):
            s = str(new_value)
            enc = prop._encoding or "ascii"
            cipher = _w.re_encrypt_string(wz.reader, s, enc)
            return cipher, s
        raise ValueError(f"{prop.type_name} is not editable in place")

    def _patch_slot_for(prop):
        """Return ``(offset, length, kind)`` describing where this property
        accepts an in-place patch. ``kind`` is "scalar" or "string"
        depending on which pair the slot came from."""
        from wzpy.properties import WzStringProperty
        if isinstance(prop, WzStringProperty):
            return prop._payload_offset, prop._payload_length, "string"
        return (
            getattr(prop, "_value_offset", None),
            getattr(prop, "_value_length", None),
            "scalar",
        )

    @app.route("/api/save", methods=["POST"])
    def api_save() -> Response:
        """Apply a batch of value edits in place. Body: ``{edits: {path: value, ...}}``.

        Returns one ``{path, status, ...}`` row per edit, plus a top-level
        ``ok`` count. Edits whose new encoded length differs from the
        original are rejected (the WZ would need a full rewrite).
        """
        body = request.get_json(silent=True) or {}
        edits = body.get("edits") or {}
        if not isinstance(edits, dict):
            abort(400, "body.edits must be an object {path: value}")

        results = []
        ok = 0
        with _SAVE_LOCK, app.config["WZ_READER_LOCK"]:
            for path, new_value in edits.items():
                target = _resolve_target(path)
                if not isinstance(target, WzProperty):
                    results.append({"path": path, "status": "error",
                                    "reason": "target is not a property"})
                    continue
                v_off, v_len, kind = _patch_slot_for(target)
                if v_off is None or v_len is None:
                    results.append({"path": path, "status": "error",
                                    "reason": f"{target.type_name} values are not "
                                              f"editable in place"})
                    continue
                try:
                    encoded, normalized = _encode_value_for(target, new_value)
                except (ValueError, TypeError) as e:
                    results.append({"path": path, "status": "error",
                                    "reason": str(e)})
                    continue
                if len(encoded) != v_len:
                    if kind == "string":
                        reason = (f"string would change size from {v_len} to "
                                  f"{len(encoded)} bytes; in-place edit needs "
                                  f"the same encoded length")
                    else:
                        reason = (f"encoded length changed ({v_len} → "
                                  f"{len(encoded)} bytes); in-place edit would "
                                  f"shift downstream offsets")
                    results.append({"path": path, "status": "error",
                                    "reason": reason})
                    continue
                wz.patch_bytes(v_off, encoded)
                target._value = normalized
                row: Dict[str, Any] = {"path": path, "status": "ok",
                                       "value": normalized}
                if kind == "string":
                    # Warn the caller if other properties also reach this
                    # exact payload via offset indirection — they will all
                    # see the new value.
                    shared = wz.reader.shared_count(v_off)
                    # ``shared`` counts indirection sites only. The direct
                    # owner (this property, if inline) doesn't count. So
                    # ``shared >= 1`` always means at least one OTHER
                    # property pointed at the same string.
                    if shared > 0:
                        row["shared_with_count"] = shared
                ok += 1
                results.append(row)
            wz.flush()
        return jsonify({"ok": ok, "total": len(results), "results": results})

    @app.route("/api/canvas/<path:subpath>", methods=["POST"])
    def api_canvas_replace(subpath: str) -> Response:
        """Replace a Canvas property's PNG payload with the uploaded image.

        Form field ``image`` should be any PIL-readable file (PNG, JPG,
        BMP, ...). The server re-encodes it into the canvas's stored
        pixel format, zlib-compresses, optionally re-wraps in listWz to
        match the original payload's framing, and patches the file in
        place. The new compressed payload must fit inside the existing
        on-disk slot — the slot is determined by the extended-property
        block boundary and cannot grow without rewriting the archive.
        Any unused trailing bytes are zero-padded; zlib stops on its own
        EOF marker so trailing junk is harmless on read.
        """
        from wzpy.properties import WzCanvasProperty
        from wzpy.canvas import (
            encode_canvas_payload, _read_canvas_bytes, _ZLIB_HEADERS,
        )
        from wzpy.crypto import WzKey

        # Helper: error responses that survive JSON parsing on the
        # client. Flask's ``abort(400, "...")`` returns an HTML error
        # page which the frontend can't introspect — and that's what
        # was breaking the slot-too-small fallback to /stage.
        def _err(status: int, reason: str, **extra: Any) -> Response:
            payload = {"ok": False, "reason": reason}
            payload.update(extra)
            r = jsonify(payload)
            r.status_code = status
            return r

        if "image" not in request.files:
            return _err(400, "missing 'image' form field")
        upload = request.files["image"]

        node, remaining = _resolve(unquote(subpath))
        if not isinstance(node, WzImage) or not remaining:
            return _err(404, "path is not a Canvas inside an image")
        prop = node.get(remaining)
        if not isinstance(prop, WzCanvasProperty) or not prop.has_pixels():
            return _err(404, "target is not a Canvas with pixels")

        try:
            uploaded_image = Image.open(upload.stream)
            uploaded_image.load()
        except Exception as exc:
            return _err(400, f"cannot decode uploaded image: {exc}")

        # Detect the original encoding form so we can write back in the
        # same shape (avoids breaking listWz-readers that don't try the
        # other path).
        with app.config["WZ_READER_LOCK"]:
            raw = _read_canvas_bytes(prop)
        original_is_listwz = (
            len(raw) >= 2
            and (raw[0] | (raw[1] << 8)) not in _ZLIB_HEADERS
        )

        region = app.config["WZ_REGION"]
        key = WzKey.for_region(region)
        fmt = prop.format + prop.format2
        try:
            new_payload = encode_canvas_payload(
                uploaded_image, fmt, prop.width, prop.height,
                key=key, listwz=original_is_listwz,
            )
        except ValueError as exc:
            return _err(400, str(exc), code="encode_failed")

        slot_total = prop._png_length
        if len(new_payload) > slot_total:
            # The frontend keys off ``code: "slot_too_small"`` to fall
            # back to the staged path automatically.
            return _err(
                400,
                f"compressed payload {len(new_payload)} > slot {slot_total} "
                f"bytes; pick a simpler image, lower zlib level, or accept "
                f"some quality loss",
                code="slot_too_small",
                slot_used=len(new_payload),
                slot_total=slot_total,
            )

        with _SAVE_LOCK, app.config["WZ_READER_LOCK"]:
            wz.patch_bytes(prop._png_offset, new_payload)
            # Zero-pad the unused tail so the next read doesn't see stale
            # bytes interleaved with the new zlib stream.
            pad = slot_total - len(new_payload)
            if pad > 0:
                wz.patch_bytes(prop._png_offset + len(new_payload), b"\x00" * pad)
            # Drop the canvas's cached compressed bytes so subsequent
            # GETs re-read the new payload.
            prop._png_data = None
            wz.flush()

        return jsonify({
            "ok": True,
            "slot_used": len(new_payload),
            "slot_total": slot_total,
            "padded": slot_total - len(new_payload),
            "format": fmt,
            "listwz": original_is_listwz,
        })

    # ── variable-length edits + Save As ──────────────────────────────
    # ``/api/edit`` mutates the in-memory tree without touching the
    # file. Used when the new value's encoded size differs from the
    # original (and the in-place /api/save would reject it). The user
    # then triggers ``/api/save_as`` to flush the modified tree to a
    # fresh WZ on disk.
    #
    # We track which paths have unsaved-on-disk edits in
    # ``app.config["WZ_DIRTY_PATHS"]`` so the UI can warn before
    # navigation. ``WZ_FORCE_FULL_REWRITE`` is set when an edit changes
    # the tree's structure (rename, etc.) — every image needs to be
    # re-emitted because directory entries / property names shift, so
    # the per-image verbatim-copy fast path can't be trusted.
    app.config["WZ_DIRTY_PATHS"] = set()
    app.config["WZ_FORCE_FULL_REWRITE"] = False

    @app.route("/api/edit", methods=["POST"])
    def api_edit() -> Response:
        """Stage variable-length edits to the in-memory tree.

        Body shape matches /api/save: ``{edits: {path: value, ...}}``.
        Returns one row per edit. Unlike /api/save, this never patches
        the file — call ``/api/save_as`` to materialize the changes.
        """
        from wzpy.properties import (
            WzShortProperty, WzIntProperty, WzLongProperty,
            WzFloatProperty, WzDoubleProperty, WzStringProperty,
        )
        body = request.get_json(silent=True) or {}
        edits = body.get("edits") or {}
        if not isinstance(edits, dict):
            abort(400, "body.edits must be an object {path: value}")
        results = []
        ok = 0
        dirty = app.config["WZ_DIRTY_PATHS"]
        with app.config["WZ_READER_LOCK"]:
            for path, new_value in edits.items():
                target = _resolve_target(path)
                if not isinstance(target, WzProperty):
                    results.append({"path": path, "status": "error",
                                    "reason": "target is not a property"})
                    continue
                try:
                    if isinstance(target, (WzShortProperty, WzIntProperty,
                                            WzLongProperty)):
                        target._value = int(new_value)
                    elif isinstance(target, (WzFloatProperty, WzDoubleProperty)):
                        target._value = float(new_value)
                    elif isinstance(target, WzStringProperty):
                        target._value = str(new_value)
                    else:
                        results.append({"path": path, "status": "error",
                                        "reason": f"{target.type_name} is not "
                                                  f"editable via /api/edit"})
                        continue
                except (ValueError, TypeError) as exc:
                    results.append({"path": path, "status": "error",
                                    "reason": str(exc)})
                    continue
                dirty.add(path)
                results.append({"path": path, "status": "ok",
                                "value": target._value})
                ok += 1
        return jsonify({"ok": ok, "total": len(results), "results": results,
                        "dirty_count": len(dirty)})

    @app.route("/api/canvas/<path:subpath>/stage", methods=["POST"])
    def api_canvas_stage(subpath: str) -> Response:
        """Variable-size canvas replacement: re-encode the upload but
        skip the slot-fit check, store the new compressed bytes on the
        canvas property in memory, mark the path dirty. The next
        ``/api/save_as`` will pick it up.

        Same form field as /api/canvas: ``image``.
        """
        from wzpy.properties import WzCanvasProperty
        from wzpy.canvas import encode_canvas_payload, _read_canvas_bytes, _ZLIB_HEADERS
        from wzpy.crypto import WzKey

        def _err(status: int, reason: str, **extra: Any) -> Response:
            payload = {"ok": False, "reason": reason}
            payload.update(extra)
            r = jsonify(payload)
            r.status_code = status
            return r

        if "image" not in request.files:
            return _err(400, "missing 'image' form field")
        upload = request.files["image"]
        node, remaining = _resolve(unquote(subpath))
        if not isinstance(node, WzImage) or not remaining:
            return _err(404, "path is not a Canvas inside an image")
        prop = node.get(remaining)
        if not isinstance(prop, WzCanvasProperty):
            return _err(404, "target is not a Canvas")

        try:
            uploaded_image = Image.open(upload.stream)
            uploaded_image.load()
        except Exception as exc:
            return _err(400, f"cannot decode uploaded image: {exc}")

        # Match the original framing on write so other readers don't
        # need the listWz fallback.
        with app.config["WZ_READER_LOCK"]:
            raw = _read_canvas_bytes(prop) if prop.has_pixels() else b""
        original_is_listwz = (
            len(raw) >= 2
            and (raw[0] | (raw[1] << 8)) not in _ZLIB_HEADERS
        )

        # Allow resizing the canvas — width/height come from the upload
        # if the user wants a different size.
        new_w = int(request.form.get("width", uploaded_image.width))
        new_h = int(request.form.get("height", uploaded_image.height))
        # Format is preserved (encoder requires a known pixel format,
        # and the existing one is the natural choice).
        fmt = prop.format + prop.format2
        try:
            new_payload = encode_canvas_payload(
                uploaded_image, fmt, new_w, new_h,
                key=WzKey.for_region(app.config["WZ_REGION"]),
                listwz=original_is_listwz,
            )
        except ValueError as exc:
            return _err(400, str(exc), code="encode_failed")

        with app.config["WZ_READER_LOCK"]:
            prop.width = new_w
            prop.height = new_h
            prop._png_data = new_payload
            prop._png_length = len(new_payload)
            app.config["WZ_DIRTY_PATHS"].add(subpath)

        return jsonify({
            "ok": True,
            "staged": True,
            "width": new_w,
            "height": new_h,
            "payload_bytes": len(new_payload),
            "listwz": original_is_listwz,
            "dirty_count": len(app.config["WZ_DIRTY_PATHS"]),
        })

    @app.route("/api/rename", methods=["POST"])
    def api_rename() -> Response:
        """Rename a node (property, image, or sub-directory).

        Body: ``{path: "...", new_name: "..."}``. The change is staged
        in memory only; the next ``/api/save_as`` flushes it. (A WZ
        rename almost always shifts byte offsets — both the name's own
        bytes inside the directory entry / string-block AND every
        downstream encrypted-offset in the parent directory — so an
        in-place patch is rarely possible.)

        Returns ``{ok, old_path, new_path, kind, dirty_count}``.
        """
        from wzpy.properties import WzSubProperty
        from wzpy.wz_image import WzImage as _WzImage
        from wzpy.wz_file import WzDirectory as _WzDirectory

        def _err(status: int, reason: str) -> Response:
            r = jsonify({"ok": False, "reason": reason})
            r.status_code = status
            return r

        body = request.get_json(silent=True) or {}
        path = (body.get("path") or "").strip("/")
        new_name = body.get("new_name")
        if not path:
            return _err(400, "cannot rename the WZ root")
        if not isinstance(new_name, str) or not new_name:
            return _err(400, "new_name is required and must be a non-empty string")
        if "/" in new_name or "\\" in new_name:
            return _err(400, "new_name must not contain path separators")

        from werkzeug.exceptions import HTTPException as _HTTPException
        with app.config["WZ_READER_LOCK"]:
            try:
                target = _resolve_target(path)
            except _HTTPException as exc:
                return _err(exc.code or 404, exc.description or "not found")
            if target is wz.root:
                return _err(400, "cannot rename the WZ root")
            old_name = getattr(target, "name", None)
            if old_name is None:
                return _err(400, f"target has no name to rename")
            if old_name == new_name:
                return jsonify({
                    "ok": True, "no_op": True,
                    "old_path": path, "new_path": path,
                })

            parent = getattr(target, "parent", None)
            if parent is None:
                return _err(400, "target has no parent (cannot reseat in dict)")

            # Collision check + dict reseat. The parent stores children
            # by name in one of three dicts depending on its kind.
            if isinstance(parent, _WzDirectory):
                if isinstance(target, _WzDirectory):
                    bucket = parent.subdirs
                    kind = "directory"
                elif isinstance(target, _WzImage):
                    bucket = parent.images
                    kind = "image"
                else:
                    return _err(400,
                        "child of a directory must be either a sub-directory or an image")
            elif isinstance(parent, WzSubProperty):
                bucket = parent._children
                kind = "property"
            else:
                return _err(400, f"unsupported parent type: {type(parent).__name__}")

            if new_name in bucket and bucket[new_name] is not target:
                return _err(409, f"{new_name!r} already exists under the same parent")

            # Reseat: remove old key, insert new key. We keep the
            # rest of the dict order untouched by rebuilding (Python
            # 3.7+ dicts preserve insertion order, so this matters
            # for the directory listing the user sees).
            new_bucket = {}
            for k, v in bucket.items():
                if k == old_name:
                    new_bucket[new_name] = target
                else:
                    new_bucket[k] = v
            if isinstance(parent, _WzDirectory):
                if kind == "directory":
                    parent.subdirs = new_bucket
                else:
                    parent.images = new_bucket
            else:
                parent._children = new_bucket
            target.name = new_name

            # Compute new path. Rename is structural — it shifts every
            # downstream encrypted offset — so we must abandon the per-
            # image verbatim-copy fast path on the next save_as.
            parts = path.split("/")
            parts[-1] = new_name
            new_path = "/".join(parts)
            app.config["WZ_FORCE_FULL_REWRITE"] = True
            app.config["WZ_DIRTY_PATHS"].add(new_path)

        return jsonify({
            "ok": True,
            "old_path": path,
            "new_path": new_path,
            "kind": kind,
            "dirty_count": len(app.config["WZ_DIRTY_PATHS"]),
        })

    @app.route("/api/remove", methods=["POST"])
    def api_remove() -> Response:
        """Remove a node (property, image, or sub-directory) from the
        in-memory tree. Body: ``{path: "..."}``.

        Like ``/api/rename``, the change is staged in memory and the
        next ``/api/save_as`` materializes it. Removing a node shifts
        every downstream encrypted offset, so we set
        ``WZ_FORCE_FULL_REWRITE`` to make save_as re-emit every image
        from the parsed tree (no verbatim-copy fast path).

        Returns ``{ok, removed_path, parent_path, kind, dirty_count}``.
        """
        from wzpy.properties import WzSubProperty
        from wzpy.wz_image import WzImage as _WzImage
        from wzpy.wz_file import WzDirectory as _WzDirectory

        def _err(status: int, reason: str) -> Response:
            r = jsonify({"ok": False, "reason": reason})
            r.status_code = status
            return r

        body = request.get_json(silent=True) or {}
        path = (body.get("path") or "").strip("/")
        if not path:
            return _err(400, "cannot remove the WZ root")

        from werkzeug.exceptions import HTTPException as _HTTPException
        with app.config["WZ_READER_LOCK"]:
            try:
                target = _resolve_target(path)
            except _HTTPException as exc:
                return _err(exc.code or 404, exc.description or "not found")
            if target is wz.root:
                return _err(400, "cannot remove the WZ root")
            old_name = getattr(target, "name", None)
            if old_name is None:
                return _err(400, "target has no name")

            parent = getattr(target, "parent", None)
            if parent is None:
                return _err(400, "target has no parent")

            if isinstance(parent, _WzDirectory):
                if isinstance(target, _WzDirectory):
                    bucket = parent.subdirs
                    kind = "directory"
                elif isinstance(target, _WzImage):
                    bucket = parent.images
                    kind = "image"
                else:
                    return _err(400,
                        "child of a directory must be a sub-directory or an image")
            elif isinstance(parent, WzSubProperty):
                bucket = parent._children
                kind = "property"
            else:
                return _err(400, f"unsupported parent type: {type(parent).__name__}")

            if old_name not in bucket or bucket[old_name] is not target:
                return _err(404,
                    f"target {old_name!r} not found under its parent's child dict")

            del bucket[old_name]

            parent_path = "/".join(path.split("/")[:-1])
            app.config["WZ_FORCE_FULL_REWRITE"] = True
            # Drop any staged dirty paths under the removed subtree —
            # they no longer exist. Then mark the parent as dirty so the
            # Save As badge updates.
            prefix = path + "/"
            kept = {
                p for p in app.config["WZ_DIRTY_PATHS"]
                if p != path and not p.startswith(prefix)
            }
            kept.add(parent_path or "<root>")
            app.config["WZ_DIRTY_PATHS"] = kept

        return jsonify({
            "ok": True,
            "removed_path": path,
            "parent_path": parent_path,
            "kind": kind,
            "dirty_count": len(app.config["WZ_DIRTY_PATHS"]),
        })

    @app.route("/api/add", methods=["POST"])
    def api_add() -> Response:
        """Add a new property under ``parent_path``.

        Body: ``{parent_path, name, kind, ...}`` where ``kind`` is
        one of the supported simple types (Null, Short, Int, Long,
        Float, Double, String, Vector, SubProperty). Extra fields:
          - scalar types: ``value``
          - Vector: ``x``, ``y``
          - Null / SubProperty: nothing more

        Canvas / Sound / UOL / Convex are intentionally not supported
        in v1 — those need richer construction (image upload, audio
        upload, target lookup, etc.). Use Save As + Replace... for
        canvases and write tooling for sounds.

        Returns ``{ok, new_path, kind, dirty_count}``.
        """
        from werkzeug.exceptions import HTTPException as _HTTPException
        from wzpy.properties import (
            WzDoubleProperty, WzFloatProperty, WzIntProperty,
            WzLongProperty, WzNullProperty, WzShortProperty,
            WzStringProperty, WzSubProperty, WzVectorProperty,
        )
        from wzpy.wz_image import WzImage as _WzImage
        from wzpy.wz_file import WzDirectory as _WzDirectory

        def _err(status: int, reason: str) -> Response:
            r = jsonify({"ok": False, "reason": reason})
            r.status_code = status
            return r

        body = request.get_json(silent=True) or {}
        parent_path = (body.get("parent_path") or "").strip("/")
        name = body.get("name")
        kind = body.get("kind")

        if not isinstance(name, str) or not name:
            return _err(400, "name is required and must be a non-empty string")
        if "/" in name or "\\" in name:
            return _err(400, "name must not contain path separators")
        if not isinstance(kind, str) or not kind:
            return _err(400, "kind is required")

        with app.config["WZ_READER_LOCK"]:
            try:
                # Empty parent_path means "add at the WZ root". Only
                # Directory / Image kinds make sense there.
                parent = _resolve_target(parent_path) if parent_path else wz.root
            except _HTTPException as exc:
                return _err(exc.code or 404, exc.description or "not found")

            new_path = f"{parent_path}/{name}" if parent_path else name

            # Directory + Image: parent must be a WzDirectory (root or
            # sub). They go into ``parent.subdirs`` / ``parent.images``
            # respectively, not into a SubProperty's child dict.
            if kind in ("Directory", "Image"):
                if not isinstance(parent, _WzDirectory):
                    return _err(400,
                        f"{kind} can only be added under a Directory")
                if name in parent.subdirs or name in parent.images:
                    return _err(409,
                        f"{name!r} already exists under the same directory")
                if kind == "Directory":
                    new_dir = _WzDirectory(name, parent=parent)
                    parent.subdirs[name] = new_dir
                else:
                    # Empty image — just an empty SubProperty root, no
                    # source bytes. ``offset/size = 0`` is fine because
                    # ``_parsed = True`` makes ``parse()`` return the
                    # in-memory root without ever seeking the file.
                    new_img = _WzImage(name, parent=parent, offset=0,
                                        size=0, wz_file=wz)
                    new_img._parsed = True
                    new_img._root = WzSubProperty(name)
                    parent.images[name] = new_img
                app.config["WZ_FORCE_FULL_REWRITE"] = True
                app.config["WZ_DIRTY_PATHS"].add(new_path)
                return jsonify({
                    "ok": True, "new_path": new_path,
                    "parent_path": parent_path, "kind": kind,
                    "dirty_count": len(app.config["WZ_DIRTY_PATHS"]),
                })

            # Property kinds: parent must be Image / SubProperty
            # (Image: descend into its root SubProperty).
            if isinstance(parent, _WzImage):
                parent.parse()
                container = parent.root
            elif isinstance(parent, WzSubProperty):
                container = parent
            else:
                return _err(
                    400,
                    f"cannot add a {kind} child to a "
                    f"{type(parent).__name__} — pick an image, sub-"
                    f"property, or canvas as the parent (or pick a "
                    f"Directory/Image kind for a Directory parent)",
                )

            if name in container._children:
                return _err(409, f"{name!r} already exists under the same parent")

            # Construct the new property based on the requested kind.
            try:
                prop = _construct_property(kind, name, body, container)
            except (ValueError, TypeError) as exc:
                return _err(400, str(exc))
            container.add(prop)

            app.config["WZ_FORCE_FULL_REWRITE"] = True
            app.config["WZ_DIRTY_PATHS"].add(new_path)

        return jsonify({
            "ok": True,
            "new_path": new_path,
            "parent_path": parent_path,
            "kind": kind,
            "dirty_count": len(app.config["WZ_DIRTY_PATHS"]),
        })

    # ── richer add: Canvas (PNG upload) and Sound (MP3 upload) ───────
    # These can't go through /api/add because they need a multipart
    # body. Same dirty-flag bookkeeping as the simple types.

    def _resolve_add_container(parent_path: str, name: str):
        """Returns ``(container, new_path)`` or aborts with a JSON
        error. Mirrors the validation in /api/add for the
        multipart variants."""
        from werkzeug.exceptions import HTTPException as _HTTPException
        from wzpy.properties import WzSubProperty
        from wzpy.wz_image import WzImage as _WzImage

        if "/" in name or "\\" in name:
            raise ValueError("name must not contain path separators")
        try:
            parent = _resolve_target(parent_path) if parent_path else None
        except _HTTPException as exc:
            raise LookupError(exc.description or "not found")
        if parent is None:
            raise LookupError("cannot add directly to the WZ root; pick an .img")
        if isinstance(parent, _WzImage):
            parent.parse()
            container = parent.root
        elif isinstance(parent, WzSubProperty):
            container = parent
        else:
            raise ValueError(
                f"cannot add a child to a {type(parent).__name__}")
        if name in container._children:
            raise FileExistsError(f"{name!r} already exists under the same parent")
        return container, (f"{parent_path}/{name}" if parent_path else name)

    @app.route("/api/add/canvas", methods=["POST"])
    def api_add_canvas() -> Response:
        """Add a new Canvas property from a PNG upload.

        Multipart body: ``image`` (PNG file) plus form fields
        ``parent_path`` and ``name``. Optional ``format`` defaults
        to 2 (BGRA8888) which is lossless and supported on every
        MapleStory client we know about.
        """
        from wzpy.canvas import encode_canvas_payload
        from wzpy.crypto import WzKey
        from wzpy.properties import WzCanvasProperty

        def _err(status: int, reason: str) -> Response:
            r = jsonify({"ok": False, "reason": reason})
            r.status_code = status
            return r

        if "image" not in request.files:
            return _err(400, "missing 'image' form field")
        upload = request.files["image"]
        # Server-side filetype enforcement. Frontend already restricts
        # via accept="image/png" but a malicious client could lie.
        head = upload.stream.read(8); upload.stream.seek(0)
        if head != b"\x89PNG\r\n\x1a\n":
            return _err(400, "uploaded file is not a PNG (magic mismatch)")

        parent_path = (request.form.get("parent_path") or "").strip("/")
        name = request.form.get("name") or ""
        if not name:
            return _err(400, "name form field required")

        try:
            uploaded_image = Image.open(upload.stream)
            uploaded_image.load()
        except Exception as exc:
            return _err(400, f"cannot decode PNG: {exc}")

        # BGRA8888 is the natural default — lossless, no quantization.
        try:
            fmt = int(request.form.get("format", 2))
        except ValueError:
            return _err(400, "format must be an integer")

        with app.config["WZ_READER_LOCK"]:
            try:
                container, new_path = _resolve_add_container(parent_path, name)
            except LookupError as exc:
                return _err(404, str(exc))
            except FileExistsError as exc:
                return _err(409, str(exc))
            except ValueError as exc:
                return _err(400, str(exc))

            try:
                payload = encode_canvas_payload(
                    uploaded_image, fmt,
                    uploaded_image.width, uploaded_image.height,
                    key=WzKey.for_region(app.config["WZ_REGION"]),
                    listwz=False,
                )
            except ValueError as exc:
                return _err(400, str(exc))

            canvas = WzCanvasProperty(name, parent=container)
            canvas.width = uploaded_image.width
            canvas.height = uploaded_image.height
            canvas.format = fmt
            canvas.format2 = 0
            canvas._png_data = payload
            canvas._png_length = len(payload)
            container.add(canvas)

            app.config["WZ_FORCE_FULL_REWRITE"] = True
            app.config["WZ_DIRTY_PATHS"].add(new_path)

        return jsonify({
            "ok": True, "new_path": new_path, "parent_path": parent_path,
            "kind": "Canvas",
            "width": uploaded_image.width, "height": uploaded_image.height,
            "format": fmt, "payload_bytes": len(payload),
            "dirty_count": len(app.config["WZ_DIRTY_PATHS"]),
        })

    @app.route("/api/add/sound", methods=["POST"])
    def api_add_sound() -> Response:
        """Add a new Sound property from an MP3 upload.

        Multipart body: ``audio`` (MP3 file) plus form fields
        ``parent_path`` and ``name``. Duration (``length_ms``) is
        estimated by walking MP3 frame headers.
        """
        from wzpy.properties import WzSoundProperty

        def _err(status: int, reason: str) -> Response:
            r = jsonify({"ok": False, "reason": reason})
            r.status_code = status
            return r

        if "audio" not in request.files:
            return _err(400, "missing 'audio' form field")
        upload = request.files["audio"]
        audio_bytes = upload.stream.read()
        if not _is_mp3_bytes(audio_bytes):
            return _err(400, "uploaded file is not an MP3 (no MPEG sync / ID3 header)")

        parent_path = (request.form.get("parent_path") or "").strip("/")
        name = request.form.get("name") or ""
        if not name:
            return _err(400, "name form field required")

        with app.config["WZ_READER_LOCK"]:
            try:
                container, new_path = _resolve_add_container(parent_path, name)
            except LookupError as exc:
                return _err(404, str(exc))
            except FileExistsError as exc:
                return _err(409, str(exc))
            except ValueError as exc:
                return _err(400, str(exc))

            length_ms = _estimate_mp3_duration_ms(audio_bytes)
            sound = WzSoundProperty(name, parent=container)
            sound.length_ms = length_ms
            sound.header = _default_mp3_header()
            sound._data_length = len(audio_bytes)
            sound._data = audio_bytes
            container.add(sound)

            app.config["WZ_FORCE_FULL_REWRITE"] = True
            app.config["WZ_DIRTY_PATHS"].add(new_path)

        return jsonify({
            "ok": True, "new_path": new_path, "parent_path": parent_path,
            "kind": "Sound",
            "length_ms": length_ms, "audio_bytes": len(audio_bytes),
            "dirty_count": len(app.config["WZ_DIRTY_PATHS"]),
        })

    @app.route("/api/save_as", methods=["POST"])
    def api_save_as() -> Response:
        """Re-serialize the entire archive (including all in-memory
        edits) to a new file. Body: ``{path: "..."}``.

        Returns ``{ok, path, bytes, dirty_cleared}``. After success,
        ``WZ_DIRTY_PATHS`` is reset.
        """
        import os as _os
        body = request.get_json(silent=True) or {}
        out_path = body.get("path")
        if not out_path or not isinstance(out_path, str):
            abort(400, "body.path is required (target output file)")
        # Guard: no overwrite-original unless the request explicitly
        # opts in. Saving over the live mmap from this process would
        # corrupt the open file.
        try:
            same = _os.path.samefile(out_path, wz.path)
        except FileNotFoundError:
            same = False
        if same and not body.get("overwrite_original"):
            abort(400, "refusing to overwrite the open WZ file; pass "
                       "overwrite_original=true to confirm")

        with _SAVE_LOCK, app.config["WZ_READER_LOCK"]:
            try:
                # Pass the dirty-path set so unedited images get the
                # fast (and bug-resistant) verbatim-copy path —
                # UNLESS something structural (a rename) requires the
                # whole archive to be re-emitted from the parsed tree.
                image_failures: List[str] = []
                effective_dirty = (
                    None  # forces full re-serialize in WzFile.save_as
                    if app.config.get("WZ_FORCE_FULL_REWRITE")
                    else app.config["WZ_DIRTY_PATHS"]
                )
                size = wz.save_as(
                    out_path,
                    dirty_paths=effective_dirty,
                    image_failures=image_failures,
                )
            except Exception as exc:
                # Log the full traceback to the server stderr so the user
                # can see exactly what failed; surface a JSON error with
                # the message + abbreviated traceback so the browser
                # console + Network tab show something useful.
                import traceback as _tb
                tb = _tb.format_exc()
                print("[save_as] FAILED:", file=sys.stderr)
                print(tb, file=sys.stderr, flush=True)
                # Last 3 frames of the traceback are the most useful here.
                short = "\n".join(tb.splitlines()[-12:])
                resp = jsonify({
                    "ok": False,
                    "error": str(exc),
                    "type": type(exc).__name__,
                    "trace": short,
                })
                resp.status_code = 500
                return resp
            cleared = len(app.config["WZ_DIRTY_PATHS"])
            app.config["WZ_DIRTY_PATHS"].clear()
            app.config["WZ_FORCE_FULL_REWRITE"] = False

        return jsonify({
            "ok": True,
            "path": out_path,
            "bytes": size,
            "dirty_cleared": cleared,
            "image_failures": image_failures,
        })

    @app.route("/api/dirty", methods=["GET"])
    def api_dirty() -> Response:
        """Tell the UI how many staged variable-length edits are pending."""
        return jsonify({
            "count": len(app.config["WZ_DIRTY_PATHS"]),
            "paths": sorted(app.config["WZ_DIRTY_PATHS"]),
        })

    return app


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Browse a MapleStory .wz file in your browser")
    parser.add_argument("wz", help="path to the .wz file")
    parser.add_argument("--region", default="auto",
                        choices=["auto", "GMS", "EMS", "BMS"],
                        help="MapleStory region (default: auto — pick the "
                             "one that decodes the root directory cleanly)")
    parser.add_argument("--version", type=int, default=None,
                        help="MapleStory patch version (skip auto-detection)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    app = create_app(args.wz, region=args.region, version=args.version)
    print(f"\n  -> open http://{args.host}:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
