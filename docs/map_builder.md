# Map Builder

The Map Builder is the first map-rendering milestone: it targets the legacy
MapleStory map schema and is verified against `data/v83`.

Run it with the complete v83 folder:

```powershell
python run.py --map data/v83 --region GMS
```

Then open `http://127.0.0.1:5000/map`. The builder provides map-name/ID search,
eight-layer visibility controls, frame-time selection for animated assets,
background/life/reactor switches, portal/foothold/rope editor overlays, the
authored minimap, and PNG export.

## Ported behavior

The compositor in `wzpy/map.py` follows Harepacker/HaCreator's `MapLoader`,
`BoardItemsManager`, and `MapSimulator` behavior:

- resolve linked map aliases through `info/link`;
- load tiles from `Map/Tile/<tS>.img/<u>/<no>`;
- load objects from `Map/Obj/<oS>.img/<l0>/<l1>/<l2>`;
- preserve eight map layers and the authored object/tile z ordering;
- place canvases by their `origin` and apply Harepacker's anchor-preserving
  horizontal flip shift;
- select animated frames from their authored delays;
- load mobs, NPCs, and reactors through their own WZ components and `info/link`;
- render regular/tiled/moving backgrounds, editor portal markers, footholds,
  and ropes/ladders.

Map bounds prefer `info/VR*`, then `miniMap` dimensions/center, then a foothold
derived fallback. Large maps can be rendered at a fractional scale, or library
callers can pass a `MapBounds` viewport.

## Modern-client boundary

Scene composition is deliberately separated from source discovery. A
`MapRenderer` accepts logical Map/String/Mob/Npc/Reactor sources, whether each
source is a legacy `WzFile` or a hierarchical `WzPackage`. The next modern-MS
phase therefore belongs in component/image resolution and modern asset
decoding. Spine map objects/backgrounds and modern per-map schema additions are
not claimed by this v83 milestone.

## Verification

```powershell
python -m unittest tests.test_map -v
```

The tests use `data/v83`, check map-name lookup and linked maps, assert known
board counts for map `100000001`, compose its scene, decode its authored
minimap, and exercise the Flask builder/render endpoints.
