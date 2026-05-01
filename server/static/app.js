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
  // Drop the OK ones from the pending map. For rejections caused by
  // length mismatch, automatically retry via /api/edit (which mutates
  // the in-memory tree without touching the file). Anything else
  // (range error, unknown property, etc.) we surface to the user.
  const stageRetry = {};
  const trueFailures = [];
  for (const row of resp.results || []) {
    if (row.status === "ok") {
      pendingEdits.delete(row.path);
      continue;
    }
    const r = row.reason || "";
    const lengthMismatch =
      r.includes("encoded length changed") ||
      r.includes("string would change size");
    if (lengthMismatch) {
      stageRetry[row.path] = edits[row.path];
    } else {
      trueFailures.push(`  • ${row.path}: ${r}`);
    }
  }

  // Length-mismatch edits go to /api/edit (stage in memory; user must
  // call Save As to commit to disk).
  let stagedCount = 0;
  if (Object.keys(stageRetry).length) {
    try {
      const sr = await fetch("/api/edit", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({edits: stageRetry}),
      });
      const sresp = await sr.json();
      for (const row of sresp.results || []) {
        if (row.status === "ok") {
          pendingEdits.delete(row.path);
          stagedCount += 1;
        } else {
          trueFailures.push(`  • ${row.path}: ${row.reason}`);
        }
      }
      saveAsButton.dataset.dirty = String(sresp.dirty_count || 0);
      updateSaveAsButton();
    } catch (err) {
      trueFailures.push(`  • /api/edit fallback: ${err.message}`);
    }
  }

  document.querySelectorAll(".value-input.dirty").forEach((el) => {
    el.classList.remove("dirty");
  });
  updateSaveButton();

  if (trueFailures.length) {
    alert(
      `Saved in place ${resp.ok}/${resp.total}` +
      (stagedCount ? `; staged ${stagedCount} for Save As` : "") +
      `. Failed:\n${trueFailures.join("\n")}`,
    );
  } else if (stagedCount) {
    saveButton.textContent = `Saved ${resp.ok}, staged ${stagedCount}`;
    setTimeout(updateSaveButton, 2000);
  } else {
    saveButton.textContent = `Saved ${resp.ok}`;
    setTimeout(updateSaveButton, 1500);
  }
}

// ── Save As — flush staged variable-length edits to a new WZ file ──
const saveAsButton = document.createElement("button");
saveAsButton.className = "save-as-btn";
saveAsButton.textContent = "Save As…";
saveAsButton.title =
  "Re-serialize the entire archive (including any staged size-changing " +
  "edits) into a new WZ file on disk.";
saveAsButton.dataset.dirty = "0";
saveAsButton.addEventListener("click", runSaveAs);

function updateSaveAsButton() {
  const n = Number(saveAsButton.dataset.dirty || 0);
  saveAsButton.textContent = n ? `Save As… (${n})` : "Save As…";
  saveAsButton.classList.toggle("dirty", n > 0);
}

// Refresh dirty count from the server on load (in case prior edits
// were staged in another tab / session).
fetch("/api/dirty").then((r) => r.json()).then((d) => {
  saveAsButton.dataset.dirty = String(d.count || 0);
  updateSaveAsButton();
}).catch(() => {});

async function runSaveAs() {
  const wzPath = document.body.dataset.wzName || "";
  const def = wzPath ? wzPath.replace(/(\.wz)?$/i, ".modified.wz") : "modified.wz";
  const target = window.prompt(
    "Save As — full output path on the server filesystem.\n\n" +
    "All staged variable-length edits will be flushed.\n" +
    "Pass the original path with overwrite_original=true to overwrite.",
    def,
  );
  if (!target) return;
  saveAsButton.disabled = true;
  const orig = saveAsButton.textContent;
  saveAsButton.textContent = "Saving…";
  try {
    let r = await fetch("/api/save_as", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({path: target}),
    });
    if (r.status === 400) {
      const body = await r.text();
      if (body.includes("refusing to overwrite") &&
          confirm("That path is the live WZ file. Overwrite anyway? (Save As will close & re-open the file.)")) {
        r = await fetch("/api/save_as", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({path: target, overwrite_original: true}),
        });
      } else {
        alert(body);
        return;
      }
    }
    if (!r.ok) {
      // Server returns JSON {ok:false, error, type, trace} on 500.
      // Show the actual cause + abbreviated trace to the user.
      let detail;
      try {
        const body = await r.json();
        detail = `${body.type || "error"}: ${body.error || r.statusText}` +
                 (body.trace ? `\n\n${body.trace}` : "");
        console.error("[save_as]", body);
      } catch {
        detail = await r.text().catch(() => r.statusText);
      }
      alert(`Save As failed (${r.status}):\n\n${detail}`);
      return;
    }
    const data = await r.json();
    saveAsButton.dataset.dirty = "0";
    updateSaveAsButton();
    alert(
      `Save As complete.\n\n` +
      `  path: ${data.path}\n` +
      `  bytes: ${data.bytes}\n` +
      `  staged edits flushed: ${data.dirty_cleared}`,
    );
  } catch (err) {
    alert(`Save As failed: ${err.message}`);
  } finally {
    saveAsButton.disabled = false;
    saveAsButton.textContent = orig;
    updateSaveAsButton();
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

// ── tree search ────────────────────────────────────────────────────
const treeSearchInput = document.getElementById("tree-search-input");
const treeSearchResults = document.getElementById("tree-search-results");

let _treeSearchSeq = 0;     // drop stale responses
let _treeSearchTimer = 0;   // debounce timer

function clearTreeSearchResults() {
  treeSearchResults.innerHTML = "";
  treeSearchResults.hidden = true;
}

async function runTreeSearch(query) {
  const seq = ++_treeSearchSeq;
  const q = query.trim();
  if (!q) {
    clearTreeSearchResults();
    return;
  }
  try {
    const data = await fetchJson(`/api/search?q=${encodeURIComponent(q)}`);
    if (seq !== _treeSearchSeq) return;  // a newer search superseded us
    treeSearchResults.innerHTML = "";
    if (!data.results.length) {
      const li = document.createElement("li");
      li.className = "ts-empty";
      li.textContent = "No matches.";
      treeSearchResults.appendChild(li);
      treeSearchResults.hidden = false;
      return;
    }
    for (const r of data.results) {
      const li = document.createElement("li");
      li.dataset.path = r.path;
      li.dataset.kind = r.kind;
      const kind = document.createElement("span");
      kind.className = "ts-kind";
      kind.textContent = r.kind === "Directory" ? "dir" : "img";
      const path = document.createElement("span");
      path.className = "ts-path";
      path.textContent = r.path;
      li.appendChild(kind);
      li.appendChild(path);
      li.title = r.path;
      li.addEventListener("click", () => selectSearchResult(r));
      treeSearchResults.appendChild(li);
    }
    if (data.truncated) {
      const li = document.createElement("li");
      li.className = "ts-truncated";
      li.textContent = `… more results (capped at ${data.results.length}). Refine the query.`;
      treeSearchResults.appendChild(li);
    }
    treeSearchResults.hidden = false;
  } catch (err) {
    if (seq !== _treeSearchSeq) return;
    treeSearchResults.innerHTML = "";
    const li = document.createElement("li");
    li.className = "ts-empty";
    li.textContent = `Search failed: ${err.message}`;
    treeSearchResults.appendChild(li);
    treeSearchResults.hidden = false;
  }
}

async function selectSearchResult(r) {
  // Highlight the chosen result so the user sees what they picked.
  for (const el of treeSearchResults.querySelectorAll("li.selected")) {
    el.classList.remove("selected");
  }
  for (const el of treeSearchResults.querySelectorAll(`li[data-path="${CSS.escape(r.path)}"]`)) {
    el.classList.add("selected");
  }
  // Walk the tree to the result, auto-expanding each ancestor along
  // the way. ``navigateToPath`` returns the final LI so we can
  // select + scroll into view; on failure (virtualized child that
  // never rendered, expand error, etc.) fall back to populating the
  // detail panel without tree navigation so the user still sees
  // *something* useful.
  const li = await navigateToPath(r.path);
  if (li) {
    if (currentlySelected) currentlySelected.classList.remove("selected");
    const node = li.querySelector(".node");
    if (node) {
      node.classList.add("selected");
      currentlySelected = node;
    }
    li.scrollIntoView({ block: "center" });
    showDetail(li._fullPath, li._meta);
  } else {
    showDetail(r.path, { name: r.name, kind: r.kind, leaf: false });
  }
}

// Walk ``targetPath`` from the synthetic file root down, expanding
// each ancestor and returning the final LI. Returns ``null`` if any
// segment can't be resolved (path no longer exists, virtual child
// failed to render, expand fetch failed, …) so the caller can fall
// back gracefully.
async function navigateToPath(targetPath) {
  const fileLi = treeRoot.firstElementChild;
  if (!fileLi || !fileLi._childUl) return null;
  const segments = String(targetPath).split("/").filter(Boolean);
  if (!segments.length) return fileLi;

  let parentUl = fileLi._childUl;
  let lastLi = null;
  for (const seg of segments) {
    const li = await ensureChildRendered(parentUl, seg);
    if (!li) return null;
    if (!li._meta.leaf) {
      if (!li._childUl) {
        try {
          await expandLi(li, li._fullPath);
        } catch (err) {
          console.warn("[navigate] expand failed:", li._fullPath, err);
          return li;  // best we can do — return what we reached
        }
      } else if (li._childUl.style.display === "none") {
        li._childUl.style.display = "";
        li._twisty.textContent = "▾";
        notifyAncestorVirtualResize(li);
      }
    }
    lastLi = li;
    parentUl = li._childUl || null;
    if (!parentUl) break;  // reached a leaf or expand-less node
  }
  return lastLi;
}

// Find the LI for child ``name`` directly under ``ul``. For
// virtualized lists the child's LI may not be in the DOM yet —
// scroll the tree panel so VirtualList's range covers the child,
// then wait a few rAFs for the render to settle.
async function ensureChildRendered(ul, name) {
  if (!ul) return null;
  // Direct children (non-virtualized list, or virtualized item that's
  // already in the rendered range).
  for (const li of ul.children) {
    if (li._meta && li._meta.name === name) return li;
  }
  if (!ul._virtual) return null;
  const v = ul._virtual;
  const child = v.children.find((c) => c.name === name);
  if (!child) return null;
  if (child._li && ul.contains(child._li)) return child._li;

  // Scroll the tree panel so this child's row falls inside the
  // virtualized list's render window. Aim the row about a third
  // of the way down the visible panel so subsequent expansions
  // don't immediately push it back out of view.
  const ulRect = ul.getBoundingClientRect();
  const panelRect = treePanel.getBoundingClientRect();
  const childYInPanel = (ulRect.top - panelRect.top) + child._cumTop;
  treePanel.scrollTop += childYInPanel - panelRect.height / 3;
  v.requestUpdate();

  // Poll for the render to land. _update runs on rAF; allow a few
  // frames before giving up so we tolerate slow style/layout passes.
  for (let attempts = 0; attempts < 12; attempts++) {
    await new Promise((res) => requestAnimationFrame(res));
    if (child._li && ul.contains(child._li)) return child._li;
  }
  return null;
}

if (treeSearchInput) {
  treeSearchInput.addEventListener("input", () => {
    const q = treeSearchInput.value;
    clearTimeout(_treeSearchTimer);
    // Short debounce — 200ms is comfortable for typing without
    // firing per keystroke. Empty input clears results immediately
    // (no point waiting).
    if (!q.trim()) {
      _treeSearchSeq++;
      clearTreeSearchResults();
      return;
    }
    _treeSearchTimer = setTimeout(() => runTreeSearch(q), 200);
  });
  // Escape clears the input without losing focus.
  treeSearchInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && treeSearchInput.value) {
      treeSearchInput.value = "";
      _treeSearchSeq++;
      clearTreeSearchResults();
    }
  });
}

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

  // Structural edits — only meaningful for nodes that have a parent
  // (i.e. not the synthetic WZ-file root LI, whose fullPath is "").
  if (fullPath) {
    addItem("Rename…", () => runRename(fullPath));
    const removeItem = document.createElement("div");
    removeItem.className = "menu-item destructive";
    removeItem.textContent = "Remove…";
    removeItem.onclick = (e) => {
      e.stopPropagation();
      hideContextMenu();
      runRemove(fullPath, labelPath);
    };
    contextMenuEl.appendChild(removeItem);
  }

  // Add — what choices appear depends on what kind of container the
  // user right-clicked. Directories take Directory/Image children;
  // Images / SubProperties / Canvases take property values.
  const isContainer = ["image", "subproperty", "property", "canvas",
                        "directory"].includes(k);
  if (isContainer) {
    const addEntry = document.createElement("div");
    addEntry.className = "menu-item has-submenu";
    addEntry.textContent = "Add ▸";
    const sub = document.createElement("div");
    sub.className = "submenu";

    if (k === "directory") {
      // Directory parent: only Directory + Image are legal children.
      const dirItem = document.createElement("div");
      dirItem.className = "menu-item";
      dirItem.textContent = "Directory";
      dirItem.onclick = (e) => {
        e.stopPropagation(); hideContextMenu();
        runAdd(fullPath, "Directory");
      };
      sub.appendChild(dirItem);
      const imgItem = document.createElement("div");
      imgItem.className = "menu-item";
      imgItem.textContent = "Image";
      imgItem.onclick = (e) => {
        e.stopPropagation(); hideContextMenu();
        runAdd(fullPath, "Image");
      };
      sub.appendChild(imgItem);
    } else {
      // Image / SubProperty / Canvas parent: the property menu.
      const simpleTypes = [
        "Int", "Short", "Long", "Float", "Double",
        "String", "Vector", "SubProperty", "Null",
      ];
      for (const t of simpleTypes) {
        const it = document.createElement("div");
        it.className = "menu-item";
        it.textContent = t;
        it.onclick = (e) => {
          e.stopPropagation();
          hideContextMenu();
          runAdd(fullPath, t);
        };
        sub.appendChild(it);
      }
      const sep = document.createElement("div");
      sep.className = "menu-sep";
      sub.appendChild(sep);
      const canvasItem = document.createElement("div");
      canvasItem.className = "menu-item";
      canvasItem.textContent = "Canvas (PNG…)";
      canvasItem.onclick = (e) => {
        e.stopPropagation(); hideContextMenu();
        runAddCanvas(fullPath);
      };
      sub.appendChild(canvasItem);
      const soundItem = document.createElement("div");
      soundItem.className = "menu-item";
      soundItem.textContent = "Sound (MP3…)";
      soundItem.onclick = (e) => {
        e.stopPropagation(); hideContextMenu();
        runAddSound(fullPath);
      };
      sub.appendChild(soundItem);
    }
    addEntry.appendChild(sub);
    contextMenuEl.appendChild(addEntry);
  }
  if (fullPath || isContainer) {
    addSep();
  }
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
  addItem("Export sounds (keep tree structure)",
    () => triggerDownload(`/api/export/sounds/${enc}?layout=nested`));
  addItem("Export sounds (flatten into one folder)",
    () => triggerDownload(`/api/export/sounds/${enc}?layout=flat`));

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

// Rename — prompt for a new name, POST to /api/rename, then refresh
// the affected branch of the tree so the new name appears.
async function runRename(fullPath) {
  const oldName = fullPath.split("/").pop();
  const next = window.prompt(
    `Rename "${oldName}" — staged in memory; flush via Save As.`,
    oldName,
  );
  if (next === null) return;             // user cancelled
  const trimmed = next.trim();
  if (!trimmed || trimmed === oldName) return;

  try {
    const r = await fetch("/api/rename", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({path: fullPath, new_name: trimmed}),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok || body.ok === false) {
      alert(`Rename failed: ${body.reason || r.statusText}`);
      return;
    }
    // Bump the Save As dirty badge so the user sees that a flush is
    // pending. /api/dirty would also work but we already have the
    // count in the response.
    saveAsButton.dataset.dirty = String(body.dirty_count || 0);
    updateSaveAsButton();
    // Refresh the affected subtree: re-fetch the *parent* of the
    // renamed node and rebuild its child list, so the LI we see is
    // the new one with the new fullPath. For a renamed root-level
    // image the parent is the synthetic WZ-file LI (whose
    // _fullPath is "").
    const parentPath = fullPath.includes("/")
      ? fullPath.slice(0, fullPath.lastIndexOf("/"))
      : "";
    await refreshChildrenAt(parentPath);
  } catch (err) {
    alert(`Rename failed: ${err.message}`);
  }
}

// Add — prompt for the new property's name + per-type extra inputs,
// POST to /api/add, refresh the parent's tree so the new node shows.
async function runAdd(parentPath, kind) {
  // Helpful default: Images conventionally end in ``.img``. The
  // prompt's pre-fill makes the convention obvious without making it
  // mandatory (a name like ``Headers`` is still legal).
  const defaultName = kind === "Image" ? "Untitled.img" : "";
  const name = window.prompt(`Add ${kind} — name:`, defaultName);
  if (name === null) return;
  const trimmed = name.trim();
  if (!trimmed) return;
  if (trimmed.includes("/") || trimmed.includes("\\")) {
    alert("Name must not contain path separators.");
    return;
  }

  const body = {parent_path: parentPath, name: trimmed, kind};

  if (["Short", "Int", "Long"].includes(kind)) {
    const raw = window.prompt(`${kind} value:`, "0");
    if (raw === null) return;
    const v = parseInt(raw, 10);
    if (Number.isNaN(v)) { alert(`Invalid integer: ${raw}`); return; }
    body.value = v;
  } else if (["Float", "Double"].includes(kind)) {
    const raw = window.prompt(`${kind} value:`, "0");
    if (raw === null) return;
    const v = parseFloat(raw);
    if (Number.isNaN(v)) { alert(`Invalid number: ${raw}`); return; }
    body.value = v;
  } else if (kind === "String") {
    const raw = window.prompt("String value:", "");
    if (raw === null) return;
    body.value = raw;
  } else if (kind === "Vector") {
    const xs = window.prompt("Vector x:", "0");
    if (xs === null) return;
    const ys = window.prompt("Vector y:", "0");
    if (ys === null) return;
    const x = parseInt(xs, 10), y = parseInt(ys, 10);
    if (Number.isNaN(x) || Number.isNaN(y)) {
      alert(`Invalid coordinates: x=${xs}, y=${ys}`); return;
    }
    body.x = x; body.y = y;
  }
  // Null and SubProperty have no extra inputs.

  try {
    const r = await fetch("/api/add", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    const result = await r.json().catch(() => ({}));
    if (!r.ok || result.ok === false) {
      alert(`Add failed: ${result.reason || r.statusText}`);
      return;
    }
    saveAsButton.dataset.dirty = String(result.dirty_count || 0);
    updateSaveAsButton();
    // Use the expand-aware helper: if the parent was previously a
    // leaf (no children → no twisty, no childUl), refreshChildrenAt
    // would no-op. ``refreshOrExpandAt`` flips the leaf bit, draws
    // the twisty, and force-expands so the new child appears.
    await refreshOrExpandAt(parentPath);
  } catch (err) {
    alert(`Add failed: ${err.message}`);
  }
}

// Pick a single file matching ``accept`` and return it (or null on
// cancel). Reuses a transient hidden <input> so we don't accumulate
// pickers in the DOM.
function pickFile(accept) {
  return new Promise((resolve) => {
    const inp = document.createElement("input");
    inp.type = "file";
    inp.accept = accept;
    inp.style.display = "none";
    inp.addEventListener("change", () => {
      const f = inp.files && inp.files[0];
      inp.remove();
      resolve(f || null);
    }, { once: true });
    // Fire ``cancel`` resolve as well; not all browsers emit it but
    // the once handler will GC if the input is garbage-collected.
    document.body.appendChild(inp);
    inp.click();
  });
}

async function runAddCanvas(parentPath) {
  const file = await pickFile(".png,image/png");
  if (!file) return;
  const name = window.prompt("Canvas name:", file.name.replace(/\.png$/i, ""));
  if (name === null) return;
  const trimmed = name.trim();
  if (!trimmed) return;

  const fd = new FormData();
  fd.append("parent_path", parentPath);
  fd.append("name", trimmed);
  fd.append("image", file);
  await _runAddUpload("/api/add/canvas", fd, parentPath);
}

async function runAddSound(parentPath) {
  const file = await pickFile(".mp3,audio/mpeg,audio/mp3");
  if (!file) return;
  const name = window.prompt("Sound name:", file.name.replace(/\.mp3$/i, ""));
  if (name === null) return;
  const trimmed = name.trim();
  if (!trimmed) return;

  const fd = new FormData();
  fd.append("parent_path", parentPath);
  fd.append("name", trimmed);
  fd.append("audio", file);
  await _runAddUpload("/api/add/sound", fd, parentPath);
}

async function _runAddUpload(url, fd, parentPath) {
  try {
    const r = await fetch(url, { method: "POST", body: fd });
    const result = await r.json().catch(() => ({}));
    if (!r.ok || result.ok === false) {
      alert(`Add failed: ${result.reason || r.statusText}`);
      return;
    }
    saveAsButton.dataset.dirty = String(result.dirty_count || 0);
    updateSaveAsButton();
    await refreshOrExpandAt(parentPath);
  } catch (err) {
    alert(`Add failed: ${err.message}`);
  }
}

// Remove — confirm, POST, and refresh the parent's tree so the node
// disappears immediately.
async function runRemove(fullPath, labelPath) {
  const ok = window.confirm(
    `Remove "${labelPath || fullPath}"?\n\n` +
    `Staged in memory; flush via Save As. The change is reversible by ` +
    `simply not saving.`,
  );
  if (!ok) return;
  try {
    const r = await fetch("/api/remove", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({path: fullPath}),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok || body.ok === false) {
      alert(`Remove failed: ${body.reason || r.statusText}`);
      return;
    }
    saveAsButton.dataset.dirty = String(body.dirty_count || 0);
    updateSaveAsButton();
    // If the removed node was the currently-displayed one, blank
    // the detail panel — its content references a node that no
    // longer exists.
    if (currentlySelected && currentlySelected.parentElement &&
        currentlySelected.parentElement._fullPath === fullPath) {
      detailEl.innerHTML = '<p class="hint">Node removed. Save As to flush, or click another node.</p>';
      crumbsEl.innerHTML = "";
      currentlySelected = null;
    }
    await refreshChildrenAt(body.parent_path || "");
  } catch (err) {
    alert(`Remove failed: ${err.message}`);
  }
}

// Like ``refreshChildrenAt`` but also handles the case where the
// target was previously a leaf — its LI has no twisty and no
// ``_childUl``, so refreshChildrenAt would no-op. Used after Add
// so that a SubProperty created with no children (rendered as a
// leaf at the time) becomes expandable as soon as a child lands.
async function refreshOrExpandAt(fullPath) {
  let li = null;
  for (const el of treeRoot.getElementsByClassName("node")) {
    const candidate = el.parentElement;
    if (candidate && candidate._fullPath === fullPath) {
      li = candidate;
      break;
    }
  }
  if (!li) return;
  // Tear down any existing children so expandLi will re-fetch.
  if (li._childUl) {
    li._childUl.remove();
    li._childUl = null;
    li._loaded = false;
  }
  // Promote a previously-leaf LI: flip the meta flag and paint a
  // twisty so the click affordance appears even before expansion.
  if (li._meta && li._meta.leaf) {
    li._meta.leaf = false;
    if (li._twisty && !li._twisty.textContent.trim()) {
      li._twisty.textContent = "▸";
    }
  }
  await expandLi(li, fullPath);
}

// Re-fetch the children of whichever node currently lives at
// ``fullPath`` and replace its child UL in place. Used after a
// structural change (rename, remove, future add).
async function refreshChildrenAt(fullPath) {
  // Find the LI for this path, if any (it might be in the virtualized
  // window or fully expanded). For the root we use the synthetic
  // WZ-file LI which has _fullPath "".
  let li = null;
  for (const el of treeRoot.getElementsByClassName("node")) {
    const candidate = el.parentElement;
    if (candidate && candidate._fullPath === fullPath) {
      li = candidate;
      break;
    }
  }
  if (!li || !li._childUl) return;       // nothing to refresh visibly

  // Tear down existing UL + force a fresh fetch via expandLi.
  li._childUl.remove();
  li._childUl = null;
  li._loaded = false;
  li._twisty.textContent = "▾";
  await expandLi(li, fullPath);
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

// Active canvas viewer's AbortController — invoked before we replace
// detailEl so the previous viewer's window-level mousemove/mouseup
// listeners are removed instead of accumulating forever.
let activeViewerAbort = null;

// Navigate the detail panel to ``path`` without touching the tree
// (expansion of intermediate nodes is best-effort and would fight with
// virtualization). Used by UOL target links — fetches the target's
// description and re-renders showDetail.
async function revealAndSelect(path) {
  try {
    const meta = await fetchJson(`/api/property/${encodeURI(path)}`);
    showDetail(path, meta);
  } catch (err) {
    console.warn(`[uol-link] could not load ${path}: ${err.message}`);
  }
}

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
    } else if (k === "target_path" && typeof v === "string") {
      // Clickable jump-to-target so the user can navigate to the node
      // the UOL points at.
      const a = document.createElement("a");
      a.href = "#";
      a.textContent = v;
      a.dataset.path = v;
      a.className = "uol-target-link";
      a.addEventListener("click", (e) => {
        e.preventDefault();
        revealAndSelect(v);
      });
      td2.appendChild(a);
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
  // UOL: inline the referenced value so the user sees the actual image,
  // sound, or scalar instead of just the relative path string.
  if (child.kind === "UOL" && child.target_kind && child.target_path) {
    if (child.target_kind === "Canvas" && child.target_renderable) {
      detailEl.appendChild(makeCanvasViewer(child.target_path, {
        name: child.name,
        width: child.target_width,
        height: child.target_height,
        format: child.target_format,
      }, { readOnly: true }));
    } else if (child.target_kind === "Sound") {
      const audio = document.createElement("audio");
      audio.controls = true;
      audio.src = `/api/sound/${encodeURI(child.target_path)}`;
      detailEl.appendChild(audio);
    }
  }
  // Animation hint: when a SubProperty's children include numbered
  // Canvases (the WZ pattern for animation frames), offer a Play
  // button. Detection happens after the tree fetch via
  // ``maybeOfferAnimation`` — at this point we don't yet know the
  // grandchildren's kinds. The placeholder gets replaced once the
  // grandchildren are listed.
  if (child.kind === "SubProperty" || child.kind === "Property") {
    const slot = document.createElement("div");
    slot.className = "animation-slot";
    slot.dataset.path = path;
    detailEl.appendChild(slot);
    maybeOfferAnimation(path, slot);
  }
}

// ── zoomable canvas viewer ───────────────────────────────────────────
// Wheel zooms toward the cursor, drag pans, +/- and 0 keys work while the
// viewer is focused, and image-rendering: pixelated keeps sprites crisp.

function makeCanvasViewer(path, meta, options = {}) {
  const readOnly = !!options.readOnly;
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
  // the toolbar layout uncluttered. Suppressed for read-only viewers
  // (e.g. UOL targets — replacing through a symlink is ambiguous).
  const filePicker = readOnly ? null : document.createElement("input");
  const replaceBtn = readOnly ? null : btn("Replace…",
    "Replace this canvas with an uploaded image (must fit the original byte slot)",
    () => filePicker.click());
  if (!readOnly) {
    filePicker.type = "file";
    filePicker.accept = "image/*";
    filePicker.style.display = "none";
    toolbar.appendChild(filePicker);
    replaceBtn.classList.add("replace-btn");
    toolbar.appendChild(replaceBtn);
  }
  if (!readOnly) filePicker.addEventListener("change", async () => {
    const file = filePicker.files && filePicker.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("image", file);
    const orig = replaceBtn.textContent;
    replaceBtn.textContent = "Replacing…";
    replaceBtn.disabled = true;
    try {
      // Try the in-place path first. If the new payload doesn't fit
      // the original byte slot — or for any other 4xx — fall back to
      // the staged path which marks the canvas dirty for Save As.
      // We treat *any* in-place 4xx as a fallback trigger because the
      // legitimate user goal is "replace this image" and /stage will
      // either succeed (slot overflow → staged for Save As) or report
      // the real reason for failure (DXT format, bad image, etc.).
      let r = await fetch(`/api/canvas/${encodeURI(path)}`,
        { method: "POST", body: fd });
      let body = await r.json().catch(() => ({}));
      let inPlaceReason = body.reason || body.message || "";

      if (!r.ok && r.status >= 400 && r.status < 500) {
        const fd2 = new FormData();
        fd2.append("image", file);
        const r2 = await fetch(`/api/canvas/${encodeURI(path)}/stage`,
          { method: "POST", body: fd2 });
        const body2 = await r2.json().catch(() => ({}));
        if (r2.ok && body2.staged) {
          img.src = `/api/canvas/${encodeURI(path)}.png?t=${Date.now()}`;
          saveAsButton.dataset.dirty = String(body2.dirty_count || 0);
          updateSaveAsButton();
          replaceBtn.textContent = `Staged ${body2.payload_bytes}b — Save As`;
          setTimeout(() => { replaceBtn.textContent = orig; }, 2400);
          return;
        }
        // Stage also failed — the JSON ``reason`` is the authoritative
        // cause (e.g. "writing canvas format 1026 (DXT) is not
        // supported"). Falls through to the alert below with whichever
        // reason we have.
        const stageReason = body2.reason || body2.message || r2.statusText;
        alert(`Replace failed.\n\nIn-place: ${inPlaceReason || r.statusText}\nStaged:   ${stageReason}`);
        return;
      }
      if (!r.ok || body.ok === false) {
        alert(`Replace failed: ${inPlaceReason || r.statusText}`);
      } else {
        // In-place succeeded — cache-bust so the viewer re-fetches.
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
  // Time the load so we can see (in the browser console) when an image
  // takes a long time end-to-end. The server's Server-Timing header
  // shows the per-phase split — read here via the Resource Timing API.
  const url = `/api/canvas/${encodeURI(path)}.png`;
  const tFetchStart = performance.now();
  img.addEventListener("load", () => {
    const total = performance.now() - tFetchStart;
    if (total < 100) return;          // ignore fast loads to keep noise down
    // Pull server timings + sizes from the Resource Timing entry (only
    // populated for cross-origin requests if Timing-Allow-Origin is set,
    // but same-origin always works).
    let st = null;
    for (const e of performance.getEntriesByType("resource")) {
      if (e.name.endsWith(url) || e.name.endsWith(url + "?t=" + tFetchStart.toString())) {
        st = e; break;
      }
    }
    const transfer = st ? `${(st.transferSize || 0) | 0}b` : "?";
    console.log(
      `[canvas ${total.toFixed(0)}ms] ${path}  transfer=${transfer}` +
      (st && st.serverTiming ?
        ("  server={" +
         st.serverTiming.map((t) => `${t.name}=${t.duration.toFixed(0)}`).join(" ") +
         "}") : ""),
    );
  });
  img.addEventListener("error", () => {
    const total = performance.now() - tFetchStart;
    console.warn(`[canvas LOAD-FAIL ${total.toFixed(0)}ms] ${path}`);
  });
  img.src = url;
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

// ── animation player ─────────────────────────────────────────────────
// WZ stores animations as a SubProperty whose children are numbered
// Canvases (0, 1, 2, ...). Each frame typically has a ``delay`` Int
// (ms) and an ``origin`` Vector (anchor) as siblings of the bitmap.
// /api/animation/<path> rolls all that into one response so the player
// only needs a single round-trip to start playing.

async function maybeOfferAnimation(path, slot) {
  let data;
  try {
    const r = await fetch(`/api/animation/${encodeURI(path)}`);
    // 404 (target isn't a SubProperty) and 204 (SubProperty but no
    // animation-shaped children) both mean "no offer" — bail without
    // painting an empty player. The 204 path is what most non-
    // animation SubProperty clicks land on, so the probe stays quiet
    // in the access log.
    if (r.status === 204 || !r.ok) return;
    data = await r.json();
  } catch {
    return;
  }
  if (!data.frames || data.frames.length < 2) return;

  // The slot may have been replaced if the user clicked a different
  // node by now; bail if our placeholder isn't on the page anymore.
  if (!slot.isConnected) return;

  const wrap = document.createElement("div");
  wrap.className = "animation-offer";
  const summary = document.createElement("div");
  summary.className = "animation-summary";
  const totalMs = data.frames.reduce((s, f) => s + f.delay_ms, 0);
  summary.textContent =
    `Animation: ${data.frames.length} frames, total ${totalMs} ms`;
  wrap.appendChild(summary);

  const playBtn = document.createElement("button");
  playBtn.className = "animation-play-btn";
  playBtn.textContent = "▶ Play animation";
  wrap.appendChild(playBtn);

  let player = null;
  playBtn.addEventListener("click", () => {
    if (player) {
      player.toggleVisible();
      return;
    }
    player = makeAnimationPlayer(data);
    wrap.appendChild(player.root);
    playBtn.style.display = "none";
  });

  slot.replaceChildren(wrap);
}

function makeAnimationPlayer(data) {
  const frames = data.frames;
  const root = document.createElement("div");
  root.className = "animation-player";

  // ── viewport sizing ──
  // To align by origin, we need a viewport whose origin point sits at
  // a fixed pixel position. Compute the bounding box across all frames
  // when each frame is positioned with its origin on (0, 0). The
  // result tells us how far left/up/right/down any pixel might extend.
  let minX = 0, minY = 0, maxX = 0, maxY = 0;
  for (const f of frames) {
    const ox = f.origin?.x || 0;
    const oy = f.origin?.y || 0;
    minX = Math.min(minX, -ox);
    minY = Math.min(minY, -oy);
    maxX = Math.max(maxX, f.width - ox);
    maxY = Math.max(maxY, f.height - oy);
  }
  const vpW = Math.max(1, maxX - minX);
  const vpH = Math.max(1, maxY - minY);
  // Position of origin (0, 0) in viewport coordinates:
  const originVpX = -minX;
  const originVpY = -minY;

  // ── DOM ──
  const toolbar = document.createElement("div");
  toolbar.className = "animation-toolbar";
  const btn = (label, title, fn) => {
    const b = document.createElement("button");
    b.className = "animation-btn";
    b.textContent = label; b.title = title;
    b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
    return b;
  };

  const playPauseBtn = btn("⏸", "Pause/play (space)", () => {
    state.playing = !state.playing;
    playPauseBtn.textContent = state.playing ? "⏸" : "▶";
    if (state.playing) tick();
  });
  toolbar.appendChild(playPauseBtn);
  toolbar.appendChild(btn("⏮", "First frame", () => seek(0)));
  toolbar.appendChild(btn("⏭", "Last frame",
    () => seek(frames.length - 1)));

  const speedSel = document.createElement("select");
  speedSel.className = "animation-speed";
  speedSel.title = "Playback speed";
  for (const v of [0.25, 0.5, 1, 2, 4]) {
    const o = document.createElement("option");
    o.value = String(v); o.textContent = v + "×";
    if (v === 1) o.selected = true;
    speedSel.appendChild(o);
  }
  speedSel.addEventListener("change", () => {
    state.speed = Number(speedSel.value);
  });
  toolbar.appendChild(speedSel);

  const loopLabel = document.createElement("label");
  loopLabel.className = "animation-toggle";
  const loopCb = document.createElement("input");
  loopCb.type = "checkbox"; loopCb.checked = true;
  loopCb.addEventListener("change", () => { state.loop = loopCb.checked; });
  loopLabel.appendChild(loopCb);
  loopLabel.appendChild(document.createTextNode(" loop"));
  toolbar.appendChild(loopLabel);

  const alignLabel = document.createElement("label");
  alignLabel.className = "animation-toggle";
  const alignCb = document.createElement("input");
  alignCb.type = "checkbox"; alignCb.checked = true;
  alignCb.title = "Align frames by their origin point so the character anchor stays fixed";
  alignCb.addEventListener("change", () => {
    state.alignOrigin = alignCb.checked;
    placeFrame(state.idx);
  });
  alignLabel.appendChild(alignCb);
  alignLabel.appendChild(document.createTextNode(" align origin"));
  toolbar.appendChild(alignLabel);

  // Fit / 1:1 toggle. Big monsters easily blow past 600 px wide and
  // need scaling to fit the right-hand panel; small sprites should
  // display at native size.
  const fitBtn = btn("Fit", "Fit / 1:1 (toggle)", () => {
    state.fitMode = !state.fitMode;
    fitBtn.textContent = state.fitMode ? "Fit" : "1:1";
    fitBtn.title = state.fitMode ? "Currently fit; click for 1:1" :
                                    "Currently 1:1; click for fit";
    applyScale();
  });
  toolbar.appendChild(fitBtn);

  const counter = document.createElement("span");
  counter.className = "animation-counter";
  toolbar.appendChild(counter);

  root.appendChild(toolbar);

  // Stage scroller wraps the scaled stage so a too-large 1:1 view
  // can scroll horizontally / vertically without breaking the rest of
  // the panel layout.
  const scroller = document.createElement("div");
  scroller.className = "animation-stage-scroller";
  root.appendChild(scroller);

  // Stage holds the viewport at its scaled (rendered) dimensions so
  // surrounding flow reserves the right amount of space.
  const stage = document.createElement("div");
  stage.className = "animation-stage";
  scroller.appendChild(stage);

  // Viewport is the unscaled coordinate space — width/height match
  // the bounding box of the (origin-aligned) frames. We apply a CSS
  // scale to it to fit the available width.
  const viewport = document.createElement("div");
  viewport.className = "animation-viewport";
  viewport.style.width = vpW + "px";
  viewport.style.height = vpH + "px";
  viewport.style.transformOrigin = "top left";
  stage.appendChild(viewport);

  // Preload all frame images and stack them — only the active one is
  // displayed at a time. This avoids a flash on swap.
  const imgs = frames.map((f, i) => {
    const im = document.createElement("img");
    im.className = "animation-frame";
    im.src = f.url;
    im.alt = "frame " + i;
    im.draggable = false;
    im.style.display = "none";
    viewport.appendChild(im);
    return im;
  });

  // Scrubber
  const slider = document.createElement("input");
  slider.type = "range";
  slider.min = "0";
  slider.max = String(frames.length - 1);
  slider.value = "0";
  slider.className = "animation-scrubber";
  slider.addEventListener("input", () => {
    state.playing = false;
    playPauseBtn.textContent = "▶";
    seek(Number(slider.value));
  });
  root.appendChild(slider);

  const state = {
    idx: 0,
    playing: true,
    speed: 1,
    loop: true,
    alignOrigin: true,
    fitMode: true,        // start fit-to-container; user can flip to 1:1
    timer: null,
    visible: true,
  };

  function applyScale() {
    if (!state.fitMode) {
      // 1:1 — let the scroller decide whether scrollbars are needed.
      viewport.style.transform = "none";
      stage.style.width = vpW + "px";
      stage.style.height = vpH + "px";
      return;
    }
    // Fit: never scale UP automatically (small sprites stay crisp);
    // scale DOWN if necessary so the stage fits the scroller's width.
    // Use the scroller's current clientWidth — that respects the
    // detail panel's own padding/sidebar.
    const availW = scroller.clientWidth || 720;
    // Cap the height so a tall animation doesn't push controls off
    // screen on short windows.
    const availH = Math.max(160, Math.min(window.innerHeight * 0.55, 800));
    const s = Math.min(availW / vpW, availH / vpH, 1);
    viewport.style.transform = `scale(${s})`;
    stage.style.width = (vpW * s) + "px";
    stage.style.height = (vpH * s) + "px";
  }

  function placeFrame(i) {
    const f = frames[i];
    const im = imgs[i];
    const ox = state.alignOrigin ? (f.origin?.x || 0) : 0;
    const oy = state.alignOrigin ? (f.origin?.y || 0) : 0;
    im.style.left = (originVpX - ox) + "px";
    im.style.top = (originVpY - oy) + "px";
  }

  function showFrame(i) {
    placeFrame(i);
    for (let j = 0; j < imgs.length; j++) {
      imgs[j].style.display = j === i ? "block" : "none";
    }
    counter.textContent =
      `${i + 1} / ${frames.length}  (${frames[i].delay_ms} ms)`;
    slider.value = String(i);
  }

  function seek(i) {
    state.idx = ((i % frames.length) + frames.length) % frames.length;
    showFrame(state.idx);
  }

  function tick() {
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }
    if (!state.playing || !state.visible) return;
    const wait = frames[state.idx].delay_ms / Math.max(0.05, state.speed);
    state.timer = setTimeout(() => {
      const next = state.idx + 1;
      if (next >= frames.length) {
        if (!state.loop) {
          state.playing = false;
          playPauseBtn.textContent = "▶";
          return;
        }
        state.idx = 0;
      } else {
        state.idx = next;
      }
      showFrame(state.idx);
      tick();
    }, wait);
  }

  // Pause when the viewport is no longer onscreen (e.g. user clicked
  // a different node) so we stop chewing CPU on a hidden animation.
  const observer = new IntersectionObserver((entries) => {
    for (const e of entries) {
      state.visible = e.isIntersecting;
      if (state.visible && state.playing) tick();
      else if (state.timer) { clearTimeout(state.timer); state.timer = null; }
    }
  });
  observer.observe(viewport);

  // Re-fit when the viewport's available width changes — e.g. user
  // resizes the tree-panel divider or the window. Falls back to the
  // global resize event for older browsers without ResizeObserver.
  let resizeObs = null;
  if (typeof ResizeObserver !== "undefined") {
    resizeObs = new ResizeObserver(() => applyScale());
    resizeObs.observe(scroller);
  } else {
    window.addEventListener("resize", applyScale);
  }

  // Initial layout — defer one frame so the scroller has a real
  // ``clientWidth``. Without this, scroller.clientWidth is 0 because
  // the player isn't in the DOM yet when ``makeAnimationPlayer``
  // returns.
  requestAnimationFrame(applyScale);
  showFrame(0);
  tick();

  return {
    root,
    toggleVisible() {
      root.style.display = root.style.display === "none" ? "" : "none";
      // Re-fit on show in case the panel size changed while hidden.
      if (root.style.display !== "none") applyScale();
    },
  };
}

async function init() {
  // Mount the global Save button into the header slot. It stays disabled
  // until the user makes at least one editable-value change. The Save As
  // button next to it commits any size-changing edits that were staged
  // (size-changing edits skip the in-place /api/save and land in
  // /api/edit instead).
  const saveSlot = document.getElementById("save-slot");
  if (saveSlot) {
    saveSlot.appendChild(saveButton);
    saveSlot.appendChild(saveAsButton);
  }

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
