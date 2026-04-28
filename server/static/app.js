// Tiny browser frontend for wzpy. Lazy-loads tree children on expand.

const treeRoot = document.getElementById("tree");
const detailEl = document.getElementById("detail");
const crumbsEl = document.getElementById("breadcrumbs");
const treePanel = document.getElementById("tree-panel");

// Above this child count, switch the rendering of a UL to true virtualization
// — only the LIs visible in the scroll viewport exist in the DOM. With ~1500
// siblings (e.g. 0400.img in Item.wz), keeping all of them in the DOM makes
// every click inside that UL pay an O(siblings) browser cost; virtualizing
// drops it to O(visible).
const VIRTUALIZE_THRESHOLD = 200;

// SubProperty/Property/Image have an expand twisty in front; using the same
// "▸" character as the kind icon would render two arrows side-by-side where
// only one rotates. Leave those kinds icon-less.
const KIND_ICONS = {
  directory: "📁", Directory: "📁",
  image: "", Image: "",
  SubProperty: "", Property: "",
  Canvas: "▦",
  Sound: "♪",
  Vector: "⊹",
  String: "✎",
  Int: "#", Short: "#", Long: "#", Float: "#", Double: "#",
  UOL: "↪",
  Convex: "◇",
  Null: "·",
};

function iconFor(kind) {
  return KIND_ICONS[kind] || "•";
}

function joinPath(base, name) {
  return base ? `${base}/${name}` : name;
}

async function fetchJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

function makeNode(child, parentPath) {
  const fullPath = joinPath(parentPath, child.name);
  const li = document.createElement("li");
  const node = document.createElement("span");
  node.className = "node";
  node.dataset.kind = child.kind;
  node.dataset.path = fullPath;

  const twisty = document.createElement("span");
  twisty.className = "twisty";
  twisty.textContent = child.leaf ? "" : "▸";
  node.appendChild(twisty);

  const icon = document.createElement("span");
  icon.className = "icon";
  icon.textContent = iconFor(child.kind);
  node.appendChild(icon);

  const name = document.createElement("span");
  name.className = "name";
  name.textContent = child.name || "(root)";
  node.appendChild(name);

  if (child.value !== undefined && child.value !== null) {
    const preview = document.createElement("span");
    preview.className = "preview";
    let text = JSON.stringify(child.value);
    if (text && text.length > 50) text = text.slice(0, 50) + "…";
    preview.textContent = `= ${text}`;
    node.appendChild(preview);
  } else if (child.kind === "Vector") {
    const preview = document.createElement("span");
    preview.className = "preview";
    preview.textContent = `(${child.x}, ${child.y})`;
    node.appendChild(preview);
  } else if (child.kind === "Canvas") {
    const preview = document.createElement("span");
    preview.className = "preview";
    preview.textContent = `${child.width}×${child.height} fmt=${child.format}`;
    node.appendChild(preview);
  } else if (typeof child.count === "number") {
    const preview = document.createElement("span");
    preview.className = "preview";
    preview.textContent = `(${child.count})`;
    node.appendChild(preview);
  }

  li.appendChild(node);

  // Stash all per-node state on the LI itself so the single delegated click
  // handler on ``treeRoot`` can recover it without us creating a closure
  // (and a retained reference to ``child``) per node.
  li._meta = child;
  li._fullPath = fullPath;
  li._parentPath = parentPath;
  li._twisty = twisty;
  li._childUl = null;
  li._loaded = false;

  return li;
}

// Single delegated click handler for the entire tree. One listener total
// instead of one-per-node — drastically lower memory + faster click delivery
// on big trees.
let currentlySelected = null;

async function onTreeClick(ev) {
  const nodeEl = ev.target.closest(".node");
  // Debug — tells us why a click might silently no-op. Filter via console
  // text "[click hit]" to see only these.
  if (!nodeEl || !treeRoot.contains(nodeEl) || !nodeEl.parentElement._meta) {
    console.log("[click hit]", {
      target: ev.target.tagName + "." + ev.target.className,
      foundNode: !!nodeEl,
      inTree: nodeEl ? treeRoot.contains(nodeEl) : null,
      hasMeta: nodeEl ? !!nodeEl.parentElement._meta : null,
    });
  }
  if (!nodeEl || !treeRoot.contains(nodeEl)) return;
  const li = nodeEl.parentElement;
  const child = li._meta;
  const fullPath = li._fullPath;
  if (!child) return;

  ev.stopPropagation();
  const tClickStart = performance.now();
  if (currentlySelected) currentlySelected.classList.remove("selected");
  nodeEl.classList.add("selected");
  currentlySelected = nodeEl;
  showDetail(fullPath, child);
  const tDetail = performance.now() - tClickStart;

  if (child.leaf) {
    if (tDetail > 30) console.log(`[click leaf] ${fullPath}  detail=${tDetail.toFixed(0)}ms`);
    return;
  }
  if (li._childUl) {
    const tToggleStart = performance.now();
    const hidden = li._childUl.style.display === "none";
    li._childUl.style.display = hidden ? "" : "none";
    li._twisty.textContent = hidden ? "▾" : "▸";
    notifyAncestorVirtualResize(li);
    const tToggle = performance.now() - tToggleStart;
    if (tToggle > 30 || tDetail > 30) {
      console.log(
        `[click toggle ${hidden ? "expand" : "collapse"}] ${fullPath}  ` +
        `detail=${tDetail.toFixed(0)}ms toggle=${tToggle.toFixed(0)}ms  ` +
        `dom=${treeRoot.getElementsByClassName("node").length} nodes`,
      );
    }
    return;
  }
  li._twisty.textContent = "…";
  try {
    const t0 = performance.now();
    const data = await fetchJson(`/api/tree/${encodeURI(fullPath)}`);
    const tFetch = performance.now() - t0;
    const t1 = performance.now();
    const ul = document.createElement("ul");
    if (data.children.length >= VIRTUALIZE_THRESHOLD) {
      const v = new VirtualList(ul, data.children, fullPath);
      v.mount();
      ul._virtual = v;
    } else {
      const frag = document.createDocumentFragment();
      for (const c of data.children) frag.appendChild(makeNode(c, fullPath));
      ul.appendChild(frag);
    }
    li.appendChild(ul);
    li._childUl = ul;
    li._loaded = true;
    li._twisty.textContent = "▾";
    // Notify ancestor virtual list (if any) that our height grew.
    notifyAncestorVirtualResize(li);
    const tDom = performance.now() - t1;
    // Always log when something noticeable could be happening.
    if (data.children.length >= 50 || tFetch > 30 || tDom > 30 || tDetail > 30) {
      console.log(
        `[click fetch] ${fullPath}: ${data.children.length} children  ` +
        `detail=${tDetail.toFixed(0)}ms fetch=${tFetch.toFixed(0)}ms dom=${tDom.toFixed(0)}ms  ` +
        `total=${treeRoot.getElementsByClassName("node").length} nodes`,
      );
    }
  } catch (err) {
    li._twisty.textContent = "▸";
    console.error(err);
  }
}

treeRoot.addEventListener("click", onTreeClick);

function makeCrumbs(path) {
  crumbsEl.innerHTML = "";
  const parts = path.split("/").filter(Boolean);
  let acc = "";
  const rootLink = document.createElement("a");
  rootLink.href = "#"; rootLink.textContent = "(root)";
  rootLink.onclick = (e) => { e.preventDefault(); detailEl.innerHTML = '<p class="hint">Click a node to inspect.</p>'; crumbsEl.innerHTML = ""; };
  crumbsEl.appendChild(rootLink);
  for (const p of parts) {
    acc = joinPath(acc, p);
    crumbsEl.appendChild(document.createTextNode(" / "));
    const a = document.createElement("a");
    a.href = "#"; a.textContent = p;
    a.dataset.path = acc;
    crumbsEl.appendChild(a);
  }
}

// Active canvas viewer's AbortController — invoked before we replace
// detailEl so the previous viewer's window-level mousemove/mouseup
// listeners are removed instead of accumulating forever.
let activeViewerAbort = null;

function showDetail(path, child) {
  if (activeViewerAbort) {
    activeViewerAbort.abort();
    activeViewerAbort = null;
  }
  makeCrumbs(path);
  detailEl.innerHTML = "";

  const title = document.createElement("h2");
  title.textContent = child.name || "(root)";
  detailEl.appendChild(title);

  const kind = document.createElement("span");
  kind.className = "kind";
  kind.textContent = child.kind;
  detailEl.appendChild(kind);

  const tbl = document.createElement("table");
  tbl.className = "props";
  for (const [k, v] of Object.entries(child)) {
    if (["name", "kind", "leaf"].includes(k)) continue;
    // Skip internal state keys we attach client-side. ``_li`` in
    // particular holds the DOM element back-pointing to ``child._meta``,
    // which would crash JSON.stringify with a circular-structure error.
    if (k.startsWith("_")) continue;
    if (v === null || v === undefined) continue;
    const tr = document.createElement("tr");
    const td1 = document.createElement("td"); td1.textContent = k;
    const td2 = document.createElement("td"); td2.textContent = JSON.stringify(v);
    tr.appendChild(td1); tr.appendChild(td2);
    tbl.appendChild(tr);
  }
  detailEl.appendChild(tbl);

  if (child.kind === "Canvas" && child.renderable) {
    detailEl.appendChild(makeCanvasViewer(path, child));
  }
  if (child.kind === "Sound") {
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.src = `/api/sound/${encodeURI(path)}`;
    detailEl.appendChild(audio);
  }
}

// ── zoomable canvas viewer ───────────────────────────────────────────
// Wheel zooms toward the cursor, drag pans, +/- and 0 keys work while the
// viewer is focused, and image-rendering: pixelated keeps sprites crisp.

function makeCanvasViewer(path, meta) {
  const ac = new AbortController();
  activeViewerAbort = ac;
  const root = document.createElement("div");
  root.className = "canvas-viewer";
  root.tabIndex = 0;

  const toolbar = document.createElement("div");
  toolbar.className = "viewer-toolbar";
  const btn = (label, title, fn) => {
    const b = document.createElement("button");
    b.textContent = label; b.title = title;
    b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
    return b;
  };
  const zoomLabel = document.createElement("span");
  zoomLabel.className = "zoom-label";
  toolbar.appendChild(btn("−", "Zoom out (−)", () => zoomBy(1 / 1.25)));
  toolbar.appendChild(btn("+", "Zoom in (+)", () => zoomBy(1.25)));
  toolbar.appendChild(btn("Fit", "Fit to viewport", () => fitToViewport()));
  toolbar.appendChild(btn("1:1", "Actual size (0)", () => setZoom(1)));
  toolbar.appendChild(btn("4×", "4× actual size", () => setZoom(4)));
  toolbar.appendChild(zoomLabel);
  root.appendChild(toolbar);

  const viewport = document.createElement("div");
  viewport.className = "viewer-viewport";
  // The transform layer is positioned at the viewport center; we translate
  // and scale it to move the image around.
  const layer = document.createElement("div");
  layer.className = "viewer-layer";
  const img = document.createElement("img");
  img.src = `/api/canvas/${encodeURI(path)}.png`;
  img.alt = meta.name;
  img.draggable = false;
  layer.appendChild(img);
  viewport.appendChild(layer);
  root.appendChild(viewport);

  // Viewer state — translation is in viewport pixels relative to its center.
  let scale = 1;
  let tx = 0, ty = 0;

  function applyTransform() {
    layer.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
    zoomLabel.textContent = `${Math.round(scale * 100)}%`;
  }

  function setZoom(newScale, anchorX, anchorY) {
    newScale = Math.max(0.05, Math.min(64, newScale));
    if (anchorX !== undefined) {
      // Keep the point under the cursor stationary in viewport coordinates.
      const rect = viewport.getBoundingClientRect();
      const cx = anchorX - rect.left - rect.width / 2;
      const cy = anchorY - rect.top - rect.height / 2;
      const factor = newScale / scale;
      tx = cx + (tx - cx) * factor;
      ty = cy + (ty - cy) * factor;
    }
    scale = newScale;
    applyTransform();
  }

  function zoomBy(factor) {
    setZoom(scale * factor);
  }

  function fitToViewport() {
    const rect = viewport.getBoundingClientRect();
    const w = meta.width || img.naturalWidth || 1;
    const h = meta.height || img.naturalHeight || 1;
    const margin = 24;
    const fitScale = Math.min(
      (rect.width - margin) / w,
      (rect.height - margin) / h,
    );
    tx = 0; ty = 0;
    setZoom(Math.max(0.1, fitScale));
  }

  // Set initial zoom in rAF (not img.load) so the first paint is already at
  // the final scale. Otherwise the will-change layer rasterizes a scale-1
  // texture before the image loads, then GPU-bilinear-scales it on zoom-up —
  // pixelated on <img> doesn't override the compositor's filter for the
  // parent layer. Using meta dimensions lets us skip waiting on the network.
  requestAnimationFrame(() => {
    const w = meta.width || img.naturalWidth || 1;
    const h = meta.height || img.naturalHeight || 1;
    const rect = viewport.getBoundingClientRect();
    if (rect.width === 0) return;
    if (w * 4 <= rect.width - 24 && h * 4 <= rect.height - 24) {
      setZoom(Math.max(1, Math.min(8, Math.floor((rect.width - 24) / w))));
    } else {
      fitToViewport();
    }
  });

  // All listeners are scoped to ``ac.signal`` so they're auto-removed when
  // the viewer is destroyed (replaced by another detail panel).
  viewport.addEventListener("wheel", (e) => {
    e.preventDefault();
    const factor = Math.exp(-e.deltaY * 0.0015);
    setZoom(scale * factor, e.clientX, e.clientY);
  }, { passive: false, signal: ac.signal });

  let dragging = false, startX = 0, startY = 0, startTx = 0, startTy = 0;
  viewport.addEventListener("mousedown", (e) => {
    dragging = true; startX = e.clientX; startY = e.clientY;
    startTx = tx; startTy = ty;
    viewport.classList.add("grabbing");
    e.preventDefault();
  }, { signal: ac.signal });
  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    tx = startTx + (e.clientX - startX);
    ty = startTy + (e.clientY - startY);
    applyTransform();
  }, { signal: ac.signal });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    viewport.classList.remove("grabbing");
  }, { signal: ac.signal });

  root.addEventListener("keydown", (e) => {
    if (e.key === "+" || e.key === "=") { zoomBy(1.25); e.preventDefault(); }
    else if (e.key === "-" || e.key === "_") { zoomBy(1 / 1.25); e.preventDefault(); }
    else if (e.key === "0") { setZoom(1); e.preventDefault(); }
    else if (e.key.toLowerCase() === "f") { fitToViewport(); e.preventDefault(); }
  }, { signal: ac.signal });

  applyTransform();
  return root;
}

// ── true DOM virtualization (spacer-based) ──────────────────────────
// Only the LIs in the scroll viewport (plus a small overscan) exist in
// the DOM. Items live in normal flow between two spacer LIs that take up
// the height of the unrendered items above and below — so item LIs are
// regular block elements that inherit the tree's padding-left indent
// and accept clicks/expansion/nested ULs without any positioning hacks.
//
// We keep ``c._li`` references alive after detaching, so an item that
// was expanded preserves its child UL when it scrolls back into view.

class VirtualList {
  constructor(ul, children, parentPath) {
    this.ul = ul;
    this.children = children;
    this.parentPath = parentPath;
    this.defaultHeight = 22;
    this.overscan = 6;
    for (const c of children) {
      c._height = this.defaultHeight;
      c._li = null;
      c._cumTop = 0;
      c._virtualList = this;
    }
    this.recompute();
    this.ul.classList.add("virtual-list");

    this.topSpacer = document.createElement("li");
    this.topSpacer.className = "v-spacer";
    this.bottomSpacer = document.createElement("li");
    this.bottomSpacer.className = "v-spacer";
    this.ul.appendChild(this.topSpacer);
    this.ul.appendChild(this.bottomSpacer);
    this._setSpacerHeights(0, this.totalHeight);

    this.startIdx = 0;
    this.endIdx = 0;
    this._scheduled = false;
    this._update = this._update.bind(this);
    this.requestUpdate = this.requestUpdate.bind(this);
  }

  recompute() {
    let acc = 0;
    for (const c of this.children) {
      c._cumTop = acc;
      acc += c._height;
    }
    this.totalHeight = acc;
  }

  _setSpacerHeights(top, bottom) {
    this.topSpacer.style.height = `${top}px`;
    this.bottomSpacer.style.height = `${bottom}px`;
  }

  // Binary search: first index whose end (cumTop + height) > y.
  _findIdxByTop(y) {
    let lo = 0, hi = this.children.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      const c = this.children[mid];
      if (c._cumTop + c._height <= y) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  mount() {
    treePanel.addEventListener("scroll", this.requestUpdate, { passive: true });
    window.addEventListener("resize", this.requestUpdate);
    this._update();
  }

  requestUpdate() {
    if (this._scheduled) return;
    this._scheduled = true;
    requestAnimationFrame(() => {
      this._scheduled = false;
      this._update();
    });
  }

  _update() {
    const ulRect = this.ul.getBoundingClientRect();
    const panelRect = treePanel.getBoundingClientRect();
    const visibleTop = Math.max(0, panelRect.top - ulRect.top);
    const visibleBot = Math.min(this.totalHeight, panelRect.bottom - ulRect.top);

    let startIdx, endIdx;
    if (visibleBot <= 0 || visibleTop >= this.totalHeight) {
      startIdx = endIdx = 0;
    } else {
      startIdx = Math.max(0, this._findIdxByTop(visibleTop) - this.overscan);
      endIdx = Math.min(
        this.children.length,
        this._findIdxByTop(visibleBot) + 1 + this.overscan,
      );
    }
    this._renderRange(startIdx, endIdx);
  }

  _renderRange(startIdx, endIdx) {
    // Detach any currently-rendered items between the spacers; we'll
    // re-insert the in-range ones in order. Detaching keeps the JS
    // reference alive, so an expanded item's child UL survives.
    let cur = this.topSpacer.nextSibling;
    while (cur && cur !== this.bottomSpacer) {
      const next = cur.nextSibling;
      cur.remove();
      cur = next;
    }
    for (let i = startIdx; i < endIdx; i++) {
      const c = this.children[i];
      if (!c._li) c._li = makeNode(c, this.parentPath);
      this.bottomSpacer.before(c._li);
    }

    const topH = startIdx > 0 ? this.children[startIdx]._cumTop : 0;
    const bottomCumStart = endIdx < this.children.length
      ? this.children[endIdx]._cumTop
      : this.totalHeight;
    this._setSpacerHeights(topH, this.totalHeight - bottomCumStart);

    this.startIdx = startIdx;
    this.endIdx = endIdx;
  }

  // Called when a child LI's height changes (it expanded or collapsed).
  childResized(child) {
    const idx = this.children.indexOf(child);
    if (idx < 0 || !child._li) return;
    const newH = child._li.offsetHeight || this.defaultHeight;
    if (Math.abs(newH - child._height) < 1) return;
    child._height = newH;
    let acc = child._cumTop + newH;
    for (let i = idx + 1; i < this.children.length; i++) {
      this.children[i]._cumTop = acc;
      acc += this.children[i]._height;
    }
    this.totalHeight = acc;
    // Update the bottom spacer to reflect the new total.
    const bottomCumStart = this.endIdx < this.children.length
      ? this.children[this.endIdx]._cumTop
      : this.totalHeight;
    this.bottomSpacer.style.height = `${this.totalHeight - bottomCumStart}px`;
    this.requestUpdate();
  }
}

// Walk up from ``li`` to the nearest VirtualList-owned UL and inform it
// that one of its items changed height. Done via rAF so the new layout
// has actually been applied before we measure ``offsetHeight``.
function notifyAncestorVirtualResize(li) {
  const meta = li._meta;
  if (!meta || !meta._virtualList) return;
  requestAnimationFrame(() => meta._virtualList.childResized(meta));
}

async function init() {
  try {
    const data = await fetchJson("/api/tree");
    for (const c of data.children) treeRoot.appendChild(makeNode(c, ""));
  } catch (err) {
    treeRoot.innerHTML = `<li style="color:#c47878">load error: ${err.message}</li>`;
  }
}

init();
