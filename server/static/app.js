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
    const wrap = document.createElement("div");
    wrap.className = "canvas-preview";
    const img = document.createElement("img");
    img.src = `/api/canvas/${encodeURI(path)}.png`;
    img.alt = child.name;
    wrap.appendChild(img);
    detailEl.appendChild(wrap);
  }
  if (child.kind === "Sound") {
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.src = `/api/sound/${encodeURI(path)}`;
    detailEl.appendChild(audio);
  }
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
