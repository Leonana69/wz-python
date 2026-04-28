// Tiny browser frontend for wzpy. Lazy-loads tree children on expand.

const treeRoot = document.getElementById("tree");
const detailEl = document.getElementById("detail");
const crumbsEl = document.getElementById("breadcrumbs");

const KIND_ICONS = {
  directory: "📁", Directory: "📁",
  image: "🖼", Image: "🖼",
  SubProperty: "▸", Property: "▸",
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
  }

  li.appendChild(node);

  let childUl = null;
  let loaded = false;

  node.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    document.querySelectorAll(".node.selected").forEach((n) => n.classList.remove("selected"));
    node.classList.add("selected");
    showDetail(fullPath, child);

    if (child.leaf) return;
    if (childUl) {
      childUl.style.display = childUl.style.display === "none" ? "" : "none";
      twisty.textContent = childUl.style.display === "none" ? "▸" : "▾";
      return;
    }
    twisty.textContent = "…";
    try {
      const data = await fetchJson(`/api/tree/${encodeURI(fullPath)}`);
      childUl = document.createElement("ul");
      for (const c of data.children) childUl.appendChild(makeNode(c, fullPath));
      li.appendChild(childUl);
      loaded = true;
      twisty.textContent = "▾";
    } catch (err) {
      twisty.textContent = "▸";
      console.error(err);
    }
  });

  return li;
}

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

function showDetail(path, child) {
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

  // Initial scale: tiny sprites look better starting at 4×; large ones fit.
  img.addEventListener("load", () => {
    const w = img.naturalWidth, h = img.naturalHeight;
    const rect = viewport.getBoundingClientRect();
    if (w * 4 <= rect.width - 24 && h * 4 <= rect.height - 24) {
      setZoom(Math.max(1, Math.min(8, Math.floor((rect.width - 24) / w))));
    } else {
      fitToViewport();
    }
  });

  // Wheel zoom
  viewport.addEventListener("wheel", (e) => {
    e.preventDefault();
    const factor = Math.exp(-e.deltaY * 0.0015);
    setZoom(scale * factor, e.clientX, e.clientY);
  }, { passive: false });

  // Drag to pan
  let dragging = false, startX = 0, startY = 0, startTx = 0, startTy = 0;
  viewport.addEventListener("mousedown", (e) => {
    dragging = true; startX = e.clientX; startY = e.clientY;
    startTx = tx; startTy = ty;
    viewport.classList.add("grabbing");
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    tx = startTx + (e.clientX - startX);
    ty = startTy + (e.clientY - startY);
    applyTransform();
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    viewport.classList.remove("grabbing");
  });

  // Keyboard
  root.addEventListener("keydown", (e) => {
    if (e.key === "+" || e.key === "=") { zoomBy(1.25); e.preventDefault(); }
    else if (e.key === "-" || e.key === "_") { zoomBy(1 / 1.25); e.preventDefault(); }
    else if (e.key === "0") { setZoom(1); e.preventDefault(); }
    else if (e.key.toLowerCase() === "f") { fitToViewport(); e.preventDefault(); }
  });

  applyTransform();
  return root;
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
