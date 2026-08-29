const $search = document.getElementById("mob-search");
const $load = document.getElementById("mob-load");
const $suggestions = document.getElementById("mob-suggestions");
const $title = document.getElementById("mob-title");
const $link = document.getElementById("mob-link");
const $stats = document.getElementById("mob-stats");
const $action = document.getElementById("mob-action");
const $flip = document.getElementById("mob-flip");
const $actionMeta = document.getElementById("mob-action-meta");
const $animation = document.getElementById("mob-animation");
const $status = document.getElementById("mob-status");
const $drops = document.getElementById("mob-drops");
const $dropCount = document.getElementById("mob-drop-count");
const $dropNote = document.getElementById("mob-drop-note");

const state = { mobId: null, info: null, searchTimer: null, animationSeq: 0 };

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

function hideSuggestions() {
  $suggestions.hidden = true;
  $suggestions.innerHTML = "";
}

function selectedAction() {
  return state.info?.actions?.find(item => item.name === $action.value) || null;
}

function renderAnimation() {
  if (!state.mobId || !$action.value) return;
  const action = selectedAction();
  $actionMeta.textContent = action
    ? `${action.frames} frame${action.frames === 1 ? "" : "s"} - ${action.durationMs} ms loop`
    : "";
  $status.textContent = `Loading ${$action.value} animation...`;
  $animation.hidden = true;
  const seq = ++state.animationSeq;
  const params = new URLSearchParams({
    action: $action.value,
    flip: $flip.checked ? "1" : "0",
  });
  // Clearing before reassigning also restarts an APNG when the user reloads
  // the same mob/action combination.
  const src = `/api/mob/${encodeURIComponent(state.mobId)}/animation.png?${params}`;
  $animation.removeAttribute("src");
  requestAnimationFrame(() => {
    if (seq === state.animationSeq) $animation.src = src;
  });
  $animation.onload = () => {
    if (seq !== state.animationSeq) return;
    $animation.hidden = false;
    $status.textContent = `${state.info.name} - ${$action.value}`;
  };
  $animation.onerror = () => {
    if (seq !== state.animationSeq) return;
    $status.textContent = `Could not render ${$action.value}.`;
  };
}

function renderStats(stats) {
  const labels = [
    ["level", "Level"], ["hp", "HP"], ["mp", "MP"], ["exp", "EXP"],
    ["weaponAttack", "W. attack"], ["magicAttack", "M. attack"],
    ["weaponDefense", "W. defense"], ["magicDefense", "M. defense"],
    ["accuracy", "Accuracy"], ["evasion", "Evasion"], ["speed", "Speed"],
  ];
  const rows = labels.filter(([key]) => stats[key] != null).map(([key, label]) =>
    `<div class="mob-stat"><span>${label}</span><b>${Number(stats[key]).toLocaleString()}</b></div>`
  );
  for (const [key, label] of [["boss", "Boss"], ["undead", "Undead"]]) {
    if (stats[key]) rows.push(`<div class="mob-stat"><span>Type</span><b>${label}</b></div>`);
  }
  $stats.innerHTML = rows.join("");
}

function renderDrops(info) {
  $dropCount.textContent = `(${info.drops.length})`;
  if (!info.hasDropData) {
    $dropNote.textContent = "No Monster Book entry exists for this mob in v83.";
    $drops.innerHTML = '<p class="mob-empty">Drop data is not available in the client files.</p>';
    return;
  }
  $dropNote.textContent = "Monster Book reward list; rates and quantities are not present in WZ data.";
  if (!info.drops.length) {
    $drops.innerHTML = '<p class="mob-empty">The Monster Book entry has no listed rewards.</p>';
    return;
  }
  $drops.innerHTML = info.drops.map(item =>
    `<article class="mob-drop">` +
      `<div class="mob-drop-icon"><img src="${escapeHtml(item.iconUrl)}" alt="" loading="lazy"></div>` +
      `<div class="mob-drop-text"><div class="mob-drop-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div>` +
      `<div class="mob-drop-id">${escapeHtml(item.id)}</div></div>` +
    `</article>`
  ).join("");
  for (const image of $drops.querySelectorAll("img")) {
    image.addEventListener("error", () => image.classList.add("is-missing"));
  }
}

function renderInfo(info) {
  $title.textContent = `${info.name} - ${info.id}`;
  $link.textContent = info.linked ? `Uses sprite data from mob ${info.sourceId}` : "";
  renderStats(info.stats || {});
  $action.innerHTML = (info.actions || []).map(item =>
    `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`
  ).join("");
  $action.disabled = !info.actions?.length;
  if (info.defaultAction) $action.value = info.defaultAction;
  renderDrops(info);
  renderAnimation();
}

async function loadMob(value) {
  const match = String(value || "").match(/\d{1,7}/);
  if (!match) {
    $status.textContent = "Enter or select a mob ID.";
    return;
  }
  const mobId = String(Number(match[0]));
  hideSuggestions();
  $status.textContent = `Loading mob ${mobId}...`;
  try {
    const response = await fetch(`/api/mob/${encodeURIComponent(mobId)}`);
    const info = await response.json();
    if (!response.ok) throw new Error(info.error || `HTTP ${response.status}`);
    state.mobId = info.id;
    state.info = info;
    $search.value = `${info.id} - ${info.name}`;
    renderInfo(info);
  } catch (error) {
    $status.textContent = `Load failed: ${error.message || error}`;
  }
}

async function searchMobs() {
  const q = $search.value.trim();
  if (!q) { hideSuggestions(); return; }
  try {
    const response = await fetch(`/api/mob/search?q=${encodeURIComponent(q)}&limit=40`);
    if (!response.ok) return;
    const items = await response.json();
    if (!items.length) { hideSuggestions(); return; }
    $suggestions.innerHTML = items.map(item =>
      `<li><button type="button" data-id="${item.id}">` +
      `<strong>${escapeHtml(item.name)}</strong><small>${item.id}</small></button></li>`
    ).join("");
    $suggestions.hidden = false;
    for (const button of $suggestions.querySelectorAll("button")) {
      button.addEventListener("click", () => loadMob(button.dataset.id));
    }
  } catch (_) {
    hideSuggestions();
  }
}

$search.addEventListener("input", () => {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(searchMobs, 180);
});
$search.addEventListener("keydown", event => {
  if (event.key === "Enter") { event.preventDefault(); loadMob($search.value); }
  if (event.key === "Escape") hideSuggestions();
});
$load.addEventListener("click", () => loadMob($search.value));
$action.addEventListener("change", renderAnimation);
$flip.addEventListener("change", renderAnimation);
document.addEventListener("click", event => {
  if (!event.target.closest(".mob-search-section")) hideSuggestions();
});

const initialMobId = document.body.dataset.initialMobId;
if (initialMobId) loadMob(initialMobId);
