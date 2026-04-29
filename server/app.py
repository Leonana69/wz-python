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
    WzProperty,
    WzSoundProperty,
    WzSubProperty,
    WzUolProperty,
    WzVectorProperty,
)
from wzpy.canvas import decode_canvas
from wzpy.wz_file import WzDirectory


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
    files even on multi-GB WZs, so doing it three times is fine."""
    best: Optional[Tuple[str, float]] = None
    for r in ("BMS", "GMS", "EMS"):
        try:
            wz = WzFile.open(wz_path, region=r, version=version)
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


def create_app(wz_path: str, region: str = "auto", version: Optional[int] = None) -> Flask:
    if region == "auto":
        print(f"auto-detecting region for {wz_path}:")
        region = _auto_detect_region(wz_path, version)
        print(f"  -> using region: {region}")
    app = Flask(__name__, template_folder="templates", static_folder="static")
    # writable=True mmaps the WZ with ACCESS_WRITE so /api/save can patch
    # scalar values in-place without copying the entire archive.
    wz = WzFile.open(wz_path, region=region, version=version, writable=True)
    app.config["WZ"] = wz
    app.config["WZ_REGION"] = region
    # Background bundle exports parse images on a worker thread; the WZ reader
    # carries shared file-position + cipher state, so we mediate access with
    # this lock. Currently only the bundle worker acquires it.
    app.config["WZ_READER_LOCK"] = threading.Lock()

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

    def _children_of(node: Any) -> List[Dict[str, Any]]:
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
                out.append(_describe_property(c))
            return out
        return out

    def _describe_property(p: WzProperty) -> Dict[str, Any]:
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
        return d

    # ── routes ───────────────────────────────────────────────────────
    @app.route("/")
    def index() -> str:
        return render_template(
            "index.html",
            wz_name=wz_path,
            wz_version=wz.version,
            wz_region=region,
        )

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
            children = _children_of(prop)
            kind = prop.type_name
        elif isinstance(node, WzImage):
            node.parse()
            children = _children_of(node)
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
        if isinstance(node, WzImage):
            if remaining:
                target = node.get(remaining)
            else:
                target = node.root
        elif remaining:
            abort(404)
        if target is None:
            abort(404)
        return jsonify(_describe_property(target) if isinstance(target, WzProperty) else {
            "name": getattr(target, "name", ""),
            "kind": "Directory",
        })

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
        if not isinstance(prop, WzCanvasProperty) or not prop.has_pixels():
            abort(404)

        region = app.config["WZ_REGION"]
        key = WzKey.for_region(region)
        fmt = prop.format + prop.format2

        t_read = t_decompress = t_decode = t_encode = 0.0
        raw_bytes = decompressed_bytes = png_bytes = 0
        try:
            t0 = time.perf_counter()
            raw = _read_canvas_bytes(prop)
            raw_bytes = len(raw)
            t_read = time.perf_counter() - t0

            t0 = time.perf_counter()
            decompressed = _decompress(prop, key)
            decompressed_bytes = len(decompressed)
            t_decompress = time.perf_counter() - t0

            t0 = time.perf_counter()
            img = _decode_pixels(decompressed, prop.width, prop.height, fmt)
            t_decode = time.perf_counter() - t0
        except Exception as exc:
            try:
                raw = _read_canvas_bytes(prop)
            except Exception:
                raw = b""
            outlink = prop.child("_outlink")
            inlink = prop.child("_inlink")
            lines = [
                f"decode error: {exc}",
                f"format={prop.format + prop.format2}  size={prop.width}x{prop.height}",
                f"data {len(raw)} bytes; first 16: {raw[:16].hex()}",
            ]
            if outlink is not None:
                lines.append(f"_outlink → {outlink.value}")
                lines.append("(actual pixels live in a sibling _Canvas WZ file)")
            if inlink is not None:
                lines.append(f"_inlink → {inlink.value}")
            placeholder = Image.new(
                "RGBA",
                (max(prop.width, 560), max(prop.height, 16 * (len(lines) + 1))),
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
                f"{prop.width}x{prop.height} fmt={fmt}  raw={raw_bytes}b  "
                f"reason={exc}  /{subpath}",
                flush=True,
            )
            return Response(buf.getvalue(), mimetype="image/png", status=200)

        t0 = time.perf_counter()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.tell()
        t_encode = time.perf_counter() - t0

        elapsed_ms = (time.perf_counter() - t_total) * 1000
        # Always log canvases that take meaningful time so the user can
        # see when something is heavy without grepping every request.
        if elapsed_ms > 30 or png_bytes > 200_000:
            print(
                f"  [canvas {elapsed_ms:6.1f} ms] "
                f"{prop.width}x{prop.height} fmt={fmt}  "
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
        resp.headers["X-Canvas-Size"] = f"{prop.width}x{prop.height}"
        return resp

    @app.route("/api/sound/<path:subpath>")
    def api_sound(subpath: str) -> Response:
        node, remaining = _resolve(unquote(subpath))
        if not isinstance(node, WzImage) or not remaining:
            abort(404)
        prop = node.get(remaining)
        if not isinstance(prop, WzSoundProperty):
            abort(404)
        r = wz.reader
        keep = r.position
        r.seek(prop._data_offset)
        data = r.read(prop._data_length)
        r.seek(keep)
        return Response(data, mimetype="audio/mpeg")

    @app.route("/api/animation/<path:subpath>")
    def api_animation(subpath: str) -> Response:
        """Gather animation frames for a SubProperty whose children are
        numbered Canvases (0, 1, 2, ...). Each WZ frame typically has
        a ``delay`` Int (ms) and an ``origin`` Vector (anchor point)
        as siblings of the bitmap; we fold those in so the client can
        play the sequence at the right cadence and align frames to a
        common anchor.

        Response: ``{path, frame_count, frames: [{index, name, url,
        width, height, delay_ms, origin: {x, y}}]}``. ``404`` if the
        target isn't a SubProperty with at least one numeric-named
        Canvas child.
        """
        from wzpy.properties import (
            WzCanvasProperty, WzIntProperty, WzShortProperty,
            WzSubProperty, WzVectorProperty,
        )
        from urllib.parse import quote as _quote

        target = _resolve_target(subpath)
        if not isinstance(target, WzSubProperty):
            abort(404, "target is not a SubProperty")

        frames: List[Dict[str, Any]] = []
        for child in target.children():
            try:
                idx = int(child.name)
            except (TypeError, ValueError):
                continue
            if not isinstance(child, WzCanvasProperty) or not child.has_pixels():
                continue
            delay_ms = 100  # MapleStory's typical default when no delay is set
            origin = {"x": 0, "y": 0}
            for sub in child.children():
                if sub.name == "delay" and isinstance(sub, (WzIntProperty, WzShortProperty)):
                    try:
                        delay_ms = int(sub.value)
                    except Exception:
                        pass
                elif sub.name == "origin" and isinstance(sub, WzVectorProperty):
                    origin = {"x": int(sub.x), "y": int(sub.y)}
            frames.append({
                "index": idx,
                "name": child.name,
                "url": f"/api/canvas/{_quote(subpath, safe='/')}/{_quote(child.name)}.png",
                "width": child.width,
                "height": child.height,
                "delay_ms": max(1, delay_ms),
                "origin": origin,
            })

        if not frames:
            abort(404, "no numeric-named Canvas children")
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
    # navigation.
    app.config["WZ_DIRTY_PATHS"] = set()

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
                # fast (and bug-resistant) verbatim-copy path.
                image_failures: List[str] = []
                size = wz.save_as(
                    out_path,
                    dirty_paths=app.config["WZ_DIRTY_PATHS"],
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
