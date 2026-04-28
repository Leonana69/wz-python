# Variable-Length WZ Edits ("Save As" pipeline)

## Context

The same-length edit pipeline shipped in commits `d095225` (scalars) and
`0a2e2a4` (strings + canvas image replacement) requires every edit to
produce **exactly the same byte count** as the original. Anything that
would shift downstream offsets is rejected:

- A string going from 8 to 12 bytes
- A canvas whose new image compresses to more than the existing slot
- Any structural change (add / delete / rename a property)

This is the right default for in-place safety, but it's also a real
limitation. Item.wz on Korean clients, for example, has names that are
naturally length-changing under translation.

The user wants edits that change size to be possible.

## Why this is hard (the cascade)

A WZ archive stores every directory entry, every IMG body, and many
property internals by **absolute file offset**. Growing a string by 4
bytes inside one IMG cascades through:

1. Bytes after the string within the same IMG shift down by 4
2. Offset-indirected string-block markers (`0x01`/`0x1B`) inside that IMG
   referencing positions past the shift point need their u32 offset
   bumped
3. The IMG itself becomes 4 bytes longer
4. The directory entry that records the IMG's `size` (a compressed-int
   field that can be 1 or 5 bytes!) may itself change byte length
5. Subsequent entries in the same directory shift, requiring their
   own size/checksum/encrypted-offset fields to be rewritten
6. Every encrypted offset in the parent directory needs to be
   recomputed because positions changed
7. This cascades all the way up to the root directory

So either we (a) painstakingly fix every cascading reference in place,
or (b) re-serialize the whole archive into a new file. (a) is fragile
and a partial failure leaves a corrupt file. (b) is canonical, safe,
and matches what HaSuite / HaRepacker actually do under the hood.

## Approach

**Adopt a "Save As" workflow that re-serializes the full archive into a
new file.** The original WZ is untouched until the user explicitly
overwrites it.

Two paths:

- **Fast path (already shipped)**: same-length edits via `/api/save`
  patch the on-disk mmap immediately. No file rewrite. Surgically
  stable. Used for >90% of edits.
- **New slow path**: a "Save As…" button collects every staged edit
  (any size, including same-length ones not yet applied) and produces
  a complete new WZ on disk via a full re-serialize.

The two paths share the same in-memory tree mutation. The difference
is whether the bytes get patched immediately or stored and serialized
later.

## What needs to be built

### 1. Property → bytes serializer

A serializer for every property type. Most leaves reuse the existing
encoders in `wzpy/writer.py` (`encode_compressed_int`, `encode_short`,
`encode_float`, `encode_double`, `re_encrypt_string`, etc.).

For the compound types we need new encoders:

| Type          | Tag / ext-type                | Body layout                                                                                              |
|---------------|-------------------------------|----------------------------------------------------------------------------------------------------------|
| `Null`        | tag 0                         | (no body)                                                                                                |
| `Short`       | tag 2 / 11                    | i16 LE (2 bytes)                                                                                         |
| `Int`         | tag 3 / 19                    | compressed-int (1 or 5 bytes)                                                                            |
| `Long`        | tag 20                        | compressed-long (1 or 9 bytes)                                                                           |
| `Float`       | tag 4                         | `0x00` for 0.0, else `0x80` + f32 LE                                                                     |
| `Double`      | tag 5                         | f64 LE (8 bytes)                                                                                         |
| `String`      | tag 8                         | string-block (marker + sign + payload)                                                                   |
| `SubProperty` | tag 9 / `Property`            | `00 00` reserved, then property-list                                                                     |
| `Vector`      | tag 9 / `Shape2D#Vector2D`    | x compressed-int, y compressed-int                                                                       |
| `Convex`      | tag 9 / `Shape2D#Convex2D`    | count compressed-int, then `count` Vector children                                                       |
| `Canvas`      | tag 9 / `Canvas`              | reserved byte, has-children byte, optional sub-children, w/h/format compressed-ints, format2 byte, 4 reserved, len-1 i32, 1 filler, PNG bytes |
| `Sound`       | tag 9 / `Sound_DX8`           | reserved byte, data_length compressed-int, length_ms compressed-int, header bytes, audio bytes           |
| `UOL`         | tag 9 / `UOL`                 | reserved byte, target string-block                                                                       |

For the extended-block types (tag 9), the wire layout is:

```
tag(1) ext_size(u32, LE) ext_type(string-block) <ext_type-specific body>
```

`ext_size` covers everything from `ext_type` through the end of the
body. We can compute the body size first, prepend the size, then write.

### 2. IMG → bytes serializer

`WzImage.serialize_to_bytes(string_table=None) -> bytes` returns the
.img body. Layout:

```
0x73 <"Property" string-block> 0x00 0x00 <property-list>
```

A property list is a compressed-int count followed by `count`
`<name string-block><tag>` pairs.

### 3. Directory → bytes serializer

`WzDirectory.serialize_to_bytes(planner) -> bytes`.

For each entry: `<kind><name><size compressed-int><checksum compressed-int><encrypted_offset u32>`.

Kind=3 is sub-directory, kind=4 is image. The `kind=2` indirected-name
form used by the parser doesn't need to be emitted on write — we can
always inline names.

### 4. Encrypted-offset encoder

We already have the algorithm in test scaffolding (`rotl`, the
`(target - fstart * 2) ^ computed` step). Lift to `wzpy/writer.py` as
`encode_offset(position, target, fstart, version_hash)`.

### 5. Header writer

Header layout:

```
"PKG1" file_size(u64) header_size(u32, = fstart) copyright(string + NUL) encrypted_version(u16)
```

`encrypted_version = derive_version_check(version_hash)` (already
exists in `wzpy/crypto.py`).

`file_size` is set after we know the final length.

### 6. WZ → file serializer

`WzFile.save_as(path: str) -> None`:

1. **Force-parse all images.** Iterate `wz.root.walk_images()` and call
   `.parse()` on each one whose tree isn't already in memory. Skip
   gracefully if a particular image fails (rare; leave the original
   bytes as-is — see "Tail-and-fixup fallback" below).
2. **Plan the layout.** Walk the tree DFS:
   - Header bytes (computed)
   - Root directory bytes (computed; sub-directory + image sizes
     known but their positions aren't yet)
   - Each sub-directory's bytes
   - Each image's bytes
3. **Two-pass for offsets.** Compute every node's serialized size
   first (no offset values needed). Then assign positions. Then write
   each piece, computing encrypted offsets in the directory entries
   from the now-known target positions.
4. **Write.** Stream to the output file in order. Optionally
   `os.replace` to do an atomic swap when overwriting the original.

The planning is straightforward because every variable-length field
(size, checksum) is a compressed-int with two possible widths (1 or
5 bytes). We pick the actual width based on the value, not a worst-
case estimate.

### 7. Edit overlay

The current `/api/save` endpoint applies edits and patches the file
in one step. For variable-length edits we need to:

- Mutate the in-memory tree (set `prop._value`, etc.)
- **Not** patch the file
- Track which IMG was touched (for later "are there unsaved changes?"
  questions)

Reuse the same in-memory mutation but split into two endpoints:

| Endpoint              | Behavior                                                                              |
|-----------------------|---------------------------------------------------------------------------------------|
| `POST /api/save`      | Same as today. Reject anything that doesn't fit. Mutate tree + patch file in place.  |
| `POST /api/edit`      | Always accept. Mutate tree only — do not touch the file. Used for size-changing edits.|
| `POST /api/save_as`   | Body: `{path: "..."}`. Trigger full archive re-serialize to that path.                |

Frontend chooses: if the user's pending edit fits the slot, send to
`/api/save`; if it doesn't (or the user explicitly chose "Save As"),
send to `/api/edit` and prompt for a target path.

### 8. Canvas replacement variable-size path

Today's `POST /api/canvas/<path>` rejects payloads that exceed the
slot. Add a query param `?fit=loose` (or default to it) that:

- Stores the new compressed bytes on `WzCanvasProperty._png_data`
- Updates `_png_length` to the new payload length
- Marks the IMG as dirty
- Returns `{ok: true, in_place: false}` so the UI knows the next
  Save As will pick this up

### 9. UI: Save As

A "Save As…" button in the header (next to Save). Click → modal:

- Default filename: `<original>.modified.wz`
- Optional checkbox "overwrite original" (warns)
- Submit → POST `/api/save_as` → progress modal (re-using the same
  pattern as the JSON-bundle export modal that already exists)

After success, show a toast with the output path and total bytes
written. Optionally offer to open the new file (POST a re-init).

### 10. Pending-edit indicator

Track "tree has unsaved size-changing edits" in the header and prompt
on navigation/refresh.

## Files to modify

| File                          | Change                                                                                           |
|-------------------------------|--------------------------------------------------------------------------------------------------|
| `wzpy/writer.py`              | Full serializer suite (header, directory, image, every property type) + `encode_offset`          |
| `wzpy/wz_file.py`             | `save_as(path)` method driving the serializer; new `dirty` flag                                  |
| `wzpy/wz_image.py`            | `serialize_to_bytes()` method; mark dirty on edit                                                |
| `wzpy/properties.py`          | `serialize_to_bytes()` per property type (or external dispatcher in writer.py — TBD)             |
| `wzpy/canvas.py`              | Already has `encode_canvas_payload`; just call it during serialize                               |
| `server/app.py`               | New `/api/edit` endpoint (variable-length stage), `/api/save_as` endpoint, dirty flag in `/api/property` responses |
| `server/static/app.js`        | "Save As…" button; route edits that don't fit through `/api/edit`; modal + progress             |
| `server/static/style.css`     | Save As button styling                                                                            |
| `server/templates/index.html` | Save As button placement                                                                          |

## Tail-and-fixup fallback (optional, V2)

For the rare case where one or two .img files fail to parse cleanly
(corrupt / truncated / format we don't yet handle), `save_as` could
fall back to byte-copying the original .img bytes verbatim. This
keeps the WZ usable end-to-end even in the presence of unknowns.

## Out of scope (V1)

- True string-table dedup. We'll always inline strings on write
  (slightly larger output, but correct).
- Editing the directory structure (renaming directories or images,
  adding new images).
- Live preview of what Save As would produce.
- True in-place variable-length edits with cascade fixup. Too fragile
  to be worth maintaining alongside Save As.
- Re-encoding 64-bit `Data/<Cat>/<file>_NNN.wz` layouts and `.ms`
  Snowcrypt packs. Save As would output legacy 32-bit format.

## Verification

### Synthetic round-trip

1. Build a synthetic WZ with one .img containing one short string and
   one small canvas (the existing test-fixture builder is already
   parameterized).
2. Apply a length-changing string edit and a same-format-but-larger
   canvas via `/api/edit`.
3. POST `/api/save_as` to a temp path.
4. Re-open the saved file with `WzFile.open` and walk every property —
   confirm both edits round-trip cleanly.
5. Compare structurally against the original (only the modified
   properties should differ; everything else byte-identical *modulo*
   any string-table layout differences).

### Real archive round-trip (no edits)

1. Load Item.wz / UI.wz / Mob.wz unchanged.
2. `save_as` to a temp path.
3. Re-open the new file. Walk the tree. The parsed tree must be
   identical to the original tree.
4. Byte-equality is *not* required (we may dedupe strings differently,
   leave no garbage, etc.) — only semantic equivalence.

### Real archive round-trip (with edits)

1. Load Item.wz.
2. Rename a few item descriptions to longer strings.
3. Replace a few iconRaws with images that don't fit the original slot.
4. `save_as`.
5. Re-open. Confirm edits, confirm rest of tree is identical.

### Performance budget

For the largest typical legacy WZ (~250 MB Map.wz), `save_as` should
finish in under 60 seconds on a developer-grade machine. If we blow
that budget, switch the per-IMG serializer to write directly into a
shared `bytearray` instead of returning fresh `bytes` per call.

## Open questions

- Should `/api/save_as` accept any user-supplied path or only paths
  inside a configured working directory? (Security trade-off; a future
  WebDAV-style sandbox is ideal but probably overkill for v1.)
- Should we offer "Apply changes" without writing — i.e. mutate the
  in-memory tree but defer the disk write entirely? Probably yes, and
  it's already how `/api/edit` would behave.
- Atomic replace on overwrite: write to `<path>.tmp`, then
  `os.replace`. Standard pattern; do this from day one.
