const VIEWS = new Set(["dashboard", "videos", "logs", "settings"]);
const PROGRESS_RE = /^(?:\[\+(\d+) en cola\] )?(.+): *(\d+)% \((.+) \/ (.+)\)$/;
const CIRCUMFERENCE = 2 * Math.PI * 15;

const state = {
  view: "dashboard",
  messages: [],
  downloaderRunning: false,
  pendingDownloadIds: [],
  filter: "all",
  search: "",
  lastStatus: null,
  progress: null,
  logLines: [],
  selectedIds: new Set(),
  lastProgressFile: null,
};

const els = {
  searchInput: document.getElementById("searchInput"),
  refreshBtn: document.getElementById("refreshBtn"),
  serviceValue: document.getElementById("serviceValue"),
  serviceCardMeta: document.getElementById("serviceCardMeta"),
  progressValue: document.getElementById("progressValue"),
  progressMeta: document.getElementById("progressMeta"),
  downloadRingFill: document.getElementById("downloadRingFill"),
  savedValue: document.getElementById("savedValue"),
  savedMeta: document.getElementById("savedMeta"),
  folderValue: document.getElementById("folderValue"),
  folderMeta: document.getElementById("folderMeta"),
  activityList: document.getElementById("activityList"),
  videosNavCount: document.getElementById("videosNavCount"),
  messagesHint: document.getElementById("messagesHint"),
  messagesTableWrap: document.getElementById("messagesTableWrap"),
  logBox: document.getElementById("logBox"),
  logProgressTitle: document.getElementById("logProgressTitle"),
  logProgressFill: document.getElementById("logProgressFill"),
  statusMeta: document.getElementById("statusMeta"),
  serviceMeta: document.getElementById("serviceMeta"),
  downloadMeta: document.getElementById("downloadMeta"),
  replaceModal: document.getElementById("replaceModal"),
  replaceList: document.getElementById("replaceList"),
  toast: document.getElementById("toast"),
  loadMessagesBtn: document.getElementById("loadMessagesBtn"),
};

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function showToast(message, isError = false) {
  els.toast.textContent = message;
  els.toast.classList.toggle("error", isError);
  els.toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    els.toast.classList.remove("show");
  }, 4200);
}

function parseProgress(line) {
  if (!line) return null;
  const match = line.match(PROGRESS_RE);
  if (!match) return { raw: line, percent: null, filename: null, queueWaiting: 0 };
  return {
    raw: line,
    queueWaiting: match[1] ? Number(match[1]) : 0,
    filename: match[2].trim(),
    percent: Number(match[3]),
    currentLabel: match[4],
    totalLabel: match[5],
  };
}

function fileExtension(filename) {
  const match = String(filename).toLowerCase().match(/\.([a-z0-9]+)$/);
  return match ? match[1] : "file";
}

function fileBadgeClass(ext) {
  if (["mp4", "mkv", "mov", "webm"].includes(ext)) return ext;
  return "other";
}

function formatMessageDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function setView(name) {
  const view = VIEWS.has(name) ? name : "dashboard";
  state.view = view;
  document.querySelectorAll(".view").forEach((node) => {
    node.classList.toggle("active", node.id === `view-${view}`);
  });
  document.querySelectorAll(".nav-item").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  document.body.classList.toggle("show-fab", view === "videos");
  const hash = `#${view}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
}

function applyHashView() {
  setView(location.hash.replace("#", ""));
}

function kv(label, value) {
  return `<div><strong>${escapeHtml(label)}</strong>${escapeHtml(value)}</div>`;
}

function isProgressLine(line) {
  return Boolean(line && (PROGRESS_RE.test(line) || /(?:\[\+\d+ en cola\] )?.+: *\d+% /.test(line)));
}

function applyProgressToList() {
  const filename = state.progress?.filename || null;
  if (filename !== state.lastProgressFile) {
    state.lastProgressFile = filename;
    if (state.messages.length) renderMessages();
    return;
  }
  const fill = els.messagesTableWrap.querySelector(".video-row .progress-fill");
  if (fill) fill.style.width = `${state.progress?.percent || 0}%`;
}

function renderProgress(line) {
  state.progress = parseProgress(line);
  const progress = state.progress;
  const percent = progress?.percent;
  const hasDownload = Boolean(progress && (percent != null || progress.raw));

  els.progressValue.textContent = percent == null ? "En espera" : `${percent}%`;
  if (!hasDownload) {
    els.progressMeta.textContent = "Sin descarga activa";
  } else if (percent == null) {
    els.progressMeta.textContent = progress.raw;
  } else {
    const queue = progress.queueWaiting ? ` · +${progress.queueWaiting} en cola` : "";
    els.progressMeta.textContent = `${progress.filename} · ${progress.currentLabel} / ${progress.totalLabel}${queue}`;
  }

  const dash = percent == null ? 0 : (percent / 100) * CIRCUMFERENCE;
  els.downloadRingFill.setAttribute("stroke-dasharray", `${dash} ${CIRCUMFERENCE}`);
  els.logProgressTitle.textContent = progress?.filename || progress?.raw || "Sin descarga activa";
  els.logProgressFill.style.width = `${percent || 0}%`;
  applyProgressToList();
}

function renderLogs(lines) {
  if (!lines.length) {
    els.logBox.innerHTML = '<div class="empty">(sin entradas)</div>';
    return;
  }
  els.logBox.innerHTML = lines
    .map((line) => `<div class="log-line">${escapeHtml(line)}</div>`)
    .join("");
  els.logBox.scrollTop = els.logBox.scrollHeight;
}

function renderActivity(lines) {
  const items = (lines || []).filter(Boolean).slice(-6).reverse();
  if (!items.length) {
    els.activityList.innerHTML = '<div class="empty">Sin actividad reciente.</div>';
    return;
  }
  els.activityList.innerHTML = items.map((line) => `
    <article class="activity-item">
      <div class="file-badge other">LOG</div>
      <div class="video-info">
        <p class="video-name">${escapeHtml(line)}</p>
      </div>
    </article>
  `).join("");
}

function renderStatus(data) {
  state.lastStatus = data;
  const running = Boolean(data.process?.running);
  els.serviceValue.textContent = data.healthy ? "Activo" : (running ? "Parcial" : "Detenido");
  els.serviceCardMeta.textContent = `PID ${data.process?.pid ?? "—"}`;
  els.savedValue.textContent = String(data.downloaded_count ?? 0);
  els.savedMeta.textContent = "Registrados en historial";
  els.folderValue.textContent = data.download_dir_size_label || "—";
  els.folderMeta.textContent = data.download_dir || "—";
  els.videosNavCount.textContent = String(state.messages.length || data.downloaded_count || 0);

  els.statusMeta.innerHTML = [
    kv("PID", data.process?.pid ?? "—"),
    kv("LaunchAgent", data.launch_agent?.loaded ? "cargado" : "no cargado"),
    kv("Estado launchd", data.launch_agent?.state ?? "—"),
  ].join("");

  els.serviceMeta.innerHTML = [
    kv("Label", data.launch_agent?.label ?? "—"),
    kv("Plist", data.launch_agent?.plist_path ?? "—"),
    kv("Instalado", data.launch_agent?.installed ? "sí" : "no"),
  ].join("");

  els.downloadMeta.innerHTML = [
    kv("Carpeta", data.download_dir ?? "—"),
    kv("Tamaño", data.download_dir_size_label ?? "—"),
    kv("Log", data.log_path ?? "—"),
  ].join("");

  renderProgress(data.progress_line);
  renderActivity(data.recent_log);
}

function messageStatus(message) {
  if (message.downloaded && message.file_exists) {
    return { label: "Descargado", className: "ok" };
  }
  if (message.file_exists) return { label: "En disco", className: "warn" };
  if (message.downloaded) return { label: "Marcado, sin archivo", className: "warn" };
  return { label: "Pendiente", className: "muted" };
}

function matchesFilter(message) {
  const downloaded = message.downloaded && message.file_exists;
  if (state.filter === "pending") return !downloaded;
  if (state.filter === "downloaded") return downloaded;
  return true;
}

function matchesSearch(message) {
  if (!state.search) return true;
  const haystack = [
    message.filename,
    message.caption || "",
    String(message.message_id),
  ].join(" ").toLowerCase();
  return haystack.includes(state.search);
}

function visibleMessages() {
  return state.messages.filter((message) => matchesFilter(message) && matchesSearch(message));
}

function isActiveFile(message) {
  const filename = state.progress?.filename;
  return Boolean(filename && message.filename === filename);
}

function renderMessages() {
  const messages = visibleMessages();
  els.videosNavCount.textContent = String(state.messages.length || state.lastStatus?.downloaded_count || 0);

  if (!state.messages.length) {
    els.messagesTableWrap.innerHTML = '<div class="empty">Sin mensajes cargados.</div>';
    return;
  }
  if (!messages.length) {
    els.messagesTableWrap.innerHTML = '<div class="empty">Ningún video coincide con el filtro.</div>';
    return;
  }

  els.messagesTableWrap.innerHTML = `<div class="video-list">${messages.map(renderVideoRow).join("")}</div>`;
}

function renderVideoRow(message) {
  const ext = fileExtension(message.filename);
  const status = messageStatus(message);
  const active = isActiveFile(message);
  const checked = state.selectedIds.has(message.message_id) ? "checked" : "";
  const percent = active ? (state.progress?.percent || 0) : 0;
  const bar = active
    ? `<div class="progress-track"><div class="progress-fill" style="width:${percent}%"></div></div>`
    : "";
  const pill = active
    ? '<span class="badge live">Descargando</span>'
    : `<span class="badge ${status.className}">${status.label}</span>`;
  const caption = message.caption ? ` · ${escapeHtml(message.caption)}` : "";

  return `
    <article class="video-row">
      <input type="checkbox" class="message-checkbox" data-id="${message.message_id}" ${checked}>
      <div class="file-badge ${fileBadgeClass(ext)}">${escapeHtml(ext.toUpperCase())}</div>
      <div class="video-info">
        <p class="video-name">${escapeHtml(message.filename)}</p>
        <div class="video-sub">${escapeHtml(formatMessageDate(message.date))} · ${escapeHtml(message.size_label)}${caption}</div>
        ${bar}
      </div>
      <div class="video-side">${pill}</div>
    </article>
  `;
}

function syncSelectedFromDom() {
  document.querySelectorAll(".message-checkbox").forEach((input) => {
    const id = Number(input.dataset.id);
    if (input.checked) state.selectedIds.add(id);
    else state.selectedIds.delete(id);
  });
}

function getSelectedMessageIds() {
  syncSelectedFromDom();
  return [...state.selectedIds];
}

function setMessagesHint(text, isWarning = false) {
  els.messagesHint.textContent = text;
  els.messagesHint.classList.toggle("warn", isWarning);
}

async function loadStatus() {
  const response = await fetch("/api/status");
  const data = await response.json();
  renderStatus(data);
}

async function loadProgress() {
  const response = await fetch("/api/progress");
  const data = await response.json();
  renderProgress(data.line);
}

async function postAction(url) {
  const response = await fetch(url, { method: "POST" });
  const data = await response.json();
  if (!response.ok) {
    showToast(data.error || "No se pudo completar la acción", true);
    return;
  }
  await loadStatus();
}

async function loadMessages() {
  setMessagesHint("Cargando mensajes del canal...");
  els.loadMessagesBtn.disabled = true;
  try {
    const response = await fetch("/api/messages?limit=150");
    const data = await response.json();
    if (!response.ok) {
      setMessagesHint(data.error || "No se pudieron cargar los mensajes.", Boolean(data.session_busy));
      return;
    }
    state.messages = data.messages || [];
    state.downloaderRunning = Boolean(data.downloader_running);
    state.selectedIds.clear();
    renderMessages();
    if (state.downloaderRunning) {
      setMessagesHint("Mensajes cargados. El servicio automático está activo: las descargas manuales se encolan.");
      return;
    }
    setMessagesHint("Mensajes cargados. Puedes seleccionar videos y descargarlos manualmente.");
  } finally {
    els.loadMessagesBtn.disabled = false;
  }
}

function collectReplaceCandidates(selectedIds) {
  const selected = new Set(selectedIds);
  return state.messages.filter(
    (message) => selected.has(message.message_id) && (message.file_exists || message.downloaded),
  );
}

function openReplaceModal(candidates) {
  els.replaceList.innerHTML = candidates
    .map((message) => `<li>${escapeHtml(message.filename)} (ID ${message.message_id})</li>`)
    .join("");
  els.replaceModal.classList.add("open");
}

function closeReplaceModal() {
  els.replaceModal.classList.remove("open");
  state.pendingDownloadIds = [];
}

async function submitDownload(messageIds, replaceIds) {
  const response = await fetch("/api/messages/download", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message_ids: messageIds,
      replace_ids: replaceIds,
    }),
  });
  const data = await response.json();
  if (!response.ok) {
    showToast(data.error || "No se pudo iniciar la descarga", true);
    return;
  }
  if (data.mode === "queued") {
    showToast(`${data.queued} descarga(s) encolada(s). El servicio automático las procesará.`);
    return;
  }
  const skipped = (data.skipped || []).length;
  const skippedNote = skipped ? ` ${skipped} omitido(s).` : "";
  showToast(`${data.queued} descarga(s) completada(s).${skippedNote}`);
  await loadMessages();
}

async function downloadSelected(replaceExisting = false) {
  const selectedIds = state.pendingDownloadIds.length
    ? state.pendingDownloadIds
    : getSelectedMessageIds();

  if (!selectedIds.length) {
    showToast("Selecciona al menos un video.", true);
    return;
  }

  const replaceCandidates = collectReplaceCandidates(selectedIds);
  if (!replaceExisting && replaceCandidates.length) {
    state.pendingDownloadIds = selectedIds;
    openReplaceModal(replaceCandidates);
    return;
  }

  const replaceIds = replaceExisting
    ? replaceCandidates.map((message) => message.message_id)
    : [];
  closeReplaceModal();
  await submitDownload(selectedIds, replaceIds);
}

function connectLogStream() {
  const source = new EventSource("/api/logs/stream");
  source.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    const line = payload.line;
    if (isProgressLine(line)) {
      renderProgress(line);
      return;
    }
    state.logLines.push(line);
    if (state.logLines.length > 500) state.logLines.shift();
    renderLogs(state.logLines);
  };
  source.onerror = () => {
    source.close();
    setTimeout(connectLogStream, 2000);
  };
}

function bindEvents() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
  document.querySelectorAll(".filter-btn").forEach((button) => {
    button.addEventListener("click", () => {
      state.filter = button.dataset.filter;
      document.querySelectorAll(".filter-btn").forEach((node) => {
        node.classList.toggle("active", node === button);
      });
      renderMessages();
    });
  });

  els.searchInput.addEventListener("input", () => {
    state.search = els.searchInput.value.trim().toLowerCase();
    if (state.view !== "videos") setView("videos");
    renderMessages();
  });

  els.refreshBtn.addEventListener("click", loadStatus);
  document.getElementById("seeLogsBtn").addEventListener("click", () => setView("logs"));
  document.getElementById("openDestCardBtn").addEventListener("click", () => postAction("/api/open-downloads"));
  document.getElementById("openDestSettingsBtn").addEventListener("click", () => postAction("/api/open-downloads"));
  document.getElementById("startBtn").addEventListener("click", () => postAction("/api/service/start"));
  document.getElementById("stopBtn").addEventListener("click", () => postAction("/api/service/stop"));
  document.getElementById("restartBtn").addEventListener("click", () => postAction("/api/service/restart"));
  els.loadMessagesBtn.addEventListener("click", loadMessages);
  document.getElementById("selectAllBtn").addEventListener("click", () => {
    visibleMessages().forEach((message) => state.selectedIds.add(message.message_id));
    renderMessages();
  });
  document.getElementById("clearSelectionBtn").addEventListener("click", () => {
    state.selectedIds.clear();
    renderMessages();
  });
  document.getElementById("downloadSelectedBtn").addEventListener("click", () => downloadSelected(false));
  document.getElementById("fabDownloadBtn").addEventListener("click", () => downloadSelected(false));
  document.getElementById("replaceCancelBtn").addEventListener("click", closeReplaceModal);
  document.getElementById("replaceConfirmBtn").addEventListener("click", () => downloadSelected(true));
  els.replaceModal.addEventListener("click", (event) => {
    if (event.target === els.replaceModal) closeReplaceModal();
  });
  els.messagesTableWrap.addEventListener("change", (event) => {
    if (event.target.classList.contains("message-checkbox")) syncSelectedFromDom();
  });
  window.addEventListener("hashchange", applyHashView);
}

applyHashView();
bindEvents();
loadStatus();
loadProgress();
connectLogStream();
setInterval(loadStatus, 10000);
setInterval(loadProgress, 500);
