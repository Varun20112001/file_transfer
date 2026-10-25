const $ = id => document.getElementById(id);
const canDeleteFiles = document.querySelector("main")?.dataset.canDelete === "true";
const jobs = [];
let running = false, cancelled = false;
const activeXhrs = new Set();
let browseImageItems = [], favoriteImageItems = [];
let previewImageItems = [], previewImageIndex = -1, previewRequestId = 0, previewItem = null, previewReturnFocus = null;

function fmtSize(b) {
  if (b < 1024) return b + " B";
  if (b < 1048576) return (b / 1024).toFixed(0) + " KB";
  if (b < 1073741824) return (b / 1048576).toFixed(1) + " MB";
  return (b / 1073741824).toFixed(2) + " GB";
}
function fmtTime(s) {
  if (!isFinite(s) || s < 0) return "--";
  s = Math.round(s);
  if (s < 60) return s + "s";
  return Math.floor(s / 60) + "m " + (s % 60) + "s";
}
function fmtSeconds(s) {
  return Number.isFinite(s) ? s.toFixed(2) + "s" : "";
}

// Stable id from path+size+lastModified so retries resume the same upload.
function hashId(s) {
  let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    h1 = Math.imul(h1 ^ c, 2654435761);
    h2 = Math.imul(h2 ^ c, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (h2 >>> 0).toString(16).padStart(8, "0") + (h1 >>> 0).toString(16).padStart(8, "0");
}

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}

/* ---------- job rows ---------- */
function makeJob(file, path) {
  const key = path + "|" + file.size + "|" + file.lastModified;
  const el = document.createElement("div");
  el.className = "file";
  el.innerHTML =
    '<div class="top"><div class="name"></div><button class="x" title="Remove">\u00d7</button></div>' +
    '<div class="meta"><span class="size"></span><span class="status"></span></div>' +
    '<div class="bar"><i></i></div>';
  el.querySelector(".name").textContent = path;
  el.querySelector(".size").textContent = fmtSize(file.size);
  const job = {
    file, path, key, id: hashId(key), el, state: "ready",
    bar: el.querySelector(".bar"), fill: el.querySelector(".bar i"),
    status: el.querySelector(".status"), inflight: {}, doneBytes: 0, remaining: 0, total: 1, finished: false,
  };
  el.querySelector(".x").onclick = () => {
    if (running) return;
    jobs.splice(jobs.indexOf(job), 1);
    el.remove();
    refreshUI();
  };
  $("list").append(el);
  setState(job, "ready", "Ready");
  return job;
}

function setState(job, state, text) {
  job.state = state;
  job.status.textContent = text;
  job.status.className = "status st-" + state;
  job.bar.className = "bar" + (state === "done" ? " ok" : state === "failed" ? " bad" : (state === "skipped" || state === "cancelled") ? " warn" : "");
  if (state === "done" || state === "skipped") job.fill.style.width = "100%";
}

function resetJob(job) {
  job.inflight = {}; job.doneBytes = 0; job.remaining = 0; job.finished = false;
  job.firstChunkAt = null; job.lastChunkAt = null;
  job.fill.style.width = "0%";
}

function sentBytes(j) {
  return j.doneBytes + Object.values(j.inflight).reduce((a, b) => a + b, 0);
}

// iOS hands over a dragged or picked folder as a 0-byte, type-less file with no extension.
function looksLikeFolder(file, path) {
  return file.size === 0 && !file.type && !file.name.includes(".") && !path.includes("/");
}

// items: [{ file, path }] where path is the relative path (folder/sub/name.ext) or just the name.
function addItems(items) {
  if (running) return;
  const force = $("force").checked;
  let folderHits = 0;
  for (const { file, path } of items) {
    if (file.name === ".DS_Store" || file.name === "Thumbs.db") continue;
    if (looksLikeFolder(file, path)) { folderHits++; continue; }
    const key = path + "|" + file.size + "|" + file.lastModified;
    const existing = jobs.find(j => j.key === key);
    if (existing) {
      if (force && (existing.state === "done" || existing.state === "skipped")) {
        resetJob(existing);
        setState(existing, "ready", "Ready");
      }
      continue;
    }
    jobs.push(makeJob(file, path));
  }
  $("notice").hidden = folderHits === 0;
  refreshUI();
}

function pendingJobs() {
  return jobs.filter(j => ["ready", "failed", "cancelled"].includes(j.state));
}

function refreshUI() {
  const n = pendingJobs().length;
  $("listCard").hidden = jobs.length === 0;
  const totalBytes = jobs.reduce((s, j) => s + j.file.size, 0);
  $("listHead").textContent = jobs.length + " file" + (jobs.length === 1 ? "" : "s") + " \u00b7 " + fmtSize(totalBytes);
  $("go").disabled = running || n === 0;
  $("go").textContent = running ? "Uploading\u2026" : n ? "Upload " + n + " file" + (n === 1 ? "" : "s") : "Upload";
  $("cancel").hidden = !running;
  $("clear").hidden = running || jobs.length === 0;
}

/* ---------- overall progress ---------- */
let run = { jobs: [], total: 0, baseline: 0, t0: 0 };
let rafPending = false;
function scheduleOverall() {
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => { rafPending = false; paintOverall(); });
}
function paintOverall() {
  for (const j of run.jobs) {
    if (j.state === "uploading") j.fill.style.width = Math.min(100, j.file.size ? sentBytes(j) / j.file.size * 100 : 100) + "%";
  }
  const sent = run.jobs.reduce((s, j) => s + (j.state === "done" ? j.file.size : sentBytes(j)), 0);
  const pct = run.total ? Math.min(100, sent / run.total * 100) : 100;
  const secs = (performance.now() - run.t0) / 1000;
  const rate = secs > 0.5 ? (sent - run.baseline) / secs : 0;
  $("ovPct").textContent = pct.toFixed(0) + "%";
  $("ovBar").firstElementChild.style.width = pct + "%";
  $("ovBytes").textContent = fmtSize(sent) + " of " + fmtSize(run.total);
  $("ovSpeed").textContent = rate > 0
    ? (rate / 1048576).toFixed(1) + " MB/s \u00b7 " + fmtTime((run.total - sent) / rate) + " left"
    : "";
}

/* ---------- chunk upload ---------- */
function sendChunk(job, index, blob) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    activeXhrs.add(xhr);
    xhr.upload.onprogress = e => { job.inflight[index] = e.loaded; scheduleOverall(); };
    xhr.onload = () => { activeXhrs.delete(xhr); xhr.status === 200 ? resolve() : reject(new Error("status " + xhr.status)); };
    xhr.onerror = () => { activeXhrs.delete(xhr); reject(new Error("network")); };
    xhr.onabort = () => { activeXhrs.delete(xhr); reject(new Error("aborted")); };
    xhr.open("POST", "/chunk?id=" + job.id + "&index=" + index);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.send(blob);
  });
}

async function sendChunkWithRetry(job, index, csize) {
  const start = index * csize;
  const blob = job.file.slice(start, Math.min(start + csize, job.file.size));
  for (let attempt = 1; attempt <= 3; attempt++) {
    if (cancelled) throw new Error("aborted");
    try {
      if (job.firstChunkAt == null) job.firstChunkAt = performance.now();
      await sendChunk(job, index, blob);
      job.inflight[index] = 0;
      job.doneBytes += blob.size;
      return;
    } catch (e) {
      job.inflight[index] = 0;
      if (cancelled || attempt === 3) throw e;
      await new Promise(r => setTimeout(r, 500 * attempt));
    }
  }
}

async function finish(job) {
  if (job.finished) return;
  job.finished = true;
  const isZip = job.file.name.toLowerCase().endsWith(".zip") && $("extract").checked;
  job.status.textContent = isZip ? "Assembling and extracting\u2026" : "Assembling\u2026";
  try {
    const r = await postJSON("/complete", {
      id: job.id,
      extract: $("extract").checked,
      delete_zip: $("delzip").checked,
    });
    if (r.extracted) {
      const timing = r.timings ? " (assembled " + fmtSeconds(r.timings.assembly_seconds) +
        ", extracted " + fmtSeconds(r.timings.extraction_seconds) + ")" : "";
      setState(job, "done", "Extracted " + r.extracted.files + " files \u2192 " + r.extracted.root + timing);
    } else if (r.extract_error) {
      const timing = r.timings ? " (assembled " + fmtSeconds(r.timings.assembly_seconds) + ")" : "";
      setState(job, "done", "Saved .zip (could not extract: " + r.extract_error + ")" + timing);
    } else {
      const timing = r.timings ? " (assembled " + fmtSeconds(r.timings.assembly_seconds) + ")" : "";
      setState(job, "done", "Saved \u2192 " + r.saved + timing);
    }
  } catch (e) {
    setState(job, "failed", "Could not assemble file");
  }
  scheduleOverall();
}

/* ---------- main run ---------- */
async function start() {
  if (running) return;
  const todo = pendingJobs();
  if (!todo.length) return;

  running = true; cancelled = false;
  $("notice").hidden = true;
  refreshUI();
  const limit = parseInt($("conc").value);
  const csize = parseInt($("csize").value) * 1048576;
  const force = $("force").checked;
  // Optional batch folder: every file in this run is saved inside it on the laptop.
  let folder = "";
  if ($("useFolder").checked) {
    folder = ($("folderName").value.trim() || $("folderName").placeholder).replace(/[\\/]+/g, "-");
  }

  $("overall").hidden = false;
  $("ovTitle").textContent = "Preparing\u2026";
  $("ovBar").className = "bar";
  $("ovBar").firstElementChild.style.width = "0%";
  $("ovBytes").textContent = ""; $("ovSpeed").textContent = "";

  // 1. Init each file; the server reports finished uploads and chunks it already holds.
  run = { jobs: [], total: 0, baseline: 0, t0: performance.now() };
  const tasks = [];
  const alreadyComplete = [];
  let skipped = 0;
  for (const job of todo) {
    if (cancelled) break;
    resetJob(job);
    setState(job, "uploading", "Preparing\u2026");
    job.total = Math.max(1, Math.ceil(job.file.size / csize));
    // The id includes the folder, so the same photo sent to two folders is not treated as a duplicate.
    job.target = folder ? folder + "/" + job.path : job.path;
    job.id = hashId(job.target + "|" + job.file.size + "|" + job.file.lastModified);
    job.el.querySelector(".name").textContent = job.target;
    try {
      const info = await postJSON("/init", {
        id: job.id, name: job.target, size: job.file.size, total: job.total, force,
      });
      if (info.done) {
        setState(job, "skipped", "Already on laptop \u2014 skipped");
        skipped++;
        continue;
      }
      const have = new Set(info.received);
      for (const i of have) job.doneBytes += Math.min(csize, job.file.size - i * csize);
      for (let i = 0; i < job.total; i++) {
        if (!have.has(i)) { tasks.push({ job, index: i }); job.remaining++; }
      }
      run.jobs.push(job);
      run.total += job.file.size;
      run.baseline += job.doneBytes;
      setState(job, "uploading", have.size ? "Resuming\u2026" : "Uploading\u2026");
      if (job.remaining === 0) {
        job.lastChunkAt = performance.now();
        alreadyComplete.push(job);
      }
    } catch (e) {
      setState(job, "failed", "Could not start (server unreachable?)");
    }
  }

  // 2. Worker pool pulls chunks across all files.
  run.t0 = performance.now();
  $("ovTitle").textContent = "Uploading";
  paintOverall();
  let cursor = 0;
  async function worker() {
    while (cursor < tasks.length && !cancelled) {
      const { job, index } = tasks[cursor++];
      if (job.state !== "uploading") continue;
      try {
        await sendChunkWithRetry(job, index, csize);
        if (--job.remaining === 0) {
          job.lastChunkAt = performance.now();
          await finish(job);
        }
      } catch (e) {
        if (!cancelled && job.state === "uploading") {
          setState(job, "failed", "Failed \u2014 tap Upload to resume");
        }
      }
      scheduleOverall();
    }
  }
  const workerTasks = Array.from({ length: limit }, worker);
  const completionTasks = alreadyComplete.map(finish);
  await Promise.all([...workerTasks, ...completionTasks]);

  // 3. Wrap up.
  for (const j of run.jobs) {
    if (j.state === "uploading" && !j.finished) setState(j, "cancelled", "Paused \u2014 tap Upload to resume");
  }
  const ok = run.jobs.filter(j => j.state === "done").length;
  const bad = run.jobs.filter(j => j.state === "failed").length;
  const secs = ((performance.now() - run.t0) / 1000);
  const mb = run.jobs.reduce((s, j) => s + j.file.size, 0) / 1048576;
  const uploadedJobs = run.jobs.filter(j => Number.isFinite(j.firstChunkAt) && Number.isFinite(j.lastChunkAt));
  const uploadSecs = uploadedJobs.length
    ? (Math.max(...uploadedJobs.map(j => j.lastChunkAt)) - Math.min(...uploadedJobs.map(j => j.firstChunkAt))) / 1000
    : 0;
  paintOverall();
  if (cancelled) {
    $("ovTitle").textContent = "Paused";
  } else if (bad) {
    $("ovTitle").textContent = ok + " uploaded, " + bad + " failed";
    $("ovBar").className = "bar bad";
  } else {
    $("ovTitle").textContent = ok + " uploaded" + (skipped ? ", " + skipped + " skipped" : "") + " \u2713";
    $("ovBar").className = "bar ok";
  }
  if (ok && secs > 0) {
    const uploadLabel = uploadSecs > 0 ? "upload " + uploadSecs.toFixed(1) + "s \u00b7 " : "";
    $("ovSpeed").textContent = uploadLabel + secs.toFixed(1) + "s end-to-end \u00b7 " + (mb / secs).toFixed(1) + " MB/s avg";
  }

  running = false;
  refreshUI();
}

/* ---------- picking and dropping ---------- */
$("files").onchange = e => {
  addItems([...e.target.files].map(f => ({ file: f, path: f.name })));
  e.target.value = "";   // lets the same selection be picked again later
};
$("folder").onchange = e => {
  addItems([...e.target.files].map(f => ({ file: f, path: f.webkitRelativePath || f.name })));
  e.target.value = "";
};

async function walk(entry, prefix, out) {
  if (entry.isFile) {
    const file = await new Promise((res, rej) => entry.file(res, rej));
    out.push({ file, path: prefix + entry.name });
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    let batch;
    do {   // readEntries returns results in batches, so loop until it returns empty
      batch = await new Promise((res, rej) => reader.readEntries(res, rej));
      for (const child of batch) await walk(child, prefix + entry.name + "/", out);
    } while (batch.length);
  }
}

async function collectDropped(dt) {
  const out = [];
  // Read dt.items before the first await; the browser clears them afterwards.
  const entries = [...(dt.items || [])].map(i => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null));
  if (entries.length && entries.every(Boolean)) {
    for (const en of entries) await walk(en, "", out);
  } else {
    for (const f of dt.files) out.push({ file: f, path: f.name });
  }
  return out;
}

$("go").onclick = start;
$("cancel").onclick = () => { cancelled = true; activeXhrs.forEach(x => x.abort()); };
$("clear").onclick = () => {
  if (running) return;
  jobs.length = 0;
  $("list").innerHTML = "";
  $("overall").hidden = true;
  $("notice").hidden = true;
  refreshUI();
};

const picks = $("picks");
["dragenter", "dragover"].forEach(ev => document.addEventListener(ev, e => { e.preventDefault(); picks.classList.add("over"); }));
document.addEventListener("dragleave", e => { if (!e.relatedTarget) picks.classList.remove("over"); });
document.addEventListener("drop", async e => {
  e.preventDefault();
  picks.classList.remove("over");
  addItems(await collectDropped(e.dataTransfer));
});

// Default folder name = today's date; editable. The field appears when the box is ticked.
(function () {
  const d = new Date();
  const p = n => String(n).padStart(2, "0");
  const def = "Upload " + d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
  $("folderName").placeholder = def;
  $("folderName").value = def;
  $("useFolder").onchange = () => {
    $("folderName").hidden = !$("useFolder").checked;
    if ($("useFolder").checked) { $("folderName").focus(); $("folderName").select(); }
  };
})();
refreshUI();

/* ---------- laptop-to-phone browser ---------- */
let browsePath = "";
let browseLoaded = false;
let selectedDrive = "";
let libraryView = "browse";
const selectedDownloadItems = new Map();

function csrfHeaders() {
  return { "Content-Type": "application/json", "X-CSRFToken": document.querySelector('meta[name="csrf-token"]')?.content || "" };
}

function loginRedirect() {
  window.location.assign("/login?next=" + encodeURIComponent("/?mode=phone"));
}

function updateDownloadSelection() {
  const count = selectedDownloadItems.size;
  $("selectionSummary").textContent = count
    ? count + " item" + (count === 1 ? "" : "s") + " selected"
    : "Nothing selected";
  $("downloadSelected").disabled = count === 0;
  $("downloadSelected").textContent = count ? "Save " + count + " item" + (count === 1 ? "" : "s") + " to phone" : "Save to phone";
}

async function loadBrowse(path = browsePath) {
  $("browseStatus").textContent = "Loading files…";
  $("browseItems").replaceChildren();
  try {
    const response = await fetch("/api/browse?" + new URLSearchParams({ drive: selectedDrive, path }));
    if (response.status === 401) return loginRedirect();
    if (!response.ok) throw new Error("Could not read this folder (" + response.status + ")");
    const data = await response.json();
    browsePath = data.path || "";
    browseImageItems = (data.items || []).filter(item => item.type === "image").map(item => ({ ...item, drive: selectedDrive }));
    $("browsePath").textContent = selectedDrive + (browsePath ? "\\" + browsePath.replaceAll("/", "\\") : "\\");
    $("browseUp").disabled = !browsePath;
    selectedDownloadItems.clear();

    if (!data.items.length) {
      const empty = document.createElement("p");
      empty.className = "empty-folder";
      empty.textContent = "This folder is empty.";
      $("browseItems").append(empty);
    }

    for (const item of data.items) {
      const row = document.createElement("div");
      row.className = "browse-row";
      row.tabIndex = 0;
      row.setAttribute("role", "group");
      row.setAttribute("aria-label", item.type === "directory" ? "Folder " + item.name : "File " + item.name);

      const entry = document.createElement("div");
      entry.className = "browse-entry";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "browse-select";
      checkbox.setAttribute("aria-label", "Select " + item.name);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) selectedDownloadItems.set(item.path, item);
        else selectedDownloadItems.delete(item.path);
        updateDownloadSelection();
      });
      const name = document.createElement("span");
      name.className = "browse-name";
      name.textContent = item.name;
      const meta = document.createElement("span");
      meta.className = "browse-meta";
      meta.textContent = item.type === "directory" ? "Folder" : fmtSize(item.size);
      entry.append(name, meta);

      if (item.type === "image") {
        const thumb = document.createElement("img");
        thumb.className = "browse-thumb"; thumb.loading = "lazy"; thumb.alt = ""; thumb.src = item.thumbnail;
        row.append(thumb);
      }
      const favorite = document.createElement("button");
      favorite.type = "button";
      favorite.className = "favorite-button";
      updateFavoriteButton(favorite, item);
      favorite.addEventListener("click", event => {
        event.stopPropagation();
        setFavorite(item, favorite);
      });
      row.append(entry, favorite);
      if (canDeleteFiles && item.type !== "directory") row.append(createDeleteButton(item, row));
      row.append(checkbox);

      row.addEventListener("click", event => {
        if (event.target.closest("button, input, a")) return;
        openBrowseItem(item);
      });
      row.addEventListener("keydown", event => {
        if ((event.key === "Enter" || event.key === " ") && event.target === row) {
          event.preventDefault();
          openBrowseItem(item);
        }
      });
      $("browseItems").append(row);
    }
    $("browseStatus").textContent = "";
    browseLoaded = true;
    updateDownloadSelection();
  } catch (error) {
    $("browseStatus").textContent = error.message || "Could not load files.";
  }
}

function renderFavoriteItems(items) {
  favoriteImageItems = items.filter(item => item.available && item.type === "image");
  const container = $("favoriteItems");
  container.replaceChildren();
  $("favoriteCount").textContent = items.length ? String(items.length) : "";
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty-folder";
    empty.textContent = "No favorites yet. Star a file or folder while browsing.";
    container.append(empty);
    return;
  }
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "browse-row favorite-row" + (item.available ? "" : " unavailable");
    row.tabIndex = 0;
    row.setAttribute("role", "group");
    row.setAttribute("aria-label", `${item.available ? "Favorite" : "Unavailable favorite"}: ${item.name}, ${item.drive}`);
    if (item.type === "image" && item.thumbnail) {
      const thumb = document.createElement("img");
      thumb.className = "browse-thumb"; thumb.loading = "lazy"; thumb.alt = ""; thumb.src = item.thumbnail;
      row.append(thumb);
    } else {
      const icon = document.createElement("span");
      icon.className = "favorite-kind";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = item.type === "directory" ? "▰" : item.type === "video" ? "▶" : "∙";
      row.append(icon);
    }
    const entry = document.createElement("div");
    entry.className = "browse-entry favorite-entry";
    const name = document.createElement("span"); name.className = "browse-name"; name.textContent = item.name;
    const meta = document.createElement("span"); meta.className = "browse-meta";
    meta.textContent = item.available
      ? `${item.drive} · ${item.type === "directory" ? "Folder" : item.size == null ? "File" : fmtSize(item.size)}`
      : `${item.drive} · Unavailable`;
    entry.append(name, meta);
    row.append(entry);

    const favorite = document.createElement("button");
    favorite.type = "button"; favorite.className = "favorite-button is-favorite";
    favorite.textContent = "★"; favorite.title = "Remove from favorites";
    favorite.setAttribute("aria-label", `Remove ${item.name} from favorites`);
    favorite.setAttribute("aria-pressed", "true");
    favorite.addEventListener("click", async event => {
      event.stopPropagation();
      await setFavorite(item, favorite);
    });
    row.append(favorite);
    if (canDeleteFiles && item.available && item.type !== "directory") row.append(createDeleteButton(item, row));
    row.addEventListener("click", event => {
      if (event.target.closest("button, input, a") || !item.available) return;
      activateFavorite(item);
    });
    row.addEventListener("keydown", event => {
      if ((event.key === "Enter" || event.key === " ") && event.target === row && item.available) {
        event.preventDefault(); activateFavorite(item);
      }
    });
    container.append(row);
  }
}

function createDeleteButton(item, row) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "delete-button";
  button.textContent = "×";
  button.title = "Delete file";
  button.setAttribute("aria-label", "Delete " + item.name);
  button.addEventListener("click", event => {
    event.stopPropagation();
    deleteFile(item, row, button);
  });
  return button;
}

async function deleteFile(item, row, button) {
  const drive = item.drive || selectedDrive;
  const fullPath = drive + "\\" + item.path.replaceAll("/", "\\");
  if (!window.confirm(`Permanently delete this file?\n\n${fullPath}\n\nThis cannot be undone.`)) return;
  const deletingFromPreview = row === null;
  const currentView = libraryView;
  button.disabled = true;
  try {
    const response = await fetch("/api/delete", {
      method: "POST",
      headers: csrfHeaders(),
      body: JSON.stringify({ drive, path: item.path }),
    });
    if (response.status === 401) return loginRedirect();
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not delete this file.");
    selectedDownloadItems.delete(item.path);
    updateDownloadSelection();
    if (row) row.remove();
    if (deletingFromPreview) $("closePreview").click();
    const status = currentView === "favorites" ? $("favoritesStatus") : $("browseStatus");
    const confirmation = `Deleted ${item.name}. Recorded under ${result.deleted_by}.`;
    if (deletingFromPreview) {
      if (currentView === "favorites") await loadFavorites();
      else if (drive === selectedDrive) await loadBrowse(browsePath);
    } else if (currentView === "favorites") {
      await loadFavorites();
    }
    status.textContent = confirmation;
  } catch (error) {
    const status = currentView === "favorites" ? $("favoritesStatus") : $("browseStatus");
    status.textContent = error.message || "Could not delete this file.";
    if (button.isConnected) button.disabled = false;
  }
}

function activateFavorite(item) {
  selectedDrive = item.drive;
  $("drivePicker").value = selectedDrive;
  if (item.type === "directory") {
    showLibraryView("browse");
    browseLoaded = false;
    loadBrowse(item.path);
  } else if (item.type === "image" || item.type === "video") {
    openPreview(item, item.type === "image" ? favoriteImageItems : []);
  } else {
    saveOne(item);
  }
}

async function loadFavorites() {
  $("favoritesStatus").textContent = "Loading favorites…";
  try {
    const response = await fetch("/api/favorites");
    if (response.status === 401) return loginRedirect();
    if (!response.ok) throw new Error("Could not load favorites (" + response.status + ")");
    const data = await response.json();
    renderFavoriteItems(data.items || []);
    $("favoritesStatus").textContent = "";
  } catch (error) {
    $("favoritesStatus").textContent = error.message || "Could not load favorites.";
  }
}

function showLibraryView(view) {
  libraryView = view;
  const favorites = view === "favorites";
  $("browseControls").hidden = favorites;
  $("favoritesPanel").hidden = !favorites;
  $("browseTab").classList.toggle("active", !favorites);
  $("favoritesTab").classList.toggle("active", favorites);
  $("browseTab").setAttribute("aria-selected", String(!favorites));
  $("favoritesTab").setAttribute("aria-selected", String(favorites));
  if (favorites) loadFavorites();
}

function updateFavoriteButton(button, item) {
  if (button.id === "previewFavorite") {
    updatePreviewFavoriteControl(item);
    return;
  }
  button.textContent = item.favorite ? "★" : "☆";
  button.classList.toggle("is-favorite", Boolean(item.favorite));
  button.setAttribute("aria-pressed", String(Boolean(item.favorite)));
  button.setAttribute("aria-label", (item.favorite ? "Remove " : "Add ") + item.name + " " + (item.favorite ? "from" : "to") + " favorites");
  button.title = item.favorite ? "Remove from favorites" : "Add to favorites";
}

async function setFavorite(item, button) {
  button.disabled = true;
  try {
    const response = await fetch("/api/favorites", {
      method: "POST",
      headers: csrfHeaders(),
      body: JSON.stringify({ drive: item.drive || selectedDrive, path: item.path, favorite: !item.favorite }),
    });
    if (response.status === 401) return loginRedirect();
    if (!response.ok) throw new Error("Could not update favorite (" + response.status + ")");
    const result = await response.json();
    item.favorite = result.favorite;
    updateFavoriteButton(button, item);
    loadFavorites();
  } catch (error) {
    $("browseStatus").textContent = error.message || "Could not update favorite.";
  } finally {
    button.disabled = false;
  }
}

function openBrowseItem(item) {
  if (item.type === "directory") loadBrowse(item.path);
  else if (item.type === "image" || item.type === "video") openPreview(item, item.type === "image" ? browseImageItems : []);
  else saveOne(item);
}

function showTransferMode(mode) {
  const uploading = mode === "upload";
  $("uploadMode").hidden = !uploading;
  $("downloadMode").hidden = uploading;
  $("uploadTab").classList.toggle("active", uploading);
  $("downloadTab").classList.toggle("active", !uploading);
  $("uploadTab").setAttribute("aria-selected", String(uploading));
  $("downloadTab").setAttribute("aria-selected", String(!uploading));
  if (!uploading && !browseLoaded) loadDrives();
}

async function loadDrives() {
  $("browseStatus").textContent = "Finding fixed drives…";
  try {
    const response = await fetch("/api/drives");
    if (response.status === 401) return loginRedirect();
    if (!response.ok) throw new Error("Could not list drives (" + response.status + ")");
    const data = await response.json();
    const picker = $("drivePicker"); picker.replaceChildren();
    for (const drive of data.drives) {
      const option = document.createElement("option"); option.value = drive.id; option.textContent = drive.label; picker.append(option);
    }
    if (!data.drives.length) throw new Error("No accessible fixed drives were found.");
    selectedDrive = data.drives[0].id; picker.value = selectedDrive; await loadBrowse("");
  } catch (error) { $("browseStatus").textContent = error.message || "Could not find drives."; }
}

function previewKey(item) {
  return (item.drive || selectedDrive) + "|" + item.path;
}

function updatePreviewNavigation() {
  const hasPrevious = previewImageIndex > 0;
  const hasNext = previewImageIndex >= 0 && previewImageIndex < previewImageItems.length - 1;
  $("previousPhoto").hidden = previewImageItems.length < 2;
  $("nextPhoto").hidden = previewImageItems.length < 2;
  $("previousPhoto").disabled = !hasPrevious;
  $("nextPhoto").disabled = !hasNext;
}

async function openPreview(item, imageItems = []) {
  if (item.type === "image") {
    previewImageItems = imageItems.length ? imageItems : [item];
    previewImageIndex = previewImageItems.findIndex(candidate => previewKey(candidate) === previewKey(item));
    if (previewImageIndex < 0) {
      previewImageItems = [item];
      previewImageIndex = 0;
    }
  } else {
    previewImageItems = [];
    previewImageIndex = -1;
  }
  const modal = $("previewModal");
  previewReturnFocus = document.activeElement;
  modal.hidden = false;
  $("previewContent").replaceChildren();
  setActivePreviewItem(item);
  updatePreviewNavigation();
  updatePreviewFavoriteControl(item);
  if (!document.fullscreenElement && document.fullscreenEnabled && typeof modal.requestFullscreen === "function") {
    try {
      const fullscreenRequest = modal.requestFullscreen({ navigationUI: "hide" });
      if (fullscreenRequest?.catch) fullscreenRequest.catch(() => {});
    } catch (_) {
      // Keep the viewport-filling preview if native fullscreen is unavailable.
    }
  }
  $("closePreview").focus({ preventScroll: true });
  await loadPreviewItem(item);
}

function updatePreviewFavoriteControl(item) {
  const button = $("previewFavorite");
  if (!button) return;
  const favorite = Boolean(item?.favorite);
  button.classList.toggle("is-favorite", favorite);
  button.setAttribute("aria-pressed", String(favorite));
  button.setAttribute("aria-label", favorite ? "Remove from favorites" : "Add to favorites");
  button.title = favorite ? "Remove from favorites" : "Add to favorites";
}

function setActivePreviewItem(item) {
  previewItem = item;
  updatePreviewFavoriteControl(item);
  $("previewName").textContent = item.type === "image" && previewImageItems.length > 1
    ? `${item.name} · ${previewImageIndex + 1} of ${previewImageItems.length}` : item.name;
  $("savePreview").onclick = () => saveOne(item);
  if ($("previewDelete")) $("previewDelete").setAttribute("aria-label", "Delete " + item.name);
}

async function loadPreviewItem(item) {
  const requestId = ++previewRequestId;
  try {
    const drive = item.drive || selectedDrive;
    const response = await fetch("/api/audit/preview", { method: "POST", headers: csrfHeaders(), body: JSON.stringify({ drive, path: item.path }) });
    if (response.status === 401) return loginRedirect();
    if (!response.ok) throw new Error("Preview could not be opened (" + response.status + ")");
    const result = await response.json();
    if (requestId !== previewRequestId) return;
    setActivePreviewItem(item);
    const content = $("previewContent"); content.replaceChildren();
    const media = document.createElement(item.type === "video" ? "video" : "img"); media.src = result.url;
    if (item.type === "video") { media.controls = true; media.playsInline = true; } else media.alt = item.name;
    content.append(media);
  } catch (error) {
    if (requestId === previewRequestId) $("browseStatus").textContent = error.message || "Could not open preview.";
  }
}

function movePreviewPhoto(direction) {
  const nextIndex = previewImageIndex + direction;
  if (nextIndex < 0 || nextIndex >= previewImageItems.length) return;
  previewImageIndex = nextIndex;
  updatePreviewNavigation();
  const item = previewImageItems[previewImageIndex];
  setActivePreviewItem(item);
  loadPreviewItem(item);
}

async function saveOne(item) {
  try {
    const drive = item.drive || selectedDrive;
    const response = await fetch("/api/download/ticket", { method: "POST", headers: csrfHeaders(), body: JSON.stringify({ drive, path: item.path }) });
    if (response.status === 401) return loginRedirect();
    if (!response.ok) throw new Error("Download could not be prepared (" + response.status + ")");
    const ticket = await response.json(); window.location.assign(ticket.url);
  } catch (error) { $("browseStatus").textContent = error.message || "Could not start the download."; }
}

$("previousPhoto").addEventListener("click", () => movePreviewPhoto(-1));
$("nextPhoto").addEventListener("click", () => movePreviewPhoto(1));
$("previewFavorite").addEventListener("click", () => {
  if (previewItem) setFavorite(previewItem, $("previewFavorite"));
});
if ($("previewDelete")) {
  $("previewDelete").addEventListener("click", () => {
    if (canDeleteFiles && previewItem) deleteFile(previewItem, null, $("previewDelete"));
  });
}
$("closePreview").addEventListener("click", () => {
  previewRequestId++;
  $("previewContent").replaceChildren();
  $("previewModal").hidden = true;
  previewImageItems = [];
  previewImageIndex = -1;
  previewItem = null;
  updatePreviewNavigation();
  if (document.fullscreenElement === $("previewModal")) {
    document.exitFullscreen?.().catch?.(() => {});
  }
  if (previewReturnFocus?.isConnected) previewReturnFocus.focus({ preventScroll: true });
  previewReturnFocus = null;
});
$("previewModal").addEventListener("click", event => { if (event.target === $("previewModal")) $("closePreview").click(); });
$("previewModal").addEventListener("keydown", event => {
  if (event.key === "ArrowLeft" && previewImageItems.length > 1) { event.preventDefault(); movePreviewPhoto(-1); }
  if (event.key === "ArrowRight" && previewImageItems.length > 1) { event.preventDefault(); movePreviewPhoto(1); }
  if (event.key === "Escape") { event.preventDefault(); $("closePreview").click(); }
});
document.addEventListener("fullscreenchange", () => {
  if (!$("previewModal").hidden && document.fullscreenElement !== $("previewModal")) $("closePreview").click();
});
let previewTouchStart = null;
$("previewContent").addEventListener("touchstart", event => {
  if (previewImageItems.length > 1 && event.touches.length === 1) previewTouchStart = event.touches[0].clientX;
}, { passive: true });
$("previewContent").addEventListener("touchend", event => {
  if (previewTouchStart === null || !event.changedTouches.length) return;
  const delta = event.changedTouches[0].clientX - previewTouchStart;
  previewTouchStart = null;
  if (Math.abs(delta) >= 45) movePreviewPhoto(delta < 0 ? 1 : -1);
}, { passive: true });

$("uploadTab").addEventListener("click", () => showTransferMode("upload"));
$("downloadTab").addEventListener("click", () => showTransferMode("download"));
$("browseTab").addEventListener("click", () => showLibraryView("browse"));
$("favoritesTab").addEventListener("click", () => showLibraryView("favorites"));
$("drivePicker").addEventListener("change", () => { selectedDrive = $("drivePicker").value; browseLoaded = false; selectedDownloadItems.clear(); loadBrowse(""); });
$("refreshFiles").addEventListener("click", () => loadBrowse(browsePath));
$("browseHome").addEventListener("click", () => loadBrowse(""));
$("browseUp").addEventListener("click", () => {
  const parts = browsePath.split("/").filter(Boolean);
  parts.pop();
  loadBrowse(parts.join("/"));
});
$("downloadSelected").addEventListener("click", async () => {
  const selected = [...selectedDownloadItems.values()];
  if (!selected.length) return;
  if (selected.length === 1 && selected[0].type !== "directory") {
    await saveOne(selected[0]);
    return;
  }

  const button = $("downloadSelected");
  button.disabled = true;
  button.textContent = "Preparing ZIP…";
  $("browseStatus").textContent = "Preparing a ZIP for download…";
  try {
    const response = await fetch("/api/download/archive", {
      method: "POST",
      headers: csrfHeaders(),
      body: JSON.stringify({ drive: selectedDrive, base: browsePath, paths: selected.map(item => item.path) }),
    });
    if (response.status === 401) return loginRedirect();
    if (!response.ok) throw new Error("Could not prepare the ZIP (" + response.status + ")");
    const archive = await response.json();
    $("browseStatus").textContent = "Starting download…";
    window.location.assign(archive.url);
  } catch (error) {
    $("browseStatus").textContent = error.message || "Could not prepare the download.";
  } finally {
    button.disabled = false;
    updateDownloadSelection();
  }
});

if (new URLSearchParams(location.search).get("mode") === "phone") showTransferMode("download");
