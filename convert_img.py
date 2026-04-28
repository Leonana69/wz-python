"""Convert WZ image data to JSON.

Two input modes:

* ``foo.wz`` — open the archive and write one ``<entry>.img.json`` per
  image (use ``--entry`` to pick a single one).
* ``foo.img`` — a standalone image extracted with another tool. The
  cipher key is picked via ``--region`` / ``--auto-region`` /
  ``--iv`` / ``--keystream`` / ``--auto-derive``.

Examples:

    python convert_img.py Map4_000.wz
    python convert_img.py Map4_000.wz --entry 400000000.img
    python convert_img.py 400000000.img --region GMS
    python convert_img.py exotic.img --auto-derive
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Make ``wzpy`` importable when the script is run from anywhere.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from wzpy import (  # noqa: E402
    StaticWzKey,
    WzFile,
    WzImage,
    WzKey,
    derive_keystream_from_property,
    detect_region_from_img,
)
from wzpy.crypto import WZ_IV  # noqa: E402
from wzpy.json_export import node_to_dict  # noqa: E402


def _resolve_key(data: bytes, args) -> "tuple[WzKey, str]":
    """Pick a cipher key from CLI args. Priority: ``--keystream`` >
    ``--auto-derive`` > ``--iv`` > ``--auto-region`` > ``--region``."""
    if args.keystream is not None:
        ks = bytes.fromhex(args.keystream)
        if not ks:
            raise SystemExit("--keystream requires at least one byte")
        return StaticWzKey(ks), f"static keystream ({len(ks)} bytes)"

    if args.auto_derive:
        try:
            ks = derive_keystream_from_property(data)
        except ValueError as e:
            raise SystemExit(str(e))
        return StaticWzKey(ks), f"auto-derived 8-byte keystream {ks.hex()}"

    if args.iv is not None:
        iv_bytes = bytes.fromhex(args.iv)
        if len(iv_bytes) != 4:
            raise SystemExit(f"--iv must be 4 hex bytes (got {len(iv_bytes)})")
        return WzKey(iv_bytes), f"custom IV {iv_bytes.hex()}"

    if args.auto_region:
        region = detect_region_from_img(data)
        if region is None:
            raise SystemExit(
                "could not auto-detect region — none of "
                f"{list(WZ_IV.keys())} decoded the header. "
                "Try --iv, --auto-derive, or --keystream."
            )
        return WzKey.for_region(region), region

    return WzKey.for_region(args.region), args.region


def _dump(img: WzImage, out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    img.parse()
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(node_to_dict(img), f, indent=2, ensure_ascii=False)
    if img.truncated:
        print(f"  WARNING: {img.name} appears truncated; emitted partial JSON",
              file=sys.stderr)


def _ensure_json_suffix(path: str) -> str:
    return path if path.endswith(".json") else f"{path}.json"


def _convert_wz(args) -> int:
    wz = WzFile.open(args.path, region=args.region, version=args.version)

    if args.entry is not None:
        node = wz.root.get(args.entry)
        if not isinstance(node, WzImage):
            kind = type(node).__name__ if node is not None else "missing"
            raise SystemExit(f"--entry must point at an .img; {args.entry!r} is {kind}")
        out = _ensure_json_suffix(
            args.output or f"{os.path.splitext(args.path)[0]}__{node.name}.json"
        )
        _dump(node, out)
        print(f"wrote {out}")
        return 0

    out_dir = args.output or f"{os.path.splitext(args.path)[0]}_json"
    images = list(wz.root.walk_images())
    if not images:
        print("(no images found)")
        return 0
    print(f"writing {len(images)} JSON files under {out_dir}/")
    for i, (rel, img) in enumerate(images, 1):
        out_file = os.path.join(out_dir, f"{rel}.json")
        try:
            _dump(img, out_file)
            print(f"  [{i:>5}/{len(images)}] {rel}")
        except Exception as e:
            print(f"  [{i:>5}/{len(images)}] {rel}: ERROR {e}", file=sys.stderr)
    return 0


def _convert_standalone_img(args) -> int:
    with open(args.path, "rb") as f:
        data = f.read()
    key, picked = _resolve_key(data, args)
    if picked != args.region:
        print(f"using key: {picked}")
    img = WzImage.from_bytes(data, key=key, name=os.path.basename(args.path))
    out = _ensure_json_suffix(args.output or f"{args.path}.json")
    _dump(img, out)
    print(f"wrote {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=("Convert WZ image data to JSON. Accepts either a .wz "
                     "container or a standalone .img file."),
    )
    parser.add_argument("path", help="path to a .wz or .img file")
    parser.add_argument("--region", default="GMS", choices=list(WZ_IV.keys()),
                        help="MapleStory region cipher (default: GMS)")
    parser.add_argument("--iv", default=None,
                        help="custom 4-byte IV in hex; overrides --region; .img only")
    parser.add_argument("--auto-region", action="store_true",
                        help="for a standalone .img, try every built-in "
                             "region key and use the one that decodes the "
                             "header to 'Property'")
    parser.add_argument("--keystream", default=None,
                        help="raw AES keystream as hex (bypasses AES key "
                             "generation; .img only)")
    parser.add_argument("--auto-derive", action="store_true",
                        help="derive an 8-byte keystream by assuming the "
                             "type_name is 'Property'; .img only. Lets short "
                             "property names decode but garbles strings >8 chars")
    parser.add_argument("--version", type=int, default=None,
                        help="WZ patch version (skip auto-detection; .wz only)")
    parser.add_argument("--entry", default=None,
                        help="path inside the .wz to a single .img")
    parser.add_argument("--output", "-o", default=None,
                        help="output file (single .img) or directory (whole .wz)")
    args = parser.parse_args()

    if not os.path.isfile(args.path):
        raise SystemExit(f"no such file: {args.path}")

    img_only_flags = (args.iv, args.auto_region, args.keystream, args.auto_derive)
    lower = args.path.lower()
    if lower.endswith(".wz"):
        if any(img_only_flags):
            raise SystemExit("--iv / --auto-region / --keystream / --auto-derive "
                             "only apply to standalone .img inputs")
        return _convert_wz(args)
    if lower.endswith(".img"):
        if args.entry is not None:
            raise SystemExit("--entry only applies when the input is a .wz file")
        return _convert_standalone_img(args)
    raise SystemExit("input must end in .wz or .img")


if __name__ == "__main__":
    sys.exit(main())
