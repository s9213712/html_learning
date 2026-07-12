'use strict';

function platformCenterSetMsg(id, text, ok = true) {
  const el = $(id);
  if (!el) return;
  el.textContent = text || "";
  el.className = text ? "msg show " + (ok ? "ok" : "err") : "msg";
  el.setAttribute("role", ok ? "status" : "alert");
  el.setAttribute("aria-live", ok ? "polite" : "assertive");
  el.setAttribute("aria-atomic", "true");
  if (typeof scheduleInlineMessageClear === "function") scheduleInlineMessageClear(el, text, ok);
}

function platformConfirm(message, options = {}) {
  if (typeof showAppConfirm === "function") return showAppConfirm(message, options);
  return Promise.resolve(window.confirm(message));
}

function platformCopyFallback(text, title = "複製連結") {
  if (typeof showCopyFallbackDialog === "function") return showCopyFallbackDialog(text, title);
  window.prompt(title, text);
  return Promise.resolve(null);
}

function platformStatusLabel(status) {
  const map = {
    queued: "排隊中",
    running: "執行中",
    waiting_external: "等待外部服務",
    paused: "已暫停",
    succeeded: "已完成",
    failed: "失敗",
    cancelled: "已取消",
    retry_wait: "等待重試",
    expired: "已逾時",
    active: "啟用中",
    revoked: "已撤銷",
    expired_share: "已到期",
    view_limit_reached: "次數已用完"
  };
  return map[status] || status || "-";
}

function platformSeverityClass(status) {
  if (["failed", "expired", "expired_share", "revoked", "view_limit_reached"].includes(status)) return "danger";
  if (["running", "waiting_external", "queued", "active"].includes(status)) return "info";
  if (status === "succeeded") return "success";
  return "muted";
}

let shareCenterCountdownTimer = null;
let shareCenterActiveTab = "links";
let shareCenterEditingKey = "";
let shareCenterLatestShares = [];
let jobCenterActiveView = "general";
let jobCenterPollTimer = null;
let jobCenterLoadPromise = null;
let platformCenterAccountGeneration = 0;
const JOB_CENTER_POLL_INTERVAL_MS = 3000;
const JOB_CENTER_LIVE_SYNC_LIMIT = 12;
const JOB_CENTER_ACTIVE_STATUSES = new Set(["queued", "running", "waiting_external", "paused", "retry_wait"]);

function jobCenterPollIntervalMs() {
  const seconds = Number(siteConfig?.job_center_refresh_seconds || 3);
  return Math.max(1, Math.min(300, Number.isFinite(seconds) ? seconds : 3)) * 1000;
}

function formatPlatformCenterNumber(value) {
  return new Intl.NumberFormat("zh-TW").format(Number(value || 0));
}

function formatPlatformCenterPoints(value) {
  return `${formatPlatformCenterNumber(value)} 點`;
}

function platformJobCenterMergeKey(job = {}) {
  const sourceModule = String(job.source_module || "").trim();
  const sourceRef = String(job.source_ref || "").trim();
  if (sourceModule && sourceRef) return `${sourceModule}:${sourceRef}`;
  return String(job.job_uuid || "").trim();
}

function platformJobCenterLivePriority(job = {}) {
  return job.live_progress || job.live_status_source || job.metadata?.live_progress ? 1 : 0;
}

function mergePlatformJobCenterJobs(jobs = []) {
  const byKey = new Map();
  jobs.filter(Boolean).forEach((job) => {
    const key = platformJobCenterMergeKey(job);
    if (!key) return;
    const existing = byKey.get(key);
    const currentLive = platformJobCenterLivePriority(job);
    const existingLive = existing ? platformJobCenterLivePriority(existing) : 0;
    const currentTs = Date.parse(job.updated_at || job.created_at || "") || 0;
    const existingTs = existing ? (Date.parse(existing.updated_at || existing.created_at || "") || 0) : -1;
    if (!existing || currentLive > existingLive || (currentLive === existingLive && currentTs >= existingTs)) {
      byKey.set(key, job);
    }
  });
  return Array.from(byKey.values()).sort((a, b) => {
    const aActive = JOB_CENTER_ACTIVE_STATUSES.has(String(a.status || "")) ? 1 : 0;
    const bActive = JOB_CENTER_ACTIVE_STATUSES.has(String(b.status || "")) ? 1 : 0;
    if (aActive !== bActive) return bActive - aActive;
    const at = Date.parse(a.updated_at || a.created_at || "") || 0;
    const bt = Date.parse(b.updated_at || b.created_at || "") || 0;
    return bt - at;
  });
}

function isJobCenterActive() {
  return currentModuleTab === "jobs" || $("module-jobs")?.classList.contains("active");
}

function isJobCenterActiveJob(job = {}) {
  return JOB_CENTER_ACTIVE_STATUSES.has(String(job.status || ""));
}

function isTradingBackgroundJob(job = {}) {
  const source = String(job.source_module || "");
  const type = String(job.job_type || "");
  return source === "trading_background" || type.startsWith("trading.background.");
}

function canFetchOwnerScopedLiveJob(job = {}) {
  const ownerId = job.owner_user_id;
  if (ownerId === null || ownerId === undefined || ownerId === "") return true;
  return String(ownerId) === String(currentUserId || "");
}

function isLowSignalJobCenterNoise(job = {}) {
  const status = String(job.status || "");
  const source = String(job.source_module || "");
  const type = String(job.job_type || "");
  if (status !== "succeeded") return false;
  if (source === "cloud_drive_upload" && ["cloud_drive.upload", "storage.upload"].includes(type)) return true;
  if (source === "cloud_drive_resumable_upload") return true;
  return false;
}

function summarizeJobCenterJobs(jobs = []) {
  const general = jobs.filter((job) => !isTradingBackgroundJob(job));
  const trading = jobs.filter(isTradingBackgroundJob);
  const selected = jobCenterActiveView === "trading" ? trading : general;
  const hidden = selected.filter(isLowSignalJobCenterNoise);
  const visible = selected.filter((job) => !isLowSignalJobCenterNoise(job));
  return {
    visible,
    hiddenCount: hidden.length,
    activeCount: visible.filter(isJobCenterActiveJob).length,
    generalCount: general.filter((job) => !isLowSignalJobCenterNoise(job)).length,
    tradingCount: trading.length,
  };
}

function setJobCenterView(view, { reload = true } = {}) {
  jobCenterActiveView = view === "trading" ? "trading" : "general";
  document.querySelectorAll("[data-job-center-view]").forEach((btn) => {
    const active = btn.dataset.jobCenterView === jobCenterActiveView;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  if (reload && isJobCenterActive()) loadJobCenter({ quiet: false, force: true });
}

function markMissingLiveSourceJobs(jobs = [], liveKeys = new Set()) {
  return jobs.map((job) => {
    const source = String(job.source_module || "");
    if (!["cloud_drive_remote_download", "cloud_drive_resumable_upload"].includes(source)) return job;
    if (!isJobCenterActiveJob(job) || liveKeys.has(platformJobCenterMergeKey(job))) return job;
    return {
      ...job,
      status: job.status === "paused" ? "paused" : "waiting_external",
      stage: "progress_unavailable",
      stage_detail: "任務中心目前沒有取得即時來源回報；可能是伺服器重啟、瀏覽器尚未重選檔案，或外部 worker 正在恢復。",
      live_progress: false,
      live_status_source: "",
    };
  });
}

function shareCenterVisibilityLabel(value) {
  const map = {
    public: "公開",
    unlisted: "持連結",
    private: "私人"
  };
  return map[value] || value || "-";
}

function parseShareCenterExpiresAt(value) {
  const raw = String(value || "").trim();
  if (!raw) return null;
  const normalized = /[zZ]|[+-]\d{2}:?\d{2}$/.test(raw) ? raw : raw.replace(" ", "T");
  const timestamp = Date.parse(normalized);
  return Number.isFinite(timestamp) ? timestamp : null;
}

function formatShareCenterCountdown(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return "已到期";
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor((totalSeconds % 86400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (days > 0) return `剩餘 ${days} 天 ${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return `剩餘 ${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function shareCenterEffectiveStatus(share = {}, now = Date.now()) {
  const status = String(share.status || "active");
  if (status !== "active") return status;
  const timestamp = parseShareCenterExpiresAt(share.expires_at);
  if (timestamp && timestamp <= now) return "expired";
  return status;
}

function updateShareCenterCountdowns() {
  const items = Array.from(document.querySelectorAll("[data-share-countdown-until]"));
  const now = Date.now();
  let shouldRerender = false;
  items.forEach((item) => {
    const timestamp = parseShareCenterExpiresAt(item.getAttribute("data-share-countdown-until"));
    const expired = !!timestamp && timestamp <= now;
    item.textContent = timestamp ? `倒數計時：${formatShareCenterCountdown(timestamp - now)}` : "";
    if (expired && item.dataset.shareCountdownStatus !== "expired") {
      shouldRerender = true;
    }
  });
  if (shouldRerender) setTimeout(rerenderShareCenter, 0);
  if (!items.length && shareCenterCountdownTimer) {
    clearInterval(shareCenterCountdownTimer);
    shareCenterCountdownTimer = null;
  }
}

function scheduleShareCenterCountdowns() {
  updateShareCenterCountdowns();
  const hasCountdown = !!document.querySelector("[data-share-countdown-until]");
  if (hasCountdown && !shareCenterCountdownTimer) {
    shareCenterCountdownTimer = setInterval(updateShareCenterCountdowns, 1000);
  }
  if (!hasCountdown && shareCenterCountdownTimer) {
    clearInterval(shareCenterCountdownTimer);
    shareCenterCountdownTimer = null;
  }
}

function renderJobCenterJobs(jobs = [], { hiddenCount = 0 } = {}) {
  const list = $("job-center-list");
  if (!list) return;
  document.querySelectorAll("[data-job-center-view]").forEach((btn) => {
    const active = btn.dataset.jobCenterView === jobCenterActiveView;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  const running = jobs.filter((j) => ["queued", "running", "waiting_external", "retry_wait"].includes(j.status)).length;
  const failed = jobs.filter((j) => ["failed", "expired"].includes(j.status)).length;
  const done = jobs.filter((j) => j.status === "succeeded").length;
  if ($("job-center-running-count")) $("job-center-running-count").textContent = String(running);
  if ($("job-center-failed-count")) $("job-center-failed-count").textContent = String(failed);
  if ($("job-center-done-count")) $("job-center-done-count").textContent = String(done);
  if (!jobs.length) {
    const hiddenNote = hiddenCount ? `已隱藏 ${hiddenCount} 筆已完成的上傳任務。` : "目前沒有任務紀錄。";
    list.innerHTML = `<p style="color:var(--muted);">${sanitize(hiddenNote)}</p>`;
    return;
  }
  const hiddenNote = hiddenCount
    ? `<div class="drive-card-sub" style="margin-bottom:.55rem;">已隱藏 ${hiddenCount} 筆已完成的上傳任務，任務中心優先顯示仍在處理、失敗或需要操作的任務。</div>`
    : "";
  list.innerHTML = hiddenNote + jobs.map((job) => {
    const percent = Math.max(0, Math.min(100, Number(job.progress_percent || 0)));
    const cls = platformSeverityClass(job.status);
    const live = job.live_status_source
      ? `<span class="badge info">同步中：${sanitize(job.live_status_source)}</span>`
      : "";
    const err = job.error_message
      ? `<div class="drive-card-sub" style="color:#ffb74d;">${sanitize(job.error_stage || job.stage || "error")}：${sanitize(job.error_message)}</div>`
      : "";
    const remoteTaskId = job.source_module === "cloud_drive_remote_download"
      ? (job.metadata?.task_id || (String(job.source_ref || "").startsWith("remote_download:") ? String(job.source_ref || "").slice("remote_download:".length) : ""))
      : "";
    const remoteControlLocked = ["saving", "pause_requested", "cancel_requested"].includes(job.stage);
    const remoteCanPause = remoteTaskId && ["queued", "running", "waiting_external"].includes(job.status) && !remoteControlLocked;
    const remoteCanResume = remoteTaskId && job.status === "paused";
    const cancel = job.cancellable && !["succeeded", "failed", "cancelled", "expired"].includes(job.status)
      && !(remoteTaskId && ["saving", "cancel_requested"].includes(job.stage))
      ? `<button class="btn btn-danger" type="button" data-job-cancel="${sanitize(job.job_uuid)}" data-job-remote-download-task="${sanitize(remoteTaskId)}">取消</button>`
      : "";
    const pause = remoteCanPause
      ? `<button class="btn" type="button" data-job-remote-action="pause" data-job-remote-download-task="${sanitize(remoteTaskId)}">暫停</button>`
      : "";
    const resume = remoteCanResume
      ? `<button class="btn btn-primary" type="button" data-job-remote-action="resume" data-job-remote-download-task="${sanitize(remoteTaskId)}">繼續</button>`
      : "";
    const retry = ["failed", "retry_wait", "expired", "cancelled"].includes(job.status)
      ? `<button class="btn" type="button" data-job-retry="${sanitize(job.job_uuid)}">重試</button>`
      : "";
    const remoteDismissAttr = remoteTaskId ? ` data-job-remote-download-task="${sanitize(remoteTaskId)}"` : "";
    const dismiss = ["succeeded", "failed", "cancelled", "expired"].includes(job.status)
      ? `<button class="btn" type="button" data-job-dismiss="${sanitize(job.job_uuid)}"${remoteDismissAttr}>移除</button>`
      : "";
    return `
      <div class="drive-file-row">
        <div>
          <strong>${sanitize(job.title || "任務")}</strong>
          <div class="drive-card-sub">${sanitize(job.source_module || "-")} · ${sanitize(job.job_type || "-")} · ${sanitize(formatChatTime(job.updated_at || job.created_at || ""))}</div>
          <div class="drive-card-sub">${sanitize(job.stage || job.status || "-")} ${job.stage_detail ? "· " + sanitize(job.stage_detail) : ""}</div>
          <div class="mini-progress" aria-label="任務進度"><span style="width:${percent}%"></span></div>
          ${err}
        </div>
        <div class="drive-file-actions">
          ${live}
          <span class="badge ${cls}">${sanitize(platformStatusLabel(job.status))} · ${percent}%</span>
          ${pause}
          ${resume}
          ${cancel}
          ${retry}
          ${dismiss}
        </div>
      </div>
    `;
  }).join("");
  list.querySelectorAll("[data-job-cancel]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const remoteTaskId = btn.dataset.jobRemoteDownloadTask || "";
      if (remoteTaskId) return updateJobCenterRemoteDownloadTask(remoteTaskId, "cancel");
      return updateJobCenterJob(btn.dataset.jobCancel, "cancel");
    });
  });
  list.querySelectorAll("[data-job-remote-action]").forEach((btn) => {
    btn.addEventListener("click", () => updateJobCenterRemoteDownloadTask(btn.dataset.jobRemoteDownloadTask || "", btn.dataset.jobRemoteAction || ""));
  });
  list.querySelectorAll("[data-job-retry]").forEach((btn) => {
    btn.addEventListener("click", () => updateJobCenterJob(btn.dataset.jobRetry, "retry"));
  });
  list.querySelectorAll("[data-job-dismiss]").forEach((btn) => {
    btn.addEventListener("click", () => dismissJobCenterJob(btn.dataset.jobDismiss, {
      remoteTaskId: btn.dataset.jobRemoteDownloadTask || "",
    }));
  });
}

function mapComfyJobStatusToPlatform(status) {
  const value = String(status || "");
  if (["completed", "succeeded", "success"].includes(value)) return "succeeded";
  if (["error", "failed"].includes(value)) return "failed";
  if (value === "cancelled") return "cancelled";
  if (value === "queued") return "queued";
  return "running";
}

function mergeComfyJobLiveProgress(job = {}, liveJob = {}) {
  const progress = liveJob.progress || {};
  const status = mapComfyJobStatusToPlatform(liveJob.status);
  const percent = Number(progress.percent);
  return {
    ...job,
    status,
    progress_percent: Number.isFinite(percent) ? Math.max(0, Math.min(100, Math.round(percent))) : job.progress_percent,
    stage: progress.phase || liveJob.status || job.stage,
    stage_detail: progress.detail || liveJob.error || job.stage_detail || "",
    error_message: status === "failed" ? (liveJob.error || job.error_message || "ComfyUI 任務失敗") : "",
    error_stage: status === "failed" ? (progress.phase || job.error_stage || "error") : "",
    result: liveJob.result || job.result,
    metadata: {
      ...(job.metadata || {}),
      comfyui_job_id: liveJob.job_id || job.metadata?.comfyui_job_id || job.source_ref,
      live_progress: true,
      live_progress_updated_at: progress.updated_at || "",
      backend_unresponsive: Boolean(progress.backend_unresponsive),
    },
    live_progress: true,
    live_status_source: progress.backend_unresponsive
      ? (String(progress.backend_kind || "").toLowerCase() === "diffusers" ? "Diffusers 無新進度" : "ComfyUI 無回應")
      : (String(progress.backend_kind || "").toLowerCase() === "diffusers" ? "Diffusers" : "ComfyUI"),
    updated_at: new Date().toISOString(),
  };
}

function mapMediaStreamStatusToPlatform(status) {
  const value = String(status || "");
  if (value === "ready") return "succeeded";
  if (["failed", "unavailable"].includes(value)) return "failed";
  if (value === "processing") return "running";
  return "queued";
}

function mergeMediaStreamLiveProgress(job = {}, asset = {}) {
  const status = mapMediaStreamStatusToPlatform(asset.status);
  const assetDetail = asset.error_message
    || (status === "succeeded" ? "HLS 已可播放" : status === "running" ? "HLS 外部轉檔仍在處理" : "等待 HLS 處理");
  const jobDetail = String(job.stage_detail || "").trim();
  const jobStage = String(job.stage || "").trim();
  const keepJobProgressDetail = status === "running" && !asset.error_message && (jobDetail || jobStage);
  const detail = keepJobProgressDetail ? (jobDetail || assetDetail) : assetDetail;
  return {
    ...job,
    status,
    progress_percent: status === "succeeded" || status === "failed" ? 100 : Math.max(Number(job.progress_percent || 0), status === "running" ? 20 : 5),
    stage: keepJobProgressDetail ? (jobStage || asset.status || job.stage) : (asset.status || job.stage),
    stage_detail: detail,
    error_message: status === "failed" ? detail : "",
    error_stage: status === "failed" ? (asset.status || "failed") : "",
    metadata: {
      ...(job.metadata || {}),
      live_progress: true,
      stream_status: asset.status || "",
      variants_count: Array.isArray(asset.variants) ? asset.variants.length : 0,
    },
    live_progress: true,
    live_status_source: "HLS",
    updated_at: asset.updated_at || new Date().toISOString(),
  };
}

async function hydrateJobCenterLiveProgress(jobs = [], { csrf = "" } = {}) {
  const liveJobs = [...jobs];
  const activeIndexes = liveJobs
    .map((job, index) => ({ job, index }))
    .filter(({ job }) => isJobCenterActiveJob(job))
    .slice(0, JOB_CENTER_LIVE_SYNC_LIMIT);
  for (const { job, index } of activeIndexes) {
    try {
      if (!canFetchOwnerScopedLiveJob(job)) continue;
      if (job.source_module === "comfyui") {
        const jobId = job.metadata?.comfyui_job_id || job.source_ref || "";
        if (!jobId) continue;
        const res = await apiFetch(API + `/comfyui/jobs/${encodeURIComponent(jobId)}`, {
          credentials: "same-origin",
          headers: { "X-CSRF-Token": csrf || "" },
        });
        const json = await res.json().catch(() => ({}));
        if (res.ok && json.ok && json.job) {
          liveJobs[index] = mergeComfyJobLiveProgress(job, json.job);
        }
      } else if (job.source_module === "media_hls_prepare") {
        const fileId = job.metadata?.file_id || String(job.source_ref || "").replace(/^media_stream:/, "");
        if (!fileId) continue;
        const res = await apiFetch(API + `/media/${encodeURIComponent(fileId)}/stream-status`, {
          credentials: "same-origin",
          headers: { "X-CSRF-Token": csrf || "" },
        });
        const json = await res.json().catch(() => ({}));
        if (res.ok && json.ok && json.asset) {
          liveJobs[index] = mergeMediaStreamLiveProgress(job, json.asset);
        }
      }
    } catch (_) {
      // Live source sync is best-effort. The persisted Job Center row is still rendered.
    }
  }
  return liveJobs;
}

async function loadJobCenter(options = {}) {
  const quiet = Boolean(options.quiet);
  if (!currentUser) return;
  if (jobCenterLoadPromise) return jobCenterLoadPromise;
  const accountGeneration = platformCenterAccountGeneration;
  if (!quiet) platformCenterSetMsg("job-center-msg", "正在同步任務實際進度...", true);
  const loadPromise = (async () => {
    await fetchCsrfToken({ force: !quiet });
    if (accountGeneration !== platformCenterAccountGeneration) return null;
    const csrf = getCsrfToken();
    const endpoint = currentUser === "root"
      ? API + "/admin/jobs?limit=80&sync=1"
      : API + "/jobs?limit=80&sync=1";
    const res = await apiFetch(endpoint, {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (accountGeneration !== platformCenterAccountGeneration) return null;
    if (!json.ok) {
      platformCenterSetMsg("job-center-msg", json.msg || "任務中心讀取失敗", false);
      renderJobCenterJobs([]);
      return;
    }
    let jobs = Array.isArray(json.jobs) ? json.jobs : [];
    let liveDriveKeys = new Set();
    if (typeof loadDriveTaskCenterJobs === "function") {
      const driveJobs = await loadDriveTaskCenterJobs({ csrf });
      liveDriveKeys = new Set(driveJobs.map(platformJobCenterMergeKey).filter(Boolean));
      jobs = mergePlatformJobCenterJobs([...jobs, ...driveJobs]);
    }
    if (typeof getVideoUploadLiveJobs === "function") {
      jobs = mergePlatformJobCenterJobs([...jobs, ...getVideoUploadLiveJobs()]);
    }
    jobs = markMissingLiveSourceJobs(jobs, liveDriveKeys);
    jobs = mergePlatformJobCenterJobs(await hydrateJobCenterLiveProgress(jobs, { csrf }));
    if (accountGeneration !== platformCenterAccountGeneration) return null;
    const summary = summarizeJobCenterJobs(jobs);
    renderJobCenterJobs(summary.visible, { hiddenCount: summary.hiddenCount });
    const time = new Date().toLocaleTimeString("zh-TW", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    if ($("job-center-general-count")) $("job-center-general-count").textContent = String(summary.generalCount);
    if ($("job-center-trading-count")) $("job-center-trading-count").textContent = String(summary.tradingCount);
    const hiddenText = summary.hiddenCount ? `，已隱藏 ${summary.hiddenCount} 筆已完成上傳` : "";
    const viewText = jobCenterActiveView === "trading" ? "交易背景" : "一般任務";
    if (!quiet) {
      platformCenterSetMsg("job-center-msg", `${viewText}：已同步 ${summary.visible.length} 筆，進行中 ${summary.activeCount} 筆${hiddenText} · ${time}`, true);
    }
    return summary;
  })();
  jobCenterLoadPromise = loadPromise;
  try {
    return await loadPromise;
  } catch (err) {
    if (accountGeneration === platformCenterAccountGeneration && err?.name !== "AbortError" && !quiet) {
      platformCenterSetMsg("job-center-msg", "任務中心讀取失敗，請稍後再試。", false);
    }
    return null;
  } finally {
    if (jobCenterLoadPromise === loadPromise) jobCenterLoadPromise = null;
  }
}

function stopJobCenterPolling() {
  if (!jobCenterPollTimer) return;
  clearInterval(jobCenterPollTimer);
  jobCenterPollTimer = null;
}

function resetPlatformCentersAccountState() {
  platformCenterAccountGeneration += 1;
  stopJobCenterPolling();
  jobCenterLoadPromise = null;
  if (shareCenterCountdownTimer) clearInterval(shareCenterCountdownTimer);
  shareCenterCountdownTimer = null;
  shareCenterActiveTab = "links";
  shareCenterEditingKey = "";
  shareCenterLatestShares = [];
  jobCenterActiveView = "general";
  closeShareCenterEvents();

  ["job-center-list", "share-center-list", "video-manage-list"].forEach((id) => {
    const el = $(id);
    if (el) el.innerHTML = "";
  });
  [
    "job-center-running-count", "job-center-failed-count", "job-center-done-count",
    "job-center-general-count", "job-center-trading-count",
    "share-center-active-count", "share-center-ended-count", "share-center-access-count",
    "video-manage-count", "video-manage-views", "video-manage-likes",
    "video-manage-revenue", "video-manage-platform-fee", "video-manage-boost",
  ].forEach((id) => {
    const el = $(id);
    if (el) el.textContent = "0";
  });
  ["job-center-msg", "share-center-msg", "video-manage-msg"].forEach((id) => {
    platformCenterSetMsg(id, "", true);
  });
  document.querySelectorAll("[data-job-center-view]").forEach((btn) => {
    const active = btn.dataset.jobCenterView === "general";
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll("[data-share-center-tab]").forEach((btn) => {
    const active = btn.dataset.shareCenterTab === "links";
    btn.classList.toggle("btn-primary", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  const linksPanel = $("share-center-links-panel");
  const videosPanel = $("share-center-videos-panel");
  if (linksPanel) linksPanel.style.display = "";
  if (videosPanel) videosPanel.style.display = "none";
  renderTradingAssetOverview({});
  const confidence = $("trading-asset-confidence");
  if (confidence) confidence.textContent = "";
  const adminRisk = $("trading-asset-admin-risk");
  if (adminRisk) {
    adminRisk.style.display = "none";
    adminRisk.textContent = "";
  }
}

document.addEventListener("hackme:account-context-changed", resetPlatformCentersAccountState);

function startJobCenterPolling({ immediate = true, force = false } = {}) {
  if (!currentUser || !isJobCenterActive()) {
    stopJobCenterPolling();
    return;
  }
  let alreadyPolling = Boolean(jobCenterPollTimer);
  if (force && alreadyPolling) {
    stopJobCenterPolling();
    alreadyPolling = false;
  }
  if (immediate && (!alreadyPolling || force)) loadJobCenter({ quiet: false });
  if (alreadyPolling) return;
  jobCenterPollTimer = setInterval(() => {
    if (!currentUser || !isJobCenterActive() || document.hidden) {
      stopJobCenterPolling();
      return;
    }
    loadJobCenter({ quiet: true });
  }, jobCenterPollIntervalMs());
}

document.addEventListener("hackme:module-changed", (event) => {
  if (event.detail?.current === "jobs") startJobCenterPolling({ immediate: true });
  else stopJobCenterPolling();
});

document.addEventListener("click", (event) => {
  const btn = event.target?.closest?.("[data-job-center-view]");
  if (!btn) return;
  event.preventDefault();
  setJobCenterView(btn.dataset.jobCenterView || "general");
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopJobCenterPolling();
  else if (isJobCenterActive()) startJobCenterPolling({ immediate: true });
});

async function updateJobCenterJob(jobUuid, action) {
  if (!jobUuid || !["cancel", "retry"].includes(action)) return;
  if (action === "cancel" && !(await platformConfirm("確定要取消這個任務？", {
    title: "取消任務",
    confirmLabel: "取消任務",
    danger: true,
  }))) return;
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + `/jobs/${encodeURIComponent(jobUuid)}/${action}`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) {
      platformCenterSetMsg("job-center-msg", json.msg || "任務更新失敗", false);
      return;
    }
    platformCenterSetMsg("job-center-msg", json.msg || "任務已更新", true);
    await loadJobCenter();
  } catch (err) {
    platformCenterSetMsg("job-center-msg", `任務更新失敗：${err?.message || err || "請稍後重試"}`, false);
  }
}

async function updateJobCenterRemoteDownloadTask(taskId, action) {
  if (!taskId || !["pause", "resume", "cancel"].includes(action)) return;
  if (action === "cancel" && !(await platformConfirm("確定要取消這個下載任務？", {
    title: "取消下載任務",
    confirmLabel: "取消下載",
    danger: true,
  }))) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/cloud-drive/remote-download/tasks/${encodeURIComponent(taskId)}/${action}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    platformCenterSetMsg("job-center-msg", json.msg || "下載任務更新失敗", false);
    return;
  }
  if (action === "resume" && typeof resumeRemoteDownloadTaskPolling === "function") {
    resumeRemoteDownloadTaskPolling(json.task || {});
  }
  platformCenterSetMsg("job-center-msg", json.msg || "下載任務已更新", true);
  await loadJobCenter();
}

async function dismissJobCenterJob(jobUuid, options = {}) {
  if (!jobUuid) return;
  if (!(await platformConfirm("將這筆已結束的任務從列表移除？審計紀錄仍會保留。", {
    title: "移除任務紀錄",
    confirmLabel: "移除",
  }))) return;
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const remoteTaskId = String(options.remoteTaskId || "").trim();
    const url = remoteTaskId
      ? API + `/cloud-drive/remote-download/tasks/${encodeURIComponent(remoteTaskId)}`
      : API + `/jobs/${encodeURIComponent(jobUuid)}`;
    const res = await apiFetch(url, {
      method: "DELETE",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) {
      platformCenterSetMsg("job-center-msg", json.msg || "任務移除失敗", false);
      return;
    }
    platformCenterSetMsg("job-center-msg", json.msg || "任務已移除", true);
    await loadJobCenter();
  } catch (err) {
    platformCenterSetMsg("job-center-msg", `任務移除失敗：${err?.message || err || "請稍後重試"}`, false);
  }
}

function setShareCenterTab(tab, { load = true } = {}) {
  shareCenterActiveTab = tab === "videos" ? "videos" : "links";
  document.querySelectorAll("[data-share-center-tab]").forEach((btn) => {
    const active = btn.dataset.shareCenterTab === shareCenterActiveTab;
    btn.classList.toggle("btn-primary", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  const linksPanel = $("share-center-links-panel");
  const videosPanel = $("share-center-videos-panel");
  if (linksPanel) linksPanel.style.display = shareCenterActiveTab === "links" ? "" : "none";
  if (videosPanel) videosPanel.style.display = shareCenterActiveTab === "videos" ? "" : "none";
  const eventsPanel = $("share-center-events");
  if (eventsPanel && shareCenterActiveTab !== "links") eventsPanel.style.display = "none";
  if (shareCenterActiveTab === "videos") platformCenterSetMsg("share-center-msg", "", true);
  else platformCenterSetMsg("video-manage-msg", "", true);
  if (load) {
    if (shareCenterActiveTab === "videos") loadVideoManageCenter();
    else loadShareCenterLinks();
  }
}

function shareCenterUrlHasFragmentKey(url) {
  if (typeof driveShareUrlHasFragmentKey === "function") return driveShareUrlHasFragmentKey(url);
  try {
    const parsed = new URL(String(url || ""), location.origin);
    return Boolean(String(parsed.hash || "").replace(/^#/, ""));
  } catch (_) {
    return false;
  }
}

function shareCenterLinkUrl(share) {
  if (!share?.share_url) return { url: "", missingFragment: false };
  try {
    const parsed = new URL(share.share_url, location.origin);
    if (parsed.origin !== location.origin) return { url: "", missingFragment: false };
    let url = parsed.href;
    const requiresFragment = share.share_type === "file" && Boolean(share.requires_fragment_key);
    if (requiresFragment && typeof driveShareUrlWithRememberedFragment === "function") {
      url = driveShareUrlWithRememberedFragment(url);
    }
    return {
      url,
      missingFragment: requiresFragment && !shareCenterUrlHasFragmentKey(url),
    };
  } catch (_) {
    return { url: "", missingFragment: false };
  }
}

function closeShareCenterEvents() {
  const legacyPanel = $("share-center-events");
  if (legacyPanel) {
    legacyPanel.style.display = "none";
    legacyPanel.className = "msg";
    legacyPanel.textContent = "";
  }
  const overlay = $("share-center-events-modal");
  const body = $("share-center-events-modal-body");
  if (body) body.innerHTML = "";
  if (overlay) {
    overlay.classList.remove("show");
    overlay.setAttribute("aria-hidden", "true");
  }
  const anyOverlayOpen = document.querySelector(".user-edit-overlay.show, .album-full-preview-overlay.show");
  if (!anyOverlayOpen) document.body.classList.remove("modal-open");
}

function renderShareCenterEventsPanel(bodyHtml = "", { bad = false } = {}) {
  const legacyPanel = $("share-center-events");
  if (legacyPanel) legacyPanel.style.display = "none";
  const overlay = $("share-center-events-modal");
  const body = $("share-center-events-modal-body");
  if (!overlay || !body) return null;
  body.className = `share-center-event-list${bad ? " err" : ""}`;
  body.innerHTML = bodyHtml;
  overlay.classList.add("show");
  overlay.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  return overlay;
}

function shareCenterItemKey(share = {}) {
  return `${share.share_type || ""}:${share.id || ""}`;
}

function shareCenterDateTimeLocal(value) {
  return sanitize(String(value || "").slice(0, 16));
}

function shareCenterCanEdit(share = {}) {
  return ["active", "expired", "view_limit_reached", "password_locked"].includes(String(share.status || ""));
}

function shareCenterCanCopy(share = {}) {
  return !["expired", "view_limit_reached", "revoked"].includes(String(share.status || ""));
}

function shareCenterCanRevoke(share = {}) {
  return String(share.status || "") !== "revoked";
}

function shareCenterEditExpiresValue(share = {}) {
  const expiresAt = String(share.expires_at || "").trim();
  if (String(share.status || "") === "expired") {
    const timestamp = parseShareCenterExpiresAt(expiresAt);
    if (!timestamp || timestamp <= Date.now()) return "";
  }
  return shareCenterDateTimeLocal(expiresAt);
}

function shareCenterEditMaxViewsValue(share = {}) {
  const maxViews = Number(share.max_views || 0);
  const accessCount = Number(share.access_count || 0);
  if (String(share.status || "") === "view_limit_reached" && maxViews > 0 && accessCount >= maxViews) {
    return String(Math.min(1000000, accessCount + 1));
  }
  return String(maxViews || 0);
}

function shareCenterReactivateHint(share = {}) {
  const status = String(share.status || "");
  if (status === "expired") {
    return "這個分享已到期。儲存時會清除過期時間；你也可以改選新的未來到期日。";
  }
  if (status === "view_limit_reached") {
    return `這個分享已用完次數。最大存取次數需大於目前 ${Number(share.access_count || 0)} 次，或填 0 表示不限次數。`;
  }
  return "";
}

function renderShareCenterEditForm(share = {}) {
  const key = shareCenterItemKey(share);
  const type = String(share.share_type || "");
  const reactivateHint = shareCenterReactivateHint(share);
  const commonFields = (type === "file" || type === "album" || type === "video")
    ? `
      <div class="field">
        <span>到期時間</span>
        ${typeof shareExpiryPickerMarkup === "function"
          ? shareExpiryPickerMarkup({ hiddenAttrs: "data-share-edit-expires", value: shareCenterEditExpiresValue(share), help: "用日曆選擇日期；只選日期時預設當天 23:59 失效。清空代表不設定到期時間。" })
          : `<input type="datetime-local" data-share-edit-expires value="${shareCenterEditExpiresValue(share)}" />`}
      </div>
      <label class="field">
        <span>最大存取次數</span>
        <input type="number" min="0" max="1000000" step="1" data-share-edit-max-views value="${sanitize(shareCenterEditMaxViewsValue(share))}" />
        <small>目前已使用 ${sanitize(String(share.access_count || 0))} 次；填 0 表示不限次數。</small>
      </label>
      ${Number(share.access_count || 0) > 0 ? '<label class="field checkbox-field"><input type="checkbox" data-share-edit-reset-access-count /> 重置已使用次數</label>' : ""}
    `
    : "";
  const fileFields = type === "file"
    ? `
      <label class="field">
        <span>分享範圍</span>
        <select data-share-edit-scope>
          <option value="link" ${share.access_scope === "account" ? "" : "selected"}>知道連結即可存取</option>
          <option value="account" ${share.access_scope === "account" ? "selected" : ""}>指定帳戶</option>
        </select>
      </label>
      <label class="field">
        <span>指定帳戶</span>
        <input type="text" data-share-edit-account value="${sanitize(share.required_username || share.required_user_id || "")}" placeholder="好友 username 或 user id" />
      </label>
      <label class="field checkbox-field"><input type="checkbox" data-share-edit-can-preview ${share.can_preview ? "checked" : ""} /> 允許瀏覽器預覽</label>
      <label class="field checkbox-field"><input type="checkbox" data-share-edit-can-download ${share.can_download ? "checked" : ""} /> 允許下載</label>
    `
    : "";
  const passwordFields = (type === "file" || type === "album" || type === "video")
    ? `
      <label class="field">
        <span>分享密碼</span>
        <input type="password" autocomplete="new-password" data-share-edit-password placeholder="${share.password_required ? "留空代表不變更" : "可選"}" />
      </label>
      <label class="field checkbox-field"><input type="checkbox" data-share-edit-clear-password /> 清除分享密碼</label>
    `
    : "";
  return `
    <div class="share-center-edit-form" data-share-edit-key="${sanitize(key)}">
      ${reactivateHint ? `<div class="drive-card-sub">${sanitize(reactivateHint)}</div>` : ""}
      <div class="settings-option-grid">
        ${fileFields}
        ${passwordFields}
        ${commonFields}
      </div>
      <div class="drive-file-actions" style="justify-content:flex-start;margin-top:.6rem;">
        <button class="btn btn-primary" type="button" data-share-edit-save="${sanitize(key)}">儲存分享選項</button>
        <button class="btn" type="button" data-share-edit-cancel>取消</button>
      </div>
    </div>
  `;
}

function rerenderShareCenter() {
  renderShareCenter(shareCenterLatestShares);
}

function renderShareCenter(shares = []) {
  shareCenterLatestShares = Array.isArray(shares) ? shares : [];
  const list = $("share-center-list");
  if (!list) return;
  const now = Date.now();
  const active = shares.filter((s) => shareCenterEffectiveStatus(s, now) === "active").length;
  const ended = shares.length - active;
  const accesses = shares.reduce((total, s) => total + Number(s.access_count || 0), 0);
  if ($("share-center-active-count")) $("share-center-active-count").textContent = String(active);
  if ($("share-center-ended-count")) $("share-center-ended-count").textContent = String(ended);
  if ($("share-center-access-count")) $("share-center-access-count").textContent = String(accesses);
  if (!shares.length) {
    list.innerHTML = '<p style="color:var(--muted);">目前沒有分享連結。</p>';
    closeShareCenterEvents();
    scheduleShareCenterCountdowns();
    return;
  }
  list.innerHTML = shares.map((share) => {
    const key = shareCenterItemKey(share);
    const effectiveStatus = shareCenterEffectiveStatus(share, now);
    const viewShare = { ...share, status: effectiveStatus };
    const status = effectiveStatus === "expired" ? "expired_share" : effectiveStatus;
    const link = shareCenterLinkUrl(share);
    const url = link.url;
    const missingFragment = link.missingFragment;
    const copy = url && shareCenterCanCopy(viewShare) ? `<button class="btn" type="button" data-share-copy="${sanitize(url)}" data-share-missing-fragment="${missingFragment ? "1" : "0"}">複製</button>` : "";
    const edit = shareCenterCanEdit(viewShare)
      ? `<button class="btn" type="button" data-share-edit="${sanitize(key)}">${effectiveStatus === "active" ? "編輯" : "重新分享設定"}</button>`
      : "";
    const events = `<button class="btn" type="button" data-share-events-type="${sanitize(share.share_type)}" data-share-events-id="${sanitize(share.id)}">紀錄</button>`;
    const revoke = shareCenterCanRevoke(viewShare)
      ? `<button class="btn btn-danger" type="button" data-share-revoke-type="${sanitize(share.share_type)}" data-share-revoke-id="${sanitize(share.id)}">撤銷</button>`
      : "";
    const countdown = share.expires_at
      ? `<div class="drive-card-sub share-center-countdown" data-share-countdown-until="${sanitize(share.expires_at)}" data-share-countdown-status="${sanitize(effectiveStatus)}">倒數計時：${sanitize(formatShareCenterCountdown((parseShareCenterExpiresAt(share.expires_at) || 0) - now))}</div>`
      : "";
    const scopeText = share.access_scope === "account"
      ? `指定帳戶：${share.required_username || share.required_user_id || "-"}`
      : "知道連結即可存取";
    const endedHint = ["expired", "view_limit_reached"].includes(String(effectiveStatus || ""))
      ? `<div class="drive-card-sub">此連結目前不可存取。請先按「重新分享設定」，調整到期時間或存取次數後再複製給對方。</div>`
      : "";
    return `
      <div class="drive-file-row">
        <div>
          <strong>${sanitize(share.resource_title || "分享連結")}</strong>
          <div class="drive-card-sub">${sanitize(share.share_type || "-")} · 建立 ${sanitize(formatChatTime(share.created_at || ""))}</div>
          <div class="drive-card-sub">${sanitize(scopeText)} · 到期 ${sanitize(share.expires_at || "無")} · 次數 ${sanitize(String(share.access_count || 0))}${share.max_views ? " / " + sanitize(String(share.max_views)) : ""} · 密碼 ${share.password_required ? "是" : "否"}</div>
          ${countdown}
          ${endedHint}
          ${url ? `<div class="drive-card-sub drive-share-link">${sanitize(url)}</div>` : ""}
          ${missingFragment ? '<div class="drive-card-sub">E2EE 分享連結缺少瀏覽器片段金鑰，請重新產生分享連結後再複製。</div>' : ""}
        </div>
        <div class="drive-file-actions">
          <span class="badge ${platformSeverityClass(status)}">${sanitize(platformStatusLabel(status))}</span>
          ${edit}
          ${copy}
          ${events}
          ${revoke}
        </div>
        ${shareCenterEditingKey === key ? renderShareCenterEditForm(viewShare) : ""}
      </div>
    `;
  }).join("");
  list.querySelectorAll("[data-share-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const url = btn.dataset.shareCopy || "";
      if (btn.dataset.shareMissingFragment === "1") {
        platformCenterSetMsg("share-center-msg", "分享連結缺少 E2EE 片段金鑰，請重新產生分享連結後再複製。", false);
        if (typeof showCopyLinkFeedback === "function") showCopyLinkFeedback(btn, "缺少 E2EE 片段金鑰", false);
        return;
      }
      try {
        await navigator.clipboard.writeText(url);
        platformCenterSetMsg("share-center-msg", "連結已複製", true);
        if (typeof showCopyLinkFeedback === "function") showCopyLinkFeedback(btn, "已完成複製", true);
      } catch (_) {
        if (typeof showCopyLinkFeedback === "function") showCopyLinkFeedback(btn, "請手動複製完整連結", false);
        await platformCopyFallback(url, "分享連結");
      }
    });
  });
  list.querySelectorAll("[data-share-edit]").forEach((btn) => {
    btn.addEventListener("click", () => {
      shareCenterEditingKey = btn.dataset.shareEdit || "";
      closeShareCenterEvents();
      rerenderShareCenter();
    });
  });
  list.querySelectorAll("[data-share-edit-cancel]").forEach((btn) => {
    btn.addEventListener("click", () => {
      shareCenterEditingKey = "";
      rerenderShareCenter();
    });
  });
  list.querySelectorAll("[data-share-edit-save]").forEach((btn) => {
    btn.addEventListener("click", () => saveShareCenterOptions(btn.dataset.shareEditSave || ""));
  });
  list.querySelectorAll("[data-share-revoke-id]").forEach((btn) => {
    btn.addEventListener("click", () => revokeShareCenterLink(btn.dataset.shareRevokeType || "", btn.dataset.shareRevokeId || ""));
  });
  list.querySelectorAll("[data-share-events-id]").forEach((btn) => {
    btn.addEventListener("click", () => loadShareCenterEvents(btn.dataset.shareEventsType || "", btn.dataset.shareEventsId || ""));
  });
  scheduleShareCenterCountdowns();
}

async function saveShareCenterOptions(key) {
  const share = shareCenterLatestShares.find((item) => shareCenterItemKey(item) === key);
  const form = Array.from(document.querySelectorAll("[data-share-edit-key]")).find((node) => node.dataset.shareEditKey === key);
  if (!share || !form) return;
  const payload = {};
  if (share.share_type === "file") {
    payload.access_scope = form.querySelector("[data-share-edit-scope]")?.value || "link";
    const account = (form.querySelector("[data-share-edit-account]")?.value || "").trim();
    if (payload.access_scope === "account") {
      if (/^\d+$/.test(account)) payload.required_user_id = Number(account);
      else payload.required_username = account;
    }
    payload.can_preview = !!form.querySelector("[data-share-edit-can-preview]")?.checked;
    payload.can_download = !!form.querySelector("[data-share-edit-can-download]")?.checked;
  }
  if (share.share_type === "file" || share.share_type === "album" || share.share_type === "video") {
    const password = form.querySelector("[data-share-edit-password]")?.value || "";
    const clearPassword = !!form.querySelector("[data-share-edit-clear-password]")?.checked;
    if (password || clearPassword) payload.share_password = clearPassword ? "" : password;
    payload.clear_password = clearPassword;
  }
  if (share.share_type === "file" || share.share_type === "album" || share.share_type === "video") {
    if (typeof syncShareExpiryPickers === "function") syncShareExpiryPickers(form);
    payload.expires_at = form.querySelector("[data-share-edit-expires]")?.value || "";
    payload.max_views = form.querySelector("[data-share-edit-max-views]")?.value || "0";
    payload.reset_access_count = !!form.querySelector("[data-share-edit-reset-access-count]")?.checked;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + `/shares/${encodeURIComponent(share.share_type)}/${encodeURIComponent(share.id)}`, {
      method: "PUT",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf || ""
      },
      body: JSON.stringify(payload)
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || "分享設定更新失敗");
    platformCenterSetMsg("share-center-msg", json.msg || "分享選項已更新", true);
    shareCenterEditingKey = "";
    await loadShareCenterLinks();
  } catch (err) {
    platformCenterSetMsg("share-center-msg", err?.message || "分享設定更新失敗", false);
  }
}

async function loadShareCenter() {
  if (shareCenterActiveTab === "videos") {
    await loadVideoManageCenter();
    return;
  }
  await loadShareCenterLinks();
}

async function loadShareCenterLinks() {
  if (!currentUser) return [];
  const accountGeneration = platformCenterAccountGeneration;
  platformCenterSetMsg("share-center-msg", "正在讀取分享連結...", true);
  try {
    await fetchCsrfToken({ force: true });
    if (accountGeneration !== platformCenterAccountGeneration) return [];
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/shares?limit=120", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (accountGeneration !== platformCenterAccountGeneration) return [];
    if (!json.ok) {
      platformCenterSetMsg("share-center-msg", json.msg || "分享連結讀取失敗", false);
      renderShareCenter([]);
      return [];
    }
    renderShareCenter(json.shares || []);
    platformCenterSetMsg("share-center-msg", `已載入 ${(json.shares || []).length} 個分享連結`, true);
    return json.shares || [];
  } catch (err) {
    if (accountGeneration === platformCenterAccountGeneration && err?.name !== "AbortError") {
      platformCenterSetMsg("share-center-msg", "分享連結讀取失敗，請稍後再試。", false);
    }
    return [];
  }
}

async function openShareCenterEditor(type, id) {
  const shareType = String(type || "").trim();
  const shareId = String(id || "").trim();
  if (typeof setShareCenterTab === "function") setShareCenterTab("links", { load: false });
  shareCenterEditingKey = shareType && shareId ? `${shareType}:${shareId}` : "";
  const shares = await loadShareCenterLinks();
  if (shareCenterEditingKey && !shares.some((share) => shareCenterItemKey(share) === shareCenterEditingKey)) {
    platformCenterSetMsg("share-center-msg", "已建立相簿分享，請在列表中調整設定。", true);
  }
}

async function revokeShareCenterLink(type, id) {
  if (!type || !id) return;
  if (!(await platformConfirm("確定要撤銷這個分享連結？撤銷後既有連結將無法再使用。", {
    title: "撤銷分享連結",
    confirmLabel: "撤銷",
    danger: true,
  }))) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/shares/${encodeURIComponent(type)}/${encodeURIComponent(id)}/revoke`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    platformCenterSetMsg("share-center-msg", json.msg || "撤銷分享連結失敗", false);
    return;
  }
  platformCenterSetMsg("share-center-msg", "分享連結已撤銷", true);
  await loadShareCenter();
}

async function loadShareCenterEvents(type, id) {
  if (!type || !id) return;
  const accountGeneration = platformCenterAccountGeneration;
  renderShareCenterEventsPanel("<div>正在讀取分享紀錄...</div>");
  try {
    await fetchCsrfToken({ force: true });
    if (accountGeneration !== platformCenterAccountGeneration) return;
    const csrf = getCsrfToken();
    const res = await apiFetch(API + `/shares/${encodeURIComponent(type)}/${encodeURIComponent(id)}/access-events`, {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (accountGeneration !== platformCenterAccountGeneration) return;
    if (!json.ok) {
      renderShareCenterEventsPanel(`<div>${sanitize(json.msg || "分享紀錄讀取失敗")}</div>`, { bad: true });
      return;
    }
    const events = json.events || [];
    if (!events.length) {
      renderShareCenterEventsPanel("<div>目前沒有分享紀錄。</div>");
      return;
    }
    renderShareCenterEventsPanel(`<div class="share-center-event-list">${events.map((event) => {
      const when = formatChatTime(event.opened_at || event.created_at || "");
      const ip = event.source_ip || event.ip || "";
      const detail = event.detail || "";
      const userAgent = event.user_agent || "";
      const eventType = event.event_type || "";
      const isOpenEvent = event.opened_at || eventType === "opened" || eventType === "accessed";
      const timeLabel = isOpenEvent ? "開啟時間" : "時間";
      const ipText = ip ? ` · IP 來源：${sanitize(ip)}` : (isOpenEvent ? " · IP 來源：未記錄" : "");
      return `<div class="share-center-event-row">
        <div class="share-center-event-title">${sanitize(event.label || event.event_type || "-")}</div>
        <div class="share-center-event-meta">${timeLabel}：${sanitize(when || "-")}${ipText}</div>
        ${detail ? `<div class="share-center-event-detail">${sanitize(detail)}</div>` : ""}
        ${userAgent ? `<div class="share-center-event-detail">${sanitize(userAgent)}</div>` : ""}
      </div>`;
    }).join("")}</div>`);
  } catch (err) {
    if (accountGeneration === platformCenterAccountGeneration && err?.name !== "AbortError") {
      renderShareCenterEventsPanel("<div>分享紀錄讀取失敗，請稍後再試。</div>", { bad: true });
    }
  }
}

function videoManageRow(videoId) {
  return Array.from(document.querySelectorAll("[data-video-manage-id]"))
    .find((row) => String(row.dataset.videoManageId || "") === String(videoId || ""));
}

function videoManageSummaryFrom(videos = []) {
  return videos.reduce((summary, video) => {
    summary.total_videos += 1;
    summary.total_views += Number(video.view_count || 0);
    summary.total_likes += Number(video.like_count || 0);
    summary.total_revenue_points += Number(video.revenue_points || 0);
    summary.total_platform_fee_points += Number(video.platform_fee_points || 0);
    summary.total_boost_points += Number(video.boost_points_total || 0);
    return summary;
  }, {
    total_videos: 0,
    total_views: 0,
    total_likes: 0,
    total_revenue_points: 0,
    total_platform_fee_points: 0,
    total_boost_points: 0
  });
}

function videoManageProcessingLabel(video = {}) {
  const status = String(video.status || "ready");
  const streamStatus = String(video.stream_asset?.status || "");
  if (status === "processing" || streamStatus === "processing") return "後台處理中";
  if (streamStatus === "failed") return "HLS 處理失敗";
  if (streamStatus === "ready") return "HLS 已就緒";
  if (status === "blocked") return "不可播放";
  return "可播放";
}

function videoManageProcessingClass(video = {}) {
  const status = String(video.status || "ready");
  const streamStatus = String(video.stream_asset?.status || "");
  if (status === "processing" || streamStatus === "processing") return "info";
  if (streamStatus === "failed" || status === "blocked") return "danger";
  if (streamStatus === "ready" || status === "ready") return "success";
  return "muted";
}

function videoManageShareUrl(video = {}) {
  const url = video.share_url || video.share_link?.url || "";
  if (!url) return "";
  try {
    const parsed = new URL(url, location.origin);
    return parsed.origin === location.origin ? parsed.href : "";
  } catch (_) {
    return "";
  }
}

function renderVideoManageCenter(payload = {}) {
  const list = $("video-manage-list");
  if (!list) return;
  const videos = Array.isArray(payload) ? payload : (payload.videos || []);
  const summary = payload.summary || videoManageSummaryFrom(videos);
  if ($("video-manage-count")) $("video-manage-count").textContent = formatPlatformCenterNumber(summary.total_videos);
  if ($("video-manage-views")) $("video-manage-views").textContent = formatPlatformCenterNumber(summary.total_views);
  if ($("video-manage-likes")) $("video-manage-likes").textContent = formatPlatformCenterNumber(summary.total_likes);
  if ($("video-manage-revenue")) $("video-manage-revenue").textContent = formatPlatformCenterNumber(summary.total_revenue_points);
  if ($("video-manage-platform-fee")) $("video-manage-platform-fee").textContent = formatPlatformCenterNumber(summary.total_platform_fee_points);
  if ($("video-manage-boost")) $("video-manage-boost").textContent = formatPlatformCenterNumber(summary.total_boost_points);
  if (!videos.length) {
    list.innerHTML = '<p style="color:var(--muted);">目前沒有自己上傳的影音。</p>';
    return;
  }
  list.innerHTML = videos.map((video) => {
    const id = String(video.id || "");
    const boostActive = !!video.boost_active;
    const boostText = boostActive
      ? `曝光中 · 到期 ${sanitize(formatChatTime(video.boost_expires_at || ""))}`
      : "未加曝光";
    const statusClass = video.visibility === "public" ? "success" : (video.visibility === "unlisted" ? "info" : "muted");
    const processingLabel = videoManageProcessingLabel(video);
    const processingClass = videoManageProcessingClass(video);
    const shareUrl = videoManageShareUrl(video);
    const shareDisabled = String(video.status || "ready") !== "ready";
    const shareState = video.share_link?.state_message || (shareUrl ? "分享連結有效" : "尚未建立分享連結");
    const streamError = video.stream_asset?.error_message ? ` · ${video.stream_asset.error_message}` : "";
    return `
      <div class="drive-file-row video-manage-row" data-video-manage-id="${sanitize(id)}">
        <div class="video-manage-main">
          <div class="video-manage-head">
            <strong>${sanitize(video.title || "未命名影音")}</strong>
            <span class="badge ${statusClass}">${sanitize(shareCenterVisibilityLabel(video.visibility))}</span>
            <span class="badge ${processingClass}">${sanitize(processingLabel)}</span>
            <span class="badge ${boostActive ? "info" : "muted"}">${boostText}</span>
          </div>
          ${shareDisabled ? `<div class="msg">影音還在後台處理 HLS，完成前不會出現在影音列表；處理完成會通知上傳者。${sanitize(streamError)}</div>` : ""}
          <div class="video-manage-fields">
            <label class="field">
              <span>標題</span>
              <input type="text" maxlength="120" data-video-manage-title value="${sanitize(video.title || "")}" />
            </label>
            <label class="field">
              <span>可見性</span>
              <select data-video-manage-visibility>
                <option value="public" ${video.visibility === "public" ? "selected" : ""}>公開</option>
                <option value="unlisted" ${video.visibility === "unlisted" ? "selected" : ""}>持連結</option>
                <option value="private" ${video.visibility === "private" ? "selected" : ""}>私人</option>
              </select>
            </label>
            <label class="field video-manage-description-field">
              <span>說明</span>
              <textarea rows="2" maxlength="2000" data-video-manage-description>${sanitize(video.description || "")}</textarea>
            </label>
          </div>
          <div class="video-manage-metrics" aria-label="影音成效">
            <span>觀看 ${formatPlatformCenterNumber(video.view_count)}</span>
            <span>按讚 ${formatPlatformCenterNumber(video.like_count)}</span>
            <span>留言 ${formatPlatformCenterNumber(video.comment_count)}</span>
            <span>投幣 ${formatPlatformCenterPoints(video.gross_points)}</span>
            <span>實收 ${formatPlatformCenterPoints(video.revenue_points)}</span>
            <span>平台分潤 ${formatPlatformCenterPoints(video.platform_fee_points)}</span>
            <span>分享 ${formatPlatformCenterNumber(video.active_share_count)} / 開啟 ${formatPlatformCenterNumber(video.share_access_count)}</span>
          </div>
          <div class="video-manage-share-box">
            <div class="drive-card-sub">分享狀態：${sanitize(shareState)}</div>
            ${shareUrl ? `<div class="drive-card-sub drive-share-link">${sanitize(shareUrl)}</div>` : `<div class="drive-card-sub">尚未產生影音分享連結。</div>`}
            <div class="drive-file-actions" style="justify-content:flex-start;margin-top:.5rem;">
              <button class="btn btn-sm btn-primary" type="button" data-video-manage-share-open="${sanitize(id)}" ${shareDisabled ? "disabled" : ""}>分享設定</button>
            </div>
          </div>
          <div class="drive-card-sub">建立 ${sanitize(formatChatTime(video.created_at || ""))} · 更新 ${sanitize(formatChatTime(video.updated_at || ""))} · 來源 ${sanitize(video.cloud_filename || video.cloud_file_id || "-")}</div>
        </div>
        <div class="drive-file-actions video-manage-actions">
          <button class="btn" type="button" data-video-manage-open="${sanitize(id)}">觀看</button>
          <button class="btn btn-primary" type="button" data-video-manage-save="${sanitize(id)}">儲存</button>
          <label class="video-manage-boost-control" title="花積分提升影音在平台列表中的排序曝光，曝光到期後可再次加值。">
            <input type="number" min="10" step="10" value="100" data-video-manage-boost-amount />
            <button class="btn" type="button" data-video-manage-boost="${sanitize(id)}">加曝光</button>
          </label>
          <button class="btn btn-danger" type="button" data-video-manage-delete="${sanitize(id)}">刪除</button>
        </div>
      </div>
    `;
  }).join("");
  list.querySelectorAll("[data-video-manage-open]").forEach((btn) => {
    btn.addEventListener("click", () => openManagedVideo(btn.dataset.videoManageOpen || ""));
  });
  list.querySelectorAll("[data-video-manage-links]").forEach((btn) => {
    btn.addEventListener("click", () => setShareCenterTab("links"));
  });
  list.querySelectorAll("[data-video-manage-share-open]").forEach((btn) => {
    btn.addEventListener("click", () => openManagedVideoShareSettings(btn.dataset.videoManageShareOpen || ""));
  });
  list.querySelectorAll("[data-video-manage-save]").forEach((btn) => {
    btn.addEventListener("click", () => saveManagedVideo(btn.dataset.videoManageSave || ""));
  });
  list.querySelectorAll("[data-video-manage-boost]").forEach((btn) => {
    btn.addEventListener("click", () => boostManagedVideo(btn.dataset.videoManageBoost || ""));
  });
  list.querySelectorAll("[data-video-manage-delete]").forEach((btn) => {
    btn.addEventListener("click", () => deleteManagedVideo(btn.dataset.videoManageDelete || ""));
  });
}

async function loadVideoManageCenter() {
  if (!currentUser) return;
  const accountGeneration = platformCenterAccountGeneration;
  platformCenterSetMsg("video-manage-msg", "正在讀取自己的影音...", true);
  try {
    await fetchCsrfToken({ force: true });
    if (accountGeneration !== platformCenterAccountGeneration) return;
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/videos/manage?limit=120", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (accountGeneration !== platformCenterAccountGeneration) return;
    if (!json.ok) {
      platformCenterSetMsg("video-manage-msg", json.msg || "我的影音讀取失敗", false);
      renderVideoManageCenter({ videos: [] });
      return;
    }
    renderVideoManageCenter(json);
    platformCenterSetMsg("video-manage-msg", `已載入 ${(json.videos || []).length} 支影音`, true);
  } catch (err) {
    if (accountGeneration === platformCenterAccountGeneration && err?.name !== "AbortError") {
      platformCenterSetMsg("video-manage-msg", "我的影音讀取失敗，請稍後再試。", false);
    }
  }
}

async function saveManagedVideo(videoId) {
  const row = videoManageRow(videoId);
  if (!row) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const payload = {
    title: row.querySelector("[data-video-manage-title]")?.value || "",
    description: row.querySelector("[data-video-manage-description]")?.value || "",
    visibility: row.querySelector("[data-video-manage-visibility]")?.value || "public"
  };
  try {
    const res = await apiFetch(API + `/videos/${encodeURIComponent(videoId)}/manage`, {
      method: "PUT",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf || ""
      },
      body: JSON.stringify(payload)
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) {
      platformCenterSetMsg("video-manage-msg", json.msg || "影音更新失敗", false);
      return;
    }
    platformCenterSetMsg("video-manage-msg", "影音資料已更新", true);
    await loadVideoManageCenter();
  } catch (_) {
    platformCenterSetMsg("video-manage-msg", "影音更新失敗，請稍後再試。", false);
  }
}

async function ensureManagedVideoCanShare(row, videoId, csrf) {
  const visibilitySelect = row.querySelector("[data-video-manage-visibility]");
  const visibility = visibilitySelect?.value || "public";
  if (visibility === "unlisted") return true;
  if (!(await platformConfirm("要將這支影音改為「持連結」並產生分享連結嗎？", {
    title: "建立影音分享",
    confirmLabel: "改為持連結",
  }))) return false;
  const payload = {
    title: row.querySelector("[data-video-manage-title]")?.value || "",
    description: row.querySelector("[data-video-manage-description]")?.value || "",
    visibility: "unlisted"
  };
  const res = await apiFetch(API + `/videos/${encodeURIComponent(videoId)}/manage`, {
    method: "PUT",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrf || ""
    },
    body: JSON.stringify(payload)
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) throw new Error(json.msg || "影音可見性更新失敗");
  if (visibilitySelect) visibilitySelect.value = "unlisted";
  return true;
}

async function saveManagedVideoShareSettings(videoId, { regenerate = false } = {}) {
  const row = videoManageRow(videoId);
  if (!row) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const canShare = await ensureManagedVideoCanShare(row, videoId, csrf);
    if (!canShare) return;
    if (typeof syncShareExpiryPickers === "function") syncShareExpiryPickers(row);
    const payload = {
      share_expires_at: row.querySelector("[data-video-manage-share-expires]")?.value || "",
      share_max_views: row.querySelector("[data-video-manage-share-max-views]")?.value || ""
    };
    const password = (row.querySelector("[data-video-manage-share-password]")?.value || "").trim();
    if (password) payload.share_password = password;
    if (regenerate) payload.regenerate = true;
    const res = await apiFetch(API + `/videos/${encodeURIComponent(videoId)}/share-link`, {
      method: "PUT",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf || ""
      },
      body: JSON.stringify(payload)
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) throw new Error(json.msg || "影音分享設定更新失敗");
    platformCenterSetMsg("video-manage-msg", regenerate ? "影音分享連結已重新產生" : "影音分享設定已更新", true);
    await loadVideoManageCenter();
  } catch (err) {
    platformCenterSetMsg("video-manage-msg", err?.message || "影音分享設定更新失敗，請稍後再試。", false);
  }
}

async function openManagedVideoShareSettings(videoId) {
  if (!videoId) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + `/videos/${encodeURIComponent(videoId)}/share-link`, {
      method: "PUT",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf || ""
      },
      body: JSON.stringify({})
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || "影音分享連結建立失敗");
    const shareId = json.share_link?.id || json.video?.share_link?.id || "";
    if (shareId) shareCenterEditingKey = `video:${shareId}`;
    if (typeof switchModuleTab === "function") switchModuleTab("shares");
    setShareCenterTab("links", { load: false });
    await loadShareCenterLinks();
    platformCenterSetMsg("share-center-msg", "已切到分享管理，請在這裡調整分享選項。", true);
  } catch (err) {
    platformCenterSetMsg("video-manage-msg", err?.message || "影音分享連結建立失敗", false);
  }
}

async function copyManagedVideoShareLink(videoId) {
  const row = videoManageRow(videoId);
  if (!row) return;
  const button = row.querySelector("[data-video-manage-share-copy]");
  const text = row.querySelector(".drive-share-link")?.textContent?.trim() || "";
  if (!text) {
    platformCenterSetMsg("video-manage-msg", "尚未產生分享連結", false);
    if (typeof showCopyLinkFeedback === "function") showCopyLinkFeedback(button, "尚未產生分享連結", false);
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    platformCenterSetMsg("video-manage-msg", "影音分享連結已複製", true);
    if (typeof showCopyLinkFeedback === "function") showCopyLinkFeedback(button, "已完成複製", true);
  } catch (_) {
    if (typeof showCopyLinkFeedback === "function") showCopyLinkFeedback(button, "請手動複製完整連結", false);
    await platformCopyFallback(text, "影音分享連結");
  }
}

async function revokeManagedVideoShareLink(videoId) {
  if (!videoId || !(await platformConfirm("確定要撤銷這支影音的分享連結？撤銷後舊連結會失效。", {
    title: "撤銷影音分享",
    confirmLabel: "撤銷",
    danger: true,
  }))) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + `/videos/${encodeURIComponent(videoId)}/share-link`, {
      method: "DELETE",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) {
      platformCenterSetMsg("video-manage-msg", json.msg || "影音分享撤銷失敗", false);
      return;
    }
    platformCenterSetMsg("video-manage-msg", "影音分享連結已撤銷", true);
    await loadVideoManageCenter();
  } catch (_) {
    platformCenterSetMsg("video-manage-msg", "影音分享撤銷失敗，請稍後再試。", false);
  }
}

async function deleteManagedVideo(videoId) {
  if (!videoId || !(await platformConfirm("確定要刪除這支影音？分享連結也會失效。", {
    title: "刪除影音",
    confirmLabel: "刪除",
    danger: true,
  }))) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  try {
    const res = await apiFetch(API + `/videos/${encodeURIComponent(videoId)}/manage`, {
      method: "DELETE",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) {
      platformCenterSetMsg("video-manage-msg", json.msg || "影音刪除失敗", false);
      return;
    }
    platformCenterSetMsg("video-manage-msg", "影音已刪除", true);
    await loadVideoManageCenter();
  } catch (_) {
    platformCenterSetMsg("video-manage-msg", "影音刪除失敗，請稍後再試。", false);
  }
}

async function boostManagedVideo(videoId) {
  const row = videoManageRow(videoId);
  if (!row) return;
  const amount = Number(row.querySelector("[data-video-manage-boost-amount]")?.value || 0);
  if (!Number.isFinite(amount) || amount < 10) {
    platformCenterSetMsg("video-manage-msg", "曝光積分至少 10 點", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const idempotency = typeof makeVideoIdempotencyKey === "function"
    ? makeVideoIdempotencyKey("video-boost")
    : `video-boost:${Date.now()}:${Math.random().toString(16).slice(2)}`;
  try {
    const res = await apiFetch(API + `/videos/${encodeURIComponent(videoId)}/boost`, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf || "",
        "Idempotency-Key": idempotency
      },
      body: JSON.stringify({ amount, idempotency_key: idempotency })
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) {
      platformCenterSetMsg("video-manage-msg", json.msg || "曝光加值失敗", false);
      return;
    }
    platformCenterSetMsg("video-manage-msg", `已投入 ${formatPlatformCenterPoints(amount)} 增加曝光`, true);
    await loadVideoManageCenter();
  } catch (_) {
    platformCenterSetMsg("video-manage-msg", "曝光加值失敗，請稍後再試。", false);
  }
}

async function openManagedVideo(videoId) {
  if (!videoId) return;
  if (typeof switchModuleTab === "function") switchModuleTab("videos");
  if (typeof openVideoDetail === "function") {
    await openVideoDetail(videoId);
  }
}

document.addEventListener("click", (event) => {
  const closeEvents = event.target?.closest?.("[data-share-events-close]");
  if (closeEvents) {
    event.preventDefault();
    closeShareCenterEvents();
    return;
  }
  const tab = event.target?.closest?.("[data-share-center-tab]");
  if (!tab) return;
  event.preventDefault();
  setShareCenterTab(tab.dataset.shareCenterTab || "links");
});

function renderTradingAssetOverview(overview = {}) {
  const map = {
    "trading-asset-total-equity": overview.total_equity_points,
    "trading-asset-available": overview.available_points,
    "trading-asset-locked": overview.locked_points,
    "trading-asset-spot": overview.spot_market_value_points,
    "trading-asset-margin": overview.margin_position_equity_points,
    "trading-asset-interest": overview.accrued_interest_points
  };
  Object.entries(map).forEach(([id, value]) => {
    if ($(id)) $(id).textContent = `${formatTradingPointsValue(Number(value || 0))} 點`;
  });
  if ($("trading-asset-confidence")) {
    $("trading-asset-confidence").textContent = `${overview.low_confidence_price_count || 0} 個低信心價格 · ${overview.confidence_note || ""}`;
  }
}

function renderTradingAdminAssetOverview(risk = {}) {
  const el = $("trading-asset-admin-risk");
  if (!el) return;
  el.style.display = "block";
  el.textContent = `管理摘要：使用者 ${risk.account_count || 0} · 開放委託 ${risk.open_order_count || 0} · 借貸倉位 ${risk.open_margin_positions || 0} · 借貸本金 ${formatTradingPointsValue(Number(risk.total_margin_principal_points || 0))} 點 · 低信心市場 ${risk.low_confidence_price_count || 0}`;
}

async function loadTradingAssetOverview({ quiet = false } = {}) {
  if (!currentUser || !canAccessModule("trading")) return;
  const accountGeneration = platformCenterAccountGeneration;
  try {
    await fetchCsrfToken({ force: true });
    if (accountGeneration !== platformCenterAccountGeneration) return;
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/trading/asset-overview", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (accountGeneration !== platformCenterAccountGeneration) return;
    if (!json.ok) {
      if (!quiet) platformCenterSetMsg("trading-inline-msg", json.msg || "交易資產總覽讀取失敗", false);
      return;
    }
    renderTradingAssetOverview(json.overview || {});
    if (currentUser === "root" || currentRole === "manager" || currentRole === "super_admin") {
      const adminRes = await apiFetch(API + "/admin/trading/asset-overview", {
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf || "" }
      });
      const adminJson = await adminRes.json().catch(() => ({}));
      if (accountGeneration !== platformCenterAccountGeneration) return;
      if (!adminJson.ok) {
        if (!quiet) platformCenterSetMsg("trading-inline-msg", adminJson.msg || "交易所風險摘要讀取失敗", false);
        return;
      }
      renderTradingAdminAssetOverview(adminJson.risk || {});
    }
  } catch (err) {
    if (accountGeneration === platformCenterAccountGeneration && err?.name !== "AbortError" && !quiet) {
      platformCenterSetMsg("trading-inline-msg", err?.message || "交易資產總覽讀取失敗", false);
    }
  }
}

document.addEventListener("click", (event) => {
  const btn = event.target?.closest?.("#trading-asset-overview-refresh-btn");
  if (!btn) return;
  event.preventDefault();
  loadTradingAssetOverview({ quiet: false });
});
