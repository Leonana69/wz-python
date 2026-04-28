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

// Empty string = render no icon for this kind; the twisty already cues that
// SubProperty/Property/Image are containers, and a duplicate ▸ would just
// look like two arrows side-by-side. Missing key = unknown type → fallback
// to "•" so something visibly shows up rather than going silent.
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
  // ?? rather than || so a deliberate empty-string mapping stays empty
  // and we know not to create the icon span at all.
  return KIND_ICONS[kind] ?? "•";
}

// Property kinds the server's /api/save can patch in place. Strings,
// Vectors, Convex, Sound, Canvas, etc. would change byte length and
// require a full WZ rewrite, which we don't do.
const EDITABLE_KINDS = new Set(["Short", "Int", "Long", "Float", "Double", "String"]);

// Encoded byte count for a string in its WZ encoding. ASCII = CP1252,
// 1 byte per char (fall back to UTF-8 length if there's a non-CP1252
// code point, which the server will then reject anyway). Unicode =
// UTF-16-LE, 2 bytes per BMP char.
function encodedStringLength(s, encoding) {
  if (encoding === "unicode") return s.length * 2;
  // Use TextEncoder UTF-8 length as a conservative ASCII-byte count;
  // any char that needs >1 UTF-8 byte certainly won't fit in cp1252
  // and the server will reject the edit anyway.
  return new TextEncoder().encode(s).length;
}

// Pending edits keyed by full property path. Cleared after a successful
// /api/save round-trip so the user sees the saved state, not their old
// staged input.
const pendingEdits = new Map();

function makeValueEditor(path, child, currentValue) {
  const wrap = document.createElement("span");
  wrap.className = "value-editor";
  const input = document.createElement("input");
  input.className = "value-input";
  const numeric = ["Short", "Int", "Long", "Float", "Double"].includes(child.kind);
  const isString = child.kind === "String";
  input.type = numeric ? "number" : "text";
  if (child.kind === "Float" || child.kind === "Double") {
    input.step = "any";
  }
  input.value = pendingEdits.has(path) ? pendingEdits.get(path) : currentValue;
  input.dataset.original = String(currentValue);

  // String-only: a live "N / max bytes" pill so the user knows when their
  // text fits. The server still validates authoritatively — this is a
  // hint, not a hard cap.
  let budget = null;
  if (isString && typeof child.payload_length === "number") {
    budget = document.createElement("span");
    budget.className = "budget";
    const refreshBudget = () => {
      const n = encodedStringLength(input.value, child.encoding || "ascii");
      const max = child.payload_length;
      budget.textContent = `${n} / ${max} bytes`;
      budget.classList.toggle("over", n !== max);
    };
    refreshBudget();
    input.addEventListener("input", refreshBudget);
    if (child.indirected) {
      // Soft-warn the user that this string is reached via offset
      // indirection — editing it changes every other property pointing
      // at the same payload offset.
      const warn = document.createElement("span");
      warn.className = "budget warn";
      warn.title = "this string is reached via offset indirection — editing it may affect other properties that share the same string-table entry";
      warn.textContent = "shared?";
      wrap.appendChild(warn);
    }
  }

  input.addEventListener("input", () => {
    const raw = input.value;
    if (raw === input.dataset.original) {
      pendingEdits.delete(path);
      input.classList.remove("dirty");
    } else {
      const parsed = numeric ? Number(raw) : raw;
      pendingEdits.set(path, parsed);
      input.classList.add("dirty");
    }
    updateSaveButton();
  });

  wrap.appendChild(input);
  if (budget) wrap.appendChild(budget);
  return wrap;
}

const saveButton = document.createElement("button");
saveButton.className = "save-btn";
saveButton.textContent = "Save";
saveButton.disabled = true;
saveButton.addEventListener("click", runSave);

function updateSaveButton() {
  const n = pendingEdits.size;
  saveButton.textContent = n ? `Save (${n})` : "Save";
  saveButton.disabled = n === 0;
}

async function runSave() {
  const edits = Object.fromEntries(pendingEdits);
  saveButton.disabled = true;
  saveButton.textContent = "Saving…";
  let resp;
  try {
    const r = await fetch("/api/save", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({edits}),
    });
    resp = await r.json();
  } catch (err) {
    alert(`save failed: ${err.message}`);
    updateSaveButton();
    return;
  }
  // Drop the OK ones from the pending map so the input chrome refreshes,
  // and surface any rejections.
  const failed = [];
  for (const row of resp.results || []) {
    if (row.status === "ok") {
      pendingEdits.delete(row.path);
    } else {
      failed.push(`  • ${row.path}: ${row.reason}`);
    }
  }
  // If the currently-displayed node had an edit, redraw it so the input's
  // "dirty" badge clears and dataset.original reflects the new on-disk value.
  document.querySelectorAll(".value-input.dirty").forEach((el) => {
    el.classList.remove("dirty");
  });
  updateSaveButton();
  if (failed.length) {
    alert(`Saved ${resp.ok}/${resp.total}. Rejected:\n${failed.join("\n")}`);
  } else {
    saveButton.textContent = `Saved ${resp.ok}`;
    setTimeout(updateSaveButton, 1500);
  }
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

  const iconText = iconFor(child.kind);
  if (iconText) {
    const icon = document.createElement("span");
    icon.className = "icon";
    icon.textContent = iconText;
    node.appendChild(icon);
  }

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
  try {
    const { tFetch, tDom, count } = await expandLi(li, fullPath);
    if (count >= 50 || tFetch > 30 || tDom > 30 || tDetail > 30) {
      console.log(
        `[click fetch] ${fullPath}: ${count} children  ` +
        `detail=${tDetail.toFixed(0)}ms fetch=${tFetch.toFixed(0)}ms dom=${tDom.toFixed(0)}ms  ` +
        `total=${treeRoot.getElementsByClassName("node").length} nodes`,
      );
    }
  } catch (err) {
    console.error(err);
  }
}

// Fetch ``/api/tree/<fullPath>`` and append the result as a child UL of
// ``li`` — virtualizing if the response is large. Shared by the click
// handler and the initial root auto-expansion.
async function expandLi(li, fullPath) {
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
    notifyAncestorVirtualResize(li);
    return { tFetch, tDom: performance.now() - t1, count: data.children.length };
  } catch (err) {
    li._twisty.textContent = "▸";
    throw err;
  }
}

treeRoot.addEventListener("click", onTreeClick);

// ── right-click export menu ─────────────────────────────────────────
const contextMenuEl = document.getElementById("context-menu");

function hideContextMenu() {
  contextMenuEl.hidden = true;
  contextMenuEl.innerHTML = "";
}

function showContextMenuFor(x, y, labelPath, fullPath, kind) {
  contextMenuEl.innerHTML = "";

  const label = document.createElement("div");
  label.className = "menu-label";
  label.textContent = labelPath || "(root)";
  contextMenuEl.appendChild(label);

  const triggerDownload = (url, suggestedName) => {
    const a = document.createElement("a");
    a.href = url;
    if (suggestedName) a.download = suggestedName;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  const addItem = (text, onClick) => {
    const it = document.createElement("div");
    it.className = "menu-item";
    it.textContent = text;
    it.onclick = (e) => { e.stopPropagation(); hideContextMenu(); onClick(); };
    contextMenuEl.appendChild(it);
  };
  const addSep = () => {
    const sep = document.createElement("div");
    sep.className = "menu-sep";
    contextMenuEl.appendChild(sep);
  };

  const enc = encodeURI(fullPath);
  // Directory targets get the per-image bundle route — exporting a whole WZ
  // root as one giant JSON would balloon to gigabytes for typical Map/Mob
  // files. For non-directory targets the single-file export is still right.
  const k = (kind || "").toLowerCase();
  const isDir = k === "directory";
  const isImg = k === "image";
  if (isDir) {
    addItem("Export data as JSON (one file per .img)",
      () => runJsonBundleExport(fullPath, labelPath));
  } else {
    addItem("Export data as JSON", () => triggerDownload(`/api/export/json/${enc}`));
  }
  addItem("Export data as XML", () => triggerDownload(`/api/export/xml/${enc}`));
  if (isImg || isDir) {
    addSep();
    if (isImg) {
      // Raw on-disk .img bytes — the same blob HaRepacker's "Save Image"
      // would (try to) write, suitable for re-opening as a loose .img.
      addItem("Export as .img (raw bytes)",
        () => triggerDownload(`/api/export/img/${enc}`));
    } else {
      addItem("Export .img bundle (.zip)",
        () => triggerDownload(`/api/export/img/${enc}`));
    }
  }
  addSep();
  addItem("Export images (keep tree structure)",
    () => triggerDownload(`/api/export/images/${enc}?layout=nested`));
  addItem("Export images (flatten into one folder)",
    () => triggerDownload(`/api/export/images/${enc}?layout=flat`));

  // Position; clamp to viewport so the menu doesn't get clipped at edges.
  contextMenuEl.hidden = false;
  const rect = contextMenuEl.getBoundingClientRect();
  const maxX = window.innerWidth - rect.width - 4;
  const maxY = window.innerHeight - rect.height - 4;
  contextMenuEl.style.left = Math.min(x, maxX) + "px";
  contextMenuEl.style.top = Math.min(y, maxY) + "px";
}

treeRoot.addEventListener("contextmenu", (ev) => {
  const nodeEl = ev.target.closest(".node");
  if (!nodeEl || !treeRoot.contains(nodeEl)) return;
  const li = nodeEl.parentElement;
  if (!li || li._meta === undefined) return;
  ev.preventDefault();
  // labelPath is what we show at the top of the menu — full visible path.
  // fullPath is what the server route gets (synthetic root LI uses "").
  const fullPath = li._fullPath || "";
  const meta = li._meta;
  const label = fullPath || meta.name || "(root)";
  showContextMenuFor(ev.clientX, ev.clientY, label, fullPath, meta.kind);
});

// Click anywhere else (or Escape) closes the menu.
document.addEventListener("click", (ev) => {
  if (!contextMenuEl.contains(ev.target)) hideContextMenu();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") hideContextMenu();
});
window.addEventListener("blur", hideContextMenu);

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
    const td2 = document.createElement("td");
    if (k === "value" && EDITABLE_KINDS.has(child.kind)) {
      td2.appendChild(makeValueEditor(path, child, v));
    } else {
      td2.textContent = JSON.stringify(v);
    }
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

  // Hidden file picker; clicking the Replace button proxies to it. Keeps
  // the toolbar layout uncluttered.
  const filePicker = document.createElement("input");
  filePicker.type = "file";
  filePicker.accept = "image/*";
  filePicker.style.display = "none";
  toolbar.appendChild(filePicker);
  const replaceBtn = btn("Replace…",
    "Replace this canvas with an uploaded image (must fit the original byte slot)",
    () => filePicker.click());
  replaceBtn.classList.add("replace-btn");
  toolbar.appendChild(replaceBtn);
  filePicker.addEventListener("change", async () => {
    const file = filePicker.files && filePicker.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("image", file);
    const orig = replaceBtn.textContent;
    replaceBtn.textContent = "Replacing…";
    replaceBtn.disabled = true;
    try {
      const r = await fetch(`/api/canvas/${encodeURI(path)}`,
        { method: "POST", body: fd });
      const body = await r.json().catch(() => ({}));
      if (!r.ok || body.ok === false) {
        const reason = body.message || body.reason || r.statusText;
        alert(`Replace failed: ${reason}`);
      } else {
        // Cache-bust so the viewer re-fetches the new PNG bytes.
        img.src = `/api/canvas/${encodeURI(path)}.png?t=${Date.now()}`;
        replaceBtn.textContent = `OK ${body.slot_used}/${body.slot_total}`;
        setTimeout(() => { replaceBtn.textContent = orig; }, 1800);
      }
    } catch (err) {
      alert(`Replace failed: ${err.message}`);
    } finally {
      replaceBtn.disabled = false;
      if (replaceBtn.textContent === "Replacing…") replaceBtn.textContent = orig;
      filePicker.value = "";
    }
  });

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

// ── per-image JSON bundle export (with progress modal) ─────────────
// Backend serializes each .img into its own JSON inside a temp ZIP on a
// worker thread; we poll status every 250 ms and surface progress here.

async function runJsonBundleExport(fullPath, labelPath) {
  const modal = createProgressModal(labelPath || "(root)");
  document.body.appendChild(modal.root);

  let jobId;
  try {
    const startResp = await fetch(`/api/export/json_bundle/start/${encodeURI(fullPath)}`, {
      method: "POST",
    });
    if (!startResp.ok) throw new Error(`start failed: ${startResp.status} ${startResp.statusText}`);
    ({ job_id: jobId } = await startResp.json());
  } catch (err) {
    modal.error(err.message);
    return;
  }

  modal.onCancel(async () => {
    try {
      await fetch(`/api/export/json_bundle/cancel/${jobId}`, { method: "POST" });
    } catch (_) {}
  });

  const poll = async () => {
    let st;
    try {
      const r = await fetch(`/api/export/json_bundle/status/${jobId}`);
      if (!r.ok) throw new Error(`status ${r.status}`);
      st = await r.json();
    } catch (err) {
      modal.error(err.message);
      return;
    }
    modal.update(st);
    if (st.status === "running") {
      setTimeout(poll, 250);
    } else if (st.status === "done") {
      // Trigger download. The browser navigation handles the streaming
      // ZIP response; the server cleans up the temp file once it's read.
      const a = document.createElement("a");
      a.href = `/api/export/json_bundle/download/${jobId}`;
      a.download = "";
      document.body.appendChild(a);
      a.click();
      a.remove();
      modal.done();
    } else if (st.status === "cancelled") {
      modal.cancelled();
    } else if (st.status === "error") {
      modal.error(st.error || "unknown error");
    }
  };
  poll();
}

function createProgressModal(labelText) {
  const root = document.createElement("div");
  root.className = "modal-backdrop";

  const card = document.createElement("div");
  card.className = "modal-card";
  root.appendChild(card);

  const title = document.createElement("div");
  title.className = "modal-title";
  title.textContent = "Exporting JSON";
  card.appendChild(title);

  const sub = document.createElement("div");
  sub.className = "modal-sub";
  sub.textContent = labelText;
  card.appendChild(sub);

  const barOuter = document.createElement("div");
  barOuter.className = "progress-bar";
  const barInner = document.createElement("div");
  barInner.className = "progress-fill";
  barOuter.appendChild(barInner);
  card.appendChild(barOuter);

  const stats = document.createElement("div");
  stats.className = "modal-stats";
  stats.textContent = "Preparing…";
  card.appendChild(stats);

  const current = document.createElement("div");
  current.className = "modal-current";
  card.appendChild(current);

  const buttons = document.createElement("div");
  buttons.className = "modal-buttons";
  const cancelBtn = document.createElement("button");
  cancelBtn.textContent = "Cancel";
  buttons.appendChild(cancelBtn);
  const closeBtn = document.createElement("button");
  closeBtn.textContent = "Close";
  closeBtn.style.display = "none";
  buttons.appendChild(closeBtn);
  card.appendChild(buttons);

  let cancelHandler = null;
  cancelBtn.onclick = () => {
    cancelBtn.disabled = true;
    cancelBtn.textContent = "Cancelling…";
    if (cancelHandler) cancelHandler();
  };
  closeBtn.onclick = () => root.remove();

  return {
    root,
    onCancel(fn) { cancelHandler = fn; },
    update(st) {
      const total = st.total || 0;
      const done = st.progress || 0;
      const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
      barInner.style.width = `${pct}%`;
      stats.textContent = total > 0
        ? `${done} / ${total} (${pct}%)`
        : "Discovering images…";
      current.textContent = st.current || "";
    },
    done() {
      title.textContent = "Export complete";
      stats.textContent = `${stats.textContent} — download started`;
      cancelBtn.style.display = "none";
      closeBtn.style.display = "";
      barInner.classList.add("done");
    },
    cancelled() {
      title.textContent = "Export cancelled";
      cancelBtn.style.display = "none";
      closeBtn.style.display = "";
    },
    error(msg) {
      title.textContent = "Export failed";
      stats.textContent = msg;
      cancelBtn.style.display = "none";
      closeBtn.style.display = "";
      barInner.classList.add("error");
    },
  };
}

async function init() {
  // Mount the global Save button into the header slot. It stays disabled
  // until the user makes at least one editable-value change.
  const saveSlot = document.getElementById("save-slot");
  if (saveSlot) saveSlot.appendChild(saveButton);

  // Wrap the WZ file's root inside a single synthetic LI showing the file
  // name. Without this wrapper the root <ul id="tree"> is populated
  // directly and bypasses VirtualList — so a 64-bit WZ where the root
  // itself has hundreds of entries would never get virtualized.
  const wzPath = document.body.dataset.wzName || "WZ file";
  const baseName = wzPath.split(/[/\\]/).pop() || wzPath;
  const fileMeta = { name: baseName, kind: "Directory", leaf: false };
  const fileLi = makeNode(fileMeta, "");
  // Children of the WZ root live at path "" on the server, not "<filename>/".
  fileLi._fullPath = "";
  treeRoot.appendChild(fileLi);
  try {
    await expandLi(fileLi, "");
  } catch (err) {
    treeRoot.innerHTML = `<li style="color:#c47878">load error: ${err.message}</li>`;
  }
}

init();
