"""Flask web UI for browsing a WZ file.

Routes:
  /                   - tree browser shell (HTML)
  /api/tree/<path>    - JSON listing of a directory or .img subtree
  /api/property/<p>   - JSON value for a leaf property
  /api/canvas/<p>.png - rendered PNG bytes for a Canvas property
  /api/sound/<p>      - raw audio bytes for a Sound property
"""

from __future__ import annotations

import io
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote


_NUMBER_RE = re.compile(r"(\d+)")


def _natural_key(s: str):
    """Sort key that orders ``"2.img"`` before ``"10.img"`` and ``"0"`` before ``"10"``."""
    return [int(p) if p.isdigit() else p.lower() for p in _NUMBER_RE.split(s)]

from flask import Flask, Response, abort, jsonify, render_template, request
from PIL import Image, ImageDraw

from wzpy import (
    WzCanvasProperty,
    WzFile,
    WzImage,
    WzNullProperty,
    WzProperty,
    WzSoundProperty,
    WzSubProperty,
    WzVectorProperty,
)
from wzpy.canvas import decode_canvas
from wzpy.wz_file import WzDirectory


def create_app(wz_path: str, region: str = "GMS", version: Optional[int] = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    wz = WzFile.open(wz_path, region=region, version=version)
    app.config["WZ"] = wz
    app.config["WZ_REGION"] = region

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
        resp = jsonify({"path": subpath, "kind": kind, "children": children})
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
        node, remaining = _resolve(unquote(subpath))
        if not isinstance(node, WzImage) or not remaining:
            abort(404)
        prop = node.get(remaining)
        if not isinstance(prop, WzCanvasProperty) or not prop.has_pixels():
            abort(404)
        try:
            img = decode_canvas(prop, region=app.config["WZ_REGION"])
        except Exception as exc:
            from wzpy.canvas import _read_canvas_bytes
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
            return Response(buf.getvalue(), mimetype="image/png", status=200)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(buf.getvalue(), mimetype="image/png")

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

    return app


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Browse a MapleStory .wz file in your browser")
    parser.add_argument("wz", help="path to the .wz file")
    parser.add_argument("--region", default="GMS", choices=["GMS", "EMS", "BMS"],
                        help="MapleStory region (default: GMS)")
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
