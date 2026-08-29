# v83 Mob Browser

The Mob Browser targets the legacy MapleStory v83 data in `data/v83`.

Run it with the complete folder:

```powershell
python run.py --mob data/v83 --region GMS
```

Then open `http://127.0.0.1:5000/mob`. Search by mob name or numeric ID,
select any authored action, optionally flip its facing direction, and browse
the mob's stats and dropped items.

## Data sources

The mode joins four v83 archives:

- `Mob.wz` supplies mob stats, linked sprites, actions, frames, and delays.
- `String.wz/Mob.img` supplies searchable mob names.
- `String.wz/MonsterBook.img/<mob>/reward` supplies the client-authored drop
  list, while the other String images supply item names.
- `Item.wz` and `Character.wz` supply ordinary-item and equipment icons.

Animations are returned as looping animated PNGs, preserving the authored
frame delays and a stable origin-aligned canvas across frames.

The client files do not contain authoritative server drop rates or item
quantities. The browser therefore labels the displayed items as Monster Book
rewards and does not fabricate percentages. Mobs absent from
`MonsterBook.img` are shown as having no client-side drop data.

## API

- `GET /api/mob/search?q=<name-or-id>&limit=40`
- `GET /api/mob/<id>`
- `GET /api/mob/<id>/animation.png?action=move&flip=0`
- `GET /api/mob/item/<item-id>/icon.png`

## Verification

```powershell
python -m unittest tests.test_mob -v
```
