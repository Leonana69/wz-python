// Welcome / open-file page driver. POSTs the path to /api/load and
// redirects on success. Used by welcome.html (rendered both at /
// when no WZ is loaded and at /open as the "Switch file…" page).

const $form = document.getElementById("open-form");
const $path = document.getElementById("open-path");
const $region = document.getElementById("open-region");
const $version = document.getElementById("open-version");
const $submit = document.getElementById("open-submit");
const $status = document.getElementById("open-status");

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
    setStatus(
      "info",
      `Loaded ${data.path} (${data.region}, v${data.version}). Redirecting…`,
    );
    const dest = window._WZPY_REDIRECT_AFTER_LOAD || "/";
    window.location.href = dest;
  } catch (err) {
    setStatus("error", `Network error: ${err.message || err}`);
    $submit.disabled = false;
  }
});

// Auto-focus the path input when the page loads with an empty value.
if ($path && !$path.value) $path.focus();
