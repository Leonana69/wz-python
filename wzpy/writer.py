"""Encoders that produce the exact byte layout the parser consumes.

Used for in-place byte patching: an edit is accepted only when the new
value's encoded form has the same length as the original, so subsequent
properties don't shift in the file. For variable-length encodings
(compressed int/long, two-form float, strings) the caller must compare
``len(encode_*(...))`` against the property's recorded ``_value_length``.

We intentionally do not implement a full WZ writer here. That would
require regenerating directory offsets, version-hashed encoded offsets,
string-table indirection, etc. — a much larger surface area.
"""

from __future__ import annotations

import struct


def encode_compressed_int(value: int) -> bytes:
    """Encode a 32-bit signed value using WZ's compressed-int format.

    Mirrors :meth:`WzBinaryReader.read_compressed_int`: 1 byte if the
    value fits in ``[-127, 127]``, otherwise 5 bytes (``0x80`` sentinel
    + little-endian i32). The full int range ``[-2**31, 2**31 - 1]`` is
    accepted on the wide path.
    """
    if -127 <= value <= 127:
        return bytes([value & 0xFF])
    return b"\x80" + struct.pack("<i", value)


def encode_compressed_long(value: int) -> bytes:
    """64-bit counterpart of :func:`encode_compressed_int`. 1 byte for
    values in ``[-127, 127]`` else 9 bytes (``0x80`` + little-endian i64)."""
    if -127 <= value <= 127:
        return bytes([value & 0xFF])
    return b"\x80" + struct.pack("<q", value)


def encode_short(value: int) -> bytes:
    """Two-byte little-endian i16. The reader uses ``read_i16`` here."""
    return struct.pack("<h", value)


def encode_float(value: float) -> bytes:
    """Encode the bytes that follow a Float (tag 4) property's name.

    Two-form encoding: a single ``0x00`` byte represents exactly ``0.0``
    (no payload); otherwise ``0x80`` followed by an IEEE-754 little-
    endian f32.
    """
    if value == 0.0:
        return b"\x00"
    return b"\x80" + struct.pack("<f", value)


def encode_double(value: float) -> bytes:
    """Eight-byte little-endian f64. Always the same length."""
    return struct.pack("<d", value)
