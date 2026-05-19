const state = {
  tracks: [],
  runs: [],
  editingTrackId: null
};

const els = {
  adminStatus: document.getElementById("adminStatus"),
  adminToken: document.getElementById("adminToken"),
  saveTokenButton: document.getElementById("saveTokenButton"),
  reloadButton: document.getElementById("reloadButton"),
  statRuns: document.getElementById("statRuns"),
  statTracks: document.getElementById("statTracks"),
  statVariants: document.getElementById("statVariants"),
  statDisk: document.getElementById("statDisk"),
  tracksBody: document.getElementById("tracksBody"),
  runsBody: document.getElementById("runsBody"),
  runTrackFilter: document.getElementById("runTrackFilter"),
  newTrackButton: document.getElementById("newTrackButton"),
  trackForm: document.getElementById("trackForm"),
  trackFormTitle: document.getElementById("trackFormTitle"),
  trackIdInput: document.getElementById("trackIdInput"),
  trackNameInput: document.getElementById("trackNameInput"),
  trackOrderInput: document.getElementById("trackOrderInput"),
  trackMinInput: document.getElementById("trackMinInput"),
  trackMaxInput: document.getElementById("trackMaxInput"),
  cancelTrackButton: document.getElementById("cancelTrackButton"),
  resetGeneratedButton: document.getElementById("resetGeneratedButton")
};

function token() {
  return els.adminToken.value || "DEMO_ADMIN_TOKEN";
}

function headers(extra = {}) {
  return {
    Authorization: `Bearer ${token()}`,
    ...extra
  };
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: headers(options.headers || {})
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || `Request failed: ${response.status}`);
  }
  return payload;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;"
  }[char]));
}

function formatTime(ms) {
  const minutes = Math.floor(ms / 60000);
  const seconds = Math.floor((ms % 60000) / 1000);
  const millis = ms % 1000;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

async function loadAll() {
  try {
    const summary = await api("/api/admin/summary");
    state.tracks = summary.tracks;
    els.statRuns.textContent = summary.run_count;
    els.statTracks.textContent = summary.tracks.length;
    els.statVariants.textContent = summary.variant_count;
    els.statDisk.textContent = formatBytes(summary.disk.free);
    renderTracks();
    renderTrackFilter();
    await loadRuns();
    els.adminStatus.textContent = "Developer tools ready.";
  } catch (error) {
    els.adminStatus.textContent = error.message;
  }
}

async function loadRuns() {
  const params = new URLSearchParams();
  if (els.runTrackFilter.value) params.set("track_id", els.runTrackFilter.value);
  const query = params.toString();
  const payload = await api(query ? `/api/admin/runs?${query}` : "/api/admin/runs");
  state.runs = payload.runs;
  renderRuns();
}

function renderTrackFilter() {
  const previous = els.runTrackFilter.value;
  els.runTrackFilter.innerHTML = `<option value="">All tracks</option>`;
  for (const track of state.tracks) {
    els.runTrackFilter.append(new Option(track.display_name, track.id));
  }
  if ([...els.runTrackFilter.options].some(option => option.value === previous)) {
    els.runTrackFilter.value = previous;
  }
}

function renderTracks() {
  els.tracksBody.innerHTML = "";
  for (const track of state.tracks) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><code>${escapeHtml(track.id)}</code></td>
      <td>${escapeHtml(track.display_name)}</td>
      <td>${track.sort_order}</td>
      <td>${track.min_time_ms}</td>
      <td>${track.max_time_ms}</td>
      <td>${track.run_count}</td>
      <td class="rowActions">
        <button type="button" data-action="edit" data-id="${escapeHtml(track.id)}">Edit</button>
        <button type="button" data-action="delete" data-id="${escapeHtml(track.id)}" ${track.run_count ? "disabled" : ""}>Delete</button>
      </td>
    `;
    els.tracksBody.append(row);
  }
}

function renderRuns() {
  els.runsBody.innerHTML = "";
  for (const run of state.runs) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${escapeHtml(run.username)}</td>
      <td>${escapeHtml(run.track_name)}</td>
      <td class="time">${formatTime(run.completion_time_ms)}</td>
      <td><span class="status ${escapeHtml(run.status)}">${escapeHtml(run.status)}</span></td>
      <td>${new Date(run.upload_time).toLocaleString()}</td>
      <td>${formatBytes(run.file_size_bytes)} / ${run.variant_count} variants</td>
      <td class="rowActions">
        <a class="button" href="${run.original_url}">Video</a>
        <button type="button" data-action="retry-thumbnail" data-id="${escapeHtml(run.id)}">Retry Thumb</button>
        <button type="button" data-action="delete-run" data-id="${escapeHtml(run.id)}">Delete</button>
      </td>
    `;
    els.runsBody.append(row);
  }
}

function showTrackForm(track = null) {
  state.editingTrackId = track?.id || null;
  els.trackForm.hidden = false;
  els.trackFormTitle.textContent = track ? `Edit ${track.display_name}` : "New Track";
  els.trackIdInput.disabled = Boolean(track);
  els.trackIdInput.value = track?.id || "";
  els.trackNameInput.value = track?.display_name || "";
  els.trackOrderInput.value = track?.sort_order ?? 100;
  els.trackMinInput.value = track?.min_time_ms ?? 1;
  els.trackMaxInput.value = track?.max_time_ms ?? 600000;
}

function hideTrackForm() {
  state.editingTrackId = null;
  els.trackForm.hidden = true;
  els.trackForm.reset();
  els.trackIdInput.disabled = false;
}

async function saveTrack(event) {
  event.preventDefault();
  const payload = {
    display_name: els.trackNameInput.value.trim(),
    sort_order: Number(els.trackOrderInput.value),
    min_time_ms: Number(els.trackMinInput.value),
    max_time_ms: Number(els.trackMaxInput.value)
  };
  if (!state.editingTrackId) payload.id = els.trackIdInput.value.trim();
  const url = state.editingTrackId ? `/api/admin/tracks/${state.editingTrackId}` : "/api/admin/tracks";
  const method = state.editingTrackId ? "PATCH" : "POST";
  try {
    await api(url, {
      method,
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    hideTrackForm();
    await loadAll();
  } catch (error) {
    els.adminStatus.textContent = error.message;
  }
}

async function handleTrackAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const track = state.tracks.find(item => item.id === button.dataset.id);
  if (button.dataset.action === "edit" && track) {
    showTrackForm(track);
  }
  if (button.dataset.action === "delete" && track) {
    if (!confirm(`Delete track "${track.display_name}"?`)) return;
    try {
      await api(`/api/admin/tracks/${track.id}`, {method: "DELETE"});
      await loadAll();
    } catch (error) {
      els.adminStatus.textContent = error.message;
    }
  }
}

async function handleRunAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const run = state.runs.find(item => item.id === button.dataset.id);
  if (!run) return;
  try {
    if (button.dataset.action === "delete-run") {
      if (!confirm(`Delete ${run.username}'s run? This removes the original video and generated variants.`)) return;
      await api(`/api/admin/runs/${run.id}`, {method: "DELETE"});
    }
    if (button.dataset.action === "retry-thumbnail") {
      await api(`/api/admin/runs/${run.id}/retry-thumbnail`, {method: "POST"});
    }
    await loadAll();
  } catch (error) {
    els.adminStatus.textContent = error.message;
  }
}

async function resetGenerated() {
  if (!confirm("Delete all generated download variants? Original videos are kept.")) return;
  try {
    const payload = await api("/api/admin/reset-generated", {method: "POST"});
    els.adminStatus.textContent = `Deleted ${payload.deleted_variants} generated variant files.`;
    await loadAll();
  } catch (error) {
    els.adminStatus.textContent = error.message;
  }
}

els.adminToken.value = localStorage.getItem("runvaultAdminToken") || "";
els.saveTokenButton.addEventListener("click", () => {
  localStorage.setItem("runvaultAdminToken", els.adminToken.value);
  loadAll();
});
els.reloadButton.addEventListener("click", loadAll);
els.runTrackFilter.addEventListener("change", loadRuns);
els.newTrackButton.addEventListener("click", () => showTrackForm());
els.cancelTrackButton.addEventListener("click", hideTrackForm);
els.trackForm.addEventListener("submit", saveTrack);
els.tracksBody.addEventListener("click", handleTrackAction);
els.runsBody.addEventListener("click", handleRunAction);
els.resetGeneratedButton.addEventListener("click", resetGenerated);

loadAll();
