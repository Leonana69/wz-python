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

Skill **stats** (cooldown, attack range, …) from the body — see
`### Cracked further` below:

```python
from wzpy import open_wz
c = open_wz("data/Packs/Skill_00000.ms")        # -> MsContainer
img = c.root.get("Skill/000.img")               # a job-group stat img
print(img.get("skill/0001001/level/1/cooltime").value)        # 120
trees = c.skill_imgs()                           # {stem: WzSubProperty}, all imgs
c.close()

c = open_wz("data/Packs/Skill_00007.ms")
img = c.root.get("Skill/520.img")
print(img.get("skill/5201017/common/lt").value)               # (-570, -200)  attack-range box
print(img.get("skill/5201017/common/rb").value)               # (0, 150)
c.close()
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

### Cracked further: the body WZ **property tree** (skill stats) is parseable

`wzpy/ms_wz.py` (`parse_skill_imgs`) recovers the actual **stat/property data**
from `Skill_*.ms` — `cooltime`, `lt`/`rb` (attack-range box), `mpCon`, `damage`,
`mobCount`, `attackCount`, `maxLevel`, `level/<n>/…`, `common/…` — as navigable
`WzSubProperty` trees, one per job-group img, **without** decrypting the header.

How the body decodes (the previously-"open" indirect-offset puzzle, solved):

- The body is standard MapleStory WZ property serialization (same tags as
  `wzpy/properties.py`) under the **BMS zero-key** string cipher (`byte ^
  (0xAA+i)`). Verified by hand against skill `0001227`'s `common` block: tag-9
  Property → `block_size` → ext-type → reserved → `count` → children, closing
  exactly on the computed block end. Real values round-trip — e.g. `0001000`
  (Three Snails): `level/{1,2,3}` `mpCon` 3/5/7, `fixdamage` 10/25/40;
  `0001001` (Recover): `cooltime` 120, `time` 30, `x` 4/8/12.
- **Three string-block markers**, and the indirect bases differ by marker:
  - `0x00`/`0x73` inline → zero-key string in place.
  - `0x1b` → a **global** type-name pool in the encrypted header, referenced by a
    tiny *constant* offset. We never decrypt it; the offsets that recur with
    huge, structurally-consistent counts across every file map directly:
    **`1`→Property, `44`→Canvas, `70`→Shape2D#Vector2D** (the `lt`/`rb`/`origin`
    vectors). Offset 70 was the key miss in the first cut — names resolved but
    vector *coordinates* were skipped until it was mapped. Other (file-specific)
    pool offsets are dispatched by **structure** at parse time
    (`_dispatch_unknown`: the type whose framing consumes exactly the block).
  - `0x01` → a **per-img** back-reference: target = `img_base + offset`.
    `img_base` (the WZ `img.Offset`) is recovered per img by `_solve_base`: the
    base making the img's refs resolve to the most *distinct* real property names
    (diversity, not raw hit count — a wrong base piles many refs onto one or two
    strings). Large offsets land in the zero-key body (resolve); small offsets
    land in the per-img **encrypted name-table** (don't — see below).
- Skills are located by their inline 7-/8-digit id followed by a tag-9 byte and
  grouped by `skillId[:-4]` (`5201001` → img `520`). An img is a *contiguous*
  file region, but the same id recurs far away as a cross-reference (inside other
  jobs' `skillList`s); each stem's anchors are therefore split into **proximity
  clusters** and a base solved per cluster — mixing a stray cross-ref's refs into
  the solve corrupts the base and makes every `0x01` name in the img fail. A
  second pass recovers skills whose id is itself a `0x01` ref (deduped). The
  first skill of each img has its *name* in the encrypted table, so it's the one
  entry that may be missed; everything else parses in full.

**The per-img encrypted name-table.** Each img begins with an encrypted region
holding a *pool of property-name strings* (`common`, `maxLevel`, the type pool,
icon labels) referenced by the small `0x01`/`0x1b` offsets; the rest of the names
and **all values** are zero-key in the body. This table resists cracking from the
data files alone: not the zero-key cipher, not any region WZ key (GMS/EMS/BMS),
and **per-img keyed** — an img's encrypted `Property` bytes occur exactly once in
the file, so there's no shared/relative keystream to exploit, and the body being
plaintext rules out a global stream. The key derivation lives in the game client
(same shape as the Snow2 `.ms` per-image keys), so it's out of reach here. Values
(`lt`/`rb`/`cooltime`/`damage`/`mpCon`) are unaffected — only some *names* are
hidden. To keep the tree navigable, `_infer_block_names` labels the unresolved
blocks by structure (never overriding a file-sourced name): a block with `lt`/`rb`
or a level-formula → `common`/`PVPcommon`; numeric `0/1/2…` sub-levels → `level`;
canvases after `icon` → `iconMouseOver`/`iconDisabled`. Field names *inside* an
encrypted table (which `_ref_<n>` is `mpCon` vs `damage`) stay `_ref_<n>`.

**Coverage** (verified across all 9 `Skill_*.ms`): **14.7k skills, 0 parse
errors**, ~3 s/file (file 8 ~6 s). After inference, **~94 %** of attack-range
blocks carry a real name (`common`/`level`); `lt`/`rb`/`cooltime` and all values
are correct throughout. The trees are wired into `MsContainer` beside `_Canvas`
(`Skill/<stem>.img/skill/<id>/…`) — navigable through `open_wz` and the web tree
browser, e.g. `Skill/1510.img/skill/15101021/common/lt` → `(-400, -130)`.
`Skill_00002` has no inline skill defs and `Skill_00005` is mostly references —
both recover less (different layout, future work).

### Still open

- **Canvas pixels (sprites)**: the per-mob bitmaps live in the `_Canvas` tree the
  `_outlink`s point to, but the canvas *pixel* payload is **not** WZ zlib (no
  valid zlib stream decompresses) — it's an unidentified codec, so raw sprite
  decoding isn't implemented. Structure, names, stats and skeletons are all
  recovered; pixels are the remaining frontier.
- **Per-img encrypted name-table / header cipher**: the per-img name pool (and
  the file's TOC) uses an **unidentified per-img-keyed cipher** — confirmed not
  zero-key, not any region WZ key, and not a shared/global keystream (an img's
  encrypted `Property` signature occurs exactly once file-wide). The key
  derivation appears to live in the game client (cf. the Snow2 per-image keys),
  so it can't be recovered from the `.ms` files alone. The body parser sidesteps
  it; block names are recovered by structural inference, but the field names
  inside these tables (and the exact img names/offsets) stay hidden. Cracking it
  would need reversing the client's key schedule.
- **Mob/Npc stat trees**: `ms_wz` anchors on skill-ids; the same body grammar
  holds for `Mob_*.ms` (`maxHP`, `PADamage`, …) but a mob-id anchor isn't wired
  up yet.
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
