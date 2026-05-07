// Welcome / open-file page driver. POSTs the path to /api/load and
// redirects on success. Used by welcome.html (rendered both at /
// when no WZ is loaded and at /open as the "Switch file…" page).

const $form = document.getElementById("open-form");
const $path = document.getElementById("open-path");
const $region = document.getElementById("open-region");
const $version = document.getElementById("open-version");
const $submit = document.getElementById("open-submit");
const $status = document.getElementById("open-status");
const $browseFile = document.getElementById("open-browse-file");
const $browseFolder = document.getElementById("open-browse-folder");
const $charMode = document.getElementById("open-char");

function setStatus(kind, text) {
  if (!$status) return;
  if (!text) {
    $status.hidden = true;
    $status.textContent = "";
    $status.className = "open-status";
    return;
  }
  $status.hidden = false;
  $status.textContent = text;
  $status.className = `open-status is-${kind}`;
}

$form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const path = $path.value.trim();
  if (!path) {
    setStatus("error", "Path is required.");
    $path.focus();
    return;
  }
  const body = {
    path,
    region: $region.value || "auto",
    version: $version.value ? Number($version.value) : null,
    char: !!($charMode && $charMode.checked),
  };
  $submit.disabled = true;
  setStatus("info", "Loading…");
  try {
    const resp = await fetch("/api/load", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) {
      setStatus("error", data.error || `HTTP ${resp.status}`);
      $submit.disabled = false;
      return;
    }
    let summary = `Loaded ${data.path} (${data.region}, v${data.version}).`;
    if (data.warning) {
      // Char-mode partial load: show the warning briefly, but still
      // redirect — the user can see the situation in the builder.
      summary += " " + data.warning;
    }
    summary += " Redirecting…";
    setStatus(data.warning ? "error" : "info", summary);
    // Destination preference:
    //   1. Explicit ``_WZPY_REDIRECT_AFTER_LOAD`` (set when the user
    //      hit /character with no WZ loaded — honor it if the new WZ
    //      can serve that page, else fall back to /).
    //   2. Character packs land on /character by default — that's the
    //      page users open them for, and it saves a click.
    //   3. Everything else lands on the tree browser.
    let dest = window._WZPY_REDIRECT_AFTER_LOAD;
    if (dest === "/character" && !data.has_character) {
      dest = "/";
    } else if (!dest) {
      dest = data.has_character ? "/character" : "/";
    }
    // Brief delay if there's a warning so the user can read it.
    setTimeout(() => { window.location.href = dest; },
               data.warning ? 1500 : 0);
  } catch (err) {
    setStatus("error", `Network error: ${err.message || err}`);
    $submit.disabled = false;
  }
});

// Native file/folder picker. Hits /api/load/browse, which spawns a
// tkinter dialog on the server (= the user's machine, since wzpy
// runs locally). On success, fills the path input; on cancel, just
// re-focuses it.
async function browse(kind) {
  const initial = $path.value.trim();
  const url = `/api/load/browse?kind=${encodeURIComponent(kind)}` +
    (initial ? `&initial=${encodeURIComponent(initial)}` : "");
  const buttons = [$browseFile, $browseFolder];
  buttons.forEach(b => b && (b.disabled = true));
  setStatus(null);
  try {
    const resp = await fetch(url);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      setStatus("error", data.error || `HTTP ${resp.status}`);
      return;
    }
    if (data.cancelled || !data.path) {
      // Silent — cancellation isn't an error.
      return;
    }
    $path.value = data.path;
    $path.focus();
  } catch (err) {
    setStatus("error", `Network error: ${err.message || err}`);
  } finally {
    buttons.forEach(b => b && (b.disabled = false));
  }
}

if ($browseFile) $browseFile.addEventListener("click", () => browse("file"));
if ($browseFolder) $browseFolder.addEventListener("click", () => browse("folder"));

// Recent-paths quick-load: prefill the path input and submit the form
// in the same flow as a manual load. Lets the user one-click between
// any of the paths the CLI was launched with.
for (const btn of document.querySelectorAll(".open-recent-btn")) {
  btn.addEventListener("click", () => {
    const p = btn.getAttribute("data-path");
    if (!p) return;
    $path.value = p;
    if ($form.requestSubmit) $form.requestSubmit();
    else $form.dispatchEvent(new Event("submit", { cancelable: true }));
  });
}

// Auto-focus the path input when the page loads with an empty value.
if ($path && !$path.value) $path.focus();
