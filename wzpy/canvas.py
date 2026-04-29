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
#
# Vectorized via NumPy. The pure-Python implementations were ~580 ms on
# a 1368×720 DXT5 (~62k blocks × 16 inner-loop iterations); NumPy moves
# everything into C-level array math so the same image decodes in
# tens of milliseconds.

import numpy as np


def _decode_dxt_color_palette(color_block: np.ndarray) -> np.ndarray:
    """Given an ``(N, 8)`` array of color blocks, return ``(N, 4, 3)``
    of uint8 RGB palette entries. Mirrors the BC2/BC3 4-color table
    (which is unconditional, unlike DXT1 where ``c0 <= c1`` swaps to
    a 3-color + 1-bit-alpha mode)."""
    c0 = color_block[:, 0].astype(np.int32) | (color_block[:, 1].astype(np.int32) << 8)
    c1 = color_block[:, 2].astype(np.int32) | (color_block[:, 3].astype(np.int32) << 8)

    def unpack(c):
        r = ((c >> 11) & 0x1F) * 255 // 31
        g = ((c >> 5) & 0x3F) * 255 // 63
        b = (c & 0x1F) * 255 // 31
        return r, g, b

    r0, g0, b0 = unpack(c0)
    r1, g1, b1 = unpack(c1)
    n = color_block.shape[0]
    pal = np.empty((n, 4, 3), dtype=np.uint8)
    pal[:, 0, 0] = r0; pal[:, 0, 1] = g0; pal[:, 0, 2] = b0
    pal[:, 1, 0] = r1; pal[:, 1, 1] = g1; pal[:, 1, 2] = b1
    pal[:, 2, 0] = (2 * r0 + r1 + 1) // 3
    pal[:, 2, 1] = (2 * g0 + g1 + 1) // 3
    pal[:, 2, 2] = (2 * b0 + b1 + 1) // 3
    pal[:, 3, 0] = (r0 + 2 * r1 + 1) // 3
    pal[:, 3, 1] = (g0 + 2 * g1 + 1) // 3
    pal[:, 3, 2] = (b0 + 2 * b1 + 1) // 3
    return pal


def _dxt_color_pixels(color_block: np.ndarray) -> np.ndarray:
    """``(N, 8)`` color blocks → ``(N, 16, 3)`` RGB pixels."""
    pal = _decode_dxt_color_palette(color_block)  # (N, 4, 3)
    bits = (
        color_block[:, 4].astype(np.uint32)
        | (color_block[:, 5].astype(np.uint32) << 8)
        | (color_block[:, 6].astype(np.uint32) << 16)
        | (color_block[:, 7].astype(np.uint32) << 24)
    )
    # 16 indices, 2 bits each, ordered (py, px) inside the 4×4 tile.
    shifts = (np.arange(16, dtype=np.uint32) * 2)
    indices = ((bits[:, None] >> shifts[None, :]) & 0x3).astype(np.intp)  # (N, 16)
    n = color_block.shape[0]
    rows = np.arange(n)[:, None]
    return pal[rows, indices]  # (N, 16, 3)


def _blocks_to_image(rgba_blocks: np.ndarray, width: int, height: int) -> Image.Image:
    """Reshape ``(N, 16, 4)`` block-major RGBA pixels back into a
    ``(height, width, 4)`` image, cropping to (width, height)."""
    bh = (height + 3) // 4
    bw = (width + 3) // 4
    grid = rgba_blocks.reshape(bh, bw, 4, 4, 4)              # (bh, bw, py, px, ch)
    img = grid.transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)
    img = img[:height, :width]
    return Image.frombytes("RGBA", (width, height), img.tobytes())


def _decode_dxt3(data: bytes, width: int, height: int) -> Image.Image:
    bw = (width + 3) // 4
    bh = (height + 3) // 4
    n = bh * bw
    blocks = np.frombuffer(data, dtype=np.uint8, count=n * 16).reshape(n, 16)

    # Color (8 bytes) → 16 RGB pixels per block
    rgb = _dxt_color_pixels(blocks[:, 8:16])  # (N, 16, 3)

    # Alpha (8 bytes) — 4 bits per pixel, low nibble of byte 2*py is the
    # left-most pixel of row py.
    alpha_bytes = blocks[:, 0:8]                                # (N, 8)
    # Each pair of bytes encodes a row of 4 pixels: bits low→high.
    rows = alpha_bytes.astype(np.uint16).reshape(n, 4, 2)
    row_packed = rows[..., 0] | (rows[..., 1] << 8)             # (N, 4)
    px_shifts = (np.arange(4, dtype=np.uint16) * 4)
    alpha4 = (row_packed[..., None] >> px_shifts[None, None, :]) & 0xF   # (N, 4, 4)
    alpha = ((alpha4 << 4) | alpha4).astype(np.uint8).reshape(n, 16)

    rgba = np.empty((n, 16, 4), dtype=np.uint8)
    rgba[..., :3] = rgb
    rgba[..., 3] = alpha
    return _blocks_to_image(rgba, width, height)


def _decode_dxt5(data: bytes, width: int, height: int) -> Image.Image:
    bw = (width + 3) // 4
    bh = (height + 3) // 4
    n = bh * bw
    blocks = np.frombuffer(data, dtype=np.uint8, count=n * 16).reshape(n, 16)

    a0 = blocks[:, 0].astype(np.int32)
    a1 = blocks[:, 1].astype(np.int32)

    # Build the 8-entry alpha palette per block. The "8-color" mode
    # (a0 > a1) interpolates 6 intermediate values; the "6-color" mode
    # interpolates 4 + plus 0 and 255 sentinels.
    alpha_pal = np.empty((n, 8), dtype=np.int32)
    alpha_pal[:, 0] = a0
    alpha_pal[:, 1] = a1
    mask8 = a0 > a1                                              # (N,)
    for i in range(1, 7):
        alpha_pal[:, 1 + i] = np.where(
            mask8,
            ((7 - i) * a0 + i * a1 + 3) // 7,
            alpha_pal[:, 1 + i],     # ignored on this branch — overwritten below
        )
    for i in range(1, 5):
        v = ((5 - i) * a0 + i * a1 + 2) // 5
        alpha_pal[~mask8, 1 + i] = v[~mask8]
    alpha_pal[~mask8, 6] = 0
    alpha_pal[~mask8, 7] = 255
    alpha_pal_u8 = alpha_pal.astype(np.uint8)

    # 48-bit alpha index field as one uint64 per block.
    ai = np.zeros(n, dtype=np.uint64)
    for k in range(6):
        ai |= blocks[:, 2 + k].astype(np.uint64) << (np.uint64(8 * k))
    px_shifts = (np.arange(16, dtype=np.uint64) * np.uint64(3))
    alpha_idx = ((ai[:, None] >> px_shifts[None, :]) & np.uint64(0x7)).astype(np.intp)
    alpha = np.take_along_axis(alpha_pal_u8, alpha_idx, axis=1)  # (N, 16)

    rgb = _dxt_color_pixels(blocks[:, 8:16])

    rgba = np.empty((n, 16, 4), dtype=np.uint8)
    rgba[..., :3] = rgb
    rgba[..., 3] = alpha
    return _blocks_to_image(rgba, width, height)


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
