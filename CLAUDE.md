# Notes for Claude

Quick reference for working in this repo: how IDs map to images, how to
spin up the dev server, and the canonical one-liners I use to verify
character-rendering changes without round-tripping through the UI.

## Loading a `WzPackage`

```python
from wzpy.wz_package import WzPackage
pkg = WzPackage.open("D:/Git/wz-python/data/Character", region="BMS")
```

- `data/Character/` is a hierarchical (64-bit) pack — pass the **directory**,
  not the `.wz` file. Subdirectories (`Cap/`, `Hair/`, `Weapon/`, …) and
  root-level imgs (`00002000.img`, …) are both visible through `pkg.get`.
- `region="BMS"` is what this user's pack needs. Use `"GMS"` / `"EMS"` for
  other regions, or omit and the server's `--region auto` will detect.
- `pkg.root.subdirs` returns subdirectory **names** (strings), not nodes.
  Use `pkg.root.child("Cap")` to walk into one.

## Looking up an equip by ID

Equip IDs are 8-digit zero-padded strings. The first 3 digits pick the
category (`wzpy/character.py:50` `_CATEGORY_BY_ID_PREFIX`):

| prefix       | category | path under `data/Character/` |
| ------------ | -------- | ---------------------------- |
| `0000xxxx`   | Body     | `00002000.img` (root)        |
| `0001xxxx`   | Head     | `00012000.img` (root)        |
| `0002/0005`  | Face     | `Face/`                      |
| `0003/0004/0006` | Hair | `Hair/`                      |
| `0100xxxx`   | Cap      | `Cap/`                       |
| `0101/0102/0103` | FaceAcc/Glass/Earring | `Accessory/` |
| `0104xxxx`   | Coat     | `Coat/`                      |
| `0105xxxx`   | Longcoat | `Longcoat/`                  |
| `0106xxxx`   | Pants    | `Pants/`                     |
| `0107xxxx`   | Shoes    | `Shoes/`                     |
| `0108xxxx`   | Glove    | `Glove/`                     |
| `0109xxxx`   | Shield   | `Shield/`                    |
| `0110xxxx`   | Cape     | `Cape/`                      |
| `0121..0160`, `0170` | Weapon | `Weapon/`              |

Use `category_for_id` from the renderer when in doubt:

```python
from wzpy.character import category_for_id, CATEGORY_DIR
cat = category_for_id("01082794")           # "Glove"
sub = CATEGORY_DIR[cat]                     # "Glove" — empty for Body/Head
img = pkg.get(f"{sub}/{cat == 'Body' or cat == 'Head' and '' or sub}/01082794.img"
              if False else f"{sub}/01082794.img")
```

In practice, just write the path directly:

```python
img = pkg.get("Glove/01082794.img")    # subdir + .img
img = pkg.get("00002000.img")          # Body/Head live at root
img.parse()
```

## Walking the property tree

```python
img.parse()
stand1 = img.get("stand1")             # SubProperty
frame0 = stand1.get("0")
for child in frame0.children():
    z      = next((c.value for c in child.children() if c.name == "z"), None)
    origin = next((c.value for c in child.children() if c.name == "origin"), None)
    print(child.name, z, origin)
```

- `children()` only on `WzSubProperty` / `WzImage`. Leaves (`int`, `string`,
  `vector`, …) expose `.value`.
- Anchors live under `map/<name>`: `child.get("map/brow")`.
- Many canvases are UOLs / `_outlink`s. The renderer (`compose`,
  `_collect_part_canvases`) resolves them; raw access via
  `decode_canvas(canvas, region="BMS")` may return a 1×1 placeholder for
  un-resolved UOLs. To get the real bitmap, use a `_Placement.pixel_canvas`
  from `_build_placements`.

## Decoding a canvas to PIL

```python
from wzpy.canvas import decode_canvas
pim = decode_canvas(canvas_property, region="BMS")    # PIL.Image.Image
```

`canvas_property` must be a `WzCanvasProperty` with `_png_length > 0`.
For UOL'd canvases inside `Body`/`Cap`/etc., go through the renderer:

```python
from wzpy.character import CharacterRenderer
r = CharacterRenderer(pkg, region="BMS")
pls, anchors = r._build_placements(
    ["00002000", "00012000", "00030020", "00020000", "01082794"],
    pose="stand1", ear_type="default",
    hide_hair_full=False, hide_hair_set=frozenset(),
    cap_vslot_tokens=frozenset(),
    frame=0, frozen_anchors=None, return_anchors=True,
)
for p in pls:
    if p.equip_id == "01082794":
        bitmap = decode_canvas(p.pixel_canvas, region="BMS")
        print(p.name, p.top_left, bitmap.size)
```

## Running the server

```bash
python run.py data/Character --region BMS
# default: http://127.0.0.1:5000
# --debug for Flask reloader, --port 5001 to move it
```

`run.py` is a thin wrapper around `server.app:main`. It accepts the path
to either a single `.wz` file or a hierarchical pack directory like
`data/Character`. See `server/app.py:2690`.

Endpoints I use most when debugging:

- `GET /api/character/parts/<Category>` — list imgs under a category
- `GET /api/character/compose?ids=00002000,00012000,...&pose=stand1&frame=0`
- `GET /api/character/compose_animation?ids=...` — JSON with 3 base64 PNGs
- `GET /api/character/equip_info/<id>` — z-slots, anchors, vslot tokens

## Self-test pattern (no UI)

Most rendering checks are easier as a Python one-liner against the
package directly. Standard preamble:

```python
PYTHONIOENCODING=utf-8 python -c "
from wzpy.wz_package import WzPackage
from wzpy.character import CharacterRenderer
import hashlib

pkg = WzPackage.open('D:/Git/wz-python/data/Character', region='BMS')
r = CharacterRenderer(pkg, region='BMS')
base = ['00002000', '00012000', '00030020', '00020000']
# ... your check
"
```

Common verifications:

```python
# Single-frame compose to PNG
img = r.compose(base + ["01082794"], frame=0, flip=True)
img.save("D:/Git/wz-python/_dbg.png")    # then Read tool to view

# 3-frame animation (compose_animation returns a list[PIL.Image])
imgs = r.compose_animation(base + ["01003559"])
hashes = [hashlib.sha256(im.tobytes()).hexdigest()[:10] for im in imgs]

# Z-order trace (after _build_placements has already run the sort)
pls, _ = r._build_placements(
    base + ["01082794"], pose="stand1", ear_type="default",
    hide_hair_full=False, hide_hair_set=frozenset(),
    cap_vslot_tokens=frozenset(),
    frame=0, frozen_anchors=None, return_anchors=True,
)
for i, p in enumerate(pls):
    print(i, p.category, p.equip_id, p.name, p.top_left)
```

PNG inspection: write to `D:/Git/wz-python/_dbg_*.png`, then use the
`Read` tool on the absolute path — Read renders PNGs inline. Clean up
debug PNGs (`rm _dbg_*.png`) before committing.

For animation stability checks, crop a region and compare hashes per
frame (e.g. `im.crop((0, 0, im.width, 30))` to isolate the cap region
above the body).

## Working directory and git

- Always pass absolute paths (`D:/Git/wz-python/...`) — `pkg.get` resolves
  relative paths against the pack root, not the cwd, but file I/O does.
- `bash` is the default shell here (Unix syntax: `/dev/null`, forward
  slashes). Never `cd` before git commands; git uses the cwd already.
- Rendering output rounds to integer pixel positions; if a check produces
  `(20, 8)` vs `(20, 7)` across frames, that 1-pixel motion is real and
  visible in the UI, not a rounding artifact.
