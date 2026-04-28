# wz-python

A Python reader for MapleStory `.wz` archives plus a small Flask UI to
browse the contents in your web browser. The parser is implemented from
scratch against the format documentation in
`Harepacker-resurrected/docs/wz-format/` (no `.NET` runtime required).

## What it supports

- Legacy 32-bit WZ container format (`PKG1` header, nested directories,
  embedded `.img` files).
- Region encryption: `GMS`, `EMS`, `BMS` (and CLASSIC, which is BMS).
- Auto-detection of the MapleStory patch version via header check + scoring.
- All common IMG property types: `Null`, `Short`, `Int`, `Long`, `Float`,
  `Double`, `String`, `Vector`, `SubProperty`, `Canvas`, `Sound`, `UOL`,
  `Convex`.
- Canvas decoding for formats `1` (ARGB4444), `2` (ARGB8888), `3`
  (down-sampled ARGB8888), `257` (ARGB1555), `513` (RGB565), `517`
  (down-sampled RGB565), including the listWz XOR-then-zlib payload variant.

Out of scope for now (the format docs cover them but they require more code):

- 64-bit `Data/<Category>/<file>_NNN.wz` directory layout
- `_Canvas` outlink resolution
- `.ms` Snowcrypt pack files (v220+)
- `.nm` MapleStoryN files

## Setup

```sh
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
```

## Run

```sh
python run.py path/to/Mob.wz
# then open http://127.0.0.1:5000
```

Useful flags:

```sh
python run.py path/to/Item.wz --region EMS
python run.py path/to/Map.wz --version 83        # skip auto-detect
python run.py path/to/UI.wz  --host 0.0.0.0 --port 8000
```

## Library use

```python
from wzpy import WzFile
from wzpy.canvas import decode_canvas

with WzFile.open("Mob.wz", region="GMS") as wz:
    print("detected version:", wz.version)
    img = wz.root.get("0100100.img")        # WzImage
    stand = img.get("stand/0")              # WzCanvasProperty
    decode_canvas(stand, region="GMS").save("mob.png")
```

## Project layout

```
wzpy/                  parser package
  crypto.py            AES key generation, region IVs, version hashing
  reader.py            WZ binary reader (compressed ints, encrypted strings)
  wz_file.py           WzFile + WzDirectory tree
  wz_image.py          WzImage (lazy property tree parsing)
  properties.py        IMG property classes + parser
  canvas.py            PNG / pixel-format decoding
server/                Flask UI
  app.py               routes + JSON API
  templates/index.html shell page
  static/              style and tiny vanilla-JS client
run.py                 convenience entry point
```

## Reference

- `Harepacker-resurrected/docs/wz-format/wz-file-overview.md`
- `Harepacker-resurrected/docs/wz-format/wz-format-history.md`
- `Harepacker-resurrected/docs/wz-format/canvas-outlink-system.md`

## License

MIT (matches the upstream HaSuite project).
