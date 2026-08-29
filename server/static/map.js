const $search = document.getElementById("map-search");
const $load = document.getElementById("map-load");
const $suggestions = document.getElementById("map-suggestions");
const $image = document.getElementById("map-image");
const $minimap = document.getElementById("map-minimap");
const $status = document.getElementById("map-status");
const $title = document.getElementById("map-title");
const $street = document.getElementById("map-street");
const $bounds = document.getElementById("map-bounds");
const $counts = document.getElementById("map-counts");
const $layers = document.getElementById("map-layers");
const $scale = document.getElementById("map-scale");
const $time = document.getElementById("map-time");
const $export = document.getElementById("map-export");

const toggleIds = [
  "map-backgrounds", "map-life", "map-reactors", "map-portals",
  "map-footholds", "map-ropes",
];

const state = {
  mapId: null,
  info: null,
  renderUrl: null,
  renderController: null,
  searchTimer: null,
};

function selectedLayers() {
  return Array.from($layers.querySelectorAll("input:checked")).map(el => el.value);
}

function query(download = false) {
  const params = new URLSearchParams({
    scale: $scale.value,
    time: String(Math.max(0, Number($time.value) || 0)),
    layers: selectedLayers().join(","),
    backgrounds: document.getElementById("map-backgrounds").checked ? "1" : "0",
    life: document.getElementById("map-life").checked ? "1" : "0",
    reactors: document.getElementById("map-reactors").checked ? "1" : "0",
    portals: document.getElementById("map-portals").checked ? "1" : "0",
    footholds: document.getElementById("map-footholds").checked ? "1" : "0",
    ropes: document.getElementById("map-ropes").checked ? "1" : "0",
  });
  if (download) params.set("download", "1");
  return params.toString();
}

async function renderMap() {
  if (!state.mapId) return;
  if (state.renderController) state.renderController.abort();
  state.renderController = new AbortController();
  $status.textContent = "Rendering…";
  $image.hidden = true;
  const started = performance.now();
  try {
    const response = await fetch(
      `/api/map/${encodeURIComponent(state.mapId)}/render.png?${query()}`,
      { signal: state.renderController.signal },
    );
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    if (state.renderUrl) URL.revokeObjectURL(state.renderUrl);
    state.renderUrl = URL.createObjectURL(blob);
    $image.src = state.renderUrl;
    $image.hidden = false;
    $status.textContent = `Rendered ${$image.naturalWidth || ""} × ${$image.naturalHeight || ""} in ${Math.round(performance.now() - started)} ms`;
    $image.onload = () => {
      $status.textContent = `Rendered ${$image.naturalWidth} × ${$image.naturalHeight} in ${Math.round(performance.now() - started)} ms`;
    };
  } catch (error) {
    if (error.name === "AbortError") return;
    $status.textContent = `Render failed: ${error.message || error}`;
  }
}

function renderInfo(info) {
  $title.textContent = `${info.name || "Unnamed map"} · ${info.id}`;
  $street.textContent = info.street || "";
  if (info.linked) $street.textContent += `${info.street ? " · " : ""}uses map ${info.sourceId}`;
  const b = info.bounds;
  $bounds.textContent = `${b.width} × ${b.height} world px · (${b.left}, ${b.top}) → (${b.right}, ${b.bottom})`;
  const labels = [
    ["tiles", "tiles"], ["objects", "objects"], ["backgrounds", "backs"],
    ["mobs", "mobs"], ["npcs", "NPCs"], ["portals", "portals"],
    ["reactors", "reactors"], ["footholds", "footholds"], ["ropes", "ropes"],
  ];
  $counts.innerHTML = labels.map(([key, label]) =>
    `<div class="map-count"><b>${info.counts[key]}</b><span>${label}</span></div>`
  ).join("");

  $layers.innerHTML = info.layers.map(layer => {
    const count = layer.tiles + layer.objects;
    return `<label><input type="checkbox" value="${layer.number}" checked>` +
      ` Layer ${layer.number} <small>${count}${layer.tileSet ? ` · ${escapeHtml(layer.tileSet)}` : ""}</small></label>`;
  }).join("");
  for (const checkbox of $layers.querySelectorAll("input")) {
    checkbox.addEventListener("change", renderMap);
  }

  if (info.minimap) {
    $minimap.src = `/api/map/${encodeURIComponent(info.id)}/minimap.png?t=${Date.now()}`;
    $minimap.hidden = false;
    $minimap.onerror = () => { $minimap.hidden = true; };
  } else {
    $minimap.hidden = true;
  }
}

async function loadMap(value) {
  const match = String(value || "").match(/\d{1,9}/);
  if (!match) {
    $status.textContent = "Enter or select a map ID.";
    return;
  }
  const mapId = String(Number(match[0]));
  $status.textContent = `Loading map ${mapId}…`;
  hideSuggestions();
  try {
    const response = await fetch(`/api/map/${encodeURIComponent(mapId)}`);
    const info = await response.json();
    if (!response.ok) throw new Error(info.error || `HTTP ${response.status}`);
    state.mapId = info.id;
    state.info = info;
    $search.value = `${info.id} — ${info.name || "Unnamed map"}`;
    renderInfo(info);
    await renderMap();
  } catch (error) {
    $status.textContent = `Load failed: ${error.message || error}`;
  }
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

function hideSuggestions() {
  $suggestions.hidden = true;
  $suggestions.innerHTML = "";
}

async function searchMaps() {
  const q = $search.value.trim();
  if (!q) { hideSuggestions(); return; }
  try {
    const response = await fetch(`/api/map/search?q=${encodeURIComponent(q)}&limit=30`);
    if (!response.ok) return;
    const items = await response.json();
    if (!items.length) { hideSuggestions(); return; }
    $suggestions.innerHTML = items.map(item =>
      `<li><button type="button" data-id="${item.id}"><strong>${escapeHtml(item.name)} · ${item.id}</strong>` +
      `<small>${escapeHtml(item.street)}</small></button></li>`
    ).join("");
    $suggestions.hidden = false;
    for (const button of $suggestions.querySelectorAll("button")) {
      button.addEventListener("click", () => loadMap(button.dataset.id));
    }
  } catch (_) {
    hideSuggestions();
  }
}

$search.addEventListener("input", () => {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(searchMaps, 180);
});
$search.addEventListener("keydown", event => {
  if (event.key === "Enter") { event.preventDefault(); loadMap($search.value); }
  if (event.key === "Escape") hideSuggestions();
});
$load.addEventListener("click", () => loadMap($search.value));
document.addEventListener("click", event => {
  if (!event.target.closest(".map-search-section")) hideSuggestions();
});

$scale.addEventListener("change", renderMap);
$time.addEventListener("change", renderMap);
for (const id of toggleIds) document.getElementById(id).addEventListener("change", renderMap);

$export.addEventListener("click", () => {
  if (!state.mapId) return;
  const anchor = document.createElement("a");
  anchor.href = `/api/map/${encodeURIComponent(state.mapId)}/render.png?${query(true)}`;
  anchor.download = `map_${state.mapId}.png`;
  anchor.click();
});

const initialMapId = document.body.dataset.initialMapId;
if (initialMapId) loadMap(initialMapId);
