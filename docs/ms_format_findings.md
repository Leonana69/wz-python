# `.ms` file support — implementation notes & reverse-engineering findings

## Summary

Two distinct things are documented here:

1. **The MapleLib / Elem8100 Snow2 `.ms` format** — implemented and verified
   against MapleLib's own C# code. Code: `wzpy/snow2.py`, `wzpy/ms_file.py`,
   `wzpy/_snow2_tables.py`.
2. **The `data/Packs/*.ms` test files** — these are **NOT** the Snow2 format.
   They turned out to be **Spine 2.1.27 skeleton containers**, now parsed by
   `wzpy/ms_spine.py` (`Mob_*.ms`: 81/81 skeletons parse byte-exact). Findings
   and remaining gaps below.

---

## 1. The Snow2 `.ms` format (implemented)

This is the format MapleLib's `WzLib/MSFile/WzMsFile.cs` reads, ported from
Elem8100's `WzComparerR2.WzLib/Ms_File.cs`. It wraps WZ `.img` images in a
Snow2-encrypted archive:

- **Snow2 stream cipher** (additive: `plain = cipher - keystream` per 32-bit LE
  word). Ported in `wzpy/snow2.py`; the constant tables are extracted
  mechanically from the C# source by `wzpy/_gen_snow2_tables.py` into
  `wzpy/_snow2_tables.py` (no hand-transcription).
- **Header**: a filename-derived random prefix, an XOR-obfuscated salt, then a
  Snow2-encrypted `{hash:i32, version:u8==2, entryCount:i32}`.
- **Entry table**: Snow2-encrypted records `{name, size, sizeAligned,
  blockIndex, entryKey[16], ...}`, keyed separately.
- **Image payloads**: page-aligned (1024) blocks, Snow2-encrypted with a
  per-image key (salt-hash + name + entry-key); the first 1024 bytes are
  double-encrypted.

### Verification

The cipher and every key-derivation path were checked **byte-for-byte against
MapleLib's actual C# code** (a small harness over the real
`Snow2CryptoTransform.cs`, see commit history / scratchpad):

| Path | Status |
| ---- | ------ |
| Snow2 keystream vs reference `snow2_fast.c` and MapleLib | ✅ identical |
| salt decode + header key (`DeriveSnowKey`, header) | ✅ identical |
| entry key (`DeriveSnowKey`, entry) | ✅ identical |
| image key (`DeriveImgKey`) | ✅ identical |
| payload encrypt/decrypt (double first 1024, single rest) | ✅ round-trips, cipher head+tail match C# |

So `MsPackage.open(path)` correctly reads genuine Snow2 `.ms` files. It is wired
into `wzpy.open_wz` (any `.ms` file, or a folder containing `.ms` files,
dispatches to `MsPackage`; always read-only, always BMS).

---

## 2. `data/Packs/*.ms` are a different format

Running MapleLib's **own** C# `ReadHeader` on `Mob_00000.ms` yields
`version = 243`, `entryCount = 1939311213`, header-hash check **fails** — i.e.
MapleLib itself cannot read these files (and our port reproduces the identical
result). So this is not a bug in either implementation; the files are simply a
different container.

### What they actually are (observed)

- **Not encrypted** as a whole: meaningful ASCII appears in the clear at
  arbitrary (non-block-aligned) offsets — e.g. UOL paths like
  `Mob.img/8881000/hitidle`, attachment names like `03_hit_idle2`, property
  names like `origin`. A whole-file block cipher (AES-ECB etc.) is ruled out:
  the most common 16-byte block recurs 322 035× but its offsets are uniformly
  distributed mod 16 (≈20 000 per residue), so there is no block alignment.
- **Not zlib/deflate/lzma/bz2**: the frequent `78 9c` bytes are coincidental,
  not zlib headers (they fail to inflate, raw or zlib-framed).
- **A structured big-endian binary serialization**:
  - Length-prefixed ASCII strings, `[N][N-1 chars]` (verified on `origin`,
    `bg`, `Mob.img/8881000/hitidle`, `03_hit_idle2`).
  - **Big-endian** floats (`3f800000`=1.0, `3fc00000`=1.5) and int16 scalars —
    note WZ is little-endian, so this is a different serializer.
  - **Spine** skeleton data (the `spine` magic appears 15×; mob IDs like
    `8881000` with bezier-looking float runs) → a Spine-animation-based
    MapleStory variant (mobile / non-PC client).
  - A pervasive 16-byte "default value" record
    `00 0b 00 00 00 00 78 9c 62 60 00 00 00 00 ff ff` repeated ~322k times.
  - Embedded high-entropy blobs (compressed bitmaps / textures; a couple of
    JPEG and PVR magics appear).
- A `Packs.ini` sibling lists `Category|maxIndex` (`Mob|3`, `Skill|8`), matching
  the split files `Mob_00000.ms … Mob_00003.ms`, `Skill_00000.ms …`.

### Cracked: it's a Spine 2.1.27 skeleton container

The big-endian "serializer" is **Spine binary** (the `[N][N-1 chars]` strings,
big-endian floats and optimize-positive varints are exactly Spine's binary
encoding; `2.1.27` is a Spine runtime version, and MapleLib bundles
`spine-runtimes-2.1.25`). `Mob_*.ms` files are containers of **Spine 2.1.x
binary skeletons** plus texture/atlas blobs.

Container framing (per skeleton entry):

```
56 c3 ce 11 bf 01 00 aa 00 55 59 5a 12 01 00 01 00   17-byte signature
<u32 little-endian skeleton length>                  declared length
<same u32 again>
01 00  08 00 00 00                                   6 bytes
<Spine 2.1 binary skeleton: [len+1]hash [len+1]"2.1.27" be-float w, h, ...>
```

`wzpy/ms_spine.py` implements this:

- `MsSpineContainer.open(path)` mmaps the file, finds every entry signature, and
  parses each skeleton.
- `read_skeleton(...)` is a faithful port of
  `spine-runtimes-2.1.25/.../SkeletonBinary.cs ReadSkeletonData` (bones, IK,
  slots, skins/attachments — region/boundingbox/mesh/skinnedmesh — events, and
  all animation timelines incl. curves/FFD/draw-order/event).

**Verification:** all 81 skeletons in `Mob_00000.ms` parse with the byte count
consumed **exactly equal** to the declared skeleton length (`consumed == size`
for 81/81), so the parse is byte-accurate. Recovered data is real: dimensions
(254×270, 225×239…), bone names (`root`, `origin`, `bg`, `shimma`, …), slot/
attachment names (`effect/big_light_01`, `body_02`, …), and animation names
(`01_stand_1`, `02_move1_1`, `01`, `02`, …).

### Cracked further: the container is a WZ Mob/Skill tree

The space "between skeletons" is not opaque — it is a **WZ property tree** using
the standard MapleStory WZ string cipher (`cipher[i] ^ (0xAA + i)`) and WZ
property tags. Decoding confirms exact WZ type names — `Canvas`,
`Shape2D#Vector2D`, `origin`, `_outlink`, `UOL`, `Sound_DX8` — and mob stat
fields (`maxHP`, `maxMP`, `speed`, `PADamage`, …). So each `.ms` is a
MapleStory **Mob.wz / Skill.wz**-style tree:

- **Canvas mobs** — animations (`stand`/`move`/`hit1`/`die1`/`attack*`/…) whose
  frames are 1×1 placeholder canvases carrying `_outlink` / `UOL` references like
  `Mob/_Canvas/<id>.img/<anim>/<frame>` (the same `_outlink` model the Character
  packs use). The recurring `00 0b 00 00 00 00 78 9c 62 60 00 00 00 00 ff ff`
  16-byte record (322 k×) is one such 1×1 placeholder canvas.
- **Spine mobs** — the embedded Spine 2.1.27 skeletons above.
- **Boss patterns / skill icons** — deeper trees, e.g.
  `Mob/BossPattern/_Canvas/BossChampionRaid.img/1040/00/000/skill1/6`,
  `Skill/_Canvas/3001.img/skill/30010112/icon`.

`wzpy/ms_container.py` (`MsContainer`) recovers the full structure of **every**
file by scanning the WZ-encoded paths (fast: it searches for the encoded
category prefix, e.g. `Mob/` → `e7 c4 ce 82`, then decodes only at real sites)
and stitches in the Spine skeletons. Verified across all 13 files: **5944
`_Canvas` imgs + 150 Spine skeletons** total, each file in 0.3–3 s.

### Usage

```python
from wzpy import open_wz                 # auto-detects the container
c = open_wz("data/Packs/Mob_00000.ms")   # -> MsContainer
print(c.summary())                       # imgs / spine / paths / animations
for img, subs in list(c.imgs.items())[:5]:
    print(img, sorted(subs)[:8])         # e.g. 0100100 -> ['die1/1','stand/0',…]
for sk in c.skeletons:                   # Spine mobs
    print(sk.hash, sk.version, sk.width, sk.height, sk.animations)
c.close()

# Raw Spine bytes for an external Spine 2.1 runtime:
from wzpy import MsSpineContainer
s = MsSpineContainer.open("data/Packs/Mob_00000.ms")
raw = s.raw_skeleton_bytes(s.entries[0])
```

(`open_wz` returns an `MsContainer` for these Pack files — a structure inventory
+ skeletons, **not** a `WzPackage`. For a genuine Snow2 `.ms` it still returns
`MsPackage`.)

**Web browser.** The server reads `.ms` files too: `python run.py
data/Packs/Mob_00000.ms` (or the welcome page's *Browse File…* / *Open*, now
`.ms`-aware) loads it and the tree browser navigates a synthetic WZ view —
`<Category>/_Canvas/<img>.img/<anim>/<frame>` for canvas mobs and
`_Spine/<NNN>_<hash>.img/{info,bones,slots,animations}` for Spine mobs.
`MsContainer.root` (in `wzpy/ms_container.py`) builds that tree lazily; canvas
leaves carry no pixels (codec unidentified), so it's a structure view.

### Still open

- **Canvas pixels (sprites)**: the per-mob bitmaps live in the `_Canvas` tree the
  `_outlink`s point to, but the canvas *pixel* payload is **not** WZ zlib (no
  valid zlib stream decompresses) — it's an unidentified codec, so raw sprite
  decoding isn't implemented. Structure, names, stats and skeletons are all
  recovered; pixels are the remaining frontier.
- **Per-img indirect-offset base**: most strings decode inline; the WZ
  `0x1b`/`0x01` indirected-string base differs per img and has format-specific
  quirks (the `Property` sub-property type name is never stored), so a fully
  navigable wzpy `WzImage` parse isn't wired up — the path scan recovers the
  same logical tree without it.
- **Textures/atlases** (Spine mobs): the blobs between skeletons are texture
  data; decoding + atlas mapping (and actual Spine *rendering*, which is a
  Spine-runtime job, not wzpy's WZ pipeline) is not implemented.
- **Skeleton-less files are still fully structured**: the entry-signature count
  matches the Spine version-string count exactly in every file, so detection is
  reliable. Several files contain **no** skeletons (`Mob_00002/00003` are boss
  patterns, most `Skill_*` are skill trees; only `Skill_00001` has 5) — but they
  are the *same* WZ-tree container and `MsContainer` reads their structure all
  the same. Verified skeleton totals: `Mob_00000` 81, `Mob_00001` 64,
  `Skill_00001` 5 — **150 skeletons, all byte-exact**.
