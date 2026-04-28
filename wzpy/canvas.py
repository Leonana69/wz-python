"""Canvas (PNG) decoding.

WZ Canvas blobs are zlib-compressed pixel data in one of several formats.
The compressed bytes themselves may also be XOR-obfuscated (the "listWz"
format) when the first two bytes do not look like a zlib header — see
``WzPngProperty.cs`` in MapleLib.
"""

from __future__ import annotations

import io
import zlib
from typing import TYPE_CHECKING

from PIL import Image

from .crypto import WzKey

if TYPE_CHECKING:
    from .properties import WzCanvasProperty


# zlib stream first-byte values we accept as "non-listWz".
_ZLIB_HEADERS = {0x9C78, 0xDA78, 0x0178, 0x5E78}


def _read_canvas_bytes(canvas: "WzCanvasProperty") -> bytes:
    if canvas._png_data is not None:
        return canvas._png_data
    wz_image = canvas._wz_image
    if wz_image is None:
        raise RuntimeError("canvas not bound to a WzImage")
    r = wz_image.wz_file.reader
    keep = r.position
    r.seek(canvas._png_offset)
    canvas._png_data = r.read(canvas._png_length)
    r.seek(keep)
    return canvas._png_data


def _try_listwz(raw: bytes, key: WzKey) -> bytes:
    """Dechunk a listWz blob and return the inner deflate payload bytes."""
    src = io.BytesIO(raw)
    out = bytearray()
    while True:
        chunk_len_bytes = src.read(4)
        if len(chunk_len_bytes) < 4:
            break
        chunk_len = int.from_bytes(chunk_len_bytes, "little")
        if chunk_len <= 0 or chunk_len > len(raw):
            raise ValueError("invalid listWz chunk length")
        chunk = src.read(chunk_len)
        if len(chunk) < chunk_len:
            raise ValueError("truncated listWz chunk")
        key.ensure(chunk_len)
        # bytes-level XOR is much faster than a Python comprehension.
        n = int.from_bytes(chunk, "big") ^ int.from_bytes(key.slice(chunk_len), "big")
        out += n.to_bytes(chunk_len, "big")
    return bytes(out)


def _zlib_lenient(data: bytes, wbits: int = zlib.MAX_WBITS) -> bytes:
    """``decompressobj`` + ``flush`` — handles trailing data gracefully."""
    d = zlib.decompressobj(wbits)
    out = d.decompress(data) + d.flush()
    return out


def _decompress(canvas: "WzCanvasProperty", key: WzKey) -> bytes:
    raw = _read_canvas_bytes(canvas)
    if len(raw) < 2:
        return b""

    attempts = (
        ("zlib", lambda: _zlib_lenient(raw)),
        ("raw-deflate-skip2", lambda: _zlib_lenient(raw[2:], wbits=-zlib.MAX_WBITS)),
        ("listwz-zlib", lambda: _zlib_lenient(_try_listwz(raw, key))),
        (
            "listwz-raw-deflate-skip2",
            lambda: _zlib_lenient(_try_listwz(raw, key)[2:], wbits=-zlib.MAX_WBITS),
        ),
    )

    errors = []
    for name, fn in attempts:
        try:
            result = fn()
            if result:
                return result
            errors.append(f"{name}: empty result")
        except Exception as exc:  # noqa: BLE001 — we want to try the next one
            errors.append(f"{name}: {exc}")

    head = raw[:16].hex()
    raise ValueError(
        f"no decoder succeeded (first 16 bytes: {head}); attempts: " + " | ".join(errors)
    )


def _decode_pixels(data: bytes, width: int, height: int, fmt: int) -> Image.Image:
    """Convert raw decompressed pixels to a PIL image."""
    if fmt == 1:
        # ARGB4444, 2 bytes per pixel
        out = bytearray(width * height * 4)
        for i in range(width * height):
            lo = data[i * 2]
            hi = data[i * 2 + 1]
            b = (lo & 0x0F) | ((lo & 0x0F) << 4)
            g = (lo & 0xF0) | ((lo & 0xF0) >> 4)
            r = (hi & 0x0F) | ((hi & 0x0F) << 4)
            a = (hi & 0xF0) | ((hi & 0xF0) >> 4)
            out[i * 4 + 0] = r
            out[i * 4 + 1] = g
            out[i * 4 + 2] = b
            out[i * 4 + 3] = a
        return Image.frombytes("RGBA", (width, height), bytes(out))
    if fmt == 2:
        # ARGB8888 stored as BGRA on disk
        return Image.frombytes("RGBA", (width, height), data, "raw", "BGRA")
    if fmt == 3:
        # downsampled (each 4x4 block stores one ARGB8888 pixel = 4 bytes)
        small_w = (width + 3) // 4
        small_h = (height + 3) // 4
        small = Image.frombytes(
            "RGBA", (small_w, small_h), data, "raw", "BGRA",
        )
        return small.resize((width, height), Image.NEAREST)
    if fmt == 257:
        # ARGB1555 — uncommon but documented
        out = bytearray(width * height * 4)
        for i in range(width * height):
            v = data[i * 2] | (data[i * 2 + 1] << 8)
            a = 0xFF if v & 0x8000 else 0x00
            r = ((v >> 10) & 0x1F) * 8
            g = ((v >> 5) & 0x1F) * 8
            b = (v & 0x1F) * 8
            out[i * 4:i * 4 + 4] = bytes([r, g, b, a])
        return Image.frombytes("RGBA", (width, height), bytes(out))
    if fmt == 513:
        # RGB565
        out = bytearray(width * height * 4)
        for i in range(width * height):
            v = data[i * 2] | (data[i * 2 + 1] << 8)
            r = ((v >> 11) & 0x1F) * 8
            g = ((v >> 5) & 0x3F) * 4
            b = (v & 0x1F) * 8
            out[i * 4:i * 4 + 4] = bytes([r, g, b, 0xFF])
        return Image.frombytes("RGBA", (width, height), bytes(out))
    if fmt == 517:
        # downsampled RGB565
        small_w = (width + 15) // 16
        small_h = (height + 15) // 16
        small_data = data[: small_w * small_h * 2]
        out = bytearray(small_w * small_h * 4)
        for i in range(small_w * small_h):
            v = small_data[i * 2] | (small_data[i * 2 + 1] << 8)
            r = ((v >> 11) & 0x1F) * 8
            g = ((v >> 5) & 0x3F) * 4
            b = (v & 0x1F) * 8
            out[i * 4:i * 4 + 4] = bytes([r, g, b, 0xFF])
        small = Image.frombytes("RGBA", (small_w, small_h), bytes(out))
        return small.resize((width, height), Image.NEAREST)
    if fmt == 1026:
        return _decode_dxt3(data, width, height)
    if fmt == 2050:
        return _decode_dxt5(data, width, height)
    raise ValueError(f"unsupported canvas format {fmt}")


# ── DXT (BC2/BC3) block decoders ─────────────────────────────────────
# Each block covers a 4×4 pixel tile. Format reference: Microsoft BCn docs
# and ``MapleLib/WzLib/WzProperties/WzPngProperty.cs``.

def _decode_dxt_color_block(block: bytes) -> list:
    """Decode a 4×4 DXT1 color block into 16 (r,g,b) tuples."""
    c0 = block[0] | (block[1] << 8)
    c1 = block[2] | (block[3] << 8)

    def unpack(c):
        r = ((c >> 11) & 0x1F) * 255 // 31
        g = ((c >> 5) & 0x3F) * 255 // 63
        b = (c & 0x1F) * 255 // 31
        return (r, g, b)

    palette = [unpack(c0), unpack(c1)]
    # In BC2/BC3 (DXT3/DXT5), the 4-color form is always used regardless of c0/c1 ordering.
    palette.append(tuple((2 * a + b + 1) // 3 for a, b in zip(palette[0], palette[1])))
    palette.append(tuple((a + 2 * b + 1) // 3 for a, b in zip(palette[0], palette[1])))

    bits = block[4] | (block[5] << 8) | (block[6] << 16) | (block[7] << 24)
    return [palette[(bits >> (2 * i)) & 0x3] for i in range(16)]


def _decode_dxt3(data: bytes, width: int, height: int) -> Image.Image:
    bw = (width + 3) // 4
    bh = (height + 3) // 4
    out = bytearray(width * height * 4)
    pos = 0
    for by in range(bh):
        for bx in range(bw):
            alpha_block = data[pos:pos + 8]
            color_block = data[pos + 8:pos + 16]
            pos += 16
            colors = _decode_dxt_color_block(color_block)
            for py in range(4):
                row = alpha_block[py * 2] | (alpha_block[py * 2 + 1] << 8)
                for px in range(4):
                    x = bx * 4 + px
                    y = by * 4 + py
                    if x >= width or y >= height:
                        continue
                    a4 = (row >> (4 * px)) & 0xF
                    a = (a4 << 4) | a4  # expand 4-bit alpha to 8-bit
                    r, g, b = colors[py * 4 + px]
                    o = (y * width + x) * 4
                    out[o:o + 4] = bytes([r, g, b, a])
    return Image.frombytes("RGBA", (width, height), bytes(out))


def _decode_dxt5(data: bytes, width: int, height: int) -> Image.Image:
    bw = (width + 3) // 4
    bh = (height + 3) // 4
    out = bytearray(width * height * 4)
    pos = 0
    for by in range(bh):
        for bx in range(bw):
            a0 = data[pos]
            a1 = data[pos + 1]
            alpha_palette = [a0, a1]
            if a0 > a1:
                for i in range(1, 7):
                    alpha_palette.append(((7 - i) * a0 + i * a1 + 3) // 7)
            else:
                for i in range(1, 5):
                    alpha_palette.append(((5 - i) * a0 + i * a1 + 2) // 5)
                alpha_palette.append(0)
                alpha_palette.append(255)

            # 48-bit alpha index field
            ai = 0
            for k in range(6):
                ai |= data[pos + 2 + k] << (8 * k)

            color_block = data[pos + 8:pos + 16]
            pos += 16
            colors = _decode_dxt_color_block(color_block)

            for py in range(4):
                for px in range(4):
                    x = bx * 4 + px
                    y = by * 4 + py
                    if x >= width or y >= height:
                        continue
                    idx = py * 4 + px
                    a = alpha_palette[(ai >> (3 * idx)) & 0x7]
                    r, g, b = colors[idx]
                    o = (y * width + x) * 4
                    out[o:o + 4] = bytes([r, g, b, a])
    return Image.frombytes("RGBA", (width, height), bytes(out))


def decode_canvas(canvas: "WzCanvasProperty", region: str = "GMS") -> Image.Image:
    """Decode the canvas's pixels into a PIL ``Image`` (RGBA).

    ``region`` is needed because listWz blocks XOR against the same regional
    key stream used for strings.
    """
    fmt = canvas.format + canvas.format2
    key = WzKey.for_region(region)
    raw = _decompress(canvas, key)
    return _decode_pixels(raw, canvas.width, canvas.height, fmt)


# ── pixel encoders (inverse of _decode_pixels) ─────────────────────────
# Used by the canvas-replacement save path. We support the same formats
# that the decoder supports for ARGB/RGB565 family. DXT is intentionally
# refused because re-encoding requires a full block compressor, which is
# out of scope for v1.

_DXT_FORMATS = (1026, 2050)


def _encode_pixels(image: Image.Image, fmt: int, width: int, height: int) -> bytes:
    """Pack ``image`` into the raw pixel-bytes representation that
    :func:`_decode_pixels` would produce in reverse. Returns the bytes
    that go into the zlib stream.
    """
    if fmt in _DXT_FORMATS:
        raise ValueError(
            f"writing canvas format {fmt} (DXT) is not supported; pick a "
            f"different sprite or convert the format externally"
        )
    if image.size != (width, height):
        # Caller may want to preserve resolution; resize bilinearly.
        image = image.resize((width, height), Image.LANCZOS)
    if image.mode != "RGBA":
        image = image.convert("RGBA")

    if fmt == 1:
        # ARGB4444: 2 bytes per pixel. High nibble of each byte encodes
        # the high 4 bits of the channel; low nibble the low 4. The
        # decoder expands a 4-bit channel ``c`` to ``(c << 4) | c``, so
        # we encode the ROUNDED top 4 bits ``(c + 8) >> 4`` clamped.
        rgba = image.tobytes("raw", "RGBA")
        out = bytearray(width * height * 2)
        for i in range(width * height):
            r = rgba[i * 4 + 0] >> 4
            g = rgba[i * 4 + 1] >> 4
            b = rgba[i * 4 + 2] >> 4
            a = rgba[i * 4 + 3] >> 4
            # Decoder: lo = b | (b << 4) for low nibble, hi nibble holds g
            #          hi = r | (r << 4) for low nibble, hi nibble holds a
            out[i * 2 + 0] = b | (g << 4)
            out[i * 2 + 1] = r | (a << 4)
        return bytes(out)

    if fmt == 2:
        # ARGB8888 stored as BGRA on disk. PIL's "raw" output with band
        # order "BGRA" gives us exactly that.
        return image.tobytes("raw", "BGRA")

    if fmt == 3:
        # Down-sampled BGRA8888: 4×4 source blocks collapse to one BGRA
        # pixel each. The decoder unpacks via NEAREST upscale.
        small_w = (width + 3) // 4
        small_h = (height + 3) // 4
        small = image.resize((small_w, small_h), Image.LANCZOS)
        return small.tobytes("raw", "BGRA")

    if fmt == 257:
        # ARGB1555: 2 bytes per pixel. 1 alpha bit + 5 each for RGB.
        rgba = image.tobytes("raw", "RGBA")
        out = bytearray(width * height * 2)
        for i in range(width * height):
            r = rgba[i * 4 + 0] >> 3
            g = rgba[i * 4 + 1] >> 3
            b = rgba[i * 4 + 2] >> 3
            a_bit = 0x8000 if rgba[i * 4 + 3] >= 128 else 0x0000
            v = a_bit | (r << 10) | (g << 5) | b
            out[i * 2 + 0] = v & 0xFF
            out[i * 2 + 1] = (v >> 8) & 0xFF
        return bytes(out)

    if fmt == 513:
        # RGB565: 5/6/5 bits — alpha discarded.
        rgb = image.convert("RGB").tobytes("raw", "RGB")
        out = bytearray(width * height * 2)
        for i in range(width * height):
            r = rgb[i * 3 + 0] >> 3
            g = rgb[i * 3 + 1] >> 2
            b = rgb[i * 3 + 2] >> 3
            v = (r << 11) | (g << 5) | b
            out[i * 2 + 0] = v & 0xFF
            out[i * 2 + 1] = (v >> 8) & 0xFF
        return bytes(out)

    if fmt == 517:
        # Down-sampled RGB565: 16×16 source blocks → one RGB565 pixel.
        small_w = (width + 15) // 16
        small_h = (height + 15) // 16
        small = image.resize((small_w, small_h), Image.LANCZOS).convert("RGB")
        rgb = small.tobytes("raw", "RGB")
        out = bytearray(small_w * small_h * 2)
        for i in range(small_w * small_h):
            r = rgb[i * 3 + 0] >> 3
            g = rgb[i * 3 + 1] >> 2
            b = rgb[i * 3 + 2] >> 3
            v = (r << 11) | (g << 5) | b
            out[i * 2 + 0] = v & 0xFF
            out[i * 2 + 1] = (v >> 8) & 0xFF
        return bytes(out)

    raise ValueError(f"unsupported canvas format {fmt} for write")


def _encode_listwz(payload: bytes, key: WzKey, chunk_size: int = 4096) -> bytes:
    """Wrap ``payload`` (already-zlib bytes) as a listWz blob: chunks of
    ``chunk_size`` bytes XORed against the WZ keystream, each preceded by
    a u32 little-endian length. Mirrors :func:`_try_listwz`.
    """
    out = bytearray()
    for start in range(0, len(payload), chunk_size):
        chunk = payload[start:start + chunk_size]
        n = len(chunk)
        key.ensure(n)
        xored = (
            int.from_bytes(chunk, "big")
            ^ int.from_bytes(key.slice(n), "big")
        )
        out += n.to_bytes(4, "little")
        out += xored.to_bytes(n, "big")
    return bytes(out)


def encode_canvas_payload(
    image: Image.Image,
    fmt: int,
    width: int,
    height: int,
    *,
    key: WzKey,
    listwz: bool = False,
    zlib_level: int = 6,
) -> bytes:
    """Full encode pipeline: pixels → zlib → optional listWz.

    Returns the bytes that should land at ``WzCanvasProperty._png_offset``
    (the caller is responsible for length-budget validation against
    ``_png_length`` and zero-padding any unused tail).
    """
    raw = _encode_pixels(image, fmt, width, height)
    compressed = zlib.compress(raw, zlib_level)
    if listwz:
        return _encode_listwz(compressed, key)
    return compressed
