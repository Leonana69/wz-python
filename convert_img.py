"""Convert WZ image data to JSON.

Supports two input modes:

1. **WZ container** (``foo.wz``) — opens the archive and writes one JSON
   file per ``.img`` entry, preserving the directory layout. Use ``--entry``
   to convert only a single entry inside the archive.

2. **Standalone .img file** — a raw byte blob extracted from a WZ. The
   file is parsed using only the region cipher (offsets inside an .img
   are local to the .img and do not need ``version_hash``).

Examples:

    # All .img entries in a WZ → one JSON each, written next to the WZ.
    python convert_img.py Map4_000.wz

    # Just one entry.
    python convert_img.py Map4_000.wz --entry 400000000.img

    # A standalone .img extracted with another tool.
    python convert_img.py 400000000.img --region GMS

The output file for an input ``foo.img`` is named ``foo.img.json`` so the
naming pattern requested by the user (``*.img`` → ``*.img.json``) holds in
both modes.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from typing import Iterator, Tuple

# Make ``wzpy`` importable when the script is run from anywhere.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from wzpy import WzFile, WzImage  # noqa: E402
from wzpy.crypto import WZ_IV, WzKey  # noqa: E402
from wzpy.json_export import node_to_dict  # noqa: E402
from wzpy.reader import WzBinaryReader  # noqa: E402
from wzpy.wz_file import WzDirectory  # noqa: E402


class _StandaloneWzFile:
    """Minimal stand-in for ``WzFile`` so a freestanding ``WzImage`` can use
    its ``parse()`` method. Only the ``reader`` attribute is touched."""

    def __init__(self, reader: WzBinaryReader, region: str = "GMS"):
        self.reader = reader
        self.region = region


class _StaticWzKey(WzKey):
    """A WzKey whose keystream bytes are fixed and never extended.

    Used by ``--keystream`` and ``--auto-derive`` modes when the user has a
    raw keystream (or only the first few bytes of one) and we need to
    bypass the AES generator. ``ensure(N)`` is a no-op past the supplied
    length, so reading more than that many bytes will fall off the end and
    raise — by design, since we'd otherwise silently produce garbage."""

    def __init__(self, data: bytes):
        super().__init__(b"\x00\x00\x00\x00")
        self._data = bytearray(data)
        self._fixed_len = len(data)

    def ensure(self, size: int) -> None:
        # Zero-extend past the supplied keystream. Strings up to ``_fixed_len``
        # chars decode correctly; longer ones get garbage bytes past that
        # boundary (which the user can spot in the output and re-run with a
        # longer ``--keystream``).
        if size > len(self._data):
            self._data = self._data + bytearray(size - len(self._data))


def _peek_type_name(data: bytes, key: WzKey) -> Tuple[int, str]:
    """Read just the ``(tag, type_name)`` header at the start of an .img
    using a candidate key. Returns ``(tag, type_name)``. Strings that fail
    to decode produce a placeholder rather than raising — callers compare
    the result to ``"Property"`` to score the candidate."""
    reader = WzBinaryReader(io.BytesIO(data), key)
    try:
        tag = reader.read_byte()
        type_name = reader.read_string()
    except Exception as e:  # truncated, encoding error, etc.
        return -1, f"<error: {e}>"
    return tag, type_name


def _derive_keystream_from_property(data: bytes) -> bytes:
    """Recover the first 8 keystream bytes by assuming the .img's
    type_name is the standard ``"Property"`` (8 ASCII chars).

    ``decrypted[i] = raw[i] XOR (0xAA + i) XOR keystream[i]``,
    so ``keystream[i] = raw[i] XOR (0xAA + i) XOR "Property"[i]``.
    """
    if len(data) < 10 or data[0] != 0x73 or data[1] != 0xF8:
        raise SystemExit(
            "--auto-derive expects a Property image header "
            "(byte 0 = 0x73, byte 1 = 0xF8 for a -8-length 'Property' string)"
        )
    raw = data[2:10]
    plain = b"Property"
    return bytes(raw[i] ^ (0xAA + i) ^ plain[i] for i in range(8))


def _resolve_key(data: bytes, region: str, iv_hex, auto: bool,
                 keystream_hex, auto_derive: bool) -> Tuple[WzKey, str]:
    """Pick a decryption key.

    Priority: ``--keystream`` > ``--auto-derive`` > ``--iv`` > ``--auto-region``
    > explicit ``--region``. The first four bypass AES entirely; the last two
    use AES-256-ECB(MapleStory user key, IV * 4) per the standard scheme.
    """
    if keystream_hex is not None:
        ks = bytes.fromhex(keystream_hex)
        if not ks:
            raise SystemExit("--keystream requires at least one byte")
        return _StaticWzKey(ks), f"static keystream ({len(ks)} bytes)"

    if auto_derive:
        ks = _derive_keystream_from_property(data)
        return _StaticWzKey(ks), f"auto-derived 8-byte keystream {ks.hex()}"

    if iv_hex is not None:
        iv_bytes = bytes.fromhex(iv_hex)
        if len(iv_bytes) != 4:
            raise SystemExit(f"--iv must be 4 hex bytes (got {len(iv_bytes)})")
        return WzKey(iv_bytes), f"custom IV {iv_bytes.hex()}"

    candidates = list(WZ_IV.keys()) if auto else [region]
    diagnostics: List[str] = []
    for r in candidates:
        key = WzKey.for_region(r)
        tag, type_name = _peek_type_name(data, key)
        diagnostics.append(f"  {r}: tag=0x{tag:02x} type_name={type_name!r}")
        if tag == 0x73 and type_name == "Property":
            return key, r

    # Nothing worked — show the user what each candidate decoded.
    msg_lines = [
        "could not decrypt the .img header — type_name didn't match 'Property'",
        "with any of the tried region keys:",
    ]
    msg_lines.extend(diagnostics)
    msg_lines.append("")
    msg_lines.append(
        "Options:\n"
        "  --auto-region    scan all built-in region keys\n"
        "  --iv HEX         supply a 4-byte AES IV (e.g. 4D23C72B for old GMS)\n"
        "  --auto-derive    assume type_name='Property' and derive an 8-byte\n"
        "                   keystream from the file itself (lets short property\n"
        "                   names decode; longer strings will error out)\n"
        "  --keystream HEX  supply the raw AES keystream directly"
    )
    raise SystemExit("\n".join(msg_lines))


def _walk_images(directory: WzDirectory, prefix: str = "") -> Iterator[Tuple[str, WzImage]]:
    """Yield ``(relative_path, WzImage)`` for every image under ``directory``."""
    for name, sub in directory.subdirs.items():
        yield from _walk_images(sub, f"{prefix}/{name}" if prefix else name)
    for name, img in directory.images.items():
        rel = f"{prefix}/{name}" if prefix else name
        yield rel, img


def _resolve_entry(wz: WzFile, entry: str):
    """Find a node (directory or image) inside ``wz`` by slash-separated path."""
    node = wz.root
    parts = [p for p in entry.replace("\\", "/").split("/") if p]
    for part in parts:
        if not isinstance(node, WzDirectory):
            raise SystemExit(f"cannot descend into a non-directory at {part!r}")
        child = node.child(part)
        if child is None:
            raise SystemExit(f"no such entry: {entry!r}")
        node = child
    return node


def _dump_image(img: WzImage, out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    img.parse()
    body = json.dumps(node_to_dict(img), indent=2, ensure_ascii=False)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(body)
    if getattr(img, "truncated", False):
        print(f"  WARNING: {img.name} appears truncated; emitted partial JSON",
              file=sys.stderr)


def _convert_wz(wz_path: str, region: str, version, entry, out: str) -> int:
    wz = WzFile.open(wz_path, region=region, version=version)
    if entry is None:
        # Walk every image. Output mirrors the WZ tree so name collisions
        # across subdirectories (e.g., the same ``Back/0.img`` in a couple
        # of Map subtrees) are kept distinct.
        out_dir = out or f"{os.path.splitext(wz_path)[0]}_json"
        images = list(_walk_images(wz.root))
        if not images:
            print("(no images found)")
            return 0
        print(f"writing {len(images)} JSON files under {out_dir}/")
        for i, (rel, img) in enumerate(images, 1):
            out_file = os.path.join(out_dir, f"{rel}.json")
            try:
                _dump_image(img, out_file)
                print(f"  [{i:>5}/{len(images)}] {rel}")
            except Exception as e:
                print(f"  [{i:>5}/{len(images)}] {rel}: ERROR {e}", file=sys.stderr)
        return 0

    node = _resolve_entry(wz, entry)
    if not isinstance(node, WzImage):
        raise SystemExit(f"--entry must point at an .img; {entry!r} is a {type(node).__name__}")
    out_file = out or f"{os.path.splitext(wz_path)[0]}__{node.name}.json"
    if not out_file.endswith(".json"):
        out_file += ".json"
    _dump_image(node, out_file)
    print(f"wrote {out_file}")
    return 0


def _convert_standalone_img(img_path: str, region: str, out: str,
                            iv_hex, auto_region: bool,
                            keystream_hex, auto_derive: bool) -> int:
    with open(img_path, "rb") as f:
        data = f.read()

    key, picked = _resolve_key(data, region=region, iv_hex=iv_hex,
                               auto=auto_region, keystream_hex=keystream_hex,
                               auto_derive=auto_derive)
    if picked != region:
        print(f"using key: {picked}")

    reader = WzBinaryReader(io.BytesIO(data), key)
    # version_hash and header_fstart aren't used inside an .img — the
    # offsets stored in property lists are local to the image's start.
    name = os.path.basename(img_path)
    img = WzImage(name=name, parent=None, offset=0, size=len(data),
                  wz_file=_StandaloneWzFile(reader, picked))
    out_file = out or f"{img_path}.json"
    if not out_file.endswith(".json"):
        out_file += ".json"
    _dump_image(img, out_file)
    print(f"wrote {out_file}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=("Convert WZ image data to JSON. Accepts either a .wz "
                     "container or a standalone .img file."),
    )
    parser.add_argument("path", help="path to a .wz or .img file")
    parser.add_argument("--region", default="GMS", choices=["GMS", "EMS", "BMS"],
                        help="MapleStory region cipher (default: GMS)")
    parser.add_argument("--iv", default=None,
                        help="custom 4-byte IV in hex (e.g. 4D23C72B); "
                             "overrides --region; .img only")
    parser.add_argument("--auto-region", action="store_true",
                        help="for a standalone .img, try every built-in "
                             "region key and use the one that decodes "
                             "the header to 'Property'")
    parser.add_argument("--keystream", default=None,
                        help="raw AES keystream as hex (bypasses AES key "
                             "generation; .img only). Useful when the file "
                             "uses a non-standard cipher")
    parser.add_argument("--auto-derive", action="store_true",
                        help="derive an 8-byte keystream by assuming the "
                             "type_name in the .img header is 'Property'. "
                             "Lets short property names decode but raises "
                             "on strings longer than 8 chars; .img only")
    parser.add_argument("--version", type=int, default=None,
                        help="WZ patch version (skip auto-detection; .wz only)")
    parser.add_argument("--entry", default=None,
                        help="path inside the .wz to a single .img to convert")
    parser.add_argument("--output", "-o", default=None,
                        help="output file (single .img) or directory (whole .wz)")
    args = parser.parse_args()

    if not os.path.isfile(args.path):
        raise SystemExit(f"no such file: {args.path}")

    lower = args.path.lower()
    if lower.endswith(".wz"):
        if args.iv or args.auto_region or args.keystream or args.auto_derive:
            raise SystemExit("--iv / --auto-region / --keystream / --auto-derive "
                             "only apply to standalone .img inputs")
        return _convert_wz(args.path, args.region, args.version, args.entry, args.output)
    if lower.endswith(".img"):
        if args.entry:
            raise SystemExit("--entry only applies when the input is a .wz file")
        return _convert_standalone_img(args.path, args.region, args.output,
                                       args.iv, args.auto_region,
                                       args.keystream, args.auto_derive)
    raise SystemExit("input must end in .wz or .img")


if __name__ == "__main__":
    sys.exit(main())
