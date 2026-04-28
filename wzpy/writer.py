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


# ── string payload (re-)encryption ──────────────────────────────────────
# These mirror :meth:`WzBinaryReader._decode_ascii` / ``_decode_unicode``:
# the cipher is plain XOR with a precomputed ``mask ^ keystream`` table,
# so encryption and decryption are the same operation.
#
# The caller is expected to fetch the reader's already-built combined
# table — the same bytes the reader XORed against during decode — so
# that the new ciphertext interleaves cleanly with everything else
# already on disk. ``WzBinaryReader._ensure_ascii_combined(n)`` /
# ``_ensure_unicode_combined(n)`` return that table.

def encode_ascii_payload(plaintext: str, combined: bytes) -> bytes:
    """Encrypt a CP1252 payload of ``len(plaintext)`` bytes."""
    raw = plaintext.encode("cp1252")
    n = len(raw)
    if n > len(combined):
        raise ValueError(
            f"keystream table is {len(combined)} bytes, need {n}"
        )
    if n == 0:
        return b""
    # Same one-shot XOR trick the reader uses (reader.py: _decode_ascii):
    # one C-level operation regardless of length.
    xored = int.from_bytes(raw, "big") ^ int.from_bytes(combined[:n], "big")
    return xored.to_bytes(n, "big")


def encode_unicode_payload(plaintext: str, combined: bytes) -> bytes:
    """Encrypt a UTF-16-LE payload of ``2 * len(plaintext)`` bytes."""
    raw = plaintext.encode("utf-16-le")
    n = len(raw)
    if n > len(combined):
        raise ValueError(
            f"keystream table is {len(combined)} bytes, need {n}"
        )
    if n == 0:
        return b""
    xored = int.from_bytes(raw, "big") ^ int.from_bytes(combined[:n], "big")
    return xored.to_bytes(n, "big")


def encoded_string_payload_size(plaintext: str, encoding: str) -> int:
    """How many bytes a re-encrypted payload would occupy. Used by the
    server to budget-check before patching."""
    if encoding == "ascii":
        return len(plaintext.encode("cp1252"))
    if encoding == "unicode":
        return len(plaintext.encode("utf-16-le"))
    raise ValueError(f"unknown encoding: {encoding!r}")


def re_encrypt_string(reader, plaintext: str, encoding: str) -> bytes:
    """Convenience wrapper: pull the right combined-keystream table off
    ``reader`` and produce the encrypted payload bytes.

    Note: WZ's ASCII string encoding uses a position-only keystream
    (``mask = 0xAA + i, key = aes_keystream[i]``), so the bytes we
    write here are valid at *any* file offset — there's no per-offset
    salt to worry about.
    """
    if encoding == "ascii":
        n = len(plaintext.encode("cp1252"))
        if n == 0:
            return b""
        table = reader._ensure_ascii_combined(n)
        return encode_ascii_payload(plaintext, table)
    if encoding == "unicode":
        char_count = len(plaintext)
        if char_count == 0:
            return b""
        table = reader._ensure_unicode_combined(char_count)
        return encode_unicode_payload(plaintext, table)
    raise ValueError(f"unknown encoding: {encoding!r}")
