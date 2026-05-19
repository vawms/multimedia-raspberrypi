const state = {
  tracks: [],
  runs: [],
  selectedRun: null,
  pollTimer: null
};

const els = {
  serverStatus: document.getElementById("serverStatus"),
  runCount: document.getElementById("runCount"),
  refreshButton: document.getElementById("refreshButton"),
  trackSelect: document.getElementById("trackSelect"),
  searchInput: document.getElementById("searchInput"),
  leaderboardBody: document.getElementById("leaderboardBody"),
  emptyState: document.getElementById("emptyState"),
  replayVideo: document.getElementById("replayVideo"),
  videoPlaceholder: document.getElementById("videoPlaceholder"),
  metaPlayer: document.getElementById("metaPlayer"),
  metaTrack: document.getElementById("metaTrack"),
  metaTime: document.getElementById("metaTime"),
  metaUploaded: document.getElementById("metaUploaded"),
  qualitySelect: document.getElementById("qualitySelect"),
  fpsSelect: document.getElementById("fpsSelect"),
  originalLink: document.getElementById("originalLink"),
  variantButton: document.getElementById("variantButton"),
  jobStatus: document.getElementById("jobStatus")
};

function formatTime(ms) {
  const minutes = Math.floor(ms / 60000);
  const seconds = Math.floor((ms % 60000) / 1000);
  const millis = ms % 1000;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

function trackName(id) {
  return state.tracks.find(track => track.id === id)?.display_name ?? id;
}

async function getJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || `Request failed: ${response.status}`);
  }
  return payload;
}

async function loadTracks() {
  const payload = await getJson("/api/tracks");
  state.tracks = payload.tracks;
  els.trackSelect.innerHTML = "";
  for (const track of state.tracks) {
    els.trackSelect.append(new Option(track.display_name, track.id));
  }
}

function leaderboardUrl(path) {
  const params = new URLSearchParams();
  if (els.trackSelect.value) params.set("track_id", els.trackSelect.value);
  if (els.searchInput.value.trim()) params.set("search", els.searchInput.value.trim());
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

async function loadLeaderboard() {
  const payload = await getJson(leaderboardUrl("/api/leaderboard"));
  state.runs = payload.runs;
  els.runCount.textContent = `${state.runs.length} run${state.runs.length === 1 ? "" : "s"}`;
  renderLeaderboard();
}

function renderLeaderboard() {
  els.leaderboardBody.innerHTML = "";
  els.emptyState.hidden = state.runs.length !== 0;
  state.runs.forEach((run, index) => {
    const row = document.createElement("tr");
    row.className = state.selectedRun?.id === run.id ? "active" : "";
    row.innerHTML = `
      <td>${index + 1}</td>
      <td>${thumbnailMarkup(run)}</td>
      <td>${escapeHtml(run.username)}</td>
      <td class="time">${formatTime(run.completion_time_ms)}</td>
      <td><span class="status ${escapeHtml(run.status)}">${escapeHtml(run.status)}</span></td>
    `;
    row.addEventListener("click", () => selectRun(run));
    els.leaderboardBody.append(row);
  });
}

function thumbnailMarkup(run) {
  if (run.thumbnail_url) {
    return `<img class="thumb" src="${run.thumbnail_url}" alt="">`;
  }
  return `<div class="thumb thumbFallback">pending</div>`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;"
  }[char]));
}

function selectRun(run) {
  state.selectedRun = run;
  els.replayVideo.src = run.original_url;
  els.replayVideo.style.display = "block";
  els.videoPlaceholder.style.display = "none";
  els.metaPlayer.textContent = run.username;
  els.metaTrack.textContent = run.track_name || trackName(run.track_id);
  els.metaTime.textContent = formatTime(run.completion_time_ms);
  els.metaUploaded.textContent = new Date(run.upload_time).toLocaleString();
  els.originalLink.href = run.original_url;
  els.originalLink.classList.remove("disabled");
  els.originalLink.removeAttribute("aria-disabled");
  els.variantButton.disabled = false;
  els.jobStatus.textContent = "";
  renderLeaderboard();
}

async function requestVariant() {
  if (!state.selectedRun) return;
  els.variantButton.disabled = true;
  els.jobStatus.textContent = "Queued...";
  try {
    const payload = await getJson(`/api/runs/${state.selectedRun.id}/downloads`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        format: "mp4",
        quality: els.qualitySelect.value,
        fps: els.fpsSelect.value
      })
    });
    watchJob(payload.job);
  } catch (error) {
    els.jobStatus.textContent = error.message;
    els.variantButton.disabled = false;
  }
}

function watchJob(job) {
  clearInterval(state.pollTimer);
  updateJob(job);
  if (job.status === "ready" || job.status === "failed") return;
  state.pollTimer = setInterval(async () => {
    try {
      const payload = await getJson(`/api/download-jobs/${job.id}`);
      updateJob(payload.job);
      if (payload.job.status === "ready" || payload.job.status === "failed") {
        clearInterval(state.pollTimer);
      }
    } catch (error) {
      els.jobStatus.textContent = error.message;
      clearInterval(state.pollTimer);
      els.variantButton.disabled = false;
    }
  }, 1500);
}

function updateJob(job) {
  if (job.status === "ready") {
    els.jobStatus.innerHTML = `<a class="button" href="${job.download_url}">Download ${escapeHtml(job.quality)} ${escapeHtml(job.fps)} FPS MP4</a>`;
    els.variantButton.disabled = false;
  } else if (job.status === "failed") {
    els.jobStatus.textContent = job.error_message || "Transcode failed";
    els.variantButton.disabled = false;
  } else {
    els.jobStatus.textContent = `Download job ${job.status}`;
  }
}

async function init() {
  try {
    const health = await getJson("/api/health");
    els.serverStatus.textContent = health.ffmpeg ? "Server ready, FFmpeg available" : "Server ready, FFmpeg missing";
    await loadTracks();
    await loadLeaderboard();
  } catch (error) {
    els.serverStatus.textContent = error.message;
  }
}

els.refreshButton.addEventListener("click", loadLeaderboard);
els.trackSelect.addEventListener("change", loadLeaderboard);
els.searchInput.addEventListener("input", () => {
  clearTimeout(els.searchInput.timer);
  els.searchInput.timer = setTimeout(loadLeaderboard, 180);
});
els.variantButton.addEventListener("click", requestVariant);

init();
