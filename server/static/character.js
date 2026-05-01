"use strict";

// Categories that the server's CharacterRenderer.list_parts() understands.
// Order is the tab order in the UI; "Body" / "Head" first since those drive
// the static frame anchors.
const CATEGORIES = [
  "Body", "Head", "Hair", "Face",
  "Cap", "Coat", "Longcoat", "Pants", "Shoes", "Glove",
  "Cape", "Shield", "Weapon", "FaceAcc", "Glass", "Earring",
];

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
  // Pose state — "stand1" (one-handed) or "stand2" (two-handed). Driven
  // by the equipped weapon: weapons that ship only one pose lock the
  // toggle, weapons that ship both expose it. Stays "stand1" when no
  // weapon is equipped.
  pose: "stand1",
  weaponPoses: ["stand1"],  // poses the currently-equipped weapon supports
  // Cache of weapon → poses so re-equipping doesn't re-fetch.
  weaponPoseCache: new Map(),
};

const $img = document.getElementById("char-img");
const $tabs = document.getElementById("char-tabs");
const $grid = document.getElementById("char-grid");
const $equipped = document.getElementById("char-equipped");
const $scale = document.getElementById("char-scale");
const $export = document.getElementById("char-export");

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
  let parts = state.parts.get(category);
  if (!parts) {
    try {
      const resp = await fetch(`/api/character/parts/${category}`);
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
  renderGrid(category, parts);
  $grid.classList.remove("loading");
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
  tile.className = "part-tile" + (part.id === equippedId ? " equipped" : "");
  tile.dataset.id = part.id;
  tile.title = part.id;

  // Server returns icon_paths (list); fall back to icon_path (legacy
  // single-string) and finally to client-side rule reconstruction so
  // the tile renders even if the server response shape changes.
  const candidates =
    part.icon_paths && part.icon_paths.length ? part.icon_paths
    : part.icon_path ? [part.icon_path]
    : iconPathsFor(category, part.id);

  const thumb = document.createElement("img");
  thumb.alt = part.id;
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
  pid.textContent = part.id;
  tile.appendChild(pid);

  tile.addEventListener("click", () => {
    equipPart(category, part.id, candidates);
  });
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
async function equipPart(category, id, iconPaths) {
  // Accept either the new candidate list or a legacy single string for
  // backwards compatibility with anything still passing one path.
  const paths = Array.isArray(iconPaths)
    ? iconPaths
    : iconPaths ? [iconPaths]
    : iconPathsFor(category, id);
  state.equipped[category] = { id, iconPaths: paths };
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
  renderEquipped();
  refreshCompose();
}

function unequipPart(category) {
  // Body / Head can be unequipped but the static composite collapses
  // without them — the preview will be tiny. That's OK; it lets the
  // user explore weird combinations.
  delete state.equipped[category];
  if (category === "Weapon") {
    state.weaponPoses = ["stand1"];
    state.pose = "stand1";
    renderPoseControls();
  }
  renderEquipped();
  refreshCompose();
  if (state.activeTab === category) {
    for (const tile of $grid.querySelectorAll(".part-tile")) {
      tile.classList.remove("equipped");
    }
  }
}

async function syncWeaponPose(weaponId) {
  let poses = state.weaponPoseCache.get(weaponId);
  if (!poses) {
    try {
      const resp = await fetch(`/api/character/weapon_poses/${weaponId}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const json = await resp.json();
      poses = json.poses && json.poses.length ? json.poses : ["stand1"];
    } catch (err) {
      console.warn("weapon_poses fetch failed:", err);
      poses = ["stand1"];
    }
    state.weaponPoseCache.set(weaponId, poses);
  }
  state.weaponPoses = poses;
  // Keep the user's current pose if the new weapon supports it; otherwise
  // snap to the weapon's first available pose so the composite doesn't
  // silently fall back to a pose the user can't see selected.
  if (!poses.includes(state.pose)) {
    state.pose = poses[0];
  }
  renderPoseControls();
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
    idSpan.textContent = slot.id;
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

// ── pose toggle ────────────────────────────────────────────────────
const POSE_LABELS = { stand1: "1H", stand2: "2H" };

function renderPoseControls() {
  // Reuses the dedicated container in character.html; if the equipped
  // weapon supports both stand1 and stand2 we expose a radio toggle,
  // otherwise we hide it (the pose is implied by the weapon).
  let host = document.getElementById("char-pose");
  if (!host) return;
  host.innerHTML = "";
  if (state.weaponPoses.length < 2) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  const label = document.createElement("span");
  label.className = "pose-label";
  label.textContent = "Pose";
  host.appendChild(label);
  for (const p of state.weaponPoses) {
    const id = `char-pose-${p}`;
    const wrap = document.createElement("label");
    wrap.htmlFor = id;
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "char-pose";
    radio.id = id;
    radio.value = p;
    radio.checked = p === state.pose;
    radio.addEventListener("change", () => {
      if (radio.checked && state.pose !== p) {
        state.pose = p;
        refreshCompose();
      }
    });
    wrap.appendChild(radio);
    wrap.appendChild(document.createTextNode(POSE_LABELS[p] || p));
    host.appendChild(wrap);
  }
}

async function refreshCompose() {
  const ids = Object.values(state.equipped).map(s => s.id);
  if (ids.length === 0) {
    $img.removeAttribute("src");
    return;
  }
  const seq = ++state.composeSeq;
  const scale = $scale.value || "2";
  const url =
    `/api/character/compose?ids=${ids.join(",")}` +
    `&pose=${encodeURIComponent(state.pose)}` +
    `&scale=${scale}&_=${seq}`;
  // Use a fetch+blob round-trip so we can drop the result if it's stale,
  // and so the image doesn't flicker while a new one loads.
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();
    if (seq !== state.composeSeq) return;  // a newer compose superseded us
    if ($img.dataset.blobUrl) URL.revokeObjectURL($img.dataset.blobUrl);
    const blobUrl = URL.createObjectURL(blob);
    $img.dataset.blobUrl = blobUrl;
    $img.src = blobUrl;
    // The server reports the pose it actually used (auto-detect path);
    // sync state so the toggle always reflects what's on screen.
    const resolved = resp.headers.get("X-Resolved-Pose");
    if (resolved && resolved !== state.pose
        && state.weaponPoses.includes(resolved)) {
      state.pose = resolved;
      renderPoseControls();
    }
  } catch (err) {
    console.warn("compose failed:", err);
  }
}

// ── controls ──────────────────────────────────────────────────────
$scale.addEventListener("change", refreshCompose);
$export.addEventListener("click", () => {
  if (!$img.src) return;
  const a = document.createElement("a");
  a.href = $img.src;
  const ids = Object.values(state.equipped).map(s => s.id).join("-");
  a.download = `character_${ids || "empty"}.png`;
  a.click();
});

// ── boot ──────────────────────────────────────────────────────────
(async function boot() {
  selectTab(state.activeTab);
  // If a weapon was seeded (none in DEFAULTS today, but kept for
  // future-proofing), sync the pose toggle before the first compose.
  if (state.equipped.Weapon) {
    await syncWeaponPose(state.equipped.Weapon.id);
  }
  renderPoseControls();
  renderEquipped();
  refreshCompose();
})();
