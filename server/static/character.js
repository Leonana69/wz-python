"use strict";

// Categories that the server's CharacterRenderer.list_parts() understands.
// Order is the tab order in the UI; "Body" / "Head" first since those drive
// the static frame anchors.
const CATEGORIES = [
  "Body", "Head", "Hair", "Face",
  "Cap", "Coat", "Longcoat", "Pants", "Shoes", "Glove",
  "Cape", "Shield", "Weapon", "FaceAcc", "Glass", "Earring",
];

// Categories that get a Cash / Non-Cash sub-tab strip. Limited to
// gear slots (the wearable equipment categories) — Body / Head are
// always non-cash character bases, and Hair / Face are character
// looks rather than equipment, so they keep the simple flat grid.
const CATEGORIES_WITH_SUBTABS = new Set([
  "Cap", "Coat", "Longcoat", "Pants", "Shoes", "Glove",
  "Cape", "Shield", "Weapon", "FaceAcc", "Glass", "Earring",
]);

// One known sensible default per category — used to seed a "fresh" character
// so the first preview isn't an empty PNG.
const DEFAULTS = {
  Body: "00002000",
  Head: "00012000",
  Hair: "00030020",
  Face: "00020000",
};

// Mirror of the server's category-to-icon-path rules so the client can
// reconstruct candidate paths for a category+id without an extra
// round-trip. Each entry returns an *ordered list* of candidates: the
// thumbnail loader tries them in order, falling back on 404 (some short
// hairs ship only ``default/hair`` and not ``default/hairOverHead``).
const ICON_PATH_RULES = {
  Body:     id => [`${id}.img/stand1/0/body`],
  Head:     id => [`${id}.img/front/head`],
  Hair:     id => [
    `Hair/${id}.img/default/hairOverHead`,
    `Hair/${id}.img/default/hair`,
  ],
  Face:     id => [`Face/${id}.img/default/face`],
  Cap:      id => [`Cap/${id}.img/info/icon`],
  Coat:     id => [`Coat/${id}.img/info/icon`],
  Longcoat: id => [`Longcoat/${id}.img/info/icon`],
  Pants:    id => [`Pants/${id}.img/info/icon`],
  Shoes:    id => [`Shoes/${id}.img/info/icon`],
  Glove:    id => [`Glove/${id}.img/info/icon`],
  Cape:     id => [`Cape/${id}.img/info/icon`],
  Shield:   id => [`Shield/${id}.img/info/icon`],
  FaceAcc:  id => [`Accessory/${id}.img/info/icon`],
  Glass:    id => [`Accessory/${id}.img/info/icon`],
  Earring:  id => [`Accessory/${id}.img/info/icon`],
  Weapon:   id => [`Weapon/${id}.img/info/icon`],
};

function iconPathsFor(category, id) {
  const rule = ICON_PATH_RULES[category];
  return rule ? rule(id) : [];
}

function firstIconPath(category, id) {
  const paths = iconPathsFor(category, id);
  return paths[0] || null;
}

// Wire ``img.src`` to the first candidate, falling back to the next on
// error. After all candidates fail, replace the img with ``onAllFail``.
function setIconWithFallback(img, candidatePaths, onAllFail) {
  let i = 0;
  const tryNext = () => {
    if (i >= candidatePaths.length) {
      onAllFail();
      return;
    }
    img.src = `/api/canvas/${candidatePaths[i]}.png`;
    i++;
  };
  img.addEventListener("error", tryNext);
  tryNext();
}

const state = {
  // category → {id, iconPaths}. Keeps at most one item per slot,
  // matching how the C# AvatarForm de-dupes (a new Cap replaces the
  // old one). ``iconPaths`` is an ordered list of candidate WZ paths
  // for the equip's thumbnail — most categories have one entry; Hair
  // has two (hairOverHead → hair fallback).
  equipped: Object.fromEntries(
    Object.entries(DEFAULTS).map(([cat, id]) => [
      cat, { id, iconPaths: iconPathsFor(cat, id) },
    ])
  ),
  activeTab: "Cap",
  // Cache of part lists keyed by category, populated on demand.
  parts: new Map(),
  // Track the inflight compose request so out-of-order responses don't
  // overwrite a newer preview with stale pixels.
  composeSeq: 0,
  // Pose state — any entry from ``bodyPoses`` (filtered down to
  // weapon-supported poses when a weapon is equipped). Defaults to
  // stand1, the one-handed rest pose.
  pose: "stand1",
  // Poses the equipped Body actually ships, with per-frame delays
  // (ms). Populated on boot via /api/character/poses; the dropdown
  // is built from this list.
  bodyPoses: [],
  // Subset of supported poses that the equipped weapon ships art
  // for. Empty array means "no weapon equipped" → no filtering.
  weaponPoses: [],
  // Cache of weapon → poses so re-equipping doesn't re-fetch.
  weaponPoseCache: new Map(),
  // Ear-type state — which canvas under ``Head/<id>.img/front/`` is
  // composited alongside ``head``. Defaults to "humanEar" (round); some
  // Heads also ship "lefEar" (pointed) and "highlefEar" (tall pointed).
  // The selector is only exposed when the Head has more than one option.
  earType: "humanEar",
  headEarTypes: ["humanEar"],
  headEarCache: new Map(),
  // Cash / Non-Cash filter, kept per-category so switching tabs keeps
  // each one's last selection. Defaults to "non-cash" so the first
  // visit to a tab shows the in-game gear (cash items are gear-shop
  // variants, less likely to be the user's first pick).
  subTab: {},
  // Free-text search query per category. Filters the grid by ID
  // substring (e.g. ``01040``) or name substring (when String.wz
  // is loaded — e.g. ``hanbok``). Empty string disables the filter.
  search: {},
  // Color index per category (currently Hair, Face). Each is the
  // currently-selected color index 0..N-1, default 0. Changing one
  // re-skins every visible thumbnail in that category and, if a
  // member is equipped, swaps the equipped ID to the new variant.
  colorByCategory: { Hair: 0, Face: 0 },
  // Facing direction. ``"left"`` is the canonical orientation in
  // which Character.wz authors the bitmaps; ``"right"`` flips the
  // final composite horizontally on the server (a simple
  // ``Image.FLIP_LEFT_RIGHT``).
  facing: "left",
};

// Per-category color configuration. ``palette`` is a list of
// {name, swatch} pairs in WZ-index order. ``variantId`` rewrites a
// base ID to the variant for the picked color (Hair: last digit;
// Face: hundreds digit). The order of ``palette`` MUST match the WZ
// digit-encoding convention or the swatch labels will lie.
const COLOR_CONFIG = {
  Hair: {
    // Last digit: 0 black, 1 red, 2 orange, 3 yellow, 4 green,
    // 5 blue, 6 purple, 7 brown.
    palette: [
      { name: "Black",  swatch: "#1a1a1a" },
      { name: "Red",    swatch: "#c43c3c" },
      { name: "Orange", swatch: "#dd7b2a" },
      { name: "Yellow", swatch: "#e0c350" },
      { name: "Green",  swatch: "#5b9b4a" },
      { name: "Blue",   swatch: "#3a6fa8" },
      { name: "Purple", swatch: "#7b4a9b" },
      { name: "Brown",  swatch: "#7a4a2c" },
    ],
    variantId: (baseId, color) => baseId.slice(0, -1) + String(color),
  },
  Face: {
    // Hundreds digit: 0 black, 1 blue, 2 red, 3 green, 4 orange,
    // 5 cyan, 6 purple, 7 pink, 8 gray.
    palette: [
      { name: "Black",  swatch: "#1a1a1a" },
      { name: "Blue",   swatch: "#3a6fa8" },
      { name: "Red",    swatch: "#c43c3c" },
      { name: "Green",  swatch: "#5b9b4a" },
      { name: "Orange", swatch: "#dd7b2a" },
      { name: "Cyan",   swatch: "#4dbdc8" },
      { name: "Purple", swatch: "#7b4a9b" },
      { name: "Pink",   swatch: "#e08fb0" },
      { name: "Gray",   swatch: "#888888" },
    ],
    variantId: (baseId, color) =>
      baseId.slice(0, -3) + String(color) + baseId.slice(-2),
  },
};

function variantIdFor(category, baseId, color) {
  const cfg = COLOR_CONFIG[category];
  return cfg ? cfg.variantId(baseId, color) : baseId;
}

function pickAvailableColor(availableColors, requested) {
  if (!availableColors || availableColors.length === 0) return 0;
  if (availableColors.includes(requested)) return requested;
  return availableColors[0];
}

const $img = document.getElementById("char-img");
const $tabs = document.getElementById("char-tabs");
const $subtabs = document.getElementById("char-subtabs");
const $search = document.getElementById("char-search");
const $grid = document.getElementById("char-grid");
const $equipped = document.getElementById("char-equipped");
const $scale = document.getElementById("char-scale");
const $export = document.getElementById("char-export");
const $exportFrames = document.getElementById("char-export-frames");
const $exportGif = document.getElementById("char-export-gif");
const $progress = document.getElementById("char-progress");

// Inflight-fetch counter that drives the bottom progress bar. Fetches
// that hit ``/api/character/*`` (parts list, compose, ear types,
// weapon poses) call ``progressBegin()`` before awaiting and
// ``progressEnd()`` in their finally block; the bar shows whenever
// the counter is > 0 and hides when it returns to 0. Indeterminate —
// no fraction tracked, just "something is happening".
let _progressInflight = 0;
function progressBegin() {
  _progressInflight++;
  if ($progress) $progress.hidden = false;
}
function progressEnd() {
  _progressInflight = Math.max(0, _progressInflight - 1);
  if (_progressInflight === 0 && $progress) $progress.hidden = true;
}
async function trackedFetch(url, init) {
  progressBegin();
  try {
    return await fetch(url, init);
  } finally {
    progressEnd();
  }
}

// ── tabs ───────────────────────────────────────────────────────────
for (const cat of CATEGORIES) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.textContent = cat;
  btn.dataset.category = cat;
  btn.addEventListener("click", () => selectTab(cat));
  $tabs.appendChild(btn);
}

function selectTab(category) {
  state.activeTab = category;
  for (const b of $tabs.querySelectorAll("button")) {
    b.classList.toggle("active", b.dataset.category === category);
  }
  loadCategory(category);
}

// ── part listing ──────────────────────────────────────────────────
async function loadCategory(category) {
  $grid.classList.add("loading");
  // Hide the sub-tab strip until we know the part list — otherwise
  // the sub-tab bar from a previous category lingers under the new
  // tab during the parts fetch.
  renderSubTabs(category, null);
  // Restore this category's saved search query so swapping tabs
  // doesn't lose the user's filter.
  if ($search) $search.value = state.search[category] || "";
  let parts = state.parts.get(category);
  if (!parts) {
    try {
      const resp = await trackedFetch(`/api/character/parts/${category}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const json = await resp.json();
      parts = json.parts || [];
      state.parts.set(category, parts);
    } catch (err) {
      $grid.classList.remove("loading");
      $grid.innerHTML = `<p class="hint">Failed to load: ${err.message}</p>`;
      return;
    }
  }
  renderSubTabs(category, parts);
  renderGrid(category, filterPartsBySubTab(category, parts));
  $grid.classList.remove("loading");
}

// Search input — debounced re-render of the active category's grid
// on each keystroke. Stores the query per-category so toggling tabs
// preserves what the user typed.
if ($search) {
  $search.addEventListener("input", () => {
    const cat = state.activeTab;
    state.search[cat] = $search.value;
    const parts = state.parts.get(cat);
    if (parts) renderGrid(cat, filterPartsBySubTab(cat, parts));
  });
}

function filterPartsBySubTab(category, parts) {
  let filtered = parts;
  if (CATEGORIES_WITH_SUBTABS.has(category)) {
    const wantCash = state.subTab[category] === "cash";
    filtered = filtered.filter(p => Boolean(p.cash) === wantCash);
  }
  const q = (state.search[category] || "").trim().toLowerCase();
  if (q) {
    filtered = filtered.filter(p =>
      p.id.toLowerCase().includes(q) ||
      (p.name && p.name.toLowerCase().includes(q))
    );
  }
  return filtered;
}

function renderSubTabs(category, parts) {
  if (!$subtabs) return;
  $subtabs.innerHTML = "";
  // The wrapping row stays visible regardless — it always carries
  // the search input. ``$subtabs`` only holds the per-category
  // sub-tab buttons, which can legitimately be empty (Body / Head)
  // or populated with TYPE / COLOR controls.
  if (parts === null) return;
  if (COLOR_CONFIG[category]) {
    renderColorSubTabs(category);
    return;
  }
  if (!CATEGORIES_WITH_SUBTABS.has(category)) return;

  const counts = { nonCash: 0, cash: 0 };
  for (const p of parts) (p.cash ? counts.cash++ : counts.nonCash++);

  // Default to "non-cash" the first time a category is visited; preserve
  // a user's prior selection on subsequent visits.
  if (!state.subTab[category]) state.subTab[category] = "non-cash";
  const active = state.subTab[category];

  const label = document.createElement("span");
  label.className = "subtab-label";
  label.textContent = "Type";
  $subtabs.appendChild(label);

  const make = (key, name, count) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.classList.toggle("active", key === active);
    btn.dataset.subtab = key;
    btn.append(document.createTextNode(name + " "));
    const c = document.createElement("span");
    c.className = "subtab-count";
    c.textContent = `(${count})`;
    btn.appendChild(c);
    btn.addEventListener("click", () => selectSubTab(category, key));
    $subtabs.appendChild(btn);
  };
  make("non-cash", "Non-Cash", counts.nonCash);
  make("cash",     "Cash",     counts.cash);
}

function renderColorSubTabs(category) {
  const cfg = COLOR_CONFIG[category];
  if (!cfg) return;
  const active = state.colorByCategory[category] ?? 0;
  const label = document.createElement("span");
  label.className = "subtab-label";
  label.textContent = "Color";
  $subtabs.appendChild(label);
  for (let i = 0; i < cfg.palette.length; i++) {
    const c = cfg.palette[i];
    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.color = String(i);
    btn.classList.add("color-btn");
    btn.classList.toggle("active", i === active);
    btn.title = c.name;
    const swatch = document.createElement("span");
    swatch.className = "color-swatch";
    swatch.style.background = c.swatch;
    btn.appendChild(swatch);
    btn.appendChild(document.createTextNode(c.name));
    btn.addEventListener("click", () => selectColor(category, i));
    $subtabs.appendChild(btn);
  }
}

function selectSubTab(category, key) {
  if (state.subTab[category] === key) return;
  state.subTab[category] = key;
  for (const b of $subtabs.querySelectorAll("button")) {
    b.classList.toggle("active", b.dataset.subtab === key);
  }
  const parts = state.parts.get(category);
  if (parts) renderGrid(category, filterPartsBySubTab(category, parts));
}

function selectColor(category, color) {
  if (state.colorByCategory[category] === color) return;
  state.colorByCategory[category] = color;
  for (const b of $subtabs.querySelectorAll("button.color-btn")) {
    b.classList.toggle("active", Number(b.dataset.color) === color);
  }
  // Re-skin every visible tile in this category to the new color and
  // update the equipped highlight (the equipped ID may shift to the
  // new variant).
  const parts = state.parts.get(category);
  if (parts) renderGrid(category, parts);
  // If a member of this category is equipped, swap to the matching
  // color variant. Pick the requested color when the equipped style
  // ships it; fall back to the first color it does ship. The boot
  // default seeds without ``baseId`` / ``colors``; derive a base
  // from the equipped ID and assume the full palette so the swap
  // still works without waiting for the parts list.
  const eq = state.equipped[category];
  if (eq) {
    const cfg = COLOR_CONFIG[category];
    const baseId = eq.baseId ?? cfg.variantId(eq.id, 0);
    const colors = eq.colors ?? cfg.palette.map((_, i) => i);
    const target = pickAvailableColor(colors, color);
    const newId = cfg.variantId(baseId, target);
    if (newId !== eq.id) {
      eq.id = newId;
      eq.iconPaths = iconPathsFor(category, newId);
      eq.baseId = baseId;
      eq.colors = colors;
      renderEquipped();
      refreshCompose();
    }
  }
}

// Progressive renderer: only mount the first ``BATCH_SIZE`` tiles, then
// add more whenever the bottom sentinel scrolls into view. Mounting all
// 1500+ hair thumbnails up front made the browser stall: even with
// loading="lazy", it still allocates DOM nodes and queues image
// requests. Batching keeps initial paint fast and image fetches
// proportional to what the user actually sees.
const BATCH_SIZE = 80;
const SENTINEL_MARGIN = 200;  // mount-ahead distance below the visible area
let _gridObserver = null;
let _gridFillRaf = 0;

function renderGrid(category, parts) {
  // Tear down any previous observer / RAF chain before swapping the grid.
  if (_gridObserver) {
    _gridObserver.disconnect();
    _gridObserver = null;
  }
  if (_gridFillRaf) {
    cancelAnimationFrame(_gridFillRaf);
    _gridFillRaf = 0;
  }
  $grid.innerHTML = "";
  if (parts.length === 0) {
    $grid.innerHTML = `<p class="hint">No parts in this category.</p>`;
    return;
  }
  const equippedId = state.equipped[category]?.id;
  let mounted = 0;

  const sentinel = document.createElement("div");
  sentinel.className = "char-grid-sentinel";

  const finish = () => {
    sentinel.remove();
    if (_gridObserver) {
      _gridObserver.disconnect();
      _gridObserver = null;
    }
  };

  const mountNext = () => {
    const end = Math.min(mounted + BATCH_SIZE, parts.length);
    const frag = document.createDocumentFragment();
    for (let i = mounted; i < end; i++) {
      frag.appendChild(makeTile(category, parts[i], equippedId));
    }
    $grid.insertBefore(frag, sentinel);
    mounted = end;
    if (mounted >= parts.length) finish();
  };

  // After a batch lands, the sentinel may still be inside the trigger
  // zone (especially on tall viewports where the first 80 tiles don't
  // fill the panel). The IntersectionObserver only fires on
  // state-change boundaries, so it won't re-trigger in that case —
  // instead, walk a RAF chain that keeps mounting until the sentinel
  // falls outside the trigger zone OR all parts are loaded.
  const fillIfVisible = () => {
    _gridFillRaf = 0;
    if (mounted >= parts.length) return;
    const rootRect = $grid.getBoundingClientRect();
    const sentinelRect = sentinel.getBoundingClientRect();
    if (sentinelRect.top < rootRect.bottom + SENTINEL_MARGIN) {
      mountNext();
      _gridFillRaf = requestAnimationFrame(fillIfVisible);
    }
  };

  $grid.appendChild(sentinel);
  mountNext();
  _gridFillRaf = requestAnimationFrame(fillIfVisible);

  if (mounted < parts.length) {
    _gridObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting && mounted < parts.length) {
          mountNext();
          // Keep mounting as long as the sentinel is still in view —
          // covers the case where one batch isn't enough to push it
          // back out of the trigger zone.
          if (!_gridFillRaf) {
            _gridFillRaf = requestAnimationFrame(fillIfVisible);
          }
        }
      }
    }, { root: $grid, rootMargin: `${SENTINEL_MARGIN}px 0px` });
    _gridObserver.observe(sentinel);
  }
}

function makeTile(category, part, equippedId) {
  const tile = document.createElement("div");

  // Hair / Face tiles each represent a style (one entry per
  // dedup-group); the displayed thumb and the equipped ID swap to
  // the variant for the active color. Other categories use the part
  // ID as-is.
  let displayId = part.id;
  let candidates;
  const cfg = COLOR_CONFIG[category];
  if (cfg) {
    const color = pickAvailableColor(part.colors, state.colorByCategory[category] ?? 0);
    displayId = cfg.variantId(part.id, color);
    candidates = iconPathsFor(category, displayId);
  } else {
    candidates =
      part.icon_paths && part.icon_paths.length ? part.icon_paths
      : part.icon_path ? [part.icon_path]
      : iconPathsFor(category, displayId);
  }

  tile.className = "part-tile" + (displayId === equippedId ? " equipped" : "");
  tile.dataset.id = displayId;
  // Tooltip on the bare tile (native browser tooltip for the
  // hover-card-skipping case): name + ID when we have a name from
  // String.wz, otherwise just the ID.
  tile.title = part.name ? `${part.name} (${displayId})` : displayId;

  const thumb = document.createElement("img");
  thumb.alt = displayId;
  thumb.loading = "lazy";
  setIconWithFallback(thumb, candidates, () => {
    // All candidate canvases failed — swap to a textual placeholder so
    // the tile is still clickable and clearly labeled.
    thumb.replaceWith(Object.assign(document.createElement("div"), {
      className: "placeholder", textContent: "no img",
    }));
  });
  tile.appendChild(thumb);

  const pid = document.createElement("div");
  pid.className = "pid";
  pid.textContent = displayId;
  tile.appendChild(pid);

  tile.addEventListener("click", () => {
    const extra = {};
    if (cfg) Object.assign(extra, { baseId: part.id, colors: part.colors });
    if (part.name) extra.name = part.name;
    equipPart(category, displayId, candidates, extra);
  });

  // Hover tooltip with in-game-style stats. Skip Body / Head / Hair /
  // Face (no equipment stats) and skip cash items (the user asked
  // for non-cash equipment specifically).
  if (TOOLTIP_CATEGORIES.has(category) && !part.cash) {
    tile.addEventListener("mouseenter", () => showTooltip(tile, displayId, category));
    tile.addEventListener("mouseleave", hideTooltip);
  }
  return tile;
}

// Equip-slot conflicts: equipping a Longcoat replaces both Coat and
// Pants (since a longcoat covers the entire torso + legs); equipping a
// Coat or Pants replaces any Longcoat. Mirrors MapleNecrocer's AddEqps
// dedupe logic — keeps the equipped list internally consistent so the
// composite never tries to render a longcoat AND a coat at once.
const SLOT_CONFLICTS = {
  Longcoat: ["Coat", "Pants"],
  Coat:     ["Longcoat"],
  Pants:    ["Longcoat"],
};

// ── equipped list / compose ───────────────────────────────────────
async function equipPart(category, id, iconPaths, extra) {
  // Accept either the new candidate list or a legacy single string for
  // backwards compatibility with anything still passing one path.
  const paths = Array.isArray(iconPaths)
    ? iconPaths
    : iconPaths ? [iconPaths]
    : iconPathsFor(category, id);
  state.equipped[category] = { id, iconPaths: paths, ...(extra || {}) };
  for (const conflicting of SLOT_CONFLICTS[category] || []) {
    delete state.equipped[conflicting];
  }
  // Update the equipped-tile highlight without reloading.
  for (const tile of $grid.querySelectorAll(".part-tile")) {
    tile.classList.toggle("equipped", tile.dataset.id === id);
  }
  if (category === "Weapon") {
    await syncWeaponPose(id);
  }
  if (category === "Head") {
    await syncHeadEars(id);
  }
  renderEquipped();
  refreshCompose();
}

function unequipPart(category) {
  // Body / Head can be unequipped but the static composite collapses
  // without them — the preview will be tiny. That's OK; it lets the
  // user explore weird combinations.
  delete state.equipped[category];
  if (category === "Weapon") {
    state.weaponPoses = [];
    renderPoseControls();
  }
  if (category === "Head") {
    state.headEarTypes = ["humanEar"];
    state.earType = "humanEar";
    renderEarControls();
  }
  renderEquipped();
  refreshCompose();
  if (state.activeTab === category) {
    for (const tile of $grid.querySelectorAll(".part-tile")) {
      tile.classList.remove("equipped");
    }
  }
}

async function syncHeadEars(headId) {
  let ears = state.headEarCache.get(headId);
  if (!ears) {
    try {
      const resp = await trackedFetch(`/api/character/ear_types/${headId}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const json = await resp.json();
      ears = Array.isArray(json.ear_types) ? json.ear_types : [];
    } catch (err) {
      console.warn("ear_types fetch failed:", err);
      ears = [];
    }
    state.headEarCache.set(headId, ears);
  }
  // Trust whatever the Head ships. If no ear canvases at all, fall
  // back to a single ``humanEar`` placeholder so the ear-type query
  // string stays well-formed; the renderer will simply find no
  // matching canvas and skip the ear, matching how the real client
  // handles a ``EarType`` with no backing canvas.
  const options = ears.length ? ears.slice() : ["humanEar"];
  state.headEarTypes = options;
  if (!options.includes(state.earType)) {
    // Prefer humanEar when the Head ships it, otherwise pick the
    // first option so the selector and the render stay in sync.
    state.earType = options.includes("humanEar") ? "humanEar" : options[0];
  }
  renderEarControls();
}

async function syncWeaponPose(weaponId) {
  let poses = state.weaponPoseCache.get(weaponId);
  if (!poses) {
    try {
      const resp = await trackedFetch(`/api/character/weapon_poses/${weaponId}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const json = await resp.json();
      poses = Array.isArray(json.poses) ? json.poses : [];
    } catch (err) {
      console.warn("weapon_poses fetch failed:", err);
      poses = [];
    }
    state.weaponPoseCache.set(weaponId, poses);
  }
  state.weaponPoses = poses;
  // Keep the user's current pose if the new weapon supports it; otherwise
  // snap to the weapon's first available pose so the composite doesn't
  // silently fall back to a pose the user can't see selected.
  if (poses.length && !poses.includes(state.pose)) {
    state.pose = poses[0];
  }
  renderPoseControls();
}

async function loadBodyPoses() {
  try {
    const resp = await trackedFetch("/api/character/poses");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const json = await resp.json();
    state.bodyPoses = Array.isArray(json.poses) ? json.poses : [];
  } catch (err) {
    console.warn("poses fetch failed:", err);
    state.bodyPoses = [{ pose: "stand1", delays: [500, 500, 500] }];
  }
}

function renderEquipped() {
  $equipped.innerHTML = "";
  const entries = Object.entries(state.equipped);
  if (entries.length === 0) {
    const li = document.createElement("li");
    li.className = "hint";
    li.textContent = "No items yet — pick parts from the right.";
    $equipped.appendChild(li);
    return;
  }
  for (const [cat, slot] of entries) {
    const li = document.createElement("li");

    const candidates = slot.iconPaths && slot.iconPaths.length
      ? slot.iconPaths
      : iconPathsFor(cat, slot.id);
    if (candidates.length) {
      const thumb = document.createElement("img");
      thumb.className = "eq-thumb";
      thumb.alt = slot.id;
      thumb.loading = "lazy";
      setIconWithFallback(thumb, candidates, () => {
        thumb.replaceWith(Object.assign(document.createElement("div"), {
          className: "eq-thumb eq-thumb-empty",
        }));
      });
      li.appendChild(thumb);
    } else {
      li.appendChild(Object.assign(document.createElement("div"), {
        className: "eq-thumb eq-thumb-empty",
      }));
    }

    const catTag = document.createElement("span");
    catTag.className = "eq-cat";
    catTag.textContent = cat;
    const idSpan = document.createElement("span");
    idSpan.className = "eq-id";
    // Show the display name when the server gave us one, falling
    // back to the bare ID. Always include the ID as ``title`` so
    // the equip-slot tooltip surfaces both for power users.
    idSpan.textContent = slot.name || slot.id;
    if (slot.name) idSpan.title = `${slot.name} — ${slot.id}`;
    const rm = document.createElement("button");
    rm.type = "button";
    rm.className = "eq-remove";
    rm.textContent = "×";
    rm.title = "Remove";
    rm.addEventListener("click", () => unequipPart(cat));
    li.appendChild(catTag);
    li.appendChild(idSpan);
    li.appendChild(rm);
    $equipped.appendChild(li);
  }
}

// ── pose dropdown ──────────────────────────────────────────────────
// Friendly labels for each pose. Anything not listed falls through
// to the raw WZ name so custom packs / future poses still surface
// identifiably.
const POSE_LABELS = {
  stand1: "Stand (1H)",
  stand2: "Stand (2H)",
  walk1: "Walk (1H)",
  walk2: "Walk (2H)",
  alert: "Alert",
  jump: "Jump",
  prone: "Prone",
  proneStab: "Prone Stab",
  sit: "Sit",
  fly: "Fly",
  ladder: "Ladder",
  rope: "Rope",
  heal: "Heal",
  swingO1: "Swing 1H A",
  swingO2: "Swing 1H B",
  swingO3: "Swing 1H C",
  swingT1: "Swing 2H A",
  swingT2: "Swing 2H B",
  swingT3: "Swing 2H C",
  stabO1: "Stab 1H A",
  stabO2: "Stab 1H B",
  stabT1: "Stab 2H A",
  stabT2: "Stab 2H B",
  shoot1: "Shoot Bow",
  shoot2: "Shoot Crossbow",
  shootF: "Shoot Gun",
  dead: "Dead",
};

function visiblePoses() {
  // Start from the body's authored poses (server-discovered). When a
  // weapon is equipped, narrow to the intersection so the user only
  // sees poses that have weapon art too — picking a pose with no
  // weapon canvas would silently render the character with an
  // invisible weapon.
  const all = state.bodyPoses.map(p => p.pose);
  if (!state.weaponPoses.length) return all;
  const wp = new Set(state.weaponPoses);
  return all.filter(p => wp.has(p));
}

function poseDelays(pose) {
  const entry = state.bodyPoses.find(p => p.pose === pose);
  return entry ? entry.delays.slice() : [500];
}

function renderPoseControls() {
  const host = document.getElementById("char-pose");
  if (!host) return;
  host.innerHTML = "";
  const options = visiblePoses();
  if (options.length === 0) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  const label = document.createElement("span");
  label.className = "pose-label";
  label.textContent = "Pose";
  host.appendChild(label);
  const select = document.createElement("select");
  select.id = "char-pose-select";
  for (const p of options) {
    const opt = document.createElement("option");
    opt.value = p;
    opt.textContent = POSE_LABELS[p] || p;
    if (p === state.pose) opt.selected = true;
    select.appendChild(opt);
  }
  // If the current pose is no longer in the option list (just lost
  // weapon support), snap to the first option and re-render so the
  // server and UI agree.
  if (!options.includes(state.pose)) {
    state.pose = options[0];
    select.value = state.pose;
  }
  select.addEventListener("change", () => {
    if (select.value !== state.pose) {
      state.pose = select.value;
      refreshCompose();
    }
  });
  host.appendChild(select);
}

// Friendly labels for the canvas names exposed by ``Head/<id>.img/front/``.
// Anything else falls through to the raw canvas name so custom ears
// (e.g. dataset mods) still show up identifiably.
const EAR_LABELS = {
  humanEar: "Human",
  lefEar: "Elf",
  highlefEar: "High Elf",
};

function renderEarControls() {
  const host = document.getElementById("char-ear");
  if (!host) return;
  host.innerHTML = "";
  if (state.headEarTypes.length < 2) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  const label = document.createElement("span");
  label.className = "ear-label";
  label.textContent = "Ear";
  host.appendChild(label);
  const select = document.createElement("select");
  select.id = "char-ear-select";
  for (const name of state.headEarTypes) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = EAR_LABELS[name] || name;
    if (name === state.earType) opt.selected = true;
    select.appendChild(opt);
  }
  select.addEventListener("change", () => {
    if (select.value !== state.earType) {
      state.earType = select.value;
      refreshCompose();
    }
  });
  host.appendChild(select);
}

// ── hover tooltip ──────────────────────────────────────────────────
const $tooltip = document.getElementById("char-tooltip");

// Categories that get an in-game-style hover tooltip. Body / Head /
// Hair / Face are character bases / cosmetics and have no equip
// stats worth surfacing.
const TOOLTIP_CATEGORIES = new Set([
  "Cap", "Coat", "Longcoat", "Pants", "Shoes", "Glove",
  "Cape", "Shield", "Weapon", "FaceAcc", "Glass", "Earring",
]);

// Cache equip_info responses so re-hovering a tile is instant.
const _equipInfoCache = new Map();
let _tooltipHoverId = null;        // ID currently being hovered
let _tooltipHoverTile = null;      // DOM node currently being hovered

// Weapon type by ID prefix. ``info/sfx`` only carries the swing /
// sound token, which collides across genuinely-different weapon
// types (a 1H sword and a dagger both report ``sfx='swordS'``), so
// the canonical discriminator is the equip ID prefix:
//
//   * 4-digit (``id // 1000``) is checked first — used where a
//     class block sub-classifies on the 5th digit (1212/1213/...,
//     1252/1253/..., 1402/1403/1404).
//   * 3-digit (``id // 10000``) is the fallback for everything else.
//
// 121 / 125 / 140 deliberately have NO 3-digit entry — only their
// 4-digit children — so unknown 4-digit codes within those blocks
// return null rather than a misleading parent label.
const WEAPON_TYPE_BY_PREFIX_4D = {
  1212: "Shining Rod",
  1213: "Bladecaster",
  1214: "Whispershot",
  1215: "Sword",
  1252: "Memorial Staff",
  1253: "Celestial Light",
  1254: "Fan",
  1402: "Two-Handed Sword",
  1403: "Martial Brace",
  1404: "Chakram",
};
const WEAPON_TYPE_BY_PREFIX_3D = {
  122: "Soul Shooter",
  123: "Desperado",
  124: "Whip Blade",
  126: "Psy-limiter",
  127: "Chain",
  128: "Lucent Gauntlet",
  129: "Ritual Fan",
  130: "One-Handed Sword",
  131: "One-Handed Axe",
  132: "One-Handed BW",
  133: "Dagger",
  134: "Katara",
  135: "Secondary Weapon",
  136: "Cane",
  137: "Wand",
  138: "Staff",
  141: "Two-Handed Axe",
  142: "Two-Handed BW",
  143: "Spear",
  144: "Polearm",
  145: "Bow",
  146: "Crossbow",
  147: "Claw",
  148: "Knuckle",
  149: "Gun",
  150: "Shovel",
  151: "Pickaxe",
  152: "Dual Bowguns",
  153: "Hand Cannon",
  154: "Katana",
  155: "Fan",
  156: "Lapis",
  157: "Lazuli",
  158: "Arm Cannon",
  159: "Ancient Bow",
};

function weaponTypeFor(equipId) {
  const n = Number(equipId);
  if (!Number.isFinite(n)) return null;
  return WEAPON_TYPE_BY_PREFIX_4D[Math.floor(n / 1000)]
      ?? WEAPON_TYPE_BY_PREFIX_3D[Math.floor(n / 10000)]
      ?? null;
}

const ATTACK_SPEED_LABELS = {
  2: "Faster (2)", 3: "Faster (3)", 4: "Fast (4)",
  5: "Normal (5)", 6: "Normal (6)",
  7: "Slow (7)",   8: "Slower (8)",  9: "Slower (9)",
};

// In-game-style display name for each ``info/incXXX`` key. Order
// matters — that's the row order in the tooltip.
const INC_FIELDS = [
  ["incSTR",   "STR"],
  ["incDEX",   "DEX"],
  ["incINT",   "INT"],
  ["incLUK",   "LUK"],
  ["incPAD",   "Weapon ATT"],
  ["incMAD",   "Magic ATT"],
  ["incPDD",   "Weapon DEF"],
  ["incMDD",   "Magic DEF"],
  ["incACC",   "Accuracy"],
  ["incEVA",   "Avoidability"],
  ["incMHP",   "MaxHP"],
  ["incMMP",   "MaxMP"],
  ["incSpeed", "Speed"],
  ["incJump",  "Jump"],
];
const REQ_FIELDS = [
  ["reqLevel", "Level"],
  ["reqSTR",   "STR"],
  ["reqDEX",   "DEX"],
  ["reqINT",   "INT"],
  ["reqLUK",   "LUK"],
  ["reqPOP",   "Pop"],
];

async function fetchEquipInfo(equipId) {
  if (_equipInfoCache.has(equipId)) return _equipInfoCache.get(equipId);
  const promise = (async () => {
    try {
      const resp = await trackedFetch(`/api/character/equip_info/${equipId}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const json = await resp.json();
      // Stash the display name on the info object so the tooltip
      // renderer can pull it without a second response field.
      const info = json.info || {};
      if (json.name) info.__name = json.name;
      return info;
    } catch (err) {
      console.warn("equip_info fetch failed:", err);
      return null;
    }
  })();
  _equipInfoCache.set(equipId, promise);
  return promise;
}

async function showTooltip(tile, equipId, category) {
  if (!$tooltip) return;
  _tooltipHoverId = equipId;
  _tooltipHoverTile = tile;
  const info = await fetchEquipInfo(equipId);
  // The user might have moved on while the request was in flight;
  // only render if they're still hovering the same tile.
  if (_tooltipHoverId !== equipId || _tooltipHoverTile !== tile) return;
  if (info === null) return;
  $tooltip.innerHTML = renderTooltipBody(equipId, category, info);
  $tooltip.hidden = false;
  positionTooltip(tile);
}

function hideTooltip() {
  _tooltipHoverId = null;
  _tooltipHoverTile = null;
  if ($tooltip) $tooltip.hidden = true;
}

function renderTooltipBody(equipId, category, info) {
  const sections = [];
  // Header — display name (when String.wz gave us one) + the bare ID
  // underneath, plus the weapon-type label for weapons.
  const nameLine = info.__name
    ? `<h4>${escapeHtml(info.__name)}</h4><div class="tt-meta">${equipId}</div>`
    : `<h4>${equipId}</h4>`;
  let header = nameLine;
  // Resolve the weapon type from the equip ID prefix; fall back to
  // ``info/sfx`` only when the prefix isn't in the lookup table
  // (rare — modded packs / event weapons). For non-weapons the
  // category itself (Cap, Coat, …) is already obvious from context
  // and we leave this line off.
  if (category === "Weapon") {
    const label = weaponTypeFor(equipId) || info.sfx || null;
    if (label) header += `<div class="tt-meta">${escapeHtml(label)}</div>`;
  }
  sections.push(header);

  // Requirements. Show ``reqLevel`` whenever it's present (even 0,
  // since "Lv. 0" is meaningful — anyone can wear it). For stat
  // reqs, only show non-zero values: class-restricted gear ships
  // ``reqSTR=0, reqDEX=0, reqINT=218, reqLUK=0`` and the 0 rows
  // for unused stats just clutter the card.
  const reqRows = REQ_FIELDS
    .filter(([k]) => k === "reqLevel"
      ? info[k] != null
      : Number(info[k] ?? 0) > 0)
    .map(([k, label]) => row(label, info[k], "req"));
  if (reqRows.length) {
    sections.push(section("Required", reqRows.join("")));
  }

  // Stat / combat increases
  const incRows = INC_FIELDS
    .filter(([k]) => Number(info[k] ?? 0) !== 0)
    .map(([k, label]) => {
      const v = info[k];
      const sign = v > 0 ? "+" : "";
      return row(label, `${sign}${v}`, "pos");
    });
  if (incRows.length) {
    sections.push(section("Stats", incRows.join("")));
  }

  // Weapon-only block (attackSpeed). Skip ``info/attack`` per the
  // user's request — its presence is implied by the weapon category.
  if (info.attackSpeed != null) {
    const label = ATTACK_SPEED_LABELS[info.attackSpeed] || `(${info.attackSpeed})`;
    sections.push(section("Weapon", row("Attack Speed", label)));
  }

  // Footer — price (always shown for non-cash equipment, even when 0).
  const priceVal = info.price != null
    ? Number(info.price).toLocaleString() + " mesos"
    : "—";
  sections.push(section("Price", row("Price", priceVal)));

  // If we somehow had no rows except the header, add a hint so the
  // tooltip isn't a confusing empty card.
  if (sections.length === 2 /* header + price */ && info.price == null) {
    sections.push('<div class="tt-empty">No stats available.</div>');
  }
  return sections.join("");
}

function section(label, body) {
  return `<div class="tt-section"><div class="tt-label">${label}</div>${body}</div>`;
}
function row(key, value, valClass) {
  const cls = valClass ? `tt-val ${valClass}` : "tt-val";
  return `<div class="tt-row"><span class="tt-key">${key}</span><span class="${cls}">${value}</span></div>`;
}
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function positionTooltip(tile) {
  // Place the card to the right of the tile when there's room,
  // otherwise to the left. Vertically clamp so it doesn't overflow
  // the viewport at the bottom.
  const margin = 8;
  const tileRect = tile.getBoundingClientRect();
  const ttRect = $tooltip.getBoundingClientRect();
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let left = tileRect.right + margin;
  if (left + ttRect.width > vw - margin) {
    left = tileRect.left - ttRect.width - margin;
  }
  if (left < margin) left = margin;
  let top = tileRect.top;
  if (top + ttRect.height > vh - margin) {
    top = vh - margin - ttRect.height;
  }
  if (top < margin) top = margin;
  $tooltip.style.left = `${left}px`;
  $tooltip.style.top = `${top}px`;
}

// Build the cycle of frame indices to play for a given pose. For
// the in-place rest poses we mirror MapleStory's classic breathing
// loop ``0 → 1 → 2 → 1`` (the third frame is "exhale"; replaying
// frame 1 on the way back avoids a hard cut from peak inhale back
// to neutral). For everything else we just play frames in order
// and loop — that matches how the in-game client cycles walk1,
// swing*, etc.
function previewCycle(pose, frameCount) {
  if ((pose === "stand1" || pose === "stand2") && frameCount === 3) {
    return [0, 1, 2, 1];
  }
  return Array.from({ length: frameCount }, (_, i) => i);
}

// Blob URLs for each rendered frame, indexed 0..N-1. We hold onto
// them between refreshes so Save PNG can grab frame 0 directly and
// so changing scale / facing doesn't briefly drop the preview.
let _previewFrameUrls = [];
let _previewTimer = 0;
let _previewCycleIdx = 0;
let _previewCycle = [0];
let _previewDelays = [500];

function _stopPreviewAnimation() {
  if (_previewTimer) {
    clearTimeout(_previewTimer);
    _previewTimer = 0;
  }
}

function _revokePreviewFrames() {
  for (const u of _previewFrameUrls) if (u) URL.revokeObjectURL(u);
  _previewFrameUrls = [];
}

async function refreshCompose() {
  const ids = Object.values(state.equipped).map(s => s.id);
  if (ids.length === 0) {
    _stopPreviewAnimation();
    _revokePreviewFrames();
    $img.removeAttribute("src");
    return;
  }
  const seq = ++state.composeSeq;
  const scale = $scale.value || "2";
  // Single-request fetch returns frames 0/1/2 as base64 PNGs all at
  // the SAME canvas dimensions and with the navel at the same
  // image-space pixel — without that shared bbox the body bitmap's
  // per-frame leans (the breathing motion) push hair / cap / weapon
  // around because they're anchored relative to the body's navel,
  // and the cycling preview wobbles.
  const url =
    `/api/character/compose_animation?ids=${ids.join(",")}` +
    `&pose=${encodeURIComponent(state.pose)}` +
    `&ear=${encodeURIComponent(state.earType)}` +
    (state.facing === "right" ? "&flip=1" : "") +
    `&scale=${scale}&_=${seq}`;
  try {
    const resp = await trackedFetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (seq !== state.composeSeq) return;  // a newer compose superseded us
    if (!Array.isArray(data.frames) || data.frames.length === 0) return;
    const blobs = data.frames.map(b64 => {
      const bin = atob(b64);
      const arr = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      return new Blob([arr], { type: "image/png" });
    });
    _stopPreviewAnimation();
    _revokePreviewFrames();
    _previewFrameUrls = blobs.map(b => URL.createObjectURL(b));
    // Per-frame delays: prefer the server's authored delays so each
    // pose plays at its native tempo (walk1=180ms, swing has
    // variable delays, prone=100ms one-shot). Fall back to a 500ms
    // breathing tempo if the server didn't include them.
    _previewDelays = Array.isArray(data.delays) && data.delays.length
      ? data.delays
      : Array.from({ length: _previewFrameUrls.length }, () => 500);
    _previewCycle = previewCycle(
      data.resolved_pose || state.pose, _previewFrameUrls.length,
    );
    _previewCycleIdx = 0;
    $img.src = _previewFrameUrls[_previewCycle[0]];
    // Single-frame poses (prone / sit / jump / dead) don't animate;
    // skip the timer entirely.
    if (_previewCycle.length > 1) {
      const tick = () => {
        _previewCycleIdx = (_previewCycleIdx + 1) % _previewCycle.length;
        const frame = _previewCycle[_previewCycleIdx];
        $img.src = _previewFrameUrls[frame];
        _previewTimer = setTimeout(tick, _previewDelays[frame] || 500);
      };
      _previewTimer = setTimeout(tick, _previewDelays[_previewCycle[0]] || 500);
    }
    // Sync the dropdown to whatever the server actually used
    // (auto-detect path).
    if (data.resolved_pose && data.resolved_pose !== state.pose
        && visiblePoses().includes(data.resolved_pose)) {
      state.pose = data.resolved_pose;
      renderPoseControls();
    }
  } catch (err) {
    console.warn("compose failed:", err);
  }
}

// ── controls ──────────────────────────────────────────────────────
$scale.addEventListener("change", refreshCompose);
for (const radio of document.querySelectorAll('input[name="char-facing"]')) {
  radio.addEventListener("change", () => {
    if (radio.checked && state.facing !== radio.value) {
      state.facing = radio.value;
      refreshCompose();
    }
  });
}
$export.addEventListener("click", async () => {
  // Save PNG always exports the canonical frame 0 — the user's
  // hovered preview could be on any frame of the breathing cycle.
  // We fetch a fresh single-frame compose instead of reusing
  // ``_previewFrameUrls[0]`` because the cycling preview frames are
  // padded to the union bbox of all three frames (so the body
  // doesn't wobble); the export should be tightly cropped to the
  // actual content for a clean-edged PNG.
  const ids = Object.values(state.equipped).map(s => s.id);
  if (ids.length === 0) return;
  const scale = $scale.value || "2";
  const url =
    `/api/character/compose?ids=${ids.join(",")}` +
    `&pose=${encodeURIComponent(state.pose)}` +
    `&ear=${encodeURIComponent(state.earType)}` +
    (state.facing === "right" ? "&flip=1" : "") +
    `&frame=0` +
    `&scale=${scale}`;
  try {
    const resp = await trackedFetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = `character_${ids.join("-") || "empty"}.png`;
    a.click();
    // Revoke after the click handler returns so the browser has a
    // chance to grab the bytes.
    setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
  } catch (err) {
    console.warn("export failed:", err);
  }
});

// Shared launcher for the two animation exports — same query params
// as ``compose_animation`` so the bundle matches what the preview is
// already showing.
async function _downloadAnimation(endpoint, suffix) {
  const ids = Object.values(state.equipped).map(s => s.id);
  if (ids.length === 0) return;
  const scale = $scale.value || "2";
  const url =
    `/api/character/${endpoint}?ids=${ids.join(",")}` +
    `&pose=${encodeURIComponent(state.pose)}` +
    `&ear=${encodeURIComponent(state.earType)}` +
    (state.facing === "right" ? "&flip=1" : "") +
    `&scale=${scale}`;
  try {
    const resp = await trackedFetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = `character_${ids.join("-") || "empty"}_${state.pose}.${suffix}`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
  } catch (err) {
    console.warn(`${endpoint} failed:`, err);
  }
}

$exportFrames.addEventListener("click", () => {
  _downloadAnimation("export_frames", "zip");
});
$exportGif.addEventListener("click", () => {
  _downloadAnimation("export_gif", "gif");
});

// ── boot ──────────────────────────────────────────────────────────
(async function boot() {
  selectTab(state.activeTab);
  // Pose dropdown is populated from the body's authored poses, so
  // we need that list before the first paint.
  await loadBodyPoses();
  // If a weapon was seeded (none in DEFAULTS today, but kept for
  // future-proofing), sync the pose dropdown before the first compose.
  if (state.equipped.Weapon) {
    await syncWeaponPose(state.equipped.Weapon.id);
  }
  if (state.equipped.Head) {
    await syncHeadEars(state.equipped.Head.id);
  }
  renderPoseControls();
  renderEarControls();
  renderEquipped();
  refreshCompose();
})();
