'use strict';

const videoState = {
  sort: "new",
  searchQuery: "",
  videos: [],
  current: null,
  viewRecordedFor: new Set(),
  browseLoaded: false,
  currentHls: null,
  currentRealtimeAbortController: null,
  currentRealtimeSeekHandler: null,
  currentRealtimeSeekPlayer: null,
  currentObjectUrl: "",
  hlsLibraryPromise: null,
  playbackSessionId: 0,
  streamDebugSnapshot: {},
  streamDebugMetrics: {},
  streamDebugInterval: 0,
  streamDebugBurden: null,
  streamDebugBurdenLast: null,
  streamDebugBurdenFetchAt: 0,
  streamDebugBurdenInflight: false,
  manualQualitySelection: false,
  autoQualityFallbackApplied: false,
  userSeeking: false,
  lastSeekAt: 0,
  lastSeekTarget: 0,
  realtimeSeekRestarting: false,
  realtimeSeekSuppressUntil: 0,
  realtimeBaseOffsetSeconds: 0,
  danmakuEnabled: true,
  danmakuDensity: "medium",
  danmakuOpacity: 0.92,
  danmakuItems: [],
  danmakuShown: new Set(),
  danmakuFetchFromMs: 0,
  danmakuFetchUntilMs: 0,
  danmakuLoading: false,
  danmakuAnimationId: 0,
  danmakuLaneUntil: [],
  subtitleShiftMs: 0,
  navigationGeneration: 0,
  detailRequestGeneration: 0,
  detailAbortController: null,
};
let videoPublishDriveFiles = [];
let videoPendingPublishSelection = null;
const videoUploadLiveJobs = new Map();
const VIDEO_SHARE_FRAGMENT_STORAGE_KEY = "hackme_web.video_share_fragments";
const VIDEO_HLS_JS_URL = "/js/hls.light.min.js?v=20260505-hlsjs";
const VIDEO_E2EE_STREAM_V2_WORKER_URL = "/js/e2ee-stream-v2-worker.js?v=20260505-e2eev2";
const VIDEO_STREAM_DEBUG_STORAGE_KEY = "hackme_web.video_stream_debug";
const VIDEO_E2EE_STREAM_V2_CHUNK_SIZE = 512 * 1024;
const VIDEO_E2EE_STREAM_V2_MAX_RETRIES = 2;
const VIDEO_E2EE_STREAM_V2_CACHE_LIMIT = 16;
const VIDEO_E2EE_LOCAL_TASK_STORAGE_KEY = "hackme_web.video_e2ee_local_task";
const VIDEO_E2EE_DERIVATIVE_TARGET_HEIGHTS = [720, 480];
const VIDEO_DANMAKU_SPECIAL_PRICES = {
  none: 0,
  outline: 10,
  glow: 30,
  rainbow: 50,
};
let activeVideoE2eeLocalTasks = 0;

function videoUploadLiveJobId() {
  return `video-upload-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function updateVideoUploadLiveJob(jobId, updates = {}) {
  if (!jobId) return null;
  const now = new Date().toISOString();
  const existing = videoUploadLiveJobs.get(jobId) || {
    job_uuid: jobId,
    source_module: "video_upload_client",
    source_ref: `video_upload:${jobId}`,
    job_type: "video.upload.client",
    title: "影音上傳",
    description: "瀏覽器端影音上傳進度與伺服器處理等待狀態",
    status: "running",
    progress_percent: 0,
    stage: "uploading",
    stage_detail: "影音檔上傳中",
    created_at: now,
    metadata: {},
    live_progress: true,
    live_status_source: "Video upload",
  };
  const next = {
    ...existing,
    ...updates,
    updated_at: now,
    metadata: { ...(existing.metadata || {}), ...((updates && updates.metadata) || {}) },
    live_progress: true,
    live_status_source: "Video upload",
  };
  videoUploadLiveJobs.set(jobId, next);
  return next;
}

window.getVideoUploadLiveJobs = function getVideoUploadLiveJobs() {
  const now = Date.now();
  const maxAgeMs = 15 * 60 * 1000;
  Array.from(videoUploadLiveJobs.entries()).forEach(([key, job]) => {
    const updated = Date.parse(job.updated_at || job.created_at || "") || 0;
    if (updated && now - updated > maxAgeMs) videoUploadLiveJobs.delete(key);
  });
  return Array.from(videoUploadLiveJobs.values());
};

function rememberVideoE2eeLocalTask(task) {
  try {
    localStorage.setItem(VIDEO_E2EE_LOCAL_TASK_STORAGE_KEY, JSON.stringify({
      ...(task || {}),
      updated_at: new Date().toISOString(),
    }));
  } catch (_) {}
}

function clearVideoE2eeLocalTask(jobId = "") {
  activeVideoE2eeLocalTasks = Math.max(0, activeVideoE2eeLocalTasks - 1);
  try {
    const raw = localStorage.getItem(VIDEO_E2EE_LOCAL_TASK_STORAGE_KEY);
    if (!raw) return;
    const task = JSON.parse(raw);
    if (!jobId || String(task?.job_id || "") === String(jobId)) {
      localStorage.removeItem(VIDEO_E2EE_LOCAL_TASK_STORAGE_KEY);
    }
  } catch (_) {
    try { localStorage.removeItem(VIDEO_E2EE_LOCAL_TASK_STORAGE_KEY); } catch (_err) {}
  }
}

function warnInterruptedVideoE2eeLocalTask() {
  try {
    const raw = localStorage.getItem(VIDEO_E2EE_LOCAL_TASK_STORAGE_KEY);
    if (!raw) return;
    const task = JSON.parse(raw);
    localStorage.removeItem(VIDEO_E2EE_LOCAL_TASK_STORAGE_KEY);
    if (Date.now() - (Date.parse(task?.updated_at || "") || 0) > 12 * 60 * 60 * 1000) return;
    videoMsg("上一個 E2EE 本機轉檔 / 加密任務因頁面重新整理或關閉而中斷；請重新選擇影音並再次建立分享省流量版本。", false);
  } catch (_) {}
}

window.addEventListener("beforeunload", (event) => {
  if (activeVideoE2eeLocalTasks <= 0) return;
  event.preventDefault();
  event.returnValue = "E2EE 本機轉檔 / 加密尚未完成，離開頁面會中斷任務。";
});

function videoMsg(text, ok = true) {
  const el = $("video-msg");
  if (el) flash(el, text, ok);
}

function videoFormatBytes(bytes) {
  if (typeof formatDriveBytes === "function") return formatDriveBytes(bytes);
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function setVideoUploadProgress({ visible = true, percent = 0, loaded = 0, total = 0, status = "準備上傳", indeterminate = false } = {}) {
  const panel = $("video-upload-progress");
  const fill = $("video-upload-progress-fill");
  const statusEl = $("video-upload-progress-status");
  const bytesEl = $("video-upload-progress-bytes");
  const percentEl = $("video-upload-progress-percent");
  if (!panel) return;
  panel.hidden = !visible;
  if (!visible) return;
  const normalized = Number.isFinite(Number(percent)) ? Math.max(0, Math.min(100, Number(percent))) : 0;
  if (statusEl) statusEl.textContent = status || "處理中";
  if (fill) {
    fill.classList.toggle("indeterminate", !!indeterminate);
    fill.style.width = indeterminate ? "45%" : `${normalized}%`;
  }
  if (bytesEl) {
    bytesEl.textContent = total
      ? `${videoFormatBytes(loaded || 0)} / ${videoFormatBytes(total)}`
      : loaded
        ? videoFormatBytes(loaded)
        : "計算中";
  }
  if (percentEl) percentEl.textContent = indeterminate ? "處理中" : `${Math.round(normalized)}%`;
}

function videoUploadFormWithProgress(url, form, onProgress) {
  const send = async (csrf) => new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.withCredentials = true;
    if (csrf) xhr.setRequestHeader("X-CSRF-Token", csrf);
    xhr.upload.onprogress = (event) => {
      if (typeof onProgress === "function") onProgress(event);
    };
    xhr.onload = () => {
      let json = {};
      try {
        json = JSON.parse(xhr.responseText || "{}");
      } catch (err) {
        json = {};
      }
      resolve({ status: xhr.status, ok: xhr.status >= 200 && xhr.status < 300, json });
    };
    xhr.onerror = () => reject(new Error("上傳連線失敗"));
    xhr.ontimeout = () => reject(new Error("上傳逾時"));
    xhr.send(form);
  });
  return (async () => {
    const firstCsrf = typeof fetchCsrfToken === "function" ? await fetchCsrfToken() : "";
    let result = await send(firstCsrf);
    if (result.status === 403 && result.json?.error === "csrf_invalid" && typeof fetchCsrfToken === "function") {
      const refreshed = await fetchCsrfToken({ force: true });
      result = await send(refreshed);
    }
    return result;
  })();
}

function destroyCurrentVideoPlaybackArtifacts() {
  if (videoState.currentRealtimeSeekHandler && videoState.currentRealtimeSeekPlayer) {
    try {
      videoState.currentRealtimeSeekPlayer.removeEventListener("seeking", videoState.currentRealtimeSeekHandler);
    } catch (_) {
      // ignore teardown failure
    }
  }
  videoState.currentRealtimeSeekHandler = null;
  videoState.currentRealtimeSeekPlayer = null;
  videoState.realtimeBaseOffsetSeconds = 0;
  if (videoState.currentHls && typeof videoState.currentHls.destroy === "function") {
    try {
      videoState.currentHls.destroy();
    } catch (_) {
      // ignore teardown failure
    }
  }
  videoState.currentHls = null;
  if (videoState.currentRealtimeAbortController) {
    try {
      videoState.currentRealtimeAbortController.abort();
    } catch (_) {
      // ignore abort failure
    }
  }
  videoState.currentRealtimeAbortController = null;
  if (videoState.currentObjectUrl) {
    try {
      URL.revokeObjectURL(videoState.currentObjectUrl);
    } catch (_) {
      // ignore revoke failure
    }
  }
  videoState.currentObjectUrl = "";
}

function setVideoPlaybackStatus(text, bad = false) {
  const status = $("video-playback-status");
  if (!status) return;
  status.textContent = text || "";
  status.dataset.state = bad ? "error" : "info";
}

function resetVideoPlaybackStatusState() {
  const status = $("video-playback-status");
  if (!status) return;
  delete status.dataset.state;
}

function videoStreamDebugRootAllowed() {
  try {
    const user = typeof currentUser !== "undefined" ? currentUser : window.currentUser;
    if (user === "root") return true;
    if (typeof user === "string") return user.toLowerCase() === "root";
    return !!(user && (user.username === "root" || user.role === "root" || user.is_root === true));
  } catch (_) {
    return false;
  }
}

function videoStreamDebugStoredEnabled() {
  if (!videoStreamDebugRootAllowed()) return false;
  try {
    return localStorage.getItem(VIDEO_STREAM_DEBUG_STORAGE_KEY) === "1";
  } catch (_) {
    return false;
  }
}

function setVideoStreamDebugStoredEnabled(enabled) {
  try {
    if (enabled) localStorage.setItem(VIDEO_STREAM_DEBUG_STORAGE_KEY, "1");
    else localStorage.removeItem(VIDEO_STREAM_DEBUG_STORAGE_KEY);
  } catch (_) {}
}

function videoPlayerBufferedRanges(player) {
  const ranges = [];
  try {
    for (let i = 0; i < player.buffered.length; i += 1) {
      ranges.push(`${player.buffered.start(i).toFixed(2)}-${player.buffered.end(i).toFixed(2)}`);
    }
  } catch (_) {}
  return ranges.join(", ");
}

function videoPlayerBufferHealthSeconds(player) {
  if (!player) return null;
  const current = Number(player.currentTime || 0);
  try {
    for (let i = 0; i < player.buffered.length; i += 1) {
      const start = player.buffered.start(i);
      const end = player.buffered.end(i);
      if (current >= start && current <= end) return Math.max(0, end - current);
    }
    if (player.buffered.length > 0) return Math.max(0, player.buffered.end(0) - current);
  } catch (_) {}
  return null;
}

function videoPlayerEdgeLatencySeconds(player) {
  if (!player) return null;
  try {
    if (!player.seekable || player.seekable.length <= 0) return null;
    const edge = player.seekable.end(player.seekable.length - 1);
    const duration = Number(player.duration || 0);
    if (Number.isFinite(duration) && duration > 0 && edge >= duration - 1) return null;
    const latency = edge - Number(player.currentTime || 0);
    if (!Number.isFinite(latency) || latency < 0) return null;
    return latency;
  } catch (_) {
    return null;
  }
}

function videoFormatDebugNumber(value, digits = 2, suffix = "") {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  return `${n.toFixed(digits)}${suffix}`;
}

function videoFormatDebugMegabitsPerSecond(bitsPerSecond) {
  const value = Number(bitsPerSecond || 0);
  if (!Number.isFinite(value) || value <= 0) return "-";
  return `${(value / 1000 / 1000).toFixed(2)} Mbit/s`;
}

function videoStreamDebugResetMetrics(sessionId) {
  videoState.streamDebugMetrics = {
    session_id: sessionId,
    started_at_ms: performance.now(),
    bytes_received: 0,
    bytes_appended: 0,
    chunks_received: 0,
    chunks_appended: 0,
    hls_fragments_loaded: 0,
    hls_fragment_bytes: 0,
    hls_fragment_latencies_ms: [],
    hls_fragment_load_ms: [],
    first_chunk_ms: 0,
    last_chunk_at_ms: 0,
    chunk_gaps_ms: [],
    media_events: {},
    first_playing_ms: 0,
    waiting_count: 0,
    stalled_count: 0,
    last_frame_sample_at_ms: 0,
    last_total_frames: 0,
    frame_rate_fps: 0,
  };
}

function videoStreamDebugRecordMediaEvent(name) {
  const metrics = videoState.streamDebugMetrics || {};
  metrics.media_events = metrics.media_events || {};
  metrics.media_events[name] = Number(metrics.media_events[name] || 0) + 1;
  if (name === "waiting") metrics.waiting_count = Number(metrics.waiting_count || 0) + 1;
  if (name === "stalled") metrics.stalled_count = Number(metrics.stalled_count || 0) + 1;
  if (name === "playing" && !metrics.first_playing_ms) {
    metrics.first_playing_ms = Math.round(performance.now() - Number(metrics.started_at_ms || performance.now()));
  }
  videoState.streamDebugMetrics = metrics;
}

function videoStreamDebugRecordChunk({ totalBytes = 0, appendedBytes = 0, totalChunks = 0, appendedChunks = 0, firstChunkMs = 0 } = {}) {
  const metrics = videoState.streamDebugMetrics || {};
  const now = performance.now();
  if (metrics.last_chunk_at_ms) {
    const gap = Math.max(0, now - metrics.last_chunk_at_ms);
    metrics.chunk_gaps_ms = [...(metrics.chunk_gaps_ms || []), gap].slice(-40);
  }
  metrics.last_chunk_at_ms = now;
  metrics.bytes_received = Number(totalBytes || metrics.bytes_received || 0);
  metrics.bytes_appended = Number(appendedBytes || metrics.bytes_appended || 0);
  metrics.chunks_received = Number(totalChunks || metrics.chunks_received || 0);
  metrics.chunks_appended = Number(appendedChunks || metrics.chunks_appended || 0);
  metrics.first_chunk_ms = Number(firstChunkMs || metrics.first_chunk_ms || 0);
  videoState.streamDebugMetrics = metrics;
}

function videoStreamDebugRecordHlsFragment(data = {}) {
  const metrics = videoState.streamDebugMetrics || {};
  const stats = data?.stats || data?.frag?.stats || {};
  const loaded = Number(stats.loaded || stats.total || data?.loaded || 0);
  const trequest = Number(stats.trequest || stats.loading?.start || 0);
  const tfirst = Number(stats.tfirst || stats.loading?.first || 0);
  const tload = Number(stats.tload || stats.loading?.end || 0);
  metrics.hls_fragments_loaded = Number(metrics.hls_fragments_loaded || 0) + 1;
  metrics.hls_fragment_bytes = Number(metrics.hls_fragment_bytes || 0) + Math.max(0, loaded);
  metrics.bytes_received = Math.max(Number(metrics.bytes_received || 0), Number(metrics.hls_fragment_bytes || 0));
  if (trequest && tfirst && tfirst >= trequest) {
    metrics.hls_fragment_latencies_ms = [...(metrics.hls_fragment_latencies_ms || []), tfirst - trequest].slice(-40);
    if (!metrics.first_chunk_ms) metrics.first_chunk_ms = Math.round(tfirst - trequest);
  }
  if (trequest && tload && tload >= trequest) {
    metrics.hls_fragment_load_ms = [...(metrics.hls_fragment_load_ms || []), tload - trequest].slice(-40);
  }
  videoState.streamDebugMetrics = metrics;
}

function videoStddev(values = []) {
  const nums = values.map(Number).filter(Number.isFinite);
  if (nums.length < 2) return 0;
  const mean = nums.reduce((sum, n) => sum + n, 0) / nums.length;
  const variance = nums.reduce((sum, n) => sum + ((n - mean) ** 2), 0) / nums.length;
  return Math.sqrt(variance);
}

function videoPlayerQualityStats(player) {
  if (!player) return {};
  let total = 0;
  let dropped = 0;
  let corrupted = 0;
  try {
    const quality = typeof player.getVideoPlaybackQuality === "function" ? player.getVideoPlaybackQuality() : null;
    total = Number(quality?.totalVideoFrames || player.webkitDecodedFrameCount || 0);
    dropped = Number(quality?.droppedVideoFrames || player.webkitDroppedFrameCount || 0);
    corrupted = Number(quality?.corruptedVideoFrames || 0);
  } catch (_) {
    total = Number(player.webkitDecodedFrameCount || 0);
    dropped = Number(player.webkitDroppedFrameCount || 0);
  }
  const metrics = videoState.streamDebugMetrics || {};
  const now = performance.now();
  if (total > 0 && metrics.last_frame_sample_at_ms && now > metrics.last_frame_sample_at_ms) {
    const deltaFrames = Math.max(0, total - Number(metrics.last_total_frames || 0));
    const deltaSeconds = (now - metrics.last_frame_sample_at_ms) / 1000;
    if (deltaSeconds > 0) metrics.frame_rate_fps = deltaFrames / deltaSeconds;
  }
  metrics.last_frame_sample_at_ms = now;
  metrics.last_total_frames = total;
  videoState.streamDebugMetrics = metrics;
  return {
    total_frames: total,
    dropped_frames: dropped,
    corrupted_frames: corrupted,
    dropped_frame_percent: total > 0 ? (dropped / total) * 100 : 0,
    frame_rate_fps: Number(metrics.frame_rate_fps || 0),
  };
}

function videoHlsDebugStats() {
  const hls = videoState.currentHls;
  if (!hls) return {};
  const levelIndex = Number.isFinite(Number(hls.currentLevel)) ? Number(hls.currentLevel) : -1;
  const level = Array.isArray(hls.levels) && levelIndex >= 0 ? hls.levels[levelIndex] : null;
  return {
    hls_bandwidth_estimate_bitsPerSecond: Number(hls.bandwidthEstimate || 0),
    hls_latency_sec: Number.isFinite(Number(hls.latency)) ? Number(hls.latency) : null,
    hls_live_sync_position: Number.isFinite(Number(hls.liveSyncPosition)) ? Number(hls.liveSyncPosition) : null,
    hls_current_level: levelIndex,
    hls_load_level: Number.isFinite(Number(hls.loadLevel)) ? Number(hls.loadLevel) : null,
    hls_next_level: Number.isFinite(Number(hls.nextLevel)) ? Number(hls.nextLevel) : null,
    hls_level_bitrate_bitsPerSecond: Number(level?.bitrate || 0),
    hls_level_resolution: level ? `${Number(level.width || 0)}x${Number(level.height || 0)}` : "",
    hls_level_codec: level ? [level.videoCodec, level.audioCodec].filter(Boolean).join(" / ") : "",
  };
}

function videoDebugUrlPath(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  try {
    return new URL(raw, window.location.href).pathname;
  } catch (_) {
    return raw.split("?", 1)[0];
  }
}

function videoDebugResourcePrefixes(snapshot = {}) {
  const paths = new Set();
  [
    snapshot.src,
    snapshot.direct_src,
    snapshot.realtime_src,
    snapshot.player?.current_src,
  ].forEach((value) => {
    const path = videoDebugUrlPath(value);
    if (path) paths.add(path);
    const hlsIndex = path.indexOf("/hls/");
    if (hlsIndex >= 0) paths.add(path.slice(0, hlsIndex + 5));
  });
  const id = snapshot.video_id;
  if (id) {
    paths.add(`/api/videos/${id}/stream`);
    paths.add(`/api/videos/${id}/realtime-proxy`);
    paths.add(`/api/videos/${id}/hls/`);
  }
  return Array.from(paths).filter(Boolean);
}

function videoResourceTimingStats(snapshot = {}) {
  const metrics = videoState.streamDebugMetrics || {};
  const started = Number(metrics.started_at_ms || 0);
  const prefixes = videoDebugResourcePrefixes(snapshot);
  if (!prefixes.length || !performance?.getEntriesByType) return {};
  const entries = performance.getEntriesByType("resource").filter((entry) => {
    if (started && Number(entry.startTime || 0) < started - 1000) return false;
    const path = videoDebugUrlPath(entry.name);
    return prefixes.some((prefix) => path === prefix || path.startsWith(prefix));
  });
  if (!entries.length) return {};
  const sorted = entries.slice().sort((a, b) => Number(a.startTime || 0) - Number(b.startTime || 0));
  const firstStart = Math.min(...sorted.map((entry) => Number(entry.startTime || 0)));
  const lastEnd = Math.max(...sorted.map((entry) => Number(entry.responseEnd || entry.duration || 0)));
  const totalBytes = sorted.reduce((sum, entry) => {
    const bytes = Number(entry.transferSize || entry.encodedBodySize || entry.decodedBodySize || 0);
    return sum + Math.max(0, bytes);
  }, 0);
  const latencies = sorted
    .map((entry) => Number(entry.responseStart || 0) - Number(entry.startTime || 0))
    .filter((value) => Number.isFinite(value) && value >= 0);
  const endGaps = [];
  for (let i = 1; i < sorted.length; i += 1) {
    const previous = Number(sorted[i - 1].responseEnd || sorted[i - 1].startTime || 0);
    const current = Number(sorted[i].startTime || 0);
    if (current >= previous) endGaps.push(current - previous);
  }
  const spanMs = Math.max(0, lastEnd - firstStart);
  return {
    resource_request_count: sorted.length,
    resource_total_bytes: totalBytes,
    resource_span_ms: spanMs,
    resource_throughput_bitsPerSecond: spanMs > 0 && totalBytes > 0 ? (totalBytes * 8 * 1000) / spanMs : 0,
    resource_first_byte_ms: latencies.length ? latencies[0] : null,
    resource_avg_latency_ms: latencies.length ? latencies.reduce((sum, n) => sum + n, 0) / latencies.length : null,
    resource_jitter_ms: videoStddev(endGaps),
  };
}

function videoStreamDebugObservedStats(player, snapshot = {}) {
  const metrics = videoState.streamDebugMetrics || {};
  const elapsedMs = Math.max(0, performance.now() - Number(metrics.started_at_ms || performance.now()));
  const resource = videoResourceTimingStats(snapshot);
  const realtimeBytes = Number(snapshot.realtime_bytes_received || 0);
  const bytes = Number(realtimeBytes || metrics.bytes_received || resource.resource_total_bytes || 0);
  const throughputBitsPerSecond = elapsedMs > 0 && bytes > 0 ? (bytes * 8 * 1000) / elapsedMs : 0;
  const chunkGaps = metrics.chunk_gaps_ms || [];
  const hlsLatencies = metrics.hls_fragment_latencies_ms || [];
  const hlsLoadTimes = metrics.hls_fragment_load_ms || [];
  const quality = videoPlayerQualityStats(player);
  const hls = videoHlsDebugStats();
  const bufferHealth = videoPlayerBufferHealthSeconds(player);
  const edgeLatency = videoPlayerEdgeLatencySeconds(player);
  return {
    observed_download_rate_bitsPerSecond: realtimeBytes
      ? throughputBitsPerSecond
      : (Number(hls.hls_bandwidth_estimate_bitsPerSecond || 0) || Number(resource.resource_throughput_bitsPerSecond || 0) || throughputBitsPerSecond),
    hls_bandwidth_estimate_bitsPerSecond: hls.hls_bandwidth_estimate_bitsPerSecond || 0,
    buffer_health_sec: bufferHealth,
    edge_latency_sec: edgeLatency,
    startup_latency_ms: Number(metrics.first_playing_ms || 0) || null,
    first_chunk_ms: Number(snapshot.realtime_first_chunk_ms || metrics.first_chunk_ms || resource.resource_first_byte_ms || 0) || null,
    avg_request_latency_ms: resource.resource_avg_latency_ms ?? (hlsLatencies.length ? hlsLatencies.reduce((sum, n) => sum + n, 0) / hlsLatencies.length : null),
    chunk_jitter_ms: videoStddev(chunkGaps.length ? chunkGaps : (hlsLoadTimes.length ? hlsLoadTimes : [])) || Number(resource.resource_jitter_ms || 0),
    chunk_gap_avg_ms: chunkGaps.length ? chunkGaps.reduce((sum, n) => sum + n, 0) / chunkGaps.length : 0,
    chunks_received: Number(snapshot.realtime_chunks_received || metrics.chunks_received || metrics.hls_fragments_loaded || resource.resource_request_count || 0),
    bytes_received: Number(snapshot.realtime_bytes_received || metrics.bytes_received || resource.resource_total_bytes || 0),
    resource_request_count: Number(resource.resource_request_count || 0),
    resource_total_bytes: Number(resource.resource_total_bytes || 0),
    waiting_count: Number(metrics.waiting_count || 0),
    stalled_count: Number(metrics.stalled_count || 0),
    ...quality,
    ...hls,
    ...resource,
  };
}

function videoStreamDebugBurdenMetric(group = {}, key = "cpu_percent") {
  const value = Number(group?.[key]);
  return Number.isFinite(value) ? value : null;
}

function videoStreamDebugGroupLabel(group = {}) {
  const count = Number(group.process_count || 0);
  const cpu = videoStreamDebugBurdenMetric(group, "cpu_percent");
  const rss = Number(group.rss_bytes || 0);
  const parts = [`${count} proc`];
  if (cpu != null) parts.push(videoFormatDebugNumber(cpu, 1, "%"));
  if (rss > 0) parts.push(videoFormatBytes(rss));
  return parts.join(" / ");
}

function videoStreamDebugGroupCpuLabel(group = {}) {
  const count = Number(group.process_count || 0);
  if (count <= 0) return "0 proc";
  const cpu = videoStreamDebugBurdenMetric(group, "cpu_percent");
  return cpu == null ? "計算中" : videoFormatDebugNumber(cpu, 1, "%");
}

function videoStreamDebugGroupRssLabel(group = {}) {
  const count = Number(group.process_count || 0);
  if (count <= 0) return "0 proc";
  const rss = Number(group.rss_bytes || 0);
  return rss > 0 ? videoFormatBytes(rss) : "0 B";
}

function videoStreamDebugRequestBurden(snapshot = {}) {
  if (!videoStreamDebugRootAllowed() || !videoStreamDebugStoredEnabled()) return;
  const videoId = Number(snapshot.video_id || videoState.current?.id || 0);
  if (!videoId || videoState.streamDebugBurdenInflight) return;
  const now = performance.now();
  if (now - Number(videoState.streamDebugBurdenFetchAt || 0) < 1500) return;
  videoState.streamDebugBurdenFetchAt = now;
  videoState.streamDebugBurdenInflight = true;
  apiFetch(API + `/videos/${encodeURIComponent(videoId)}/stream-burden`, { credentials: "same-origin" })
    .then((res) => res.json().catch(() => ({})))
    .then((json) => {
      if (!json || !json.ok) return;
      const previous = videoState.streamDebugBurdenLast || null;
      const sampledAtMs = Date.parse(json.sampled_at || "") || Date.now();
      const elapsed = previous ? Math.max(0.001, (sampledAtMs - Number(previous.sampled_at_ms || 0)) / 1000) : 0;
      const groups = { ...(json.groups || {}) };
      if (previous && elapsed > 0) {
        Object.keys(groups).forEach((name) => {
          const current = groups[name] || {};
          const before = previous.groups?.[name] || {};
          const deltaCpu = Number(current.cpu_time_seconds || 0) - Number(before.cpu_time_seconds || 0);
          current.cpu_percent = Math.max(0, (deltaCpu / elapsed) * 100);
          groups[name] = current;
        });
      }
      const next = {
        sampled_at: json.sampled_at || new Date(sampledAtMs).toISOString(),
        sampled_at_ms: sampledAtMs,
        mode: snapshot.playback_source_mode || snapshot.selected_service_mode || "",
        groups,
        system: json.system || {},
        realtime_proxy_runtime: json.realtime_proxy_runtime || {},
        video_source_present: json.video_source_present,
      };
      videoState.streamDebugBurden = next;
      videoState.streamDebugBurdenLast = next;
      if (videoStreamDebugStoredEnabled()) {
        window.requestAnimationFrame(() => renderVideoStreamDebugPanel());
      }
    })
    .catch(() => {})
    .finally(() => {
      videoState.streamDebugBurdenInflight = false;
    });
}

function videoHlsDebugFallbackStats(stats = {}, snapshot = {}) {
  const metrics = videoState.streamDebugMetrics || {};
  const entries = typeof performance?.getEntriesByType === "function"
    ? performance.getEntriesByType("resource")
    : [];
  const videoId = String(snapshot?.video_id || "");
  const hlsEntries = entries.filter((entry) => {
    const path = videoDebugUrlPath(entry?.name || "");
    if (!path.includes("/hls/")) return false;
    if (!videoId) return true;
    return path.includes(`/api/videos/${encodeURIComponent(videoId)}/hls/`)
      || path.includes(`/api/videos/${videoId}/hls/`);
  });
  const playlistEntries = hlsEntries.filter((entry) => {
    const path = videoDebugUrlPath(entry?.name || "");
    return path.endsWith(".m3u8");
  });
  const segmentEntries = hlsEntries.filter((entry) => {
    const path = videoDebugUrlPath(entry?.name || "");
    return !path.endsWith(".m3u8") && !path.endsWith(".vtt");
  });
  const segmentBytes = segmentEntries.reduce((sum, entry) => (
    sum + Number(entry.transferSize || entry.encodedBodySize || entry.decodedBodySize || 0)
  ), 0);
  const segmentDurations = segmentEntries
    .map((entry) => Number(entry.duration || 0))
    .filter((value) => Number.isFinite(value) && value > 0);
  const avgSegmentMs = segmentDurations.length
    ? segmentDurations.reduce((sum, value) => sum + value, 0) / segmentDurations.length
    : null;
  const jitterMs = segmentDurations.length > 1
    ? Math.sqrt(segmentDurations.reduce((sum, value) => sum + Math.pow(value - avgSegmentMs, 2), 0) / segmentDurations.length)
    : null;
  const firstStart = segmentEntries.reduce((min, entry) => Math.min(min, Number(entry.startTime || Infinity)), Infinity);
  const lastEnd = segmentEntries.reduce((max, entry) => Math.max(max, Number(entry.responseEnd || (entry.startTime || 0) + (entry.duration || 0))), 0);
  const elapsedSec = Number.isFinite(firstStart) && lastEnd > firstStart ? (lastEnd - firstStart) / 1000 : 0;
  const hlsEventLoads = Array.isArray(metrics.hls_fragment_load_ms) ? metrics.hls_fragment_load_ms : [];
  const hlsEventLatencies = Array.isArray(metrics.hls_fragment_latencies_ms) ? metrics.hls_fragment_latencies_ms : [];
  const avgEventLoadMs = hlsEventLoads.length
    ? hlsEventLoads.reduce((sum, value) => sum + Number(value || 0), 0) / hlsEventLoads.length
    : null;
  const avgEventLatencyMs = hlsEventLatencies.length
    ? hlsEventLatencies.reduce((sum, value) => sum + Number(value || 0), 0) / hlsEventLatencies.length
    : null;
  const eventFragments = Number(metrics.hls_fragments_loaded || stats.hls_fragments_loaded || 0);
  const eventBytes = Number(metrics.hls_fragment_bytes || stats.hls_fragment_bytes || 0);
  const eventBandwidth = Number(metrics.hls_bandwidth_estimate_bitsPerSecond || stats.hls_bandwidth_estimate_bitsPerSecond || 0);
  return {
    ...stats,
    hls_debug_source: eventFragments ? "hls.js" : (segmentEntries.length ? "resource_timing" : "none"),
    hls_playlist_requests: Number(stats.hls_playlist_requests || playlistEntries.length || 0),
    hls_segment_requests: Number(stats.hls_segment_requests || segmentEntries.length || 0),
    hls_fragments_loaded: Number(eventFragments || stats.hls_fragments_loaded || segmentEntries.length || 0),
    hls_fragment_bytes: Number(eventBytes || stats.hls_fragment_bytes || segmentBytes || 0),
    hls_segment_bytes: Number(stats.hls_segment_bytes || segmentBytes || 0),
    hls_avg_fragment_load_ms: stats.hls_avg_fragment_load_ms ?? avgEventLoadMs ?? avgSegmentMs,
    hls_avg_fragment_latency_ms: stats.hls_avg_fragment_latency_ms ?? avgEventLatencyMs ?? avgSegmentMs,
    hls_segment_jitter_ms: stats.hls_segment_jitter_ms ?? jitterMs,
    hls_observed_bandwidth_bitsPerSecond: Number(stats.hls_observed_bandwidth_bitsPerSecond || eventBandwidth || (elapsedSec > 0 && segmentBytes > 0 ? (segmentBytes * 8) / elapsedSec : 0)),
  };
}

function renderVideoStreamDebugSummary(stats = {}, snapshot = {}) {
  const summary = $("video-stream-debug-summary");
  if (!summary) return;
  const modeText = String(snapshot.playback_source_mode || snapshot.selected_service_mode || "").trim();
  const selectedText = String(snapshot.selected_service_mode || "").trim();
  const isHlsMode = modeText.includes("hls") || selectedText === "prepared_hls";
  const isRealtimeMode = modeText.includes("realtime_proxy") || selectedText === "realtime_proxy";
  const isDirectMode = !isHlsMode && !isRealtimeMode && (modeText.includes("direct") || selectedText === "direct");
  const burden = snapshot.server_burden || {};
  const burdenGroups = burden.groups || {};
  const runtime = burden.realtime_proxy_runtime || {};
  const firstValue = (...values) => values.find((value) => value !== undefined && value !== null && value !== "");
  const startupMs = firstValue(stats.startup_latency_ms, stats.startup_ms, snapshot.startup_latency_ms, snapshot.startup_ms);
  const bufferHealthSec = firstValue(stats.buffer_health_sec, stats.realtime_buffer_ahead_seconds, snapshot.realtime_buffer_ahead_seconds);
  const waitingCount = Number(firstValue(stats.waiting_count, stats.wait_count, snapshot.player?.waiting_count, 0) || 0);
  const stalledCount = Number(firstValue(stats.stalled_count, stats.stall_count, snapshot.player?.stalled_count, 0) || 0);
  const droppedFrames = Number(firstValue(stats.dropped_frames, snapshot.player?.dropped_frames, 0) || 0);
  const totalFrames = Number(firstValue(stats.total_frames, stats.decoded_frames, snapshot.player?.total_frames, snapshot.player?.decoded_frames, 0) || 0);
  const droppedPercent = totalFrames ? (droppedFrames / Math.max(1, totalFrames)) * 100 : Number(stats.dropped_frame_percent || 0);
  const loadavg = Array.isArray(burden.system?.loadavg)
    ? burden.system.loadavg
    : (Array.isArray(burden.loadavg) ? burden.loadavg : (Array.isArray(snapshot.loadavg) ? snapshot.loadavg : []));
  const bufferedRanges = firstValue(snapshot.mse_source_buffered_ranges, stats.mse_source_buffered_ranges);
  const rangeLabel = (ranges) => Array.isArray(ranges) && ranges.length
    ? ranges.slice(0, 3).map((range) => `${videoFormatDebugNumber(range?.[0] || 0, 1)}-${videoFormatDebugNumber(range?.[1] || 0, 1)}s`).join(" / ")
    : "-";
  const rows = [
    ["模式", snapshot.playback_source_mode || snapshot.selected_service_mode || "-"],
    ["Server CPU", videoStreamDebugGroupCpuLabel(burdenGroups.server || {})],
    ["Server RSS", videoStreamDebugGroupRssLabel(burdenGroups.server || {})],
    ["Loadavg", loadavg.length ? loadavg.map((n) => videoFormatDebugNumber(n, 2)).join(" / ") : "-"],
    ["緩衝長度", bufferHealthSec == null ? "-" : videoFormatDebugNumber(bufferHealthSec, 2, " s")],
    ["啟播延遲", startupMs == null ? "-" : videoFormatDebugNumber(startupMs, 0, " ms")],
    ["解析度", snapshot.player?.video_size || stats.hls_level_resolution || "-"],
    ["FPS", stats.frame_rate_fps ? videoFormatDebugNumber(stats.frame_rate_fps, 1) : "-"],
    ["掉幀", totalFrames ? `${droppedFrames}/${totalFrames} (${videoFormatDebugNumber(droppedPercent, 2, "%")})` : "-"],
    ["等待/停滯", `${waitingCount}/${stalledCount}`],
  ];
  if (snapshot.player?.error_code) {
    rows.splice(1, 0, ["播放錯誤", `${snapshot.player.error_code}: ${snapshot.player.error_message || "media error"}`]);
  }
  if (isDirectMode) {
    rows.splice(1, 0,
      ["直接串流速率", videoFormatDebugMegabitsPerSecond(stats.observed_download_rate_bitsPerSecond)],
      ["直接請求延遲", stats.avg_request_latency_ms == null ? "-" : videoFormatDebugNumber(stats.avg_request_latency_ms, 0, " ms")],
      ["直接請求數", String(stats.resource_request_count || 0)],
      ["直接 bytes", stats.bytes_received ? videoFormatBytes(stats.bytes_received) : "-"],
    );
  }
  if (isRealtimeMode) {
    rows.splice(1, 0,
      ["ffmpeg CPU", videoStreamDebugGroupCpuLabel(burdenGroups.ffmpeg_all || burdenGroups.ffmpeg || {})],
      ["ffmpeg RSS", videoStreamDebugGroupRssLabel(burdenGroups.ffmpeg_all || burdenGroups.ffmpeg || {})],
      ["本片 ffmpeg CPU", videoStreamDebugGroupCpuLabel(burdenGroups.video_ffmpeg || {})],
      ["本片 ffmpeg RSS", videoStreamDebugGroupRssLabel(burdenGroups.video_ffmpeg || {})],
      ["即時槽位", runtime.limit ? `${runtime.active || 0}/${runtime.limit} (${runtime.scope || "-"})` : "-"],
      ["即時狀態", snapshot.realtime_state || stats.realtime_state || "-"],
      ["即時 HTTP", snapshot.realtime_http_status || stats.realtime_http_status || "-"],
      ["即時 Source API", snapshot.selected_source_api || snapshot.source_api || "-"],
      ["即時 codec 支援", snapshot.is_type_supported_result == null ? "-" : String(snapshot.is_type_supported_result)],
      ["即時串流速率", videoFormatDebugMegabitsPerSecond(stats.observed_download_rate_bitsPerSecond)],
      ["即時首包延遲", firstValue(stats.first_chunk_ms, stats.realtime_first_chunk_ms) == null ? "-" : videoFormatDebugNumber(firstValue(stats.first_chunk_ms, stats.realtime_first_chunk_ms), 0, " ms")],
      ["即時 chunk 抖動", firstValue(stats.chunk_jitter_ms, stats.realtime_chunk_jitter_ms) ? videoFormatDebugNumber(firstValue(stats.chunk_jitter_ms, stats.realtime_chunk_jitter_ms), 0, " ms") : "-"],
      ["即時 chunks", String(firstValue(stats.chunks_received, stats.realtime_chunks_received, 0))],
      ["即時 bytes", (stats.realtime_bytes_received || stats.bytes_received) ? videoFormatBytes(stats.realtime_bytes_received || stats.bytes_received) : "-"],
      ["MSE 狀態", snapshot.mse_ready_state || stats.mse_ready_state || "-"],
      ["MSE buffer", rangeLabel(bufferedRanges)],
    );
  }
  if (isHlsMode) {
    rows.splice(1, 0,
      ["HLS 估計頻寬", videoFormatDebugMegabitsPerSecond(stats.hls_bandwidth_estimate_bitsPerSecond)],
      ["HLS 實測頻寬", videoFormatDebugMegabitsPerSecond(stats.hls_observed_bandwidth_bitsPerSecond)],
      ["HLS 數據來源", stats.hls_debug_source || "-"],
      ["HLS 清單/片段", `${stats.hls_playlist_requests || 0}/${stats.hls_segment_requests || stats.hls_fragments_loaded || 0}`],
      ["HLS 片段載入", stats.hls_avg_fragment_load_ms == null ? "-" : videoFormatDebugNumber(stats.hls_avg_fragment_load_ms, 0, " ms")],
      ["HLS 片段抖動", stats.hls_segment_jitter_ms == null ? "-" : videoFormatDebugNumber(stats.hls_segment_jitter_ms, 0, " ms")],
      ["HLS 片段 bytes", stats.hls_fragment_bytes ? videoFormatBytes(stats.hls_fragment_bytes) : "-"],
    );
  }
  const burdenRowLabels = new Set([
    "Server CPU",
    "Server RSS",
    "ffmpeg CPU",
    "ffmpeg RSS",
    "本片 ffmpeg CPU",
    "本片 ffmpeg RSS",
    "Loadavg",
  ]);
  const burdenTextByLabel = {};
  const compactRows = [];
  rows.forEach(([label, value]) => {
    const text = String(value ?? "").trim();
    if (burdenRowLabels.has(label)) {
      if (text && text !== "-" && text !== "0 proc") burdenTextByLabel[label] = text;
      return;
    }
    compactRows.push([label, value]);
  });
  const burdenCpuWidth = (text) => {
    const match = String(text || "").match(/([0-9]+(?:\.[0-9]+)?)\s*%/);
    if (!match) return 0;
    return Math.max(2, Math.min(100, Number(match[1] || 0)));
  };
  const burdenCpuTone = (width) => {
    if (width >= 80) return "#ef4444";
    if (width >= 50) return "#f59e0b";
    return "#22c55e";
  };
  const burdenMetricLine = (label, text, type = "text") => {
    if (!text) return "";
    const width = type === "cpu" ? burdenCpuWidth(text) : 0;
    return `
      <div style="display:grid;grid-template-columns:3.5rem minmax(6rem,1fr);gap:.45rem;align-items:center;margin-top:.32rem;">
        <span style="font-size:.72rem;font-weight:800;letter-spacing:.06em;color:var(--muted);text-transform:uppercase;">${sanitize(label)}</span>
        <span style="display:flex;align-items:center;gap:.45rem;min-width:0;">
          ${type === "cpu" ? `<span aria-hidden="true" style="width:4.8rem;height:.42rem;border-radius:999px;background:rgba(148,163,184,.2);overflow:hidden;box-shadow:inset 0 0 0 1px rgba(148,163,184,.16);"><span style="display:block;width:${width}%;height:100%;border-radius:inherit;background:${burdenCpuTone(width)};"></span></span>` : ""}
          <span style="font-size:.78rem;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${sanitize(text)}</span>
        </span>
      </div>
    `;
  };
  const burdenCard = (title, cpuText, rssText) => {
    if (!cpuText && !rssText) return "";
    return `
      <div style="min-width:13rem;padding:.55rem .65rem;border-radius:.8rem;background:rgba(15,23,42,.05);box-shadow:inset 0 0 0 1px rgba(148,163,184,.18);">
        <div style="font-size:.8rem;font-weight:900;color:var(--text);">${sanitize(title)}</div>
        ${burdenMetricLine("CPU", cpuText, "cpu")}
        ${burdenMetricLine("RSS", rssText, "rss")}
      </div>
    `;
  };
  const burdenLoadCard = (text) => text ? `
    <div style="min-width:10rem;padding:.55rem .65rem;border-radius:.8rem;background:rgba(15,23,42,.05);box-shadow:inset 0 0 0 1px rgba(148,163,184,.18);">
      <div style="font-size:.8rem;font-weight:900;color:var(--text);">系統負載</div>
      ${burdenMetricLine("AVG", text, "rss")}
    </div>
  ` : "";
  const currentFfmpegCard = burdenCard("目前串流處理", burdenTextByLabel["本片 ffmpeg CPU"], burdenTextByLabel["本片 ffmpeg RSS"]);
  const fallbackFfmpegCard = currentFfmpegCard
    ? ""
    : burdenCard("背景轉檔處理", burdenTextByLabel["ffmpeg CPU"], burdenTextByLabel["ffmpeg RSS"]);
  const burdenHtml = [
    burdenCard("網站服務", burdenTextByLabel["Server CPU"], burdenTextByLabel["Server RSS"]),
    currentFfmpegCard || fallbackFfmpegCard,
    burdenLoadCard(burdenTextByLabel["Loadavg"]),
  ].filter(Boolean).join("");
  const streamSettingLabels = new Set([
    "模式",
    "解析度",
    "畫質",
    "音軌",
    "MediaSource available",
    "WebKitMediaSource available",
    "ManagedMediaSource available",
    "selected source API",
    "即時 Source API",
    "mime codec string",
    "即時 codec",
    "isTypeSupported result",
    "即時 codec 支援",
    "disableRemotePlayback",
  ]);
  const rowTextValue = (value) => {
    if (value && typeof value === "object" && value.html) return String(value.html || "").trim();
    return String(value ?? "").trim();
  };
  const isVisibleDebugRow = ([label, value]) => {
    if (value && typeof value === "object" && value.html) return true;
    const text = String(value ?? "").trim();
    return text && text !== "-" && !/^\d+\s+proc\s+\/\s+0(?:\.0)?%\s+\/\s+-$/i.test(text);
  };
  const isStreamSettingLabel = (label) => {
    const raw = String(label || "");
    const lower = raw.toLowerCase();
    return streamSettingLabels.has(raw)
      || lower.includes("source api")
      || lower.includes("codec")
      || lower.includes("mediasource")
      || lower.includes("istypesupported")
      || lower.includes("mse content");
  };
  const streamSettingRows = [];
  const qualityRows = [];
  compactRows.forEach((row) => {
    if (!isVisibleDebugRow(row)) return;
    if (isStreamSettingLabel(row[0])) streamSettingRows.push(row);
    else qualityRows.push(row);
  });
  const streamSettingHtml = streamSettingRows.map(([label, value]) => `
    <span style="display:inline-flex;align-items:center;gap:.35rem;max-width:100%;padding:.38rem .5rem;border-radius:999px;background:rgba(59,130,246,.08);box-shadow:inset 0 0 0 1px rgba(59,130,246,.18);">
      <b style="font-size:.72rem;color:var(--muted);">${sanitize(label)}</b>
      <span style="font-size:.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${sanitize(rowTextValue(value))}</span>
    </span>
  `).join("");
  const metricRowsHtml = (items) => items.map(([label, value]) => `
    <div class="video-stream-debug-metric">
      <span>${sanitize(label)}</span>
      <strong>${sanitize(rowTextValue(value))}</strong>
    </div>
  `).join("");
  const sectionHtml = (title, body, className = "") => body ? `
    <section class="video-stream-debug-section ${sanitize(className)}">
      <div class="video-stream-debug-section-title">${sanitize(title)}</div>
      ${body}
    </section>
  ` : "";
  const qualityHtml = metricRowsHtml(qualityRows);
  summary.innerHTML = [
    sectionHtml("伺服器負擔", burdenHtml ? `<div class="video-stream-debug-burden-grid">${burdenHtml}</div>` : "", "video-stream-debug-section-burden"),
    sectionHtml("串流設定", streamSettingHtml ? `<div class="video-stream-debug-chip-grid">${streamSettingHtml}</div>` : "", "video-stream-debug-section-settings"),
    sectionHtml("串流品質數據", qualityHtml ? `<div class="video-stream-debug-metric-grid">${qualityHtml}</div>` : "", "video-stream-debug-section-quality"),
  ].filter(Boolean).join("");
}

function ensureVideoStreamDebugPanel(player = $("video-player")) {
  let wrap = $("video-stream-debug-panel");
  if (!videoStreamDebugRootAllowed()) {
    if (wrap) wrap.hidden = true;
    return null;
  }
  if (!wrap) {
    wrap = document.createElement("section");
    wrap.id = "video-stream-debug-panel";
    wrap.className = "video-stream-debug-panel";
    wrap.innerHTML = `
      <label class="video-stream-debug-toggle">
        <input type="checkbox" id="video-stream-debug-toggle" />
        <span>root 串流診斷</span>
      </label>
      <div class="video-stream-debug-body" id="video-stream-debug-body" hidden>
        <div class="video-stream-debug-title">串流數據診斷</div>
        <div class="video-stream-debug-summary" id="video-stream-debug-summary"></div>
        <details class="video-stream-debug-raw">
          <summary>相容性 / 原始資料</summary>
          <pre id="video-stream-debug-output" class="video-stream-debug-output"></pre>
        </details>
      </div>
    `;
    const slot = $("video-stream-debug-slot");
    const status = $("video-playback-status");
    if (slot) {
      slot.appendChild(wrap);
    } else if (status?.parentElement) {
      status.insertAdjacentElement("afterend", wrap);
    } else if (player?.parentElement) {
      player.insertAdjacentElement("afterend", wrap);
    } else {
      document.body.appendChild(wrap);
    }
    $("video-stream-debug-toggle")?.addEventListener("change", (event) => {
      const checked = !!event.target.checked;
      setVideoStreamDebugStoredEnabled(checked);
      const body = $("video-stream-debug-body");
      if (body) body.hidden = !checked;
      renderVideoStreamDebugPanel();
    });
  }
  const slot = $("video-stream-debug-slot");
  if (slot && wrap.parentElement !== slot) {
    slot.appendChild(wrap);
  }
  const status = $("video-playback-status");
  if (!slot && status?.parentElement && wrap.previousElementSibling !== status) {
    status.insertAdjacentElement("afterend", wrap);
  }
  wrap.hidden = false;
  const toggle = $("video-stream-debug-toggle");
  const enabled = videoStreamDebugStoredEnabled();
  if (toggle) toggle.checked = enabled;
  const body = $("video-stream-debug-body");
  if (body) body.hidden = !enabled;
  return wrap;
}

function renderVideoStreamDebugPanel(extra = {}) {
  if (!videoStreamDebugRootAllowed()) return;
  const player = $("video-player");
  ensureVideoStreamDebugPanel(player);
  Object.assign(videoState.streamDebugSnapshot, extra || {});
  const output = $("video-stream-debug-output");
  if (!output || !videoStreamDebugStoredEnabled()) return;
  const err = player?.error;
  const baseSnapshot = {
    updated_at: new Date().toISOString(),
    ...videoState.streamDebugSnapshot,
    player: player ? {
      current_src: player.currentSrc || player.src || "",
      network_state: player.networkState,
      ready_state: player.readyState,
      paused: player.paused,
      current_time: Number(player.currentTime || 0).toFixed(3),
      duration: Number.isFinite(Number(player.duration)) ? Number(player.duration).toFixed(3) : String(player.duration || ""),
      buffered: videoPlayerBufferedRanges(player),
      video_size: player.videoWidth || player.videoHeight ? `${player.videoWidth}x${player.videoHeight}` : "",
      error_code: err?.code || "",
      error_message: err?.message || "",
    } : null,
  };
  videoStreamDebugRequestBurden(baseSnapshot);
  const stats = videoHlsDebugFallbackStats(videoStreamDebugObservedStats(player, baseSnapshot), baseSnapshot);
  const snapshot = { ...baseSnapshot, stats, server_burden: videoState.streamDebugBurden || null };
  renderVideoStreamDebugSummary(stats, snapshot);
  output.textContent = JSON.stringify(snapshot, null, 2);
}

function bindVideoStreamDebugPlayerEvents(player) {
  if (!player || player.dataset.streamDebugBound === "1") return;
  player.dataset.streamDebugBound = "1";
  ["loadstart", "loadedmetadata", "canplay", "playing", "waiting", "stalled", "error", "pause", "ended"].forEach((name) => {
    player.addEventListener(name, () => {
      videoStreamDebugRecordMediaEvent(name);
      renderVideoStreamDebugPanel({ last_media_event: name });
    });
  });
}

function startVideoStreamDebugSession(player, video, playback, playbackSource, sessionId) {
  if (!videoStreamDebugRootAllowed()) return;
  videoStreamDebugResetMetrics(sessionId);
  videoState.streamDebugBurden = null;
  videoState.streamDebugBurdenLast = null;
  videoState.streamDebugBurdenFetchAt = 0;
  videoState.streamDebugBurdenInflight = false;
  bindVideoStreamDebugPlayerEvents(player);
  ensureVideoStreamDebugPanel(player);
  const caps = realtimeProxyMediaSourceCapabilities(playback);
  const MediaSourceCtor = caps.source_api;
  const mseType = caps.mime_codec_string;
  const mseSupported = caps.is_type_supported_result;
  videoState.streamDebugSnapshot = {
    video_id: video?.id || "",
    title: video?.title || "",
    selected_service_mode: videoSelectedServiceMode(video, playback || {}),
    playback_payload_mode: playback?.mode || "",
    playback_source_mode: playbackSource?.mode || "",
    source_mode: playback?.source_mode || "",
    src: playbackSource?.src || playbackSource?.masterUrl || "",
    player_strategy: playback?.player_strategy || "",
    realtime_available: playback?.realtime_proxy?.available,
    realtime_reason: playback?.realtime_proxy?.reason || "",
    realtime_output_container: playback?.realtime_proxy?.output_container || "",
    realtime_mse_content_type: mseType,
    realtime_mse_supported: mseSupported,
    selected_source_api: caps.selected_source_api,
    media_source_api: caps.selected_source_api,
    media_source_available: caps.media_source_available,
    webkit_media_source_available: caps.webkit_media_source_available,
    managed_media_source_available: caps.managed_media_source_available,
    source_api_available: Boolean(MediaSourceCtor),
    mime_codec_string: caps.mime_codec_string,
    is_type_supported_result: caps.is_type_supported_result,
    disable_remote_playback: Boolean(player?.disableRemotePlayback),
    user_agent: navigator.userAgent,
    session_id: sessionId,
  };
  renderVideoStreamDebugPanel();
  window.clearInterval(videoState.streamDebugInterval);
  videoState.streamDebugInterval = window.setInterval(() => {
    if (sessionId !== videoState.playbackSessionId) return;
    renderVideoStreamDebugPanel();
  }, 1000);
}

function loadVideoHlsLibrary() {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (videoState.hlsLibraryPromise) return videoState.hlsLibraryPromise;
  videoState.hlsLibraryPromise = new Promise((resolve, reject) => {
    const existing = document.querySelector('script[data-video-hls-js="1"]');
    if (existing) {
      existing.addEventListener("load", () => resolve(window.Hls || null), { once: true });
      existing.addEventListener("error", () => reject(new Error("HLS.js 載入失敗")), { once: true });
      return;
    }
    const script = document.createElement("script");
    script.src = VIDEO_HLS_JS_URL;
    script.async = true;
    script.defer = true;
    script.dataset.videoHlsJs = "1";
    script.onload = () => resolve(window.Hls || null);
    script.onerror = () => reject(new Error("HLS.js 載入失敗"));
    document.head.appendChild(script);
  }).catch((err) => {
    videoState.hlsLibraryPromise = null;
    throw err;
  });
  return videoState.hlsLibraryPromise;
}

function loadVideoShareFragments() {
  try {
    return JSON.parse(sessionStorage.getItem(VIDEO_SHARE_FRAGMENT_STORAGE_KEY) || "{}") || {};
  } catch (_) {
    return {};
  }
}

function saveVideoShareFragments(data) {
  try {
    sessionStorage.setItem(VIDEO_SHARE_FRAGMENT_STORAGE_KEY, JSON.stringify(data || {}));
  } catch (_) {
    // ignore session storage failure
  }
}

function rememberVideoShareFragment(shareUrl, fragmentKey) {
  const url = String(shareUrl || "").trim();
  const fragment = String(fragmentKey || "").trim();
  if (!url || !fragment) return;
  const state = loadVideoShareFragments();
  state[url] = fragment;
  saveVideoShareFragments(state);
}

function getRememberedVideoShareFragment(shareUrl) {
  const state = loadVideoShareFragments();
  return String(state[String(shareUrl || "").trim()] || "").trim();
}

function forgetRememberedVideoShareFragment(shareUrl) {
  const state = loadVideoShareFragments();
  const key = String(shareUrl || "").trim();
  if (!key || !Object.prototype.hasOwnProperty.call(state, key)) return;
  delete state[key];
  saveVideoShareFragments(state);
}

function videoSelectedDriveFile() {
  const target = String($("video-publish-file")?.value || "");
  return videoPublishDriveFiles.find((file) => String(file?.id || file?.file_id || "") === target) || null;
}

function videoShareBytesToBase64(bytes) {
  const buffer = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || []);
  let binary = "";
  for (let i = 0; i < buffer.length; i += 1) binary += String.fromCharCode(buffer[i]);
  return btoa(binary);
}

function videoShareBytesToBase64Url(bytes) {
  return videoShareBytesToBase64(bytes).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function videoBase64ToBytes(value) {
  const binary = atob(String(value || "").replace(/\s+/g, ""));
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i);
  return out;
}

async function exportRawDriveFileKey(fileKey) {
  const exported = await window.crypto.subtle.exportKey("raw", fileKey);
  return new Uint8Array(exported);
}

async function decryptDriveE2eeBlobWithFileKey(blob, e2ee, fileKey) {
  const plaintext = await window.crypto.subtle.decrypt(
    { name: "AES-GCM", iv: videoBase64ToBytes(e2ee.nonce) },
    fileKey,
    await blob.arrayBuffer()
  );
  const metadata = await decryptDriveJsonMetadata(fileKey, e2ee.encrypted_metadata);
  return {
    blob: new Blob([plaintext], { type: metadata.mime_type || "application/octet-stream" }),
    filename: metadata.filename || "download",
    metadata,
  };
}

async function buildVideoE2eeStreamV2Package(fileKey, decryptedBlob, metadata) {
  const contentType = String(metadata?.mime_type || decryptedBlob?.type || "application/octet-stream").toLowerCase();
  if (!contentType.startsWith("video/") && !contentType.startsWith("audio/")) {
    throw new Error("E2EE Streaming v2 只支援影片或音訊檔。");
  }
  const plaintext = new Uint8Array(await decryptedBlob.arrayBuffer());
  const rawKey = await exportRawDriveFileKey(fileKey);
  const chunks = [];
  const bundleParts = [];
  let ciphertextOffset = 0;
  for (let index = 0, plainOffset = 0; plainOffset < plaintext.byteLength; index += 1, plainOffset += VIDEO_E2EE_STREAM_V2_CHUNK_SIZE) {
    const plainChunk = plaintext.slice(plainOffset, Math.min(plainOffset + VIDEO_E2EE_STREAM_V2_CHUNK_SIZE, plaintext.byteLength));
    const nonce = new Uint8Array(12);
    window.crypto.getRandomValues(nonce);
    const chunkKey = await window.crypto.subtle.importKey("raw", rawKey, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);
    const ciphertext = await window.crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, chunkKey, plainChunk);
    const cipherBytes = new Uint8Array(ciphertext);
    const digest = await window.crypto.subtle.digest("SHA-256", cipherBytes);
    bundleParts.push(cipherBytes);
    chunks.push({
      chunk_index: index,
      nonce: videoShareBytesToBase64(nonce),
      ciphertext_offset: ciphertextOffset,
      ciphertext_size: cipherBytes.byteLength,
      plaintext_offset: plainOffset,
      plaintext_size: plainChunk.byteLength,
      ciphertext_sha256: Array.from(new Uint8Array(digest)).map((byte) => byte.toString(16).padStart(2, "0")).join(""),
    });
    ciphertextOffset += cipherBytes.byteLength;
  }
  return {
    manifest_json: JSON.stringify({
      e2ee_stream_version: 2,
      algorithm: "AES-GCM",
      chunk_size: VIDEO_E2EE_STREAM_V2_CHUNK_SIZE,
      chunk_count: chunks.length,
      content_type: contentType,
      duration_hint: 0,
      byte_range_hint: {
        total_plaintext_bytes: plaintext.byteLength,
      },
      created_at: new Date().toISOString(),
      chunks,
    }),
    bundle_blob: new Blob(bundleParts, { type: "application/octet-stream" }),
  };
}

function videoE2eeDerivativeSupported() {
  return !!(
    window.MediaRecorder
    && document.createElement("canvas").captureStream
    && document.createElement("video").captureStream
  );
}

function videoE2eeRecorderMimeType() {
  const candidates = [
    "video/webm;codecs=vp9,opus",
    "video/webm;codecs=vp8,opus",
    "video/webm",
  ];
  return candidates.find((type) => MediaRecorder.isTypeSupported?.(type)) || "";
}

function videoHexDigest(bytes) {
  return Array.from(new Uint8Array(bytes)).map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function loadVideoMetadataFromBlob(blob) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    const video = document.createElement("video");
    video.preload = "metadata";
    video.muted = true;
    video.playsInline = true;
    const cleanup = () => URL.revokeObjectURL(url);
    video.onloadedmetadata = () => {
      resolve({
        video,
        url,
        width: Number(video.videoWidth || 0),
        height: Number(video.videoHeight || 0),
        duration: Number(video.duration || 0),
        cleanup,
      });
    };
    video.onerror = () => {
      cleanup();
      reject(new Error("瀏覽器無法讀取 E2EE 影音中繼資料，無法產生省流量版本。"));
    };
    video.src = url;
  });
}

async function transcodeVideoBlobToHeight(sourceBlob, targetHeight) {
  const mimeType = videoE2eeRecorderMimeType();
  if (!mimeType) throw new Error("目前瀏覽器不支援本機 E2EE 影音轉檔。");
  const meta = await loadVideoMetadataFromBlob(sourceBlob);
  const sourceWidth = Math.max(1, meta.width || 1);
  const sourceHeight = Math.max(1, meta.height || 1);
  if (!sourceHeight || sourceHeight <= targetHeight) {
    meta.cleanup();
    return null;
  }
  const targetWidth = Math.max(2, Math.round((sourceWidth * targetHeight) / sourceHeight / 2) * 2);
  const canvas = document.createElement("canvas");
  canvas.width = targetWidth;
  canvas.height = targetHeight;
  const ctx = canvas.getContext("2d", { alpha: false });
  if (!ctx) {
    meta.cleanup();
    throw new Error("瀏覽器無法建立本機轉檔畫布。");
  }
  const stream = canvas.captureStream(24);
  const originalStream = typeof meta.video.captureStream === "function" ? meta.video.captureStream() : null;
  (originalStream?.getAudioTracks?.() || []).forEach((track) => stream.addTrack(track));
  const chunks = [];
  const recorder = new MediaRecorder(stream, { mimeType });
  recorder.ondataavailable = (event) => {
    if (event.data && event.data.size > 0) chunks.push(event.data);
  };
  const done = new Promise((resolve, reject) => {
    recorder.onerror = () => reject(new Error("本機 E2EE 省流量版本轉檔失敗。"));
    recorder.onstop = () => resolve();
  });
  const draw = () => {
    if (meta.video.ended || meta.video.paused) return;
    ctx.drawImage(meta.video, 0, 0, targetWidth, targetHeight);
    requestAnimationFrame(draw);
  };
  try {
    meta.video.currentTime = 0;
    recorder.start(1000);
    await meta.video.play();
    draw();
    await new Promise((resolve) => {
      meta.video.onended = resolve;
    });
    if (recorder.state !== "inactive") recorder.stop();
    await done;
  } finally {
    stream.getTracks().forEach((track) => track.stop());
    (originalStream?.getTracks?.() || []).forEach((track) => track.stop());
    meta.cleanup();
  }
  return {
    blob: new Blob(chunks, { type: mimeType.split(";")[0] || "video/webm" }),
    width: targetWidth,
    height: targetHeight,
    duration: meta.duration,
  };
}

function allowedVideoE2eeTargetHeights(metadata = {}) {
  const sourceHeight = Number(metadata?.height || metadata?.video_height || metadata?.natural_height || 0);
  return VIDEO_E2EE_DERIVATIVE_TARGET_HEIGHTS.filter((height) => !sourceHeight || sourceHeight > height);
}

async function buildVideoE2eeDerivativePackages(fileKey, decryptedBlob, metadata, originalCiphertextDigest = "", jobId = "") {
  const contentType = String(metadata?.mime_type || decryptedBlob?.type || "").toLowerCase();
  if (!contentType.startsWith("video/")) return [];
  if (!videoE2eeDerivativeSupported()) {
    videoMsg("此瀏覽器不支援本機產生 E2EE 省流量畫質；仍可使用原始加密串流。", false);
    return [];
  }
  const originalDigest = String(originalCiphertextDigest || "").trim();
  const sourceSize = Number(decryptedBlob.size || 0);
  const packages = [];
  const heights = allowedVideoE2eeTargetHeights(metadata);
  for (const height of heights) {
    setVideoUploadProgress({
      visible: true,
      percent: 0,
      loaded: 0,
      total: sourceSize,
      status: `正在瀏覽器端產生 E2EE ${height}p 省流量版本；請保持此分頁開啟。`,
      indeterminate: true,
    });
    if (jobId) {
      const stageDetail = `瀏覽器端產生 E2EE ${height}p 省流量版本；請保持此分頁開啟。`;
      updateVideoUploadLiveJob(jobId, {
        status: "running",
        progress_percent: Math.max(5, Math.min(75, 10 + packages.length * 18)),
        stage: "local_transcode",
        stage_detail: stageDetail,
      });
      rememberVideoE2eeLocalTask({ job_id: jobId, stage: "local_transcode", stage_detail: stageDetail });
    }
    try {
      const derivative = await transcodeVideoBlobToHeight(decryptedBlob, height);
      if (!derivative || !derivative.blob || derivative.blob.size <= 0) continue;
      if (sourceSize > 0 && derivative.blob.size >= sourceSize) {
        videoMsg(`E2EE ${height}p 產物比原檔大，已依政策跳過並隱藏該畫質。`, false);
        continue;
      }
      const streamV2 = await buildVideoE2eeStreamV2Package(fileKey, derivative.blob, {
        mime_type: derivative.blob.type || "video/webm",
      });
      if (jobId) {
        const stageDetail = `E2EE ${height}p 已本機加密，等待上傳 encrypted derivative。`;
        updateVideoUploadLiveJob(jobId, {
          status: "running",
          progress_percent: Math.max(15, Math.min(82, 20 + packages.length * 18)),
          stage: "local_encrypt",
          stage_detail: stageDetail,
        });
        rememberVideoE2eeLocalTask({ job_id: jobId, stage: "local_encrypt", stage_detail: stageDetail });
      }
      packages.push({
        name: `q${height}`,
        label: `${height}p`,
        width: derivative.width,
        height: derivative.height,
        bitrate: derivative.duration > 0 ? Math.round((derivative.blob.size * 8) / derivative.duration) : 0,
        derived_from_original_sha256: originalDigest,
        stream_v2_manifest_json: streamV2.manifest_json,
        stream_v2_bundle_blob: streamV2.bundle_blob,
      });
    } catch (err) {
      videoMsg(err.message || `E2EE ${height}p 省流量版本產生失敗`, false);
    }
  }
  return packages;
}

async function prepareVideoE2eeShareArtifacts(fileId) {
  if (!window.crypto?.subtle || typeof fetchDriveE2eeKey !== "function" || typeof unwrapDriveFileKey !== "function") {
    throw new Error("目前瀏覽器無法建立 E2EE 分享串流授權。");
  }
  if (!getCsrfToken()) {
    await fetchCsrfToken();
  }
  const localJobId = videoUploadLiveJobId();
  activeVideoE2eeLocalTasks += 1;
  updateVideoUploadLiveJob(localJobId, {
    source_module: "video_e2ee_client",
    source_ref: `video_e2ee_derivatives:${fileId}:${localJobId}`,
    job_type: "video.e2ee_derivatives.client",
    title: "E2EE 本機轉檔 / 加密",
    description: "瀏覽器端產生 strict E2EE 省流量版本；重新整理會中斷，需要重新選擇檔案。",
    status: "running",
    progress_percent: 1,
    stage: "waiting_password",
    stage_detail: "等待 E2EE 原始密碼，只會在瀏覽器端使用。",
    live_status_source: "E2EE local",
    metadata: { file_id: fileId },
  });
  rememberVideoE2eeLocalTask({ job_id: localJobId, file_id: fileId, stage: "waiting_password", stage_detail: "等待 E2EE 原始密碼。" });
  const csrf = getCsrfToken() || "";
  try {
    const e2ee = await fetchDriveE2eeKey(fileId, csrf);
    const passphrase = await getDriveE2eeSessionPassphrase(
      fileId,
      "請輸入此 E2EE 影音原始加密密碼。密碼只會在瀏覽器端使用，用來建立分享授權與 Streaming v2 分段。",
      { allowPrompt: true }
    );
    if (!passphrase) {
      throw new Error("E2EE 影音分享需要先輸入原始加密密碼。");
    }
    updateVideoUploadLiveJob(localJobId, {
      status: "running",
      progress_percent: 5,
      stage: "local_decrypt",
      stage_detail: "正在瀏覽器端解密原片以建立 encrypted stream；伺服器不會取得明文。",
    });
    rememberVideoE2eeLocalTask({ job_id: localJobId, file_id: fileId, stage: "local_decrypt", stage_detail: "瀏覽器端解密原片。" });
    const fileKey = await unwrapDriveFileKey(e2ee.encrypted_file_key, passphrase);
    rememberDriveE2eeSessionPassphrase(fileId, passphrase);
    const rawFileKey = await window.crypto.subtle.exportKey("raw", fileKey);
    const shareKeyBytes = new Uint8Array(32);
    window.crypto.getRandomValues(shareKeyBytes);
    const shareKey = await window.crypto.subtle.importKey("raw", shareKeyBytes, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);
    const nonce = new Uint8Array(12);
    window.crypto.getRandomValues(nonce);
    const ciphertext = await window.crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, shareKey, rawFileKey);
    const cipherBlob = await fetchDriveE2eeCiphertext(fileId, csrf);
    const originalCiphertextDigest = String(e2ee?.ciphertext_sha256 || "").trim()
      || videoHexDigest(await window.crypto.subtle.digest("SHA-256", await cipherBlob.arrayBuffer()));
    const decrypted = await decryptDriveE2eeBlobWithFileKey(cipherBlob, e2ee, fileKey);
    updateVideoUploadLiveJob(localJobId, {
      status: "running",
      progress_percent: 12,
      stage: "stream_v2_encrypt",
      stage_detail: "正在瀏覽器端建立原畫質 encrypted Streaming v2 bundle。",
    });
    const streamV2 = await buildVideoE2eeStreamV2Package(fileKey, decrypted.blob, decrypted.metadata);
    const derivativePackages = await buildVideoE2eeDerivativePackages(fileKey, decrypted.blob, decrypted.metadata, originalCiphertextDigest, localJobId);
    updateVideoUploadLiveJob(localJobId, {
      status: "running",
      progress_percent: 84,
      stage: "ready_to_upload",
      stage_detail: "E2EE 本機加密完成，等待上傳 encrypted bundles。",
      metadata: { file_id: fileId, derivative_count: derivativePackages.length },
    });
    rememberVideoE2eeLocalTask({ job_id: localJobId, file_id: fileId, stage: "ready_to_upload", stage_detail: "等待上傳 encrypted bundles。" });
    return {
      share_wrapped_file_key_envelope: JSON.stringify({
        alg: "AES-GCM",
        v: 1,
        nonce: videoShareBytesToBase64(nonce),
        ciphertext: videoShareBytesToBase64(new Uint8Array(ciphertext)),
      }),
      share_fragment_key: videoShareBytesToBase64Url(shareKeyBytes),
      stream_v2_manifest_json: streamV2.manifest_json,
      stream_v2_bundle_blob: streamV2.bundle_blob,
      derivative_packages: derivativePackages,
      local_job_id: localJobId,
    };
  } catch (err) {
    updateVideoUploadLiveJob(localJobId, {
      status: "failed",
      progress_percent: 100,
      stage: "failed",
      stage_detail: err?.message || "E2EE 本機轉檔 / 加密失敗",
      error_message: err?.message || "E2EE 本機轉檔 / 加密失敗",
    });
    clearVideoE2eeLocalTask(localJobId);
    throw err;
  }
}

async function uploadVideoE2eeStreamV2Package(fileId, artifacts) {
  if (!artifacts?.stream_v2_manifest_json || !artifacts?.stream_v2_bundle_blob) return null;
  const form = new FormData();
  form.append("manifest_json", artifacts.stream_v2_manifest_json);
  form.append("bundle", artifacts.stream_v2_bundle_blob, "e2ee-stream-v2.bundle");
  setVideoUploadProgress({
    visible: true,
    percent: 0,
    loaded: 0,
    total: artifacts.stream_v2_bundle_blob.size || 0,
    status: "E2EE Streaming v2 密文分段上傳中",
  });
  if (artifacts.local_job_id) {
    updateVideoUploadLiveJob(artifacts.local_job_id, {
      status: "running",
      progress_percent: 86,
      stage: "upload_original_stream",
      stage_detail: "正在上傳原畫質 encrypted Streaming v2 bundle。",
    });
  }
  const upload = await videoUploadFormWithProgress(`/api/media/${encodeURIComponent(fileId)}/e2ee-stream-v2`, form, (event) => {
    if (event.lengthComputable) {
      setVideoUploadProgress({
        visible: true,
        percent: (event.loaded / event.total) * 100,
        loaded: event.loaded,
        total: event.total,
        status: event.loaded >= event.total ? "E2EE Streaming v2 manifest 儲存中" : "E2EE Streaming v2 密文分段上傳中",
      });
    } else {
      setVideoUploadProgress({ visible: true, percent: 0, loaded: event.loaded || 0, total: 0, status: "E2EE Streaming v2 密文分段上傳中", indeterminate: true });
    }
  });
  const json = upload.json || {};
  if (!upload.ok || !json.ok) throw new Error(json.msg || `HTTP ${upload.status}`);
  setVideoUploadProgress({
    visible: true,
    percent: 100,
    loaded: artifacts.stream_v2_bundle_blob.size || 0,
    total: artifacts.stream_v2_bundle_blob.size || 0,
    status: "E2EE Streaming v2 已建立",
  });
  if (artifacts.local_job_id) {
    updateVideoUploadLiveJob(artifacts.local_job_id, {
      status: "running",
      progress_percent: 90,
      stage: "upload_derivatives",
      stage_detail: "原畫質 encrypted stream 已建立，準備上傳省流量版本。",
    });
  }
  return json.asset || null;
}

async function uploadVideoE2eeDerivativePackages(fileId, artifacts) {
  const packages = Array.isArray(artifacts?.derivative_packages) ? artifacts.derivative_packages : [];
  const uploaded = [];
  for (const item of packages) {
    if (!item?.name || !item?.stream_v2_manifest_json || !item?.stream_v2_bundle_blob) continue;
    const form = new FormData();
    form.append("manifest_json", item.stream_v2_manifest_json);
    form.append("bundle", item.stream_v2_bundle_blob, `${item.name}.e2ee-stream-v2.bundle`);
    form.append("label", item.label || item.name);
    form.append("width", String(item.width || 0));
    form.append("height", String(item.height || 0));
    form.append("bitrate", String(item.bitrate || 0));
    form.append("derived_from_original_sha256", item.derived_from_original_sha256 || "");
    setVideoUploadProgress({
      visible: true,
      percent: 0,
      loaded: 0,
      total: item.stream_v2_bundle_blob.size || 0,
      status: `E2EE ${item.label || item.name} 加密省流量版本上傳中`,
    });
    if (artifacts.local_job_id) {
      updateVideoUploadLiveJob(artifacts.local_job_id, {
        status: "running",
        progress_percent: Math.min(98, 90 + uploaded.length * 3),
        stage: "upload_derivative",
        stage_detail: `正在上傳 ${item.label || item.name} encrypted derivative。`,
      });
    }
    try {
      const upload = await videoUploadFormWithProgress(`/api/media/${encodeURIComponent(fileId)}/e2ee-stream-v2/variants/${encodeURIComponent(item.name)}`, form, (event) => {
        if (event.lengthComputable) {
          setVideoUploadProgress({
            visible: true,
            percent: (event.loaded / event.total) * 100,
            loaded: event.loaded,
            total: event.total,
            status: event.loaded >= event.total ? `E2EE ${item.label || item.name} manifest 儲存中` : `E2EE ${item.label || item.name} 加密省流量版本上傳中`,
          });
        }
      });
      const json = upload.json || {};
      if (!upload.ok || !json.ok) throw new Error(json.msg || `HTTP ${upload.status}`);
      uploaded.push(json.variant);
    } catch (err) {
      videoMsg(`E2EE ${item.label || item.name} 省流量版本未建立：${err.message || "請稍後重試"}`, false);
    }
  }
  if (uploaded.length) videoMsg(`已建立 ${uploaded.length} 組 E2EE 省流量畫質。`, true);
  if (artifacts.local_job_id) {
    updateVideoUploadLiveJob(artifacts.local_job_id, {
      status: "succeeded",
      progress_percent: 100,
      stage: "completed",
      stage_detail: uploaded.length ? `已建立 ${uploaded.length} 組 E2EE 省流量畫質。` : "原畫質 encrypted stream 已建立；沒有可用的省流量 derivative。",
      metadata: { file_id: fileId, derivative_count: uploaded.length },
    });
    clearVideoE2eeLocalTask(artifacts.local_job_id);
  }
  return uploaded;
}

async function buildVideoE2eeShareEnvelope(fileId) {
  const artifacts = await prepareVideoE2eeShareArtifacts(fileId);
  try {
    return {
      share_wrapped_file_key_envelope: artifacts.share_wrapped_file_key_envelope,
      share_fragment_key: artifacts.share_fragment_key,
    };
  } finally {
    if (artifacts.local_job_id) {
      updateVideoUploadLiveJob(artifacts.local_job_id, {
        status: "succeeded",
        progress_percent: 100,
        stage: "completed",
        stage_detail: "E2EE 分享授權已建立。",
      });
      clearVideoE2eeLocalTask(artifacts.local_job_id);
    }
  }
}

function videoDisplayName(file) {
  const displayName = String(file?.display_name || file?.storage_display_name || "").trim();
  const virtualPath = String(file?.virtual_path || file?.storage_virtual_path || "").trim();
  const virtualName = virtualPath.split("/").filter(Boolean).pop() || "";
  const originalName = String(file?.original_filename_plain_for_public || file?.filename || "").trim();
  return displayName || virtualName || originalName || file?.id || file?.file_id || "影音檔";
}

function videoTitleFromFilename(name = "") {
  return String(name || "影音檔").replace(/\.[^.]+$/, "").trim() || "影音檔";
}

function videoPublishFileId(file) {
  return String(file?.id || file?.file_id || "");
}

function videoPublishFileById(fileId) {
  const target = String(fileId || "");
  return videoPublishDriveFiles.find((file) => videoPublishFileId(file) === target) || null;
}

function videoPublishSizeText(file) {
  if (typeof formatDriveBytes === "function") return formatDriveBytes(file?.size_bytes || 0);
  const bytes = Number(file?.size_bytes || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${Math.round(bytes)} B`;
}

function videoPublishPrivacyLabel(file) {
  const mode = String(file?.privacy_mode || "standard_plain");
  if (mode === "e2ee") return "E2EE，發布後由瀏覽器端處理";
  if (mode === "server_encrypted") return "伺服器端加密，可解密預覽";
  return "一般影音";
}

function videoPublishMediaKind(file) {
  const name = videoDisplayName(file).toLowerCase();
  const mime = videoMime(file);
  if (mime.startsWith("audio/") || [".mp3", ".m4a", ".aac", ".flac", ".wav", ".weba", ".opus", ".oga", ".ogg"].some((ext) => name.endsWith(ext))) return "audio";
  return "video";
}

function videoPublishFileExtension(name = "") {
  const text = String(name || "").toLowerCase().trim();
  const index = text.lastIndexOf(".");
  return index >= 0 ? text.slice(index) : "";
}

function videoPublishShouldPreferPreparedHls(file) {
  if (!file || videoPublishMediaKind(file) !== "video") return false;
  if (String(file?.privacy_mode || "").trim().toLowerCase() === "e2ee") return false;
  const name = videoDisplayName(file);
  const ext = videoPublishFileExtension(name);
  const mime = videoMime(file);
  if (mime.includes("matroska") || mime.includes("x-msvideo") || mime.includes("quicktime")) return true;
  if ([".mkv", ".avi", ".mov", ".flv", ".wmv", ".ts", ".mts", ".m2ts"].includes(ext)) return true;
  return false;
}

function syncVideoPublishStreamingModeForFile(file) {
  const select = $("video-streaming-modes");
  if (!select || !file) return;
  select.value = videoPublishShouldPreferPreparedHls(file) ? "prepared_hls" : "direct";
}

function videoPublishPreviewUrl(file) {
  if (String(file?.privacy_mode || "") === "e2ee") return "";
  const id = videoPublishFileId(file);
  return id ? `${API}/cloud-drive/files/${encodeURIComponent(id)}/preview/content` : "";
}

function syncVideoPublishDriveGallerySelection(fileId) {
  const selectedId = String(fileId || "");
  const gallery = $("video-publish-file-gallery");
  if (!gallery) return;
  gallery.querySelectorAll("[data-video-publish-cloud-id]").forEach((card) => {
    const active = String(card.dataset.videoPublishCloudId || "") === selectedId;
    card.classList.toggle("active", active);
    card.setAttribute("aria-selected", active ? "true" : "false");
  });
}

function applyVideoPublishDriveSelection(fileId, title = "") {
  const select = $("video-publish-file");
  const target = String(fileId || "").trim();
  if (!select || !target) return false;
  const options = Array.from(select.options || []);
  const matched = options.find((option) => String(option.value || "") === target);
  if (!matched) return false;
  select.value = target;
  syncVideoPublishDriveGallerySelection(target);
  const uploadInput = $("video-upload-file");
  if (uploadInput) uploadInput.value = "";
  const titleInput = $("video-publish-title");
  if (titleInput && !titleInput.value.trim()) {
    titleInput.value = videoTitleFromFilename(title || videoDisplayName(videoPublishFileById(target)) || matched.textContent || "");
  }
  syncVideoPublishStreamingModeForFile(videoPublishFileById(target));
  return true;
}

function videoMime(file) {
  return String(file.mime_type_plain_for_public || file.mime_type || "").toLowerCase();
}

function isCloudMediaFile(file) {
  const name = videoDisplayName(file).toLowerCase();
  const mime = videoMime(file);
  return mime.startsWith("video/")
    || mime.startsWith("audio/")
    || [".mp4", ".m4v", ".mov", ".webm", ".ogv", ".avi", ".mkv", ".mp3", ".m4a", ".aac", ".flac", ".wav", ".weba", ".opus", ".oga", ".ogg"].some((ext) => name.endsWith(ext));
}

function renderVideoPublishDriveGallery(files = []) {
  const gallery = $("video-publish-file-gallery");
  if (!gallery) return;
  if (!files.length) {
    gallery.innerHTML = `<div class="drive-empty video-cloud-empty">雲端硬碟目前沒有可發布的影音檔</div>`;
    return;
  }
  const selectedId = String($("video-publish-file")?.value || "");
  gallery.innerHTML = files.map((file) => {
    const id = videoPublishFileId(file);
    const name = videoDisplayName(file);
    const mime = videoMime(file);
    const mediaKind = videoPublishMediaKind(file);
    const previewUrl = videoPublishPreviewUrl(file);
    const active = id && id === selectedId;
    let preview = `<div class="video-cloud-preview-fallback">${mediaKind === "audio" ? "音訊" : "影片"}</div>`;
    if (previewUrl && mediaKind === "audio") {
      preview = `<audio class="video-cloud-media" preload="none" controls src="${sanitize(previewUrl)}"></audio>`;
    } else if (previewUrl) {
      preview = `<video class="video-cloud-media" preload="none" controls playsinline src="${sanitize(previewUrl)}"></video>`;
    } else if (String(file?.privacy_mode || "") === "e2ee") {
      preview = `<div class="video-cloud-preview-fallback">E2EE</div>`;
    }
    return `
      <div class="video-cloud-card${active ? " active" : ""}" data-video-publish-cloud-id="${sanitize(id)}" role="option" tabindex="0" aria-selected="${active ? "true" : "false"}">
        <div class="video-cloud-preview">${preview}</div>
        <div class="video-cloud-main">
          <strong>${sanitize(name)}</strong>
          <span>${sanitize(videoPublishSizeText(file))} · ${sanitize(videoPublishPrivacyLabel(file))}${mime ? ` · ${sanitize(mime)}` : ""}</span>
        </div>
        <button class="btn btn-sm" type="button" data-video-publish-cloud-select="${sanitize(id)}">使用此影音</button>
      </div>
    `;
  }).join("");
}

function formatVideoCount(value, unit = "") {
  const number = Number(value || 0);
  if (number >= 10000) return `${(number / 10000).toFixed(1)}萬${unit}`;
  return `${number}${unit}`;
}

function normalizeVideoSearchQuery(value) {
  return String(value || "").replace(/\s+/g, " ").trim().slice(0, 80);
}

function syncVideoSearchControls() {
  const input = $("video-search-input");
  const clear = $("video-search-clear");
  const status = $("video-search-status");
  if (input && document.activeElement !== input) input.value = videoState.searchQuery || "";
  if (clear) clear.hidden = !videoState.searchQuery;
  if (status) {
    status.textContent = videoState.searchQuery
      ? `搜尋「${videoState.searchQuery}」`
      : "";
  }
}

function videoVisibilityLabel(value) {
  if (value === "private") return "私人";
  if (value === "unlisted") return "持連結可看";
  return "公開";
}

function videoStreamUrl(video) {
  const id = Number(video?.id || 0);
  return video?.stream_url || (id ? `/api/videos/${id}/stream` : "");
}

function videoPlaybackUrl(video) {
  const id = Number(video?.id || 0);
  return video?.playback_url || (id ? `/api/videos/${id}/playback` : "");
}

function videoThumbMarkup(video) {
  if (video.cover_url) {
    return `
      <div class="video-thumb video-thumb-cover">
        <img class="video-thumb-image" src="${sanitize(video.cover_url)}" alt="${sanitize(video.title || "影音封面")}" loading="lazy" />
        <span class="video-thumb-play">${video.media_type === "audio" ? "♪" : "▶"}</span>
      </div>
    `;
  }
  const url = videoStreamUrl(video);
  if (video.media_type === "audio") {
    return `<div class="video-thumb video-thumb-audio"><span>♪</span></div>`;
  }
  const privacyMode = String(video?.cloud_privacy_mode || "").trim().toLowerCase();
  const directPreviewAllowed = video?.direct_stream_allowed !== false && !["server_encrypted", "e2ee"].includes(privacyMode);
  if (!directPreviewAllowed) {
    return `<div class="video-thumb"><span>▶</span></div>`;
  }
  if (!url) {
    return `<div class="video-thumb"><span>▶</span></div>`;
  }
  return `
    <div class="video-thumb video-thumb-media-wrap">
      <video class="video-thumb-media" muted playsinline preload="metadata" src="${sanitize(url)}#t=0.1" aria-hidden="true"></video>
      <span class="video-thumb-play">▶</span>
    </div>
  `;
}

function makeVideoIdempotencyKey(prefix = "video-tip") {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return `${prefix}:${window.crypto.randomUUID()}`;
  }
  return `${prefix}:${Date.now()}:${Math.random().toString(16).slice(2)}`;
}

function cancelPendingVideoDetailRequest({ advanceNavigation = false } = {}) {
  videoState.detailRequestGeneration += 1;
  if (advanceNavigation) videoState.navigationGeneration += 1;
  if (videoState.detailAbortController) {
    videoState.detailAbortController.abort();
    videoState.detailAbortController = null;
  }
}

function showVideoBrowseView({ updateHash = false } = {}) {
  cancelPendingVideoDetailRequest({ advanceNavigation: updateHash });
  const browse = $("video-browse-view");
  const watch = $("video-watch-view");
  const detail = $("video-detail");
  videoState.playbackSessionId += 1;
  resetVideoDanmakuState();
  destroyCurrentVideoPlaybackArtifacts();
  if (browse) browse.style.display = "";
  if (watch) watch.style.display = "none";
  if (detail) detail.innerHTML = "";
  videoState.current = null;
  if (updateHash && /^#videos\/\d+$/.test(location.hash || "")) {
    history.pushState(null, "", `${location.pathname}${location.search}#videos`);
  }
}

function showVideoWatchView() {
  const browse = $("video-browse-view");
  const watch = $("video-watch-view");
  if (browse) browse.style.display = "none";
  if (watch) watch.style.display = "";
}

function setVideoPublishPanelVisible(visible, options = {}) {
  const panel = $("video-publish-panel");
  const toggle = $("video-publish-open-btn");
  const show = !!visible;
  if (panel) {
    panel.hidden = !show;
    if ("open" in panel) panel.open = show;
  }
  if (toggle) {
    toggle.setAttribute("aria-expanded", show ? "true" : "false");
    toggle.textContent = show ? "收起發布影音" : "發布影音";
  }
  if (show && options.loadFiles) {
    loadVideoPublishFiles();
  }
  if (show && options.focus !== false) {
    setTimeout(() => {
      ($("video-upload-file") || $("video-publish-file") || $("video-publish-title"))?.focus?.();
    }, 80);
  }
}

function toggleVideoPublishPanel() {
  const panel = $("video-publish-panel");
  setVideoPublishPanelVisible(!!panel?.hidden, { loadFiles: true });
}

async function loadVideoPublishFiles() {
  const select = $("video-publish-file");
  if (!select) return;
  select.innerHTML = `<option value="">讀取影音檔...</option>`;
  const gallery = $("video-publish-file-gallery");
  if (gallery) gallery.innerHTML = `<div class="drive-empty video-cloud-empty">讀取雲端影音中...</div>`;
  try {
    const res = await apiFetch(API + "/cloud-drive/files", { credentials: "same-origin" });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    const files = (json.files || []).filter(isCloudMediaFile);
    videoPublishDriveFiles = files;
    select.innerHTML = files.length
      ? files.map((file) => `<option value="${sanitize(videoPublishFileId(file))}">${sanitize(videoDisplayName(file))}</option>`).join("")
      : `<option value="">雲端硬碟目前沒有可發布的影音檔</option>`;
    renderVideoPublishDriveGallery(files);
    if (videoPendingPublishSelection?.fileId) {
      const applied = applyVideoPublishDriveSelection(videoPendingPublishSelection.fileId, videoPendingPublishSelection.title);
      if (applied) setVideoPublishPanelVisible(true, { focus: false });
    }
  } catch (err) {
    videoPublishDriveFiles = [];
    select.innerHTML = `<option value="">影音檔讀取失敗</option>`;
    if (gallery) gallery.innerHTML = `<div class="drive-empty video-cloud-empty">影音檔讀取失敗</div>`;
    videoMsg(err.message || "影音檔讀取失敗", false);
  }
}

async function openVideoPublishFromDrive(fileId, options = {}) {
  const target = String(fileId || "").trim();
  if (!target) return false;
  videoPendingPublishSelection = {
    fileId: target,
    title: options.title || "",
    createdAt: Date.now(),
  };
  if (typeof switchModuleTab === "function") {
    switchModuleTab("videos");
  }
  if (location.hash !== "#videos") {
    history.pushState(null, "", `${location.pathname}${location.search}#videos`);
  }
  showVideoBrowseView();
  setVideoPublishPanelVisible(true, { focus: false });
  videoMsg("已帶入雲端硬碟影音，正在載入發布設定...", true);
  await loadVideoPublishFiles();
  const applied = applyVideoPublishDriveSelection(target, options.title || "");
  if (!applied) {
    videoMsg("這個檔案不是可發布的影音檔，或目前帳號沒有檔案權限。", false);
    return false;
  }
  $("video-publish-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  const titleInput = $("video-publish-title");
  const visibilityInput = $("video-publish-visibility");
  setTimeout(() => (titleInput || visibilityInput)?.focus?.(), 150);
  setTimeout(() => {
    if (videoPendingPublishSelection?.fileId === target) videoPendingPublishSelection = null;
  }, 8000);
  videoMsg("已選擇雲端硬碟影音，請完成標題、可見性、分享與封面設定後發布。", true);
  return true;
}

async function publishVideoFromDrive() {
  syncVideoPublishStreamingModeForPrivacy();
  const button = $("video-publish-btn");
  const directFile = $("video-upload-file")?.files?.[0] || null;
  const coverFile = $("video-cover-file")?.files?.[0] || null;
  const sharePassword = ($("video-share-password")?.value || "").trim();
  const selectedFile = directFile ? null : videoSelectedDriveFile();
  const streamingModes = selectedVideoPublishStreamingModes();
  const payload = {
    cloud_file_id: $("video-publish-file")?.value || "",
    title: ($("video-publish-title")?.value || "").trim(),
    description: ($("video-publish-description")?.value || "").trim(),
    visibility: $("video-publish-visibility")?.value || "public",
    share_password: sharePassword,
    share_expires_at: typeof getShareExpiryPickerValue === "function"
      ? getShareExpiryPickerValue("video-share-expires-at")
      : ($("video-share-expires-at")?.value || "").trim(),
    share_max_views: ($("video-share-max-views")?.value || "").trim(),
    streaming_modes: streamingModes,
  };
  if (!directFile && !payload.cloud_file_id) return videoMsg("請選擇要直接上傳的影音檔，或選擇雲端硬碟中的影音檔", false);
  if (!payload.title && !directFile) return videoMsg("請輸入影音標題", false);
  if (!directFile && selectedFile?.privacy_mode === "e2ee" && payload.visibility === "public") {
    payload.visibility = "unlisted";
    const visibilitySelect = $("video-publish-visibility");
    if (visibilitySelect) visibilitySelect.value = "unlisted";
    videoMsg("E2EE 影音對外觀看已改用「持連結可看」；觀看者需使用完整分享連結，不需要知道原始 E2EE 密碼。", true);
  }
  let e2eeShare = null;
  let liveUploadJobId = "";
  let publishedVideoId = 0;
  const publishNavigationGeneration = videoState.navigationGeneration;
  if (!directFile && selectedFile?.privacy_mode === "e2ee" && payload.visibility === "unlisted") {
    try {
      e2eeShare = await prepareVideoE2eeShareArtifacts(selectedFile.id);
      payload.share_wrapped_file_key_envelope = e2eeShare.share_wrapped_file_key_envelope;
    } catch (err) {
      return videoMsg(err.message || "E2EE 影音分享授權建立失敗", false);
    }
  }
  if (button) button.disabled = true;
  try {
    let status = 0;
    let json = {};
    if (directFile) {
      liveUploadJobId = videoUploadLiveJobId();
      const uploadPrivacyMode = $("video-upload-privacy-mode")?.value || "standard_plain";
      const uploadDoneStatus = uploadPrivacyMode === "server_encrypted"
        ? "上傳完成，伺服器端加密與掃描中；若有選 HLS 才會在後台轉檔"
        : "上傳完成，伺服器儲存與掃描中；若有選 HLS 才會在後台轉檔";
      updateVideoUploadLiveJob(liveUploadJobId, {
        title: `影音上傳：${directFile.name}`,
        progress_percent: 1,
        stage: "queued",
        stage_detail: `準備上傳 ${directFile.name}`,
        metadata: { filename: directFile.name, size_bytes: directFile.size, privacy_mode: uploadPrivacyMode },
      });
      const form = new FormData();
      form.append("video", directFile);
      form.append("title", payload.title || directFile.name.replace(/\.[^.]+$/, ""));
      form.append("description", payload.description);
      form.append("visibility", payload.visibility);
      form.append("share_password", payload.share_password);
      form.append("share_expires_at", payload.share_expires_at);
      form.append("share_max_views", payload.share_max_views);
      form.append("privacy_mode", uploadPrivacyMode);
      form.append("streaming_modes", JSON.stringify(streamingModes));
      if (coverFile) form.append("cover", coverFile);
      videoMsg("影音檔上傳中。上傳完成後會直接以你選的串流方式發布；只有選 HLS 才會建立背景轉檔任務。", true);
      setVideoUploadProgress({ visible: true, percent: 0, loaded: 0, total: directFile.size, status: `準備上傳 ${directFile.name}` });
      const upload = await videoUploadFormWithProgress(API + "/videos/upload", form, (event) => {
        if (event.lengthComputable) {
          const percent = (event.loaded / event.total) * 100;
          const uploadComplete = event.loaded >= event.total;
          setVideoUploadProgress({
            visible: true,
            percent: Math.min(95, percent * 0.95),
            loaded: event.loaded,
            total: event.total,
            status: uploadComplete ? uploadDoneStatus : "影音檔上傳中",
            indeterminate: uploadComplete,
          });
          updateVideoUploadLiveJob(liveUploadJobId, {
            status: "running",
            progress_percent: event.loaded >= event.total ? 88 : Math.max(1, Math.min(87, Math.round(percent * 0.87))),
            stage: event.loaded >= event.total ? "server_processing" : "uploading",
            stage_detail: event.loaded >= event.total ? uploadDoneStatus : "影音檔上傳中",
            metadata: { loaded_bytes: event.loaded, total_bytes: event.total },
          });
        } else {
          setVideoUploadProgress({ visible: true, percent: 0, loaded: event.loaded || 0, total: 0, status: "影音檔上傳中", indeterminate: true });
          updateVideoUploadLiveJob(liveUploadJobId, {
            status: "running",
            progress_percent: 5,
            stage: "uploading",
            stage_detail: "影音檔上傳中",
            metadata: { loaded_bytes: event.loaded || 0 },
          });
        }
      });
      status = upload.status;
      json = upload.json || {};
    } else if (coverFile) {
      const form = new FormData();
      form.append("cloud_file_id", payload.cloud_file_id);
      form.append("title", payload.title);
      form.append("description", payload.description);
      form.append("visibility", payload.visibility);
      form.append("share_password", payload.share_password);
      form.append("share_expires_at", payload.share_expires_at);
      form.append("share_max_views", payload.share_max_views);
      if (payload.share_wrapped_file_key_envelope) form.append("share_wrapped_file_key_envelope", payload.share_wrapped_file_key_envelope);
      form.append("streaming_modes", JSON.stringify(streamingModes));
      form.append("cover", coverFile);
      videoMsg("影音封面上傳中，請稍候...", true);
      setVideoUploadProgress({ visible: true, percent: 0, loaded: 0, total: coverFile.size, status: `準備上傳封面 ${coverFile.name}` });
      const upload = await videoUploadFormWithProgress(API + "/videos/publish", form, (event) => {
        if (event.lengthComputable) {
          const percent = (event.loaded / event.total) * 100;
          const uploadComplete = event.loaded >= event.total;
          setVideoUploadProgress({
            visible: true,
            percent: Math.min(95, percent * 0.95),
            loaded: event.loaded,
            total: event.total,
            status: uploadComplete ? "封面上傳完成，伺服器處理中" : "封面上傳中",
            indeterminate: uploadComplete,
          });
        } else {
          setVideoUploadProgress({ visible: true, percent: 0, loaded: event.loaded || 0, total: 0, status: "封面上傳中", indeterminate: true });
        }
      });
      status = upload.status;
      json = upload.json || {};
    } else {
      const res = await apiFetch(API + "/videos/publish", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      status = res.status;
      json = await res.json().catch(() => ({}));
    }
    if (status < 200 || status >= 300 || !json.ok) throw new Error(json.msg || `HTTP ${status}`);
    publishedVideoId = Number(json.video?.id || 0);
    const input = $("video-upload-file");
    if (input) input.value = "";
    const coverInput = $("video-cover-file");
    if (coverInput) coverInput.value = "";
    const shareInput = $("video-share-password");
    if (shareInput) shareInput.value = "";
    const shareMaxViews = $("video-share-max-views");
    if (shareMaxViews) shareMaxViews.value = "";
    const shareExpiresAt = $("video-share-expires-at");
    if (typeof setShareExpiryPickerValue === "function") setShareExpiryPickerValue(shareExpiresAt || "video-share-expires-at", "");
    else if (shareExpiresAt) shareExpiresAt.value = "";
    videoPendingPublishSelection = null;
    if (e2eeShare && json.video?.share_url) {
      rememberVideoShareFragment(json.video.share_url, e2eeShare.share_fragment_key);
    }
    if (e2eeShare && selectedFile?.id) {
      try {
        await uploadVideoE2eeStreamV2Package(selectedFile.id, e2eeShare);
        await uploadVideoE2eeDerivativePackages(selectedFile.id, e2eeShare);
      } catch (err) {
        if (e2eeShare.local_job_id) {
          updateVideoUploadLiveJob(e2eeShare.local_job_id, {
            status: "failed",
            progress_percent: 100,
            stage: "failed",
            stage_detail: err.message || "E2EE Streaming v2 建立失敗",
            error_message: err.message || "E2EE Streaming v2 建立失敗",
          });
          clearVideoE2eeLocalTask(e2eeShare.local_job_id);
        }
        videoMsg(`影音已發布，但 E2EE Streaming v2 建立失敗：${err.message || "請稍後重試"}`, false);
      }
    }
    if (json.video?.id) {
      const preferredMode = streamingModes.includes("prepared_hls")
        ? "prepared_hls"
        : (streamingModes.includes("realtime_proxy") ? "realtime_proxy" : "direct");
      saveVideoSelectedServiceMode(json.video, preferredMode);
    }
    setVideoPublishPanelVisible(false, { focus: false });
    await loadVideoPublishFiles();
    await loadVideos(videoState.sort);
    if (publishedVideoId && videoState.navigationGeneration === publishNavigationGeneration) {
      await openVideoDetail(publishedVideoId);
    }
    if (directFile || coverFile) {
      const completedBytes = directFile?.size || coverFile?.size || 0;
      setVideoUploadProgress({ visible: true, percent: 100, loaded: completedBytes, total: completedBytes, status: "處理完成" });
      if (liveUploadJobId) {
        updateVideoUploadLiveJob(liveUploadJobId, {
          status: "succeeded",
          progress_percent: 100,
          stage: "completed",
          stage_detail: "影音上傳與伺服器處理完成；若需要 HLS，後續轉檔會以另一筆任務顯示。",
          metadata: { video_id: publishedVideoId, file_id: json.file?.file_id || json.video?.cloud_file_id },
        });
      }
    }
    if (json.stream_warning) {
      videoMsg(`影音已發布；${json.stream_warning}`, false);
    } else if (json.stream_asset?.status === "ready") {
      videoMsg("影音已發布，HLS 串流已就緒", true);
    } else if (json.stream_asset?.status === "processing") {
      videoMsg("影音已發布，HLS 正在後台轉檔；你可以先做別的事，進度會顯示在任務中心，完成後會通知上傳者。", true);
    } else {
      videoMsg("影音已發布", true);
    }
  } catch (err) {
    if (e2eeShare?.local_job_id) {
      updateVideoUploadLiveJob(e2eeShare.local_job_id, {
        status: "failed",
        progress_percent: 100,
        stage: "failed",
        stage_detail: err.message || "影音發布失敗，E2EE 本機任務已停止。",
        error_message: err.message || "影音發布失敗",
      });
      clearVideoE2eeLocalTask(e2eeShare.local_job_id);
    }
    if (publishedVideoId) {
      const completedBytes = directFile?.size || coverFile?.size || 0;
      setVideoUploadProgress({ visible: true, percent: 100, loaded: completedBytes, total: completedBytes, status: "影音已發布，畫面同步失敗" });
      if (liveUploadJobId) {
        updateVideoUploadLiveJob(liveUploadJobId, {
          status: "succeeded",
          progress_percent: 100,
          stage: "completed_with_warning",
          stage_detail: `影音已發布，但畫面同步失敗：${err.message || "請刷新影音頁"}`,
          metadata: { video_id: publishedVideoId },
        });
      }
      videoMsg(`影音已發布，但畫面同步失敗：${err.message || "請刷新影音頁"}`, false);
    } else if (directFile || coverFile) {
      setVideoUploadProgress({ visible: true, percent: 100, loaded: 0, total: 0, status: err.message || "影音發布失敗" });
      if (liveUploadJobId) {
        updateVideoUploadLiveJob(liveUploadJobId, {
          status: "failed",
          progress_percent: 100,
          stage: "failed",
          stage_detail: err.message || "影音發布失敗",
          error_message: err.message || "影音發布失敗",
        });
      }
    }
    if (!publishedVideoId) videoMsg(err.message || "影音發布失敗", false);
  } finally {
    if (button) button.disabled = false;
  }
}

function renderVideoList() {
  const list = $("video-list");
  if (!list) return;
  if (!videoState.videos.length) {
    const emptyText = videoState.searchQuery
      ? `找不到與「${videoState.searchQuery}」相關的影音`
      : "目前沒有可觀看的影音";
    list.innerHTML = `<div class="drive-empty">${sanitize(emptyText)}</div>`;
    return;
  }
  list.innerHTML = videoState.videos.map((video) => `
    <a class="video-card" href="#videos/${Number(video.id || 0)}" data-video-open="${Number(video.id || 0)}">
      ${videoThumbMarkup(video)}
      <div class="video-card-body">
        <strong>${sanitize(video.title || "未命名影片")}</strong>
        <div class="drive-card-sub video-card-owner">${userIdentityMarkup(video.owner_user_id, video.owner_username || video.owner_nickname || "使用者", `${formatVideoCount(video.view_count, " 次觀看")}`, "video-owner-line", video.owner_avatar_file_id || "")}</div>
        <div class="drive-card-sub">${sanitize(videoVisibilityLabel(video.visibility))} · 👍 ${formatVideoCount(video.like_count)} · 💬 ${formatVideoCount(video.comment_count)} · 分享 ${formatVideoCount(video.share_count || 0)} · 互動 ${formatVideoCount(video.interaction_score || 0)}</div>
      </div>
    </a>
  `).join("");
  bindAvatarFallbacks(list);
}

async function loadVideos(sort = "new", options = {}) {
  videoState.sort = sort;
  if (Object.prototype.hasOwnProperty.call(options, "query")) {
    videoState.searchQuery = normalizeVideoSearchQuery(options.query);
  }
  videoState.browseLoaded = true;
  syncVideoSearchControls();
  const list = $("video-list");
  if (list) list.innerHTML = `<div class="drive-empty">影音載入中...</div>`;
  try {
    const params = new URLSearchParams({ sort });
    if (videoState.searchQuery) params.set("q", videoState.searchQuery);
    const res = await apiFetch(API + `/videos?${params.toString()}`, { credentials: "same-origin" });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    videoState.videos = Array.isArray(json.videos) ? json.videos : [];
    renderVideoList();
  } catch (err) {
    if (list) list.innerHTML = `<div class="drive-empty">${sanitize(err.message || "影音列表載入失敗")}</div>`;
  }
}

function renderVideoComments(comments) {
  if (!comments || !comments.length) return `<div class="drive-empty">尚無留言</div>`;
  return comments.map((comment) => `
    <div class="video-comment ${comment.parent_id ? "video-comment-reply" : ""}">
      ${userIdentityMarkup(comment.user_id, comment.username || comment.nickname || "使用者", comment.created_at || "", "video-comment-author", comment.avatar_file_id || "")}
      <p>${sanitize(comment.content || "")}</p>
    </div>
  `).join("");
}

function hlsMimeForVideo(mediaType = "video") {
  return mediaType === "audio" ? "application/vnd.apple.mpegurl" : "application/vnd.apple.mpegurl";
}

function browserSupportsNativeHls(mediaType = "video") {
  const probe = document.createElement(mediaType === "audio" ? "audio" : "video");
  const canPlayHls = !!(probe && typeof probe.canPlayType === "function" && probe.canPlayType(hlsMimeForVideo(mediaType)));
  if (!canPlayHls) return false;
  const ua = String(navigator.userAgent || "");
  const vendor = String(navigator.vendor || "");
  const platform = String(navigator.platform || "");
  const isIos = /\b(iPad|iPhone|iPod)\b/i.test(ua) || (platform === "MacIntel" && Number(navigator.maxTouchPoints || 0) > 1);
  const isSafari = /Safari/i.test(ua)
    && /Apple/i.test(vendor)
    && !/(Chrome|Chromium|CriOS|FxiOS|Edg|OPR|Android)/i.test(ua);
  return isIos || isSafari;
}

function selectedVideoPublishStreamingModes() {
  const select = $("video-streaming-modes");
  const mode = String(select?.value || "prepared_hls").trim();
  if (mode === "prepared_hls") return ["prepared_hls"];
  if (mode === "realtime_proxy") return ["realtime_proxy"];
  return ["realtime_proxy"];
}

function syncVideoPublishStreamingModeForPrivacy() {
  const privacyMode = String($("video-upload-privacy-mode")?.value || "standard_plain").trim();
  const select = $("video-streaming-modes");
  if (!select) return;
  const selectedFile = videoSelectedDriveFile();
  if (selectedFile) {
    syncVideoPublishStreamingModeForFile(selectedFile);
    return;
  }
  const directFile = $("video-upload-file")?.files?.[0] || null;
  if (directFile) {
    syncVideoPublishStreamingModeForFile({
      id: "",
      original_filename_plain_for_public: directFile.name,
      mime_type_plain_for_public: directFile.type,
    });
    return;
  }
  if (privacyMode === "e2ee" && select.value === "prepared_hls") select.value = "direct";
}

async function applyVideoPublishStreamingChoices(video, json, modes) {
  const selected = new Set(Array.isArray(modes) ? modes : []);
  if (!selected.has("prepared_hls")) return;
  const fileId = json?.file?.file_id || video?.cloud_file_id || json?.video?.cloud_file_id || "";
  if (!fileId) return;
  try {
    await prepareVideoStream(fileId);
  } catch (err) {
    videoMsg(`影音已發布，但 HLS 串流建立失敗：${err.message || "請稍後重試"}`, false);
  }
}

function videoStreamingOptions(playback = {}) {
  const rows = Array.isArray(playback?.streaming_options) ? playback.streaming_options : [];
  return rows.filter((item) => item && item.mode).map((item) => ({
    mode: String(item.mode || ""),
    label: String(item.label || item.service_tier_label || item.mode || ""),
    tier: String(item.service_tier_label || item.service_tier || ""),
    fee: String(item.fee_label || item.fee_level || ""),
    available: !!item.available,
    reason: String(item.availability_reason || ""),
    summary: String(item.customer_summary || item.notes || ""),
  }));
}

function videoServiceModeStorageKey(video) {
  return `hackme_web.video_service_mode.${String(video?.id || "")}`;
}

function videoDefaultServiceMode(playback = {}) {
  const policy = playback?.service_policy || {};
  const preferred = String(policy.default_mode || policy.recommended_mode || "").trim();
  if (preferred) return preferred;
  if (playback?.mode === "hls") return "prepared_hls";
  if (playback?.mode === "realtime_proxy") return "realtime_proxy";
  return "prepared_hls";
}

function videoSelectedServiceMode(video, playback = {}) {
  const options = videoStreamingOptions(playback);
  const availableModes = new Set(options.filter((option) => option.available).map((option) => option.mode));
  let saved = "";
  try {
    saved = localStorage.getItem(videoServiceModeStorageKey(video)) || "";
  } catch (_) {
    saved = "";
  }
  if (saved && availableModes.has(saved)) return saved;
  const preferred = videoDefaultServiceMode(playback);
  if (availableModes.has(preferred)) return preferred;
  if (availableModes.has("prepared_hls")) return "prepared_hls";
  if (availableModes.has("realtime_proxy")) return "realtime_proxy";
  return preferred || "realtime_proxy";
}

function saveVideoSelectedServiceMode(video, mode) {
  try {
    localStorage.setItem(videoServiceModeStorageKey(video), String(mode || ""));
  } catch (_) {}
}

function selectedVideoAudioTrack(playback = {}) {
  const tracks = videoPlaybackAudioTracks(playback).filter((track) => track.selectable !== false && track.playlistUrl !== "");
  if (!tracks.length) return null;
  const select = $("video-audio-track-select");
  if (select) {
    const selected = Math.max(0, Math.min(tracks.length - 1, Number(select.value || 0)));
    return tracks[selected] || tracks[0];
  }
  return tracks.find((track) => track.isDefault) || tracks[0];
}

function videoRealtimeProxyUrl(playback = {}, startSeconds = 0) {
  const raw = String(playback?.realtime_proxy_url || playback?.realtime_proxy?.url || "").trim();
  if (!raw) return "";
  const url = new URL(raw, window.location.origin);
  const track = selectedVideoAudioTrack(playback);
  if (track?.name) url.searchParams.set("audio", track.name);
  const start = Number(startSeconds || 0);
  if (Number.isFinite(start) && start > 0) url.searchParams.set("start", String(Math.max(0, Math.round(start * 1000) / 1000)));
  return `${url.pathname}${url.search}`;
}

function videoRealtimeProxyStartSeconds(src = "") {
  try {
    const url = new URL(String(src || ""), window.location.origin);
    const start = Number(url.searchParams.get("start") || 0);
    return Number.isFinite(start) && start > 0 ? start : 0;
  } catch (_) {
    return 0;
  }
}

function videoPlaybackDurationSeconds(playback = {}) {
  const candidates = [
    playback?.duration_seconds,
    playback?.duration,
    playback?.status?.duration_seconds,
    playback?.status?.duration,
  ];
  for (const value of candidates) {
    const parsed = Number(value || 0);
    if (Number.isFinite(parsed) && parsed > 0) return parsed;
  }
  return 0;
}

function videoBufferedContains(player, seconds, margin = 0.5) {
  const target = Number(seconds || 0);
  if (!player || !Number.isFinite(target)) return false;
  try {
    for (let index = 0; index < player.buffered.length; index += 1) {
      if (target >= player.buffered.start(index) - margin && target <= player.buffered.end(index) + margin) {
        return true;
      }
    }
  } catch (_) {
    return false;
  }
  return false;
}

function renderVideoStreamingServiceControl(video, playback = {}, selectedMode = "") {
  const options = videoStreamingOptions(playback);
  if (!options.length || playback?.mode === "e2ee_stream_v2" || playback?.mode === "e2ee_direct") return "";
  const selected = selectedMode || videoSelectedServiceMode(video, playback);
  return `
    <div class="video-quality-control video-service-mode-control" id="video-service-mode-control">
      <label for="video-service-mode-select">方案</label>
      <select id="video-service-mode-select">
        ${options.map((option) => `
          <option value="${sanitize(option.mode)}"${option.mode === selected ? " selected" : ""}${option.available ? "" : " disabled"}>
            ${sanitize(option.tier ? `${option.tier} · ${option.label}` : option.label)}
          </option>
        `).join("")}
      </select>
      <span class="drive-card-sub">${sanitize((options.find((option) => option.mode === selected) || options[0] || {}).summary || "")}</span>
    </div>
  `;
}

function bindVideoStreamingServiceControl(video, playback = {}) {
  const select = $("video-service-mode-select");
  if (!select) return;
  select.addEventListener("change", () => {
    saveVideoSelectedServiceMode(video, select.value);
    openVideoDetail(video?.id || videoState.current?.id || 0);
  });
}

function videoPublishedStreamingModes(video, playback = {}) {
  const raw = Array.isArray(playback?.published_streaming_modes)
    ? playback.published_streaming_modes
    : (Array.isArray(video?.streaming_modes) ? video.streaming_modes : []);
  const modes = raw.map((mode) => String(mode || "").trim()).filter(Boolean);
  return modes.length ? modes : ["direct"];
}

function renderVideoStreamingModeSettings(video, playback = {}) {
  if (!video?.can_edit) return "";
  const selected = new Set(videoPublishedStreamingModes(video, playback));
  const rows = [
    ["realtime_proxy", "Standard 即時轉封裝", "播放時即時輸出 fragmented MP4；適合原檔音訊或容器不友善但不想先轉 HLS。"],
    ["prepared_hls", "HLS 預處理", "會產生額外串流檔案；只在你明確開啟時排程，適合高相容性播放。"],
  ];
  return `
    <details class="drive-collapsible-panel video-streaming-mode-settings">
      <summary>
        <span>
          <span class="drive-card-title">串流方式設定</span>
          <span class="drive-card-sub">影片擁有者可事後增加或減少觀看者可用的串流方案。</span>
        </span>
      </summary>
      <div class="drive-collapsible-body">
        <div class="video-streaming-mode-choice-grid">
          ${rows.map(([mode, label, help]) => `
            <label class="video-streaming-mode-choice">
              <input type="checkbox" name="video-streaming-mode-choice" value="${sanitize(mode)}"${selected.has(mode) ? " checked" : ""} />
              <span>
                <strong>${sanitize(label)}</strong>
                <small>${sanitize(help)}</small>
              </span>
            </label>
          `).join("")}
        </div>
        <div class="field-help">至少保留一種串流方式。關閉 HLS 不會刪除既有 HLS 產物，只是不再提供給觀看者；重新開啟時會沿用已完成產物，沒有產物才排程處理。</div>
        <div class="drive-file-actions compact-actions" style="justify-content:flex-start;margin-top:.65rem;">
          <button class="btn btn-sm btn-primary" type="button" data-video-streaming-modes-save="${Number(video.id || 0)}">儲存串流方式</button>
        </div>
      </div>
    </details>
  `;
}

async function updateVideoStreamingModes(videoId) {
  const id = Number(videoId || videoState.current?.id || 0);
  if (!id) return;
  const modes = Array.from(document.querySelectorAll('input[name="video-streaming-mode-choice"]:checked'))
    .map((input) => String(input.value || "").trim())
    .filter(Boolean);
  if (!modes.length) {
    videoMsg("請至少保留一種串流方式。", false);
    return;
  }
  const button = document.querySelector(`[data-video-streaming-modes-save="${id}"]`);
  if (button) button.disabled = true;
  try {
    const res = await apiFetch(API + `/videos/${encodeURIComponent(id)}/streaming-modes`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ streaming_modes: modes }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    const currentMode = videoSelectedServiceMode(videoState.current || { id }, json.playback || {});
    if (!modes.includes(currentMode)) {
      saveVideoSelectedServiceMode(videoState.current || { id }, modes[0] || "realtime_proxy");
    }
    videoMsg(json.stream_queued ? "串流方式已更新，HLS 已排程處理。" : "串流方式已更新。", true);
    await openVideoDetail(id);
  } catch (err) {
    videoMsg(err.message || "串流方式更新失敗", false);
  } finally {
    if (button) button.disabled = false;
  }
}

function playbackSourceForVideo(video, playback, selectedMode = "") {
  if (playback?.mode === "e2ee_stream_v2") {
    return {
      mode: "e2ee_stream_v2",
      src: "",
      statusText: "正在使用 E2EE Streaming v2：密文分段下載、瀏覽器端解密；若裝置不支援會退回舊版完整解密播放。",
    };
  }
  if (playback?.mode === "e2ee_direct") {
    return {
      mode: "e2ee_direct",
      src: "",
      statusText: "端到端加密影音會在瀏覽器端完整解密播放，速度會較慢。",
    };
  }
  const requestedMode = selectedMode || videoSelectedServiceMode(video, playback || {});
  if (requestedMode === "realtime_proxy") {
    const proxy = playback?.realtime_proxy || {};
    const url = proxy.available === false ? "" : videoRealtimeProxyUrl(playback, 0);
    if (!url) {
      return {
        mode: "waiting_stream",
        src: "",
        statusText: proxy.reason || "Standard 即時轉封裝目前不可用。",
      };
    }
    return {
      mode: "realtime_proxy",
      src: url,
      statusText: "目前使用 Standard 即時轉封裝；伺服器會即時轉出瀏覽器較好播放的音訊。",
    };
  }
  if (!playback || playback.mode !== "hls") {
    return {
      mode: "waiting_stream",
      src: "",
      statusText: playback?.stream_warning || "HLS 尚未就緒，且即時轉封裝目前不可用。",
    };
  }
  const preferredVariant = preferredVideoQualityVariant(playback);
  const preferredHlsUrl = preferredVariant?.playlistUrl || playback.master_url || "";
  if (browserSupportsNativeHls(video.media_type)) {
    return {
      mode: "hls_native",
      src: preferredHlsUrl || "",
      statusText: preferredVariant
        ? `Safari / 原生 HLS 已啟用，預設 ${preferredVariant.label}。`
        : "Safari / 原生 HLS 已啟用。",
    };
  }
  if (playback.master_url) {
    return {
      mode: "hls_js",
      src: "",
      masterUrl: playback.master_url,
      fallbackUrl: "",
      statusText: "桌機瀏覽器將使用內建 HLS.js 播放。",
    };
  }
  return {
    mode: "waiting_stream",
    src: "",
    statusText: playback.stream_warning || "HLS 尚未就緒，且即時轉封裝目前不可用。",
  };
}

function humanVideoStreamStatus(playback) {
  const status = playback?.status || {};
  const streamStatus = String(status.status || "").trim();
  if (streamStatus === "direct_only") return status.error_message || "此影音只支援瀏覽器端解密播放。";
  if (streamStatus === "ready") return "HLS 串流已就緒";
  if (streamStatus === "processing") return "HLS 串流準備中";
  if (streamStatus === "failed") return `HLS 串流失敗：${status.error_message || "請稍後重試"}`;
  if (streamStatus === "unavailable") return status.error_message || "目前檔案無法建立伺服器端串流衍生檔";
  if (streamStatus === "pending") return "目前尚未建立 HLS 串流，可先用即時轉封裝播放";
  return "";
}

async function prepareVideoStream(fileId, videoId) {
  if (!fileId) return videoMsg("找不到對應影音檔案", false);
  const button = document.querySelector(`[data-video-prepare-stream="${String(fileId)}"]`);
  if (button) button.disabled = true;
  try {
    const res = await apiFetch(`/api/media/${encodeURIComponent(fileId)}/prepare-stream`, {
      method: "POST",
      credentials: "same-origin",
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    videoMsg(json.msg || "HLS 串流已排入背景處理；你可以先做別的事，進度會顯示在任務中心，完成後會通知上傳者。", true);
    await openVideoDetail(videoId);
  } catch (err) {
    videoMsg(err.message || "HLS 串流準備失敗", false);
  } finally {
    if (button) button.disabled = false;
  }
}

async function updateVideoShareLink(video, options = {}) {
  const payload = {};
  if (Object.prototype.hasOwnProperty.call(options, "share_password")) payload.share_password = options.share_password;
  if (Object.prototype.hasOwnProperty.call(options, "share_wrapped_file_key_envelope")) payload.share_wrapped_file_key_envelope = options.share_wrapped_file_key_envelope;
  if (Object.prototype.hasOwnProperty.call(options, "share_expires_at")) payload.share_expires_at = options.share_expires_at;
  if (Object.prototype.hasOwnProperty.call(options, "share_max_views")) payload.share_max_views = options.share_max_views;
  if (options.regenerate) payload.regenerate = true;
  const res = await apiFetch(`/api/videos/${encodeURIComponent(video.id)}/share-link`, {
    method: "PUT",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
  return json;
}

function videoShareStateSummary(video) {
  const share = video?.share_link || null;
  if (!share || !share.url) {
    return {
      state: "missing",
      label: "尚未建立分享連結",
      remaining: "剩餘觀看次數：尚未啟用",
    };
  }
  const state = String(share.state || "active");
  const remainingText = Number(share.max_views || 0) > 0
    ? `剩餘觀看次數：${Number(share.remaining_views || 0)} / ${Number(share.max_views || 0)}`
    : "剩餘觀看次數：不限";
  return {
    state,
    label: share.state_message || "分享連結有效",
    remaining: remainingText,
  };
}

function videoNeedsE2eeShareEnvelope(video, options = {}) {
  const visibility = String(video?.visibility || "");
  const isE2ee = String(video?.cloud_privacy_mode || "") === "e2ee";
  if (visibility !== "unlisted" || !isE2ee) return false;
  if (options.regenerate) return true;
  if (!video?.share_url) return true;
  return false;
}

async function saveVideoShareSettings(video, { clearPassword = false, regenerate = false } = {}) {
  if (!video?.id || video?.visibility !== "unlisted") return;
  const passwordInput = $("video-share-password-manage");
  const expiresInput = $("video-share-expires-at-manage");
  const maxViewsInput = $("video-share-max-views-manage");
  const button = $("video-share-save-btn");
  const payload = {
    share_expires_at: typeof getShareExpiryPickerValue === "function"
      ? getShareExpiryPickerValue(expiresInput || "video-share-expires-at-manage")
      : (expiresInput?.value || "").trim(),
    share_max_views: (maxViewsInput?.value || "").trim(),
  };
  if (clearPassword) {
    payload.share_password = "";
  } else {
    const passwordValue = (passwordInput?.value || "").trim();
    if (passwordValue) payload.share_password = passwordValue;
  }
  let e2eeShare = null;
  if (videoNeedsE2eeShareEnvelope(video, { regenerate })) {
    try {
      e2eeShare = await prepareVideoE2eeShareArtifacts(video.cloud_file_id);
      payload.share_wrapped_file_key_envelope = e2eeShare.share_wrapped_file_key_envelope;
    } catch (err) {
      return videoMsg(err.message || "E2EE 分享授權建立失敗", false);
    }
  }
  if (regenerate) payload.regenerate = true;
  if (button) button.disabled = true;
  try {
    const json = await updateVideoShareLink(video, payload);
    if (video.share_url) forgetRememberedVideoShareFragment(video.share_url);
    if (e2eeShare && json.share_link?.url) {
      rememberVideoShareFragment(json.share_link.url, e2eeShare.share_fragment_key);
      try {
        await uploadVideoE2eeStreamV2Package(video.cloud_file_id, e2eeShare);
        await uploadVideoE2eeDerivativePackages(video.cloud_file_id, e2eeShare);
      } catch (err) {
        if (e2eeShare.local_job_id) {
          updateVideoUploadLiveJob(e2eeShare.local_job_id, {
            status: "failed",
            progress_percent: 100,
            stage: "failed",
            stage_detail: err.message || "E2EE Streaming v2 建立失敗",
            error_message: err.message || "E2EE Streaming v2 建立失敗",
          });
          clearVideoE2eeLocalTask(e2eeShare.local_job_id);
        }
        videoMsg(`分享設定已更新，但 E2EE Streaming v2 建立失敗：${err.message || "請稍後重試"}`, false);
      }
    }
    if (passwordInput) passwordInput.value = "";
    videoMsg(regenerate ? "分享連結與設定已更新。" : "分享設定已儲存。", true);
    await loadVideos(videoState.sort);
    await openVideoDetail(video.id);
  } catch (err) {
    videoMsg(err.message || "分享設定更新失敗", false);
  } finally {
    if (button) button.disabled = false;
  }
}

async function regenerateVideoShareLink(video) {
  if (!video?.id || video?.visibility !== "unlisted") return;
  await saveVideoShareSettings(video, { regenerate: true });
}

async function revokeVideoShareLink(video) {
  if (!video?.id || !video?.share_url) return;
  try {
    const res = await apiFetch(`/api/videos/${encodeURIComponent(video.id)}/share-link`, {
      method: "DELETE",
      credentials: "same-origin",
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    forgetRememberedVideoShareFragment(video.share_url);
    videoMsg("分享連結已撤銷。", true);
    await loadVideos(videoState.sort);
    await openVideoDetail(video.id);
  } catch (err) {
    videoMsg(err.message || "撤銷分享連結失敗", false);
  }
}

async function hydrateVideoE2eePlayer(video, playback, sessionId) {
  const player = $("video-player");
  if (!player) return;
  if (!getCsrfToken()) {
    await fetchCsrfToken();
  }
  const csrf = getCsrfToken() || "";
  const decrypted = await decryptVideoE2eePlaybackBlob(video, playback, csrf);
  if (!decrypted?.blob) {
    throw new Error("E2EE 影音解密播放失敗");
  }
  if (sessionId !== videoState.playbackSessionId) return;
  destroyCurrentVideoPlaybackArtifacts();
  videoState.currentObjectUrl = URL.createObjectURL(decrypted.blob);
  player.src = videoState.currentObjectUrl;
  const status = $("video-playback-status");
  if (status) {
    status.textContent = "已在瀏覽器端以原始 E2EE 密碼解密播放；本次登入 session 內密碼會暫存在瀏覽器記憶體。";
  }
}

function playerTimeBuffered(player, timeSeconds) {
  if (!player?.buffered) return false;
  const target = Number(timeSeconds || 0);
  for (let i = 0; i < player.buffered.length; i += 1) {
    if (target >= player.buffered.start(i) && target <= player.buffered.end(i)) return true;
  }
  return false;
}

function videoSupportsE2eeStreamV2() {
  return Boolean(window.MediaSource && window.Worker && window.crypto?.subtle);
}

function createVideoE2eeStreamWorker() {
  return new Worker(VIDEO_E2EE_STREAM_V2_WORKER_URL);
}

function decryptVideoE2eeChunkWithWorker(worker, keyBytes, nonce, ciphertext) {
  return new Promise((resolve, reject) => {
    const id = `${Date.now()}:${Math.random().toString(16).slice(2)}`;
    const keyBuffer = keyBytes.buffer.slice(0);
    const handleMessage = (event) => {
      const payload = event?.data || {};
      if (payload.id !== id) return;
      worker.removeEventListener("message", handleMessage);
      if (payload.type === "decrypt-chunk-ok") {
        resolve(payload.plaintext);
      } else {
        reject(new Error(payload.message || "E2EE Streaming v2 chunk 解密失敗"));
      }
    };
    worker.addEventListener("message", handleMessage);
    worker.postMessage(
      {
        type: "decrypt-chunk",
        id,
        keyBytes: keyBuffer,
        nonce,
        ciphertext,
      },
      [keyBuffer, ciphertext]
    );
  });
}

function appendSourceBufferAsync(sourceBuffer, payload) {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      sourceBuffer.removeEventListener("updateend", onEnd);
      sourceBuffer.removeEventListener("error", onErr);
    };
    const onEnd = () => {
      cleanup();
      resolve();
    };
    const onErr = () => {
      cleanup();
      reject(new Error("MediaSource append 失敗"));
    };
    sourceBuffer.addEventListener("updateend", onEnd, { once: true });
    sourceBuffer.addEventListener("error", onErr, { once: true });
    sourceBuffer.appendBuffer(payload);
  });
}

function videoE2eeChunkIndexForTime(manifest, timeSeconds) {
  const chunkCount = Number(manifest?.chunk_count || 0);
  const duration = Number(manifest?.duration_hint || 0);
  const target = Number(timeSeconds || 0);
  if (!Number.isFinite(chunkCount) || chunkCount <= 0 || !Number.isFinite(duration) || duration <= 0) return null;
  if (!Number.isFinite(target) || target <= 0) return 0;
  return Math.max(0, Math.min(chunkCount - 1, Math.floor((target / duration) * chunkCount)));
}

function pruneVideoE2eeChunkCache(cache, keepAroundIndex) {
  if (!cache || cache.size <= VIDEO_E2EE_STREAM_V2_CACHE_LIMIT) return;
  const keep = Number(keepAroundIndex || 0);
  const keys = Array.from(cache.keys()).sort((a, b) => Math.abs(a - keep) - Math.abs(b - keep));
  const keepSet = new Set(keys.slice(0, VIDEO_E2EE_STREAM_V2_CACHE_LIMIT));
  for (const key of cache.keys()) {
    if (!keepSet.has(key)) cache.delete(key);
  }
}

async function fetchVideoE2eeChunkWithRetry(url, retries = VIDEO_E2EE_STREAM_V2_MAX_RETRIES) {
  let lastError = null;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      const chunkRes = await apiFetch(url, { credentials: "same-origin" });
      if (!chunkRes.ok) {
        const payload = await chunkRes.json().catch(() => ({}));
        throw new Error(payload.msg || `HTTP ${chunkRes.status}`);
      }
      return chunkRes.arrayBuffer();
    } catch (err) {
      lastError = err;
      if (attempt >= retries) break;
      await new Promise((resolve) => setTimeout(resolve, 200 * (attempt + 1)));
    }
  }
  throw lastError || new Error("E2EE Streaming v2 分段下載失敗");
}

async function fetchVideoPlaybackE2eeKey(video, playback, csrf) {
  const url = playback?.e2ee_key_url || "";
  if (url) {
    const res = await apiFetch(url, {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok || !json.e2ee) throw new Error(json.msg || "E2EE 解密資訊讀取失敗");
    return json.e2ee;
  }
  return fetchDriveE2eeKey(video.cloud_file_id, csrf);
}

async function fetchVideoPlaybackE2eeCiphertext(video, playback, csrf) {
  const url = playback?.ciphertext_url || playback?.fallback_url || "";
  if (url) {
    const res = await apiFetch(url, {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    if (!res.ok) {
      const json = await res.json().catch(() => ({}));
      throw new Error(json.msg || "E2EE 密文讀取失敗");
    }
    return res.blob();
  }
  return fetchDriveE2eeCiphertext(video.cloud_file_id, csrf);
}

async function decryptVideoE2eePlaybackBlob(video, playback, csrf) {
  const e2ee = await fetchVideoPlaybackE2eeKey(video, playback, csrf);
  const ciphertext = await fetchVideoPlaybackE2eeCiphertext(video, playback, csrf);
  const fileId = e2ee.file_id || video.cloud_file_id;
  const promptText = "請輸入此 E2EE 影音的原始加密密碼。公開影音仍只會在瀏覽器端解密，伺服器無法看到明文。";
  for (const passphrase of getDriveE2eeSessionPassphraseCandidates(fileId)) {
    try {
      const decrypted = await decryptDriveE2eeBlob(ciphertext, e2ee, passphrase);
      rememberDriveE2eeSessionPassphrase(fileId, passphrase);
      return decrypted;
    } catch (_) {
      forgetDriveE2eeSessionPassphrase(fileId);
    }
  }
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const passphrase = await getDriveE2eeSessionPassphrase(fileId, promptText, { force: true, allowPrompt: true });
    if (!passphrase) return null;
    try {
      const decrypted = await decryptDriveE2eeBlob(ciphertext, e2ee, passphrase);
      rememberDriveE2eeSessionPassphrase(fileId, passphrase);
      return decrypted;
    } catch (err) {
      forgetDriveE2eeSessionPassphrase(fileId);
      if (attempt > 0) throw err;
      alert("E2EE 密碼不正確或檔案已損壞，請重新輸入。");
    }
  }
  return null;
}

async function resolveVideoE2eePlaybackKey(video, playback) {
  if (!getCsrfToken()) {
    await fetchCsrfToken();
  }
  const csrf = getCsrfToken() || "";
  const e2ee = await fetchVideoPlaybackE2eeKey(video, playback, csrf);
  const fileId = e2ee.file_id || video.cloud_file_id;
  for (const passphrase of getDriveE2eeSessionPassphraseCandidates(fileId)) {
    try {
      const fileKey = await unwrapDriveFileKey(e2ee.encrypted_file_key, passphrase);
      rememberDriveE2eeSessionPassphrase(fileId, passphrase);
      return new Uint8Array(await window.crypto.subtle.exportKey("raw", fileKey));
    } catch (_) {
      forgetDriveE2eeSessionPassphrase(fileId);
    }
  }
  const passphrase = await getDriveE2eeSessionPassphrase(
    fileId,
    "請輸入此 E2EE 影音的原始加密密碼。公開影音與 strict E2EE Streaming v2 都只在瀏覽器端解密，伺服器無法看到明文。",
    { force: true, allowPrompt: true }
  );
  if (!passphrase) throw new Error("E2EE 影音播放需要原始加密密碼。");
  const fileKey = await unwrapDriveFileKey(e2ee.encrypted_file_key, passphrase);
  rememberDriveE2eeSessionPassphrase(fileId, passphrase);
  return new Uint8Array(await window.crypto.subtle.exportKey("raw", fileKey));
}

async function attachVideoE2eeStreamV2Player(video, playback, sessionId) {
  const player = $("video-player");
  if (!player) return;
  if (!videoSupportsE2eeStreamV2()) {
    setVideoPlaybackStatus("目前裝置不支援 E2EE Streaming v2，已退回舊版完整解密播放。", false);
    await hydrateVideoE2eePlayer(video, playback, sessionId);
    return;
  }
  let activeVariant = selectedVideoE2eeQualityVariant(playback);
  let activeManifestUrl = activeVariant?.manifestUrl || playback.manifest_url || "";
  let activeChunkUrlTemplate = activeVariant?.chunkUrlTemplate || playback.chunk_url_template || "";
  let manifestRes = activeManifestUrl ? await apiFetch(activeManifestUrl, { credentials: "same-origin" }) : null;
  let manifestJson = manifestRes ? await manifestRes.json().catch(() => ({})) : {};
  if ((!manifestRes?.ok || manifestJson.available === false) && activeVariant?.name !== "original" && playback.manifest_url) {
    activeVariant = videoPlaybackQualityOptions(playback).find((variant) => variant.name === "original") || null;
    activeManifestUrl = activeVariant?.manifestUrl || playback.manifest_url || "";
    activeChunkUrlTemplate = activeVariant?.chunkUrlTemplate || playback.chunk_url_template || "";
    manifestRes = await apiFetch(activeManifestUrl, { credentials: "same-origin" });
    manifestJson = await manifestRes.json().catch(() => ({}));
    setVideoPlaybackStatus("選擇的 E2EE 省流量畫質尚未建立，已回到原始加密串流。", false);
  }
  if (!manifestRes?.ok || manifestJson.available === false) {
    setVideoPlaybackStatus(manifestJson.msg || "此 strict E2EE 影音尚未建立 Streaming v2 manifest，已退回舊版完整解密播放。", false);
    await hydrateVideoE2eePlayer(video, playback, sessionId);
    return;
  }
  const rawKeyBytes = await resolveVideoE2eePlaybackKey(video, playback);
  destroyCurrentVideoPlaybackArtifacts();
  const mediaSource = new MediaSource();
  const objectUrl = URL.createObjectURL(mediaSource);
  videoState.currentObjectUrl = objectUrl;
  player.src = objectUrl;
  setVideoPlaybackStatus(`正在使用 E2EE Streaming v2${activeVariant?.label ? ` · ${activeVariant.label}` : ""}：密文分段下載、瀏覽器端 Web Worker 解密，伺服器無法看到明文。`, false);
  const worker = createVideoE2eeStreamWorker();
  let nextChunk = 0;
  let closed = false;
  let sourceBuffer = null;
  let pendingSeekChunk = null;
  const chunkCache = new Map();
  const cleanup = () => {
    if (closed) return;
    closed = true;
    try { worker.terminate(); } catch (_) {}
  };
  const fallbackToFull = async (reason, seekTarget = null) => {
    cleanup();
    setVideoPlaybackStatus(reason, false);
    await hydrateVideoE2eePlayer(video, playback, sessionId);
    if (seekTarget !== null) {
      const onLoaded = () => {
        player.removeEventListener("loadedmetadata", onLoaded);
        try { player.currentTime = seekTarget; } catch (_) {}
      };
      player.addEventListener("loadedmetadata", onLoaded);
    }
  };
  player.addEventListener("seeking", () => {
    if (closed || !sourceBuffer) return;
    const target = Number(player.currentTime || 0);
    const targetChunk = videoE2eeChunkIndexForTime(manifestJson, target);
    if (!playerTimeBuffered(player, target) && targetChunk !== null && targetChunk >= nextChunk) {
      pendingSeekChunk = targetChunk;
      setVideoPlaybackStatus(`快轉目標尚未緩衝，正在以 Streaming v2 追上分段 ${targetChunk + 1}。`, false);
      return;
    }
    if (!playerTimeBuffered(player, target) && nextChunk < Number(manifestJson.chunk_count || 0)) {
      fallbackToFull("偵測到尚未緩衝區段的快轉，已退回舊版完整解密播放以確保可用性。", target).catch((err) => {
        setVideoPlaybackStatus(err?.message || "E2EE 影音快轉 fallback 失敗", true);
      });
    }
  });
  mediaSource.addEventListener("sourceopen", () => {
    if (closed || sessionId !== videoState.playbackSessionId) {
      cleanup();
      return;
    }
    try {
      sourceBuffer = mediaSource.addSourceBuffer(manifestJson.content_type || playback.status?.content_type || video.cloud_mime_type || "video/mp4");
    } catch (err) {
      fallbackToFull("目前裝置無法以 MediaSource 播放此 strict E2EE 影音，已退回舊版完整解密播放。").catch(() => {});
      return;
    }
    const pump = async () => {
      if (closed || sessionId !== videoState.playbackSessionId || !sourceBuffer) return;
      if (nextChunk >= Number(manifestJson.chunk_count || 0)) {
        if (mediaSource.readyState === "open" && !sourceBuffer.updating) {
          try { mediaSource.endOfStream(); } catch (_) {}
        }
        cleanup();
        setVideoPlaybackStatus("正在使用 E2EE Streaming v2；若裝置或格式不支援快轉，系統會退回舊版完整解密播放。", false);
        return;
      }
      const chunkMeta = manifestJson.chunks?.[nextChunk];
      if (!chunkMeta) {
        fallbackToFull("E2EE Streaming v2 chunk metadata 缺失，已退回舊版完整解密播放。").catch(() => {});
        return;
      }
      try {
        let plaintext = chunkCache.get(Number(chunkMeta.chunk_index));
        if (!plaintext) {
          const chunkUrl = activeChunkUrlTemplate.replace("__INDEX__", String(chunkMeta.chunk_index));
          const cipher = await fetchVideoE2eeChunkWithRetry(chunkUrl);
          plaintext = await decryptVideoE2eeChunkWithWorker(worker, new Uint8Array(rawKeyBytes), chunkMeta.nonce, cipher);
          chunkCache.set(Number(chunkMeta.chunk_index), plaintext);
          pruneVideoE2eeChunkCache(chunkCache, Number(chunkMeta.chunk_index));
        }
        await appendSourceBufferAsync(sourceBuffer, new Uint8Array(plaintext));
        nextChunk += 1;
        if (pendingSeekChunk !== null && nextChunk > pendingSeekChunk) pendingSeekChunk = null;
        const seekNote = pendingSeekChunk !== null ? "，正在追上快轉目標" : "";
        setVideoPlaybackStatus(`正在使用 E2EE Streaming v2：已解密分段 ${nextChunk} / ${manifestJson.chunk_count}${seekNote}。`, false);
        queueMicrotask(() => { pump().catch(() => {}); });
      } catch (err) {
        fallbackToFull(`E2EE Streaming v2 分段播放失敗，已退回舊版完整解密播放。${err?.message ? ` (${err.message})` : ""}`).catch(() => {});
      }
    };
    pump().catch((err) => {
      fallbackToFull(err?.message || "E2EE Streaming v2 初始化失敗").catch(() => {});
    });
  }, { once: true });
}

function fallbackVideoPlayerToDirect(player, playback, message, bad = false) {
  destroyCurrentVideoPlaybackArtifacts();
  const proxy = playback?.realtime_proxy || {};
  const fallbackSrc = proxy.available === false ? "" : videoRealtimeProxyUrl(playback, 0);
  if (fallbackSrc) {
    player.src = fallbackSrc;
    if (typeof player.load === "function") player.load();
    setVideoPlaybackStatus(message || "HLS 初始化失敗，已改用即時轉封裝。", bad);
    return;
  }
  player.removeAttribute("src");
  if (typeof player.load === "function") player.load();
  setVideoPlaybackStatus(message || "HLS 初始化失敗，且即時轉封裝目前不可用。", true);
}

function realtimeProxyMseContentType(playback = {}) {
  return String(playback?.realtime_proxy?.mse_content_type || 'video/mp4; codecs="avc1.42E01E, mp4a.40.2"').trim();
}

function realtimeProxyMediaSourceCtor() {
  return window.ManagedMediaSource || window.MediaSource || window.WebKitMediaSource;
}

function realtimeProxyMediaSourceName() {
  if (window.ManagedMediaSource) return "ManagedMediaSource";
  if (window.MediaSource) return "MediaSource";
  if (window.WebKitMediaSource) return "WebKitMediaSource";
  return "";
}

function realtimeProxyMediaSourceCapabilities(playback = {}) {
  const SourceApi = realtimeProxyMediaSourceCtor();
  const mime = realtimeProxyMseContentType(playback);
  const supported = !!(
    SourceApi
    && typeof SourceApi.isTypeSupported === "function"
    && SourceApi.isTypeSupported(mime)
  );
  return {
    source_api: SourceApi,
    selected_source_api: realtimeProxyMediaSourceName(),
    media_source_available: Boolean(window.MediaSource),
    webkit_media_source_available: Boolean(window.WebKitMediaSource),
    managed_media_source_available: Boolean(window.ManagedMediaSource),
    mime_codec_string: mime,
    is_type_supported_result: supported,
  };
}

function browserSupportsRealtimeMse(playback = {}) {
  return realtimeProxyMediaSourceCapabilities(playback).is_type_supported_result;
}

function waitForSourceBufferIdle(sourceBuffer) {
  if (!sourceBuffer || !sourceBuffer.updating) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      sourceBuffer.removeEventListener("updateend", onEnd);
      sourceBuffer.removeEventListener("error", onErr);
    };
    const onEnd = () => {
      cleanup();
      resolve();
    };
    const onErr = () => {
      cleanup();
      reject(new Error("MediaSource 更新失敗"));
    };
    sourceBuffer.addEventListener("updateend", onEnd, { once: true });
    sourceBuffer.addEventListener("error", onErr, { once: true });
  });
}

async function attachVideoRealtimeProxyMsePlayer(player, playback = {}, playbackSource = {}, sessionId = 0) {
  const src = String(playbackSource?.src || "").trim();
  const realtimeStartSeconds = Number(playbackSource?.startSeconds || videoRealtimeProxyStartSeconds(src) || 0);
  const mseType = realtimeProxyMseContentType(playback);
  const caps = realtimeProxyMediaSourceCapabilities(playback);
  if (!src) {
    renderVideoStreamDebugPanel({
      realtime_state: "missing_source",
      realtime_error: "missing src",
      mse_content_type: mseType,
      mime_codec_string: caps.mime_codec_string,
      selected_source_api: caps.selected_source_api,
    });
    setVideoPlaybackStatus(playbackSource?.statusText || "Standard 即時轉封裝目前沒有可播放來源。", true);
    return;
  }
  if (!caps.is_type_supported_result) {
    renderVideoStreamDebugPanel({
      realtime_state: "mse_not_supported",
      realtime_error: "source api or codec is not supported",
      mse_supported: false,
      mse_content_type: mseType,
      mime_codec_string: caps.mime_codec_string,
      selected_source_api: caps.selected_source_api,
      media_source_available: caps.media_source_available,
      webkit_media_source_available: caps.webkit_media_source_available,
      managed_media_source_available: caps.managed_media_source_available,
      is_type_supported_result: caps.is_type_supported_result,
      disable_remote_playback: Boolean(player?.disableRemotePlayback),
    });
    setVideoPlaybackStatus("此裝置不支援即時轉封裝播放，需使用 HLS 預處理版本。", true);
    return;
  }
  destroyCurrentVideoPlaybackArtifacts();
  const MediaSourceCtor = caps.source_api;
  if (window.ManagedMediaSource && MediaSourceCtor === window.ManagedMediaSource && "disableRemotePlayback" in player) {
    player.disableRemotePlayback = true;
  }
  const mediaSource = new MediaSourceCtor();
  const objectUrl = URL.createObjectURL(mediaSource);
  const controller = new AbortController();
  videoState.currentRealtimeAbortController = controller;
  videoState.currentObjectUrl = objectUrl;
  videoState.realtimeBaseOffsetSeconds = 0;
  player.preload = "none";
  player.src = objectUrl;
  if (realtimeStartSeconds > 0) {
    videoState.realtimeSeekSuppressUntil = performance.now() + 2500;
  }
  if (typeof player.load === "function") player.load();
  setVideoPlaybackStatus(playbackSource.statusText || "正在初始化 Standard 即時轉封裝...", false);
  renderVideoStreamDebugPanel({
    realtime_state: "opening",
    realtime_src: src,
    realtime_start_seconds: realtimeStartSeconds,
    realtime_timeline_mode: "absolute",
    realtime_timeline_offset_seconds: 0,
    mse_supported: true,
    selected_source_api: caps.selected_source_api,
    media_source_api: caps.selected_source_api,
    media_source_available: caps.media_source_available,
    webkit_media_source_available: caps.webkit_media_source_available,
    managed_media_source_available: caps.managed_media_source_available,
    disable_remote_playback: Boolean(player.disableRemotePlayback),
    mse_content_type: mseType,
    mime_codec_string: caps.mime_codec_string,
    is_type_supported_result: caps.is_type_supported_result,
  });

  const seekHandler = () => {
    if (sessionId !== videoState.playbackSessionId) return;
    const target = Number(player.currentTime || 0);
    const now = performance.now();
    if (!Number.isFinite(target) || target <= 0.25) return;
    if (now < Number(videoState.realtimeSeekSuppressUntil || 0)) return;
    if (videoState.realtimeSeekRestarting) return;
    if (videoBufferedContains(player, target, 0.75)) return;
    const nextUrl = videoRealtimeProxyUrl(playback, target);
    if (!nextUrl) return;
    videoState.realtimeSeekRestarting = true;
    videoState.lastSeekAt = Date.now();
    videoState.lastSeekTarget = target;
    renderVideoStreamDebugPanel({
      realtime_state: "seek_restart",
      realtime_seek_target_seconds: target,
      realtime_seek_url: nextUrl,
    });
    setVideoPlaybackStatus(`正在從 ${Math.round(target)} 秒重新啟動 Standard 即時轉封裝。`, false);
    window.setTimeout(() => {
      if (sessionId !== videoState.playbackSessionId) {
        videoState.realtimeSeekRestarting = false;
        return;
      }
      attachVideoRealtimeProxyMsePlayer(
        player,
        playback,
        {
          mode: "realtime_proxy",
          src: nextUrl,
          startSeconds: target,
          statusText: `已跳到 ${Math.round(target)} 秒，正在重新接續 Standard 即時轉封裝。`,
        },
        sessionId
      ).finally(() => {
        videoState.realtimeSeekRestarting = false;
      });
    }, 0);
  };
  player.addEventListener("seeking", seekHandler);
  videoState.currentRealtimeSeekHandler = seekHandler;
  videoState.currentRealtimeSeekPlayer = player;

  mediaSource.addEventListener("sourceopen", async () => {
    if (sessionId !== videoState.playbackSessionId) return;
    let sourceBuffer = null;
    let totalBytes = 0;
    let appendedBytes = 0;
    let totalChunks = 0;
    let appendedChunks = 0;
    const startMs = performance.now();
    let firstChunkMs = 0;
    let lastRenderMs = 0;
    let realtimeSeekPlaybackStarted = realtimeStartSeconds <= 0;
    let realtimeTimestampOffsetApplied = realtimeStartSeconds <= 0;
    let realtimeTimestampOffsetError = "";
    const sourceBufferRanges = () => {
      const ranges = [];
      try {
        for (let index = 0; index < sourceBuffer.buffered.length; index += 1) {
          ranges.push([sourceBuffer.buffered.start(index), sourceBuffer.buffered.end(index)]);
        }
      } catch (_) {}
      return ranges;
    };
    const realtimeCurrentSeconds = () => Math.max(
      Number(player.currentTime || 0),
      Number(realtimeStartSeconds || 0)
    );
    const realtimeBufferedAheadSeconds = () => {
      const current = realtimeCurrentSeconds();
      let bufferedEnd = current;
      sourceBufferRanges().forEach((range) => {
        const start = Number(range[0] || 0);
        const end = Number(range[1] || 0);
        if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return;
        if (start <= current + 0.75 && end > current) bufferedEnd = Math.max(bufferedEnd, end);
      });
      return Math.max(0, bufferedEnd - current);
    };
    const waitForRealtimeBufferRoom = async () => {
      const maxAhead = 45;
      const resumeAhead = 25;
      let wasThrottled = false;
      while (
        sessionId === videoState.playbackSessionId
        && mediaSource.readyState === "open"
        && sourceBuffer
      ) {
        await waitForSourceBufferIdle(sourceBuffer);
        const ahead = realtimeBufferedAheadSeconds();
        if (ahead < maxAhead) {
          if (wasThrottled) {
            renderVideoStreamDebugPanel({
              realtime_state: "buffer_throttle_resume",
              realtime_buffer_ahead_seconds: ahead,
              mse_source_buffered_ranges: sourceBufferRanges(),
            });
          }
          return;
        }
        wasThrottled = true;
        renderVideoStreamDebugPanel({
          realtime_state: "buffer_throttle_wait",
          realtime_buffer_ahead_seconds: ahead,
          realtime_buffer_resume_below_seconds: resumeAhead,
          mse_source_buffered_ranges: sourceBufferRanges(),
        });
        while (
          sessionId === videoState.playbackSessionId
          && mediaSource.readyState === "open"
          && realtimeBufferedAheadSeconds() > resumeAhead
        ) {
          await new Promise((resolve) => window.setTimeout(resolve, 250));
        }
      }
    };
    const trimRealtimeSourceBuffer = async (aggressive = false) => {
      if (!sourceBuffer || mediaSource.readyState !== "open") return;
      await waitForSourceBufferIdle(sourceBuffer);
      const current = realtimeCurrentSeconds();
      const keepBehind = aggressive ? 8 : 45;
      const keepAhead = aggressive ? 90 : 240;
      const ranges = sourceBufferRanges();
      for (const range of ranges) {
        const start = Number(range[0] || 0);
        const end = Number(range[1] || 0);
        if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue;
        let removeStart = null;
        let removeEnd = null;
        if (end < current - keepBehind) {
          removeStart = start;
          removeEnd = end;
        } else if (start < current - keepBehind) {
          removeStart = start;
          removeEnd = current - keepBehind;
        } else if (start > current + keepAhead) {
          removeStart = start;
          removeEnd = end;
        } else if (aggressive && end > current + keepAhead && current + keepAhead > start + 0.01) {
          removeStart = current + keepAhead;
          removeEnd = end;
        }
        if (removeStart == null || removeEnd == null || removeEnd <= removeStart + 0.01) continue;
        try {
          sourceBuffer.remove(removeStart, removeEnd);
          await waitForSourceBufferIdle(sourceBuffer);
          renderVideoStreamDebugPanel({
            realtime_state: "buffer_evicted",
            realtime_buffer_evicted_start: removeStart,
            realtime_buffer_evicted_end: removeEnd,
            realtime_buffer_eviction_aggressive: aggressive,
          });
        } catch (_) {}
      }
    };
    const appendRealtimeChunk = async (value) => {
      await waitForRealtimeBufferRoom();
      await trimRealtimeSourceBuffer(false);
      await waitForSourceBufferIdle(sourceBuffer);
      try {
        sourceBuffer.appendBuffer(value);
        await waitForSourceBufferIdle(sourceBuffer);
        return;
      } catch (err) {
        const message = String(err?.message || err || "");
        const full = err?.name === "QuotaExceededError" || /full|quota|appendBuffer/i.test(message);
        if (!full) throw err;
        renderVideoStreamDebugPanel({
          realtime_state: "buffer_full_eviction",
          realtime_error: message,
          mse_source_buffered_ranges: sourceBufferRanges(),
        });
        await trimRealtimeSourceBuffer(true);
        await waitForRealtimeBufferRoom();
        await waitForSourceBufferIdle(sourceBuffer);
        sourceBuffer.appendBuffer(value);
        await waitForSourceBufferIdle(sourceBuffer);
      }
    };
    try {
      renderVideoStreamDebugPanel({
        realtime_state: "sourceopen",
        mse_ready_state: mediaSource.readyState,
        mse_content_type: mseType,
      });
      sourceBuffer = mediaSource.addSourceBuffer(mseType);
      try {
        sourceBuffer.mode = "segments";
      } catch (_) {
        sourceBuffer.mode = "segments";
      }
      if (realtimeStartSeconds > 0) {
        try {
          sourceBuffer.timestampOffset = realtimeStartSeconds;
          realtimeTimestampOffsetApplied = Math.abs(Number(sourceBuffer.timestampOffset || 0) - realtimeStartSeconds) < 0.25;
        } catch (err) {
          realtimeTimestampOffsetError = err?.message || "timestampOffset failed";
        }
        if (!realtimeTimestampOffsetApplied) {
          throw new Error(`此瀏覽器不支援即時轉封裝絕對時間軸：${realtimeTimestampOffsetError || "timestampOffset 無法套用"}`);
        }
      }
      const durationSeconds = videoPlaybackDurationSeconds(playback);
      if (durationSeconds > 0) {
        try {
          mediaSource.duration = Math.max(1, durationSeconds);
        } catch (_) {}
      }
      renderVideoStreamDebugPanel({
        realtime_state: "fetching",
        mse_ready_state: mediaSource.readyState,
        mse_source_buffer_mode: sourceBuffer.mode,
        mse_duration_seconds: mediaSource.duration,
        realtime_start_seconds: realtimeStartSeconds,
        realtime_timeline_mode: "absolute",
        realtime_timeline_offset_seconds: 0,
        realtime_timestamp_offset: sourceBuffer.timestampOffset,
        realtime_timestamp_offset_applied: realtimeTimestampOffsetApplied,
        realtime_timestamp_offset_error: realtimeTimestampOffsetError,
      });
      const response = await fetch(src, {
        credentials: "same-origin",
        signal: controller.signal,
      });
      if (!response.ok || !response.body || typeof response.body.getReader !== "function") {
        throw new Error(`HTTP ${response.status || "stream"}`);
      }
      const headerType = response.headers.get("X-Hackme-MSE-Content-Type") || "";
      const requestId = response.headers.get("X-Request-Id") || response.headers.get("X-Hackme-Request-Id") || "";
      renderVideoStreamDebugPanel({
        realtime_state: "connected",
        realtime_http_status: response.status,
        realtime_content_length: response.headers.get("Content-Length") || "",
        realtime_content_type: response.headers.get("Content-Type") || "",
        realtime_header_mse_content_type: headerType,
        realtime_request_id: requestId,
        mse_ready_state: mediaSource.readyState,
      });
      if (headerType && headerType !== mseType) {
        setVideoPlaybackStatus(`Standard 即時轉封裝已連線：${headerType}`, false);
      }
      const reader = response.body.getReader();
      while (sessionId === videoState.playbackSessionId) {
        await waitForRealtimeBufferRoom();
        const { done, value } = await reader.read();
        if (done) break;
        if (!value || !value.byteLength) continue;
        totalChunks += 1;
        totalBytes += value.byteLength;
        if (!firstChunkMs) firstChunkMs = Math.round(performance.now() - startMs);
        await appendRealtimeChunk(value);
        appendedChunks += 1;
        appendedBytes += value.byteLength;
        if (realtimeStartSeconds > 0 && appendedChunks === 1) {
          await waitForSourceBufferIdle(sourceBuffer);
          renderVideoStreamDebugPanel({
            realtime_state: "seek_stream_ready",
            realtime_start_seconds: realtimeStartSeconds,
            realtime_timeline_mode: "absolute",
            realtime_timeline_offset_seconds: 0,
            realtime_timestamp_offset: sourceBuffer.timestampOffset,
            realtime_timestamp_offset_applied: realtimeTimestampOffsetApplied,
          });
          videoState.realtimeSeekSuppressUntil = performance.now() + 1000;
        }
        if (realtimeStartSeconds > 0 && !realtimeSeekPlaybackStarted) {
          await waitForSourceBufferIdle(sourceBuffer);
          const ranges = sourceBufferRanges();
          const firstRange = ranges[0] || [];
          const firstRangeEnd = Number(firstRange[1] || 0);
          if (Number.isFinite(firstRangeEnd) && firstRangeEnd > realtimeStartSeconds + 0.05) {
            realtimeSeekPlaybackStarted = true;
            videoState.realtimeSeekSuppressUntil = performance.now() + 1000;
            try {
              player.currentTime = Math.max(0.001, realtimeStartSeconds + 0.001);
            } catch (_) {}
            try {
              await player.play();
            } catch (_) {}
            renderVideoStreamDebugPanel({
              realtime_state: "seek_playing_absolute",
              realtime_start_seconds: realtimeStartSeconds,
              realtime_timeline_mode: "absolute",
              realtime_timeline_offset_seconds: 0,
              realtime_timestamp_offset: sourceBuffer.timestampOffset,
              mse_source_buffered_ranges: ranges,
            });
          }
        }
        videoStreamDebugRecordChunk({
          totalBytes,
          appendedBytes,
          totalChunks,
          appendedChunks,
          firstChunkMs,
        });
        const now = performance.now();
        if (!lastRenderMs || now - lastRenderMs >= 500) {
          lastRenderMs = now;
          renderVideoStreamDebugPanel({
            realtime_state: "receiving",
            realtime_bytes_received: totalBytes,
            realtime_bytes_appended: appendedBytes,
            realtime_chunks_received: totalChunks,
            realtime_chunks_appended: appendedChunks,
            realtime_first_chunk_ms: firstChunkMs,
            realtime_elapsed_ms: Math.round(now - startMs),
            mse_ready_state: mediaSource.readyState,
            mse_source_buffered_ranges: sourceBufferRanges(),
          });
        }
      }
      await waitForSourceBufferIdle(sourceBuffer);
      if (mediaSource.readyState === "open") {
        try {
          mediaSource.endOfStream();
        } catch (_) {}
      }
      renderVideoStreamDebugPanel({
        realtime_state: "ended",
        realtime_bytes_received: totalBytes,
        realtime_bytes_appended: appendedBytes,
        realtime_chunks_received: totalChunks,
        realtime_chunks_appended: appendedChunks,
        realtime_first_chunk_ms: firstChunkMs,
        realtime_elapsed_ms: Math.round(performance.now() - startMs),
        mse_ready_state: mediaSource.readyState,
        mse_source_buffered_ranges: sourceBufferRanges(),
      });
    } catch (err) {
      if (controller.signal.aborted || sessionId !== videoState.playbackSessionId) return;
      if (mediaSource.readyState === "open") {
        try {
          mediaSource.endOfStream("decode");
        } catch (_) {}
      }
      renderVideoStreamDebugPanel({
        realtime_state: "error",
        realtime_error: err?.message || "unknown",
        realtime_bytes_received: totalBytes,
        realtime_bytes_appended: appendedBytes,
        realtime_chunks_received: totalChunks,
        realtime_chunks_appended: appendedChunks,
        realtime_first_chunk_ms: firstChunkMs,
        realtime_elapsed_ms: Math.round(performance.now() - startMs),
        mse_ready_state: mediaSource.readyState,
        mse_source_buffered_ranges: sourceBufferRanges(),
      });
      setVideoPlaybackStatus(`Standard 即時轉封裝 MediaSource 播放失敗：${err?.message || "unknown"}`, true);
    }
  }, { once: true });
}

function videoPlaybackQualityOptions(playback = {}) {
  const variants = Array.isArray(playback?.variants)
    ? playback.variants
    : (Array.isArray(playback?.status?.variants) ? playback.status.variants : []);
  return variants
    .filter((variant) => variant && variant.name)
    .map((variant) => {
      const height = Number(variant.height || 0);
      const label = variant.label
        || (variant.name === "original" ? (height ? `原畫質 ${height}p` : "原畫質") : (height ? `${height}p` : variant.name));
      const sizeBytes = videoQualitySizeBytes(variant, playback);
      const sizeLabel = sizeBytes > 0 ? videoFormatBytes(sizeBytes) : "";
      const displayLabel = sizeLabel ? `${label} · ${sizeLabel}` : label;
      return {
        name: String(variant.name || ""),
        label: String(displayLabel || variant.name || ""),
        baseLabel: String(label || variant.name || ""),
        sizeBytes,
        sizeLabel,
        height,
        bitrate: Number(variant.bitrate || 0),
        playlistUrl: String(variant.playlist_url || ""),
        manifestUrl: String(variant.manifest_url || ""),
        chunkUrlTemplate: String(variant.chunk_url_template || ""),
      };
    });
}

function videoQualitySizeBytes(variant = {}, playback = {}) {
  const candidates = [
    variant.size_bytes,
    variant.hls_size_bytes,
    variant.segments_total_bytes,
    variant.total_bytes,
    variant.byte_size,
  ];
  if (String(variant.name || "") === "original") {
    candidates.push(variant.source_size_bytes, playback.source_size_bytes, playback.status?.source_size_bytes);
  }
  for (const value of candidates) {
    const bytes = Number(value || 0);
    if (Number.isFinite(bytes) && bytes > 0) return bytes;
  }
  return 0;
}

function videoPlaybackSubtitles(playback = {}) {
  const tracks = Array.isArray(playback?.subtitles)
    ? playback.subtitles
    : (Array.isArray(playback?.status?.subtitles) ? playback.status.subtitles : []);
  return tracks
    .filter((track) => track && track.name && track.url)
    .map((track) => ({
      name: String(track.name || ""),
      label: String(track.label || track.language || "字幕"),
      language: String(track.language || "und"),
      url: String(track.url || ""),
      isDefault: !!track.is_default,
    }));
}

function videoPlaybackAudioTracks(playback = {}) {
  const tracks = Array.isArray(playback?.audio_tracks)
    ? playback.audio_tracks
    : (Array.isArray(playback?.status?.audio_tracks) ? playback.status.audio_tracks : []);
  return tracks
    .filter((track) => track && track.name)
    .map((track) => ({
      name: String(track.name || ""),
      label: String(track.label || track.language || track.name || "音軌"),
      language: String(track.language || "und"),
      playlistUrl: String(track.playlist_url || track.url || ""),
      streamIndex: Number(track.stream_index ?? -1),
      isDefault: !!track.is_default,
      selectable: track.selectable !== false,
      source: String(track.source || ""),
    }));
}

function syncVideoSubtitleTracks(player, playback = {}) {
  if (!player) return;
  Array.from(player.querySelectorAll('track[data-video-subtitle="1"]')).forEach((track) => track.remove());
  const tracks = videoPlaybackSubtitles(playback);
  tracks.forEach((track, index) => {
    const el = document.createElement("track");
    el.kind = "subtitles";
    el.label = track.label || track.language || "字幕";
    el.srclang = track.language || "und";
    el.src = videoSubtitleUrlWithShift(track.url, videoState.subtitleShiftMs);
    el.dataset.videoSubtitle = "1";
    if (track.isDefault || index === 0) el.default = true;
    player.appendChild(el);
  });
  window.setTimeout(() => applyVideoSubtitleTrackSelection(playback, { silent: true }), 0);
}

function clampSubtitleShiftMs(value) {
  const parsed = Number(value || 0);
  if (!Number.isFinite(parsed)) return 0;
  return Math.max(-3600000, Math.min(3600000, Math.round(parsed)));
}

function subtitleShiftSecondsValue(ms) {
  const seconds = clampSubtitleShiftMs(ms) / 1000;
  return Number.isInteger(seconds) ? String(seconds) : seconds.toFixed(1).replace(/\.0$/, "");
}

function videoSubtitleUrlWithShift(url, shiftMs) {
  const raw = String(url || "");
  if (!raw) return raw;
  const offset = clampSubtitleShiftMs(shiftMs);
  try {
    const parsed = new URL(raw, window.location.origin);
    if (offset) parsed.searchParams.set("shift_ms", String(offset));
    else parsed.searchParams.delete("shift_ms");
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch (_) {
    if (!offset) return raw;
    const separator = raw.includes("?") ? "&" : "?";
    return `${raw}${separator}shift_ms=${encodeURIComponent(String(offset))}`;
  }
}

function videoSubtitleShiftStorageKey(videoId) {
  return `hackme_web.video_subtitle_shift_ms.${String(videoId || "")}`;
}

function loadVideoSubtitleShiftMs(videoId) {
  try {
    return clampSubtitleShiftMs(localStorage.getItem(videoSubtitleShiftStorageKey(videoId)) || 0);
  } catch (_) {
    return 0;
  }
}

function saveVideoSubtitleShiftMs(videoId, value) {
  const offset = clampSubtitleShiftMs(value);
  try {
    const key = videoSubtitleShiftStorageKey(videoId);
    if (offset) localStorage.setItem(key, String(offset));
    else localStorage.removeItem(key);
  } catch (_) {}
  return offset;
}

function applyVideoSubtitleShift(video, playback, nextMs) {
  videoState.subtitleShiftMs = saveVideoSubtitleShiftMs(video?.id, nextMs);
  const input = $("video-subtitle-shift-seconds");
  if (input) input.value = subtitleShiftSecondsValue(videoState.subtitleShiftMs);
  const player = $("video-player");
  if (player) syncVideoSubtitleTracks(player, playback || {});
  setVideoPlaybackStatus(
    videoState.subtitleShiftMs
      ? `字幕時間校正：${subtitleShiftSecondsValue(videoState.subtitleShiftMs)} 秒。`
      : "字幕時間校正已重置。",
    false,
  );
}

function applyVideoSubtitleTrackSelection(playback = {}, options = {}) {
  const player = $("video-player");
  const select = $("video-subtitle-track-select");
  if (!player || !select) return;
  const subtitles = videoPlaybackSubtitles(playback);
  const selected = Math.max(-1, Math.min(subtitles.length - 1, Number(select.value || -1)));
  if (String(selected) !== String(select.value || "")) select.value = String(selected);
  Array.from(player.querySelectorAll('track[data-video-subtitle="1"]')).forEach((track, index) => {
    if (track.track) track.track.mode = index === selected ? "showing" : "disabled";
  });
  if (!options.silent) {
    setVideoPlaybackStatus(
      selected >= 0
        ? `字幕：${subtitles[selected]?.label || "字幕"}。`
        : "字幕已關閉。",
      false,
    );
  }
}

function bindVideoSubtitleShiftControls(video, playback = {}) {
  const select = $("video-subtitle-track-select");
  if (select) {
    select.addEventListener("change", () => applyVideoSubtitleTrackSelection(playback));
  }
  const input = $("video-subtitle-shift-seconds");
  if (!input) return;
  input.addEventListener("change", () => {
    applyVideoSubtitleShift(video, playback, Number(input.value || 0) * 1000);
  });
  document.querySelectorAll("[data-video-subtitle-shift-step]").forEach((button) => {
    button.addEventListener("click", () => {
      const step = Number(button.dataset.videoSubtitleShiftStep || 0);
      applyVideoSubtitleShift(video, playback, videoState.subtitleShiftMs + step);
    });
  });
  const reset = document.querySelector("[data-video-subtitle-shift-reset]");
  if (reset) {
    reset.addEventListener("click", () => applyVideoSubtitleShift(video, playback, 0));
  }
}

function preferredVideoQualityVariant(playback = {}) {
  const options = videoPlaybackQualityOptions(playback);
  if (!options.length) return null;
  const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
  if (connection?.saveData || Number(connection?.downlink || 0) > 0 && Number(connection.downlink) < 2) {
    const low = options.find((option) => Number(option.height || 0) === 480)
      || options.find((option) => Number(option.height || 0) === 360);
    if (low) return low;
  }
  const preferredName = String(playback?.default_quality || playback?.quality_policy?.default_quality || "").trim();
  if (preferredName) {
    const named = options.find((option) => option.name === preferredName);
    if (named) return named;
  }
  return options.find((option) => Number(option.height || 0) === 720)
    || options.find((option) => Number(option.height || 0) === 480)
    || options.find((option) => option.name !== "original" && option.name !== "audio")
    || options[0]
    || null;
}

function fallbackVideoQualityVariant(playback = {}) {
  const options = videoPlaybackQualityOptions(playback);
  const fallbackName = String(playback?.fallback_quality || playback?.quality_policy?.fallback_quality || "").trim();
  if (fallbackName) {
    const named = options.find((option) => option.name === fallbackName);
    if (named) return named;
  }
  return options.find((option) => Number(option.height || 0) === 480) || null;
}

function renderVideoQualityControl(playback = {}) {
  const options = videoPlaybackQualityOptions(playback);
  const audioTracks = videoPlaybackAudioTracks(playback);
  if (options.length < 2 && audioTracks.length < 2) return "";
  const preferred = preferredVideoQualityVariant(playback);
  const preferredName = preferred?.name || "";
  const defaultAudioIndex = Math.max(0, audioTracks.findIndex((track) => track.isDefault));
  const audioMarkup = audioTracks.length >= 2 ? `
      <label for="video-audio-track-select">選擇音軌</label>
      <select id="video-audio-track-select">
        ${audioTracks.map((track, index) => `<option value="${index}" data-track-name="${sanitize(track.name)}"${index === defaultAudioIndex ? " selected" : ""}>${sanitize(track.label)}</option>`).join("")}
      </select>
    ` : "";
  const qualityMarkup = options.length >= 2 ? `
      <label for="video-quality-select">畫質</label>
      <select id="video-quality-select">
        <option value="auto"${preferredName ? "" : " selected"}>自動</option>
        ${options.map((option) => `<option value="${sanitize(option.name)}"${option.name === preferredName ? " selected" : ""}>${sanitize(option.label)}</option>`).join("")}
      </select>
    ` : "";
  return `
    <div class="video-quality-control" id="video-quality-control">
      ${qualityMarkup}
      ${audioMarkup}
      <span class="drive-card-sub">預處理 HLS 支援多畫質與多音軌；串流衍生檔不佔用你的雲端硬碟容量。</span>
    </div>
  `;
}

function renderVideoRealtimeProxyControl(playback = {}) {
  const audioTracks = videoPlaybackAudioTracks(playback);
  if (audioTracks.length < 2) return "";
  const defaultAudioIndex = Math.max(0, audioTracks.findIndex((track) => track.isDefault));
  return `
    <div class="video-quality-control" id="video-realtime-proxy-control">
      <label for="video-audio-track-select">選擇音軌</label>
      <select id="video-audio-track-select">
        ${audioTracks.map((track, index) => `<option value="${index}" data-track-name="${sanitize(track.name)}"${index === defaultAudioIndex ? " selected" : ""}>${sanitize(track.label)}</option>`).join("")}
      </select>
      <span class="drive-card-sub">Standard 即時轉封裝一次輸出選定音軌；多人同時觀看會消耗即時 CPU。</span>
    </div>
  `;
}

function renderVideoE2eeQualityControl(playback = {}) {
  const options = videoPlaybackQualityOptions(playback).filter((option) => option.manifestUrl && option.chunkUrlTemplate);
  if (options.length < 2) return "";
  const preferred = preferredVideoQualityVariant({ ...playback, variants: options });
  return `
    <div class="video-quality-control" id="video-e2ee-quality-control">
      <label for="video-e2ee-quality-select">E2EE 畫質</label>
      <select id="video-e2ee-quality-select">
        ${options.map((option) => `<option value="${sanitize(option.name)}"${option.name === preferred?.name ? " selected" : ""}>${sanitize(option.label)}</option>`).join("")}
      </select>
      <span class="drive-card-sub">省流量畫質由發布者瀏覽器本機產生並加密上傳；伺服器沒有解密或轉檔。</span>
    </div>
  `;
}

function renderVideoSubtitleControls(video, playback = {}) {
  if (!video || video.media_type !== "video") return "";
  const subtitles = videoPlaybackSubtitles(playback);
  const canUpload = !!video.can_edit && playback?.mode !== "e2ee_stream_v2" && playback?.mode !== "e2ee_direct";
  if (!subtitles.length && !canUpload && !video.can_edit) return "";
  const defaultSubtitleIndex = subtitles.length
    ? Math.max(0, subtitles.findIndex((item) => item.isDefault))
    : -1;
  const selector = subtitles.length
    ? `
      <div class="video-quality-control">
        <label for="video-subtitle-track-select">選擇字幕</label>
        <select id="video-subtitle-track-select">
          <option value="-1">關閉字幕</option>
          ${subtitles.map((item, index) => {
            const label = `${item.label || "字幕"}${item.language ? ` · ${item.language}` : ""}`;
            return `<option value="${index}"${index === defaultSubtitleIndex ? " selected" : ""}>${sanitize(label)}</option>`;
          }).join("")}
        </select>
      </div>
    `
    : `<div class="drive-empty">尚無字幕軌，可上傳 SRT/VTT/ASS 字幕檔。</div>`;
  const shiftControl = subtitles.length
    ? `
      <div class="video-quality-control video-subtitle-shift-control">
        <label for="video-subtitle-shift-seconds">字幕延遲</label>
        <button class="btn btn-sm" type="button" data-video-subtitle-shift-step="-500">-0.5s</button>
        <input id="video-subtitle-shift-seconds" type="number" min="-3600" max="3600" step="0.1" value="${sanitize(subtitleShiftSecondsValue(videoState.subtitleShiftMs))}" />
        <button class="btn btn-sm" type="button" data-video-subtitle-shift-step="500">+0.5s</button>
        <button class="btn btn-sm" type="button" data-video-subtitle-shift-reset="1">重置</button>
      </div>
    `
    : "";
  return `
    <details class="drive-collapsible-panel">
      <summary>
        <span>
          <span class="drive-card-title">字幕</span>
          <span class="drive-card-sub">${subtitles.length ? `${subtitles.length} 軌` : "尚未掛載"}</span>
        </span>
      </summary>
      <div class="drive-collapsible-body">
        ${selector}
        ${shiftControl}
        ${canUpload ? `
          <div class="video-share-manage-grid" style="margin-top:.65rem;">
            <label>
              <span class="drive-card-sub">字幕檔</span>
              <input id="video-subtitle-file" type="file" accept=".srt,.vtt,.ass,.ssa,text/vtt,application/x-subrip" />
            </label>
            <label>
              <span class="drive-card-sub">標籤</span>
              <input id="video-subtitle-label" type="text" maxlength="80" placeholder="例如：繁中、English" />
            </label>
            <label>
              <span class="drive-card-sub">語言</span>
              <input id="video-subtitle-language" type="text" maxlength="16" placeholder="zh-Hant" />
            </label>
          </div>
          <div class="drive-file-actions" style="justify-content:flex-start;margin-top:.55rem;">
            <button class="btn btn-sm" type="button" data-video-subtitle-upload="${Number(video.id || 0)}">上傳字幕</button>
          </div>
        ` : (video.can_edit ? `<div class="field-help">strict E2EE 影音不支援伺服器端字幕掛載。</div>` : "")}
      </div>
    </details>
  `;
}

function selectedVideoQualityVariant(playback = {}) {
  const select = $("video-quality-select");
  const selected = String(select?.value || "auto");
  if (!selected || selected === "auto") return null;
  return videoPlaybackQualityOptions(playback).find((variant) => variant.name === selected) || null;
}

function selectedVideoE2eeQualityVariant(playback = {}) {
  const options = videoPlaybackQualityOptions(playback).filter((variant) => variant.manifestUrl && variant.chunkUrlTemplate);
  if (!options.length) return null;
  const selected = String($("video-e2ee-quality-select")?.value || "").trim();
  if (selected) {
    const match = options.find((variant) => variant.name === selected);
    if (match) return match;
  }
  const preferredName = String(playback?.default_quality || playback?.quality_policy?.default_quality || "").trim();
  return options.find((variant) => variant.name === preferredName)
    || options.find((variant) => Number(variant.height || 0) === 720)
    || options.find((variant) => Number(variant.height || 0) === 480)
    || options.find((variant) => variant.name === "original")
    || options[0];
}

function bindVideoSeekProtection(player) {
  if (!player || player.dataset.videoSeekProtectionBound === "1") return;
  player.dataset.videoSeekProtectionBound = "1";
  player.addEventListener("seeking", () => {
    videoState.userSeeking = true;
    videoState.lastSeekAt = Date.now();
    videoState.lastSeekTarget = Number(player.currentTime || 0);
  });
  const clearSeeking = () => {
    videoState.lastSeekAt = Date.now();
    videoState.lastSeekTarget = Number(player.currentTime || videoState.lastSeekTarget || 0);
    window.setTimeout(() => {
      if (Date.now() - Number(videoState.lastSeekAt || 0) >= 900) {
        videoState.userSeeking = false;
      }
    }, 950);
  };
  player.addEventListener("seeked", clearSeeking);
  player.addEventListener("playing", clearSeeking);
}

function videoQualityFallbackDeferredForSeek(player) {
  if (!player) return false;
  const recentSeek = Date.now() - Number(videoState.lastSeekAt || 0) < 1500;
  return !!(player.seeking || videoState.userSeeking || recentSeek);
}

function videoPlaybackResumeTime(player) {
  if (!player) return 0;
  const seekTarget = Number(videoState.lastSeekTarget || 0);
  if (videoQualityFallbackDeferredForSeek(player) && Number.isFinite(seekTarget) && seekTarget > 0) {
    return seekTarget;
  }
  const current = Number(player.currentTime || 0);
  return Number.isFinite(current) ? current : 0;
}

function applyVideoQualitySelection(playback = {}) {
  const select = $("video-quality-select");
  if (!select) return;
  const variant = selectedVideoQualityVariant(playback);
  if (videoState.currentHls && Array.isArray(videoState.currentHls.levels)) {
    if (!variant) {
      videoState.currentHls.currentLevel = -1;
      setVideoPlaybackStatus("畫質：自動；播放器會依網路狀況調整。", false);
      return;
    }
    const levelIndex = videoState.currentHls.levels.findIndex((level) => Number(level.height || 0) === Number(variant.height || 0));
    if (levelIndex >= 0) {
      videoState.currentHls.currentLevel = levelIndex;
      setVideoPlaybackStatus(`畫質：${variant.label}。`, false);
      return;
    }
  }
  const player = $("video-player");
  if (!player) return;
  const nextUrl = variant?.playlistUrl || playback.master_url || "";
  if (!nextUrl) return;
  const resumeAt = videoPlaybackResumeTime(player);
  const wasPaused = player.paused;
  player.src = nextUrl;
  if (typeof player.load === "function") player.load();
  player.addEventListener("loadedmetadata", () => {
    try {
      if (resumeAt > 0 && Number.isFinite(resumeAt)) player.currentTime = resumeAt;
      if (!wasPaused && typeof player.play === "function") player.play().catch(() => {});
    } catch (_) {
      // ignore native HLS seek restore failure
    }
  }, { once: true });
  setVideoPlaybackStatus(variant ? `畫質：${variant.label}。` : "畫質：自動。", false);
}

function fallbackVideoPlaybackToLowerQuality(playback = {}, reason = "") {
  if (videoState.manualQualitySelection || videoState.autoQualityFallbackApplied) return false;
  const player = $("video-player");
  if (videoQualityFallbackDeferredForSeek(player)) {
    setVideoPlaybackStatus("正在跳轉到指定時間，暫不自動切換畫質。", false);
    return false;
  }
  const fallback = fallbackVideoQualityVariant(playback);
  if (!fallback) return false;
  const current = selectedVideoQualityVariant(playback);
  if (current && current.name === fallback.name) return false;
  const select = $("video-quality-select");
  if (!select) return false;
  if (select) select.value = fallback.name;
  videoState.autoQualityFallbackApplied = true;
  applyVideoQualitySelection(playback);
  const suffix = reason ? `；${reason}` : "";
  setVideoPlaybackStatus(`網路狀況不穩，已自動切換為 ${fallback.label}${suffix}。`, false);
  return true;
}

function bindVideoQualityControl(playback = {}) {
  const select = $("video-quality-select");
  if (select) {
    select.addEventListener("change", () => {
      videoState.manualQualitySelection = true;
      applyVideoQualitySelection(playback);
    });
  }
  const audioSelect = $("video-audio-track-select");
  if (audioSelect) {
    audioSelect.addEventListener("change", () => applyVideoAudioTrackSelection(playback));
  }
}

function applyVideoAudioTrackSelection(playback = {}) {
  const tracks = videoPlaybackAudioTracks(playback);
  const select = $("video-audio-track-select");
  if (!select || tracks.length < 2) return;
  const selected = Math.max(0, Math.min(tracks.length - 1, Number(select.value || 0)));
  const chosen = tracks[selected];
  const player = $("video-player");
  if (videoSelectedServiceMode(videoState.current, playback) === "realtime_proxy") {
    if (!player) return;
    const resumeAt = videoPlaybackResumeTime(player);
    const nextUrl = videoRealtimeProxyUrl(playback, resumeAt);
    if (!nextUrl) return;
    attachVideoRealtimeProxyMsePlayer(
      player,
      playback,
      {
        mode: "realtime_proxy",
        src: nextUrl,
        statusText: `音軌：${tracks[selected]?.label || "音軌"}。`,
      },
      videoState.playbackSessionId
    );
    setVideoPlaybackStatus(`音軌：${tracks[selected]?.label || "音軌"}。`, false);
    return;
  }
  const chosenName = String(chosen?.name || "").toLowerCase();
  const chosenLabel = String(chosen?.label || "").toLowerCase();
  const chosenLanguage = String(chosen?.language || "").toLowerCase();
  const chosenUrl = String(chosen?.playlistUrl || "").toLowerCase();
  const chosenPath = (() => {
    try {
      return chosenUrl ? new URL(chosenUrl, window.location.origin).pathname.toLowerCase() : "";
    } catch (_) {
      return chosenUrl;
    }
  })();
  const matchesChosenTrack = (track) => {
    const name = String(track?.name || track?.label || "").toLowerCase();
    const label = String(track?.label || track?.name || "").toLowerCase();
    const lang = String(track?.lang || track?.language || "").toLowerCase();
    const uri = String(track?.url || track?.uri || track?.attrs?.URI || track?.details?.url || "").toLowerCase();
    return (chosenName && (name === chosenName || label === chosenName || uri.includes(`/${chosenName}/`) || uri.endsWith(`${chosenName}/playlist.m3u8`)))
      || (chosenLabel && (name === chosenLabel || label === chosenLabel))
      || (chosenPath && (uri === chosenPath || uri.endsWith(chosenPath) || uri.includes(chosenPath)))
      || (chosenLanguage && chosenLanguage !== "und" && lang === chosenLanguage);
  };
  if (videoState.currentHls && Array.isArray(videoState.currentHls.audioTracks)) {
    let index = videoState.currentHls.audioTracks.findIndex(matchesChosenTrack);
    if (index < 0 && selected >= 0 && selected < videoState.currentHls.audioTracks.length) index = selected;
    if (index >= 0) {
      videoState.currentHls.audioTrack = index;
      setVideoPlaybackStatus(`音軌：${chosen.label}。`, false);
      return;
    }
  }
  const nativeTracks = player?.audioTracks;
  if (nativeTracks && typeof nativeTracks.length === "number") {
    let matched = false;
    for (let index = 0; index < nativeTracks.length; index += 1) {
      const enabled = !!matchesChosenTrack(nativeTracks[index]);
      nativeTracks[index].enabled = enabled;
      matched = matched || enabled;
    }
    if (matched) {
      setVideoPlaybackStatus(`音軌：${chosen.label}。`, false);
      return;
    }
  }
  setVideoPlaybackStatus(`已選擇音軌：${tracks[selected]?.label || "音軌"}。`, false);
}

function bindVideoE2eeQualityControl(video, playback = {}, sessionId = 0) {
  const select = $("video-e2ee-quality-select");
  if (!select) return;
  select.addEventListener("change", () => {
    const player = $("video-player");
    const resumeAt = Number(player?.currentTime || 0);
    const wasPaused = !!player?.paused;
    attachVideoE2eeStreamV2Player(video, playback, sessionId || videoState.playbackSessionId).then(() => {
      const nextPlayer = $("video-player");
      if (!nextPlayer) return;
      nextPlayer.addEventListener("loadedmetadata", () => {
        try {
          if (resumeAt > 0 && Number.isFinite(resumeAt)) nextPlayer.currentTime = resumeAt;
          if (!wasPaused && typeof nextPlayer.play === "function") nextPlayer.play().catch(() => {});
        } catch (_) {}
      }, { once: true });
    }).catch((err) => {
      setVideoPlaybackStatus(err?.message || "E2EE 畫質切換失敗", true);
    });
  });
}

function clearVideoPlaybackAction() {
  const wrap = $("video-playback-action");
  if (wrap) wrap.innerHTML = "";
}

function setVideoPlaybackActionButton(label, onClick, helperText = "") {
  const wrap = $("video-playback-action");
  if (!wrap) return;
  wrap.innerHTML = `
    <button class="btn btn-primary" type="button" id="video-playback-start-btn">${sanitize(label || "開始播放")}</button>
    ${helperText ? `<div class="drive-card-sub">${sanitize(helperText)}</div>` : ""}
  `;
  const button = $("video-playback-start-btn");
  if (!button) return;
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    try {
      clearVideoPlaybackAction();
      await onClick();
    } catch (err) {
      const message = err?.message || "影音播放初始化失敗";
      setVideoPlaybackStatus(message, true);
      videoMsg(message, false);
      button.disabled = false;
      setVideoPlaybackActionButton(label, onClick, helperText);
    }
  }, { once: true });
}

async function attachVideoHlsJsPlayer(player, playback, sessionId) {
  const statusText = "已使用 HLS.js 播放，桌機 Chrome / Firefox / Edge 可穩定播放 HLS；若網路或格式異常會嘗試改用即時轉封裝。";
  let HlsCtor = null;
  try {
    HlsCtor = await loadVideoHlsLibrary();
    if (!HlsCtor || typeof HlsCtor.isSupported !== "function" || !HlsCtor.isSupported()) {
      throw new Error("目前瀏覽器不支援 HLS.js 所需的 MediaSource。");
    }
    if (sessionId !== videoState.playbackSessionId) return;
  } catch (err) {
    if (sessionId !== videoState.playbackSessionId) return;
    fallbackVideoPlayerToDirect(player, playback, `HLS.js 載入失敗，已改用即時轉封裝。${err?.message ? ` (${err.message})` : ""}`, true);
    return;
  }
  destroyCurrentVideoPlaybackArtifacts();
  const hls = new HlsCtor({
    enableWorker: true,
    backBufferLength: 30,
  });
  videoState.currentHls = hls;
  hls.on(HlsCtor.Events.MANIFEST_PARSED, () => {
    if (sessionId !== videoState.playbackSessionId) return;
    if (selectedVideoQualityVariant(playback)) {
      applyVideoQualitySelection(playback);
    } else {
      setVideoPlaybackStatus(statusText, false);
    }
    applyVideoAudioTrackSelection(playback);
  });
  if (HlsCtor.Events.AUDIO_TRACKS_UPDATED) {
    hls.on(HlsCtor.Events.AUDIO_TRACKS_UPDATED, () => {
      if (sessionId !== videoState.playbackSessionId) return;
      applyVideoAudioTrackSelection(playback);
    });
  }
  if (HlsCtor.Events.FRAG_LOADED) {
    hls.on(HlsCtor.Events.FRAG_LOADED, (_event, data) => {
      if (sessionId !== videoState.playbackSessionId) return;
      videoStreamDebugRecordHlsFragment(data || {});
      renderVideoStreamDebugPanel({ hls_last_event: "FRAG_LOADED" });
    });
  }
  if (HlsCtor.Events.LEVEL_LOADED) {
    hls.on(HlsCtor.Events.LEVEL_LOADED, (_event, data) => {
      if (sessionId !== videoState.playbackSessionId) return;
      renderVideoStreamDebugPanel({
        hls_last_event: "LEVEL_LOADED",
        hls_live: !!data?.details?.live,
        hls_targetduration: data?.details?.targetduration || "",
      });
    });
  }
  hls.on(HlsCtor.Events.ERROR, (_event, data) => {
    if (sessionId !== videoState.playbackSessionId) return;
    const detail = data?.details ? String(data.details) : "";
    const type = data?.type ? String(data.type) : "";
    const shouldTryAutoFallback = detail.toLowerCase().includes("buffer") || type.toLowerCase().includes("network") || data?.fatal;
    if (shouldTryAutoFallback && videoQualityFallbackDeferredForSeek(player)) {
      setVideoPlaybackStatus("正在跳轉到指定時間，暫不因緩衝等待切換畫質。", false);
      if (data?.fatal && typeof hls.recoverMediaError === "function") {
        try { hls.recoverMediaError(); } catch (_) {}
      }
      return;
    }
    if (shouldTryAutoFallback && fallbackVideoPlaybackToLowerQuality(playback, detail ? detail : "已降低串流負擔")) {
      if (data?.fatal && typeof hls.recoverMediaError === "function") {
        try { hls.recoverMediaError(); } catch (_) {}
      }
      return;
    }
    if (!data?.fatal) return;
    const detailText = detail ? ` (${detail})` : "";
    fallbackVideoPlayerToDirect(player, playback, `HLS.js 播放失敗，已改用即時轉封裝。${detailText}`, true);
  });
  hls.loadSource(playback.master_url || "");
  hls.attachMedia(player);
}

async function activateVideoPlaybackMode(video, playback, playbackSource, sessionId) {
  videoState.currentPlaybackSource = playbackSource || {};
  applyVideoPlaybackPreferences(video, playback || {});
  const player = $("video-player");
  if (!player) return;
  resetVideoPlaybackStatusState();
  startVideoStreamDebugSession(player, video, playback, playbackSource, sessionId);
  syncVideoSubtitleTracks(player, playback || {});
  if (playback?.mode === "e2ee_stream_v2") {
    clearVideoPlaybackAction();
    setVideoPlaybackStatus("此 strict E2EE 影音會在瀏覽器端解密。按下「開始 E2EE 播放」後才會讀取分享授權或要求密碼。", false);
    setVideoPlaybackActionButton(
      "開始 E2EE 播放",
      () => attachVideoE2eeStreamV2Player(video, playback, sessionId),
      "未按下播放前，不會主動要求 E2EE 密碼。"
    );
    return;
  }
  if (playback?.mode === "e2ee_direct") {
    clearVideoPlaybackAction();
    setVideoPlaybackStatus("此 strict E2EE 影音會在瀏覽器端完整解密。按下「開始 E2EE 播放」後才會要求原始密碼。", false);
    setVideoPlaybackActionButton(
      "開始 E2EE 播放",
      () => hydrateVideoE2eePlayer(video, playback, sessionId),
      "未按下播放前，不會主動要求 E2EE 密碼。"
    );
    return;
  }
  clearVideoPlaybackAction();
  if (playbackSource?.mode === "hls_js") {
    setVideoPlaybackStatus(playbackSource.statusText || "正在初始化 HLS.js 播放器...", false);
    await attachVideoHlsJsPlayer(player, playback, sessionId);
    return;
  }
  if (playbackSource?.mode === "realtime_proxy") {
    await attachVideoRealtimeProxyMsePlayer(player, playback, playbackSource, sessionId);
    return;
  }
  destroyCurrentVideoPlaybackArtifacts();
  if (playbackSource?.mode === "hls_native") {
    const stalledHandler = () => {
      if (sessionId !== videoState.playbackSessionId) return;
      fallbackVideoPlaybackToLowerQuality(playback, "原生 HLS 偵測到載入停滯");
    };
    player.addEventListener("stalled", stalledHandler);
    player.addEventListener("waiting", stalledHandler);
    player.addEventListener("error", () => {
      if (sessionId !== videoState.playbackSessionId) return;
      if (videoQualityFallbackDeferredForSeek(player)) {
        setVideoPlaybackStatus("正在跳轉到指定時間，暫不因播放錯誤切換來源。", false);
        return;
      }
      if (!fallbackVideoPlaybackToLowerQuality(playback, "原生 HLS 播放錯誤")) {
        fallbackVideoPlayerToDirect(player, playback, "HLS 播放失敗，已改用即時轉封裝。", true);
      }
    }, { once: true });
  }
  const src = String(playbackSource?.src || "").trim();
  if (!src) {
    renderVideoStreamDebugPanel({
      direct_state: "missing_source",
      direct_mode: playbackSource?.mode || "",
    });
    setVideoPlaybackStatus(playbackSource?.statusText || "目前沒有可播放來源。", true);
    return;
  }
  player.preload = playbackSource?.mode === "realtime_proxy" ? "none" : "metadata";
  player.src = src;
  if (typeof player.load === "function") player.load();
  setVideoPlaybackStatus(playbackSource?.statusText || "", false);
  renderVideoStreamDebugPanel({
    direct_state: "attached",
    direct_mode: playbackSource?.mode || "",
    direct_src: src,
    direct_preload: player.preload,
  });
  if (playbackSource?.mode === "direct" || playbackSource?.mode === "realtime_proxy") {
    player.addEventListener("error", () => {
      if (sessionId !== videoState.playbackSessionId) return;
      const code = Number(player.error?.code || 0);
      const modeLabel = playbackSource.mode === "realtime_proxy" ? "Standard 即時轉封裝" : "Standard 即時轉封裝";
      renderVideoStreamDebugPanel({
        direct_state: "error",
        direct_mode: playbackSource?.mode || "",
        direct_error_code: code || "unknown",
        direct_error_message: player.error?.message || "",
      });
      setVideoPlaybackStatus(`${modeLabel} 已連到來源但瀏覽器回報無法解碼或載入。錯誤碼：${code || "unknown"}。`, true);
    }, { once: true });
  }
}

function stopVideoDanmakuLoop() {
  if (videoState.danmakuAnimationId) {
    cancelAnimationFrame(videoState.danmakuAnimationId);
    videoState.danmakuAnimationId = 0;
  }
  const layer = $("video-danmaku-layer");
  if (layer) layer.replaceChildren();
}

function resetVideoDanmakuState() {
  stopVideoDanmakuLoop();
  videoState.danmakuItems = [];
  videoState.danmakuShown = new Set();
  videoState.danmakuFetchFromMs = 0;
  videoState.danmakuFetchUntilMs = 0;
  videoState.danmakuLoading = false;
  videoState.danmakuLaneUntil = [];
}

function setVideoDanmakuStatus(text, bad = false) {
  const status = $("video-danmaku-status");
  if (!status) return;
  status.textContent = text || "";
  status.dataset.state = bad ? "error" : "info";
}

function videoDanmakuDensityLimit() {
  if (videoState.danmakuDensity === "low") return 12;
  if (videoState.danmakuDensity === "high") return 40;
  return 24;
}

function videoDanmakuLaneLimit(layer) {
  const height = Math.max(120, Number(layer?.clientHeight || 0));
  const byHeight = Math.max(4, Math.floor(height / 30));
  if (videoState.danmakuDensity === "low") return Math.min(6, byHeight);
  if (videoState.danmakuDensity === "high") return Math.min(14, byHeight);
  return Math.min(10, byHeight);
}

function videoDanmakuSpecialPrice(effect) {
  return Number(VIDEO_DANMAKU_SPECIAL_PRICES[String(effect || "none")] || 0);
}

function updateVideoDanmakuSpecialHint() {
  const select = $("video-danmaku-effect");
  const hint = $("video-danmaku-special-hint");
  if (!hint) return;
  const price = videoDanmakuSpecialPrice(select?.value || "none");
  const priceText = typeof formatPoints === "function" ? formatPoints(price) : `${price} 點`;
  hint.textContent = price > 0
    ? `特製彈幕會扣 ${priceText}，收入進官方財庫。`
    : "普通彈幕不加收特製費。";
}

function mergeVideoDanmakuItems(items = []) {
  const map = new Map((videoState.danmakuItems || []).map((item) => [Number(item.id), item]));
  items.forEach((item) => {
    const id = Number(item?.id || 0);
    if (id > 0) map.set(id, item);
  });
  videoState.danmakuItems = Array.from(map.values()).sort((a, b) => Number(a.time_ms || 0) - Number(b.time_ms || 0));
}

async function loadVideoDanmakuWindow(videoId, fromMs, toMs, { replace = false } = {}) {
  if (!videoId || videoState.danmakuLoading) return;
  videoState.danmakuLoading = true;
  try {
    const start = Math.max(0, Math.floor(Number(fromMs || 0)));
    const end = Math.max(start + 1000, Math.floor(Number(toMs || start + 60000)));
    const res = await apiFetch(API + `/videos/${encodeURIComponent(videoId)}/danmaku?from_ms=${encodeURIComponent(start)}&to_ms=${encodeURIComponent(end)}&limit=300`, {
      credentials: "same-origin",
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    if (replace) {
      videoState.danmakuItems = [];
      videoState.danmakuShown = new Set();
      videoState.danmakuFetchFromMs = start;
    }
    mergeVideoDanmakuItems(json.danmaku || []);
    videoState.danmakuFetchFromMs = replace ? start : Math.min(videoState.danmakuFetchFromMs || start, start);
    videoState.danmakuFetchUntilMs = Math.max(videoState.danmakuFetchUntilMs || 0, end);
    setVideoDanmakuStatus(`${(videoState.danmakuItems || []).length} 則彈幕已同步`);
  } catch (err) {
    setVideoDanmakuStatus(err.message || "彈幕載入失敗", true);
  } finally {
    videoState.danmakuLoading = false;
  }
}

function renderVideoDanmakuControls(video) {
  if (!video || video.media_type === "audio") return "";
  return `
    <div class="video-danmaku-controls" aria-label="影片彈幕控制">
      <div class="video-danmaku-toolbar">
        <button class="btn btn-sm" type="button" id="video-danmaku-toggle" data-video-danmaku-toggle>
          ${videoState.danmakuEnabled ? "彈幕開" : "彈幕關"}
        </button>
        <label>
          <span>密度</span>
          <select id="video-danmaku-density">
            <option value="low" ${videoState.danmakuDensity === "low" ? "selected" : ""}>低</option>
            <option value="medium" ${videoState.danmakuDensity === "medium" ? "selected" : ""}>中</option>
            <option value="high" ${videoState.danmakuDensity === "high" ? "selected" : ""}>高</option>
          </select>
        </label>
        <label>
          <span>透明度</span>
          <input id="video-danmaku-opacity" type="range" min="35" max="100" step="5" value="${Math.round(Number(videoState.danmakuOpacity || 0.92) * 100)}" />
        </label>
        <label>
          <span>位置</span>
          <select id="video-danmaku-mode">
            <option value="scroll">滾動（右到左）</option>
            <option value="top">頂部固定</option>
            <option value="bottom">底部固定</option>
          </select>
        </label>
        <label>
          <span>大小</span>
          <select id="video-danmaku-size">
            <option value="normal">標準</option>
            <option value="small">小</option>
            <option value="large">大</option>
          </select>
        </label>
        <label>
          <span>特製</span>
          <select id="video-danmaku-effect">
            <option value="none">無</option>
            <option value="outline">描邊 +10</option>
            <option value="glow">發光 +30</option>
            <option value="rainbow">彩虹 +50</option>
          </select>
        </label>
        <label>
          <span>顏色</span>
          <input id="video-danmaku-color" type="color" value="#ffffff" />
        </label>
      </div>
      <div class="video-danmaku-compose">
        <input id="video-danmaku-input" type="text" maxlength="80" placeholder="在目前時間點送出彈幕" autocomplete="off" />
        <button class="btn btn-primary btn-sm" type="button" data-video-danmaku-send="${Number(video.id || 0)}">送出彈幕</button>
      </div>
      <div class="drive-card-sub" id="video-danmaku-special-hint">普通彈幕不加收特製費。</div>
      <div class="drive-card-sub" id="video-danmaku-status">彈幕會綁定目前播放時間，最多 80 字。</div>
    </div>
  `;
}

function clearVideoDanmakuLayer() {
  const layer = $("video-danmaku-layer");
  if (layer) layer.replaceChildren();
  videoState.danmakuShown = new Set();
  videoState.danmakuLaneUntil = [];
}

function spawnVideoDanmakuItem(item) {
  const layer = $("video-danmaku-layer");
  if (!layer || !videoState.danmakuEnabled) return;
  const itemId = Number(item?.id || 0);
  if (itemId > 0 && Array.from(layer.children).some((child) => Number(child.dataset.videoDanmakuId || 0) === itemId)) return;
  const maxActive = videoDanmakuDensityLimit();
  if (layer.children.length >= maxActive) return;
  const el = document.createElement("span");
  const mode = ["top", "bottom"].includes(String(item.mode || "")) ? String(item.mode) : "scroll";
  const size = ["small", "large"].includes(String(item.size || "")) ? String(item.size) : "normal";
  const effect = ["outline", "glow", "rainbow"].includes(String(item.effect || "")) ? String(item.effect) : "none";
  const lanes = videoDanmakuLaneLimit(layer);
  const now = Date.now();
  let lane = 0;
  let earliest = Number.POSITIVE_INFINITY;
  for (let idx = 0; idx < lanes; idx += 1) {
    const until = Number(videoState.danmakuLaneUntil[idx] || 0);
    if (until <= now) {
      lane = idx;
      break;
    }
    if (until < earliest) {
      earliest = until;
      lane = idx;
    }
  }
  const laneHeight = Math.max(24, Math.floor((layer.clientHeight || 220) / Math.max(1, lanes)));
  const top = mode === "bottom"
    ? Math.max(4, (layer.clientHeight || 220) - ((lane + 1) * laneHeight))
    : Math.max(4, lane * laneHeight + 4);
  const classes = ["video-danmaku-item", `video-danmaku-size-${size}`];
  classes.push(mode === "scroll" ? "video-danmaku-scroll" : `video-danmaku-${mode}`);
  classes.push(`video-danmaku-effect-${effect}`);
  if (Number(item.paid_points || 0) > 0) classes.push("video-danmaku-paid");
  el.className = classes.join(" ");
  if (itemId > 0) el.dataset.videoDanmakuId = String(itemId);
  el.textContent = String(item.content || "");
  el.style.color = /^#[0-9a-fA-F]{6}$/.test(String(item.color || "")) ? item.color : "#ffffff";
  el.style.opacity = String(Math.max(0.35, Math.min(1, Number(videoState.danmakuOpacity || 0.92))));
  el.style.top = `${top}px`;
  layer.appendChild(el);
  if (mode === "scroll") {
    const travel = Math.max(240, Math.ceil(Number(layer.clientWidth || 0) + Number(el.offsetWidth || 0) + 32));
    el.style.setProperty("--video-danmaku-travel", `-${travel}px`);
  }
  const ttl = mode === "scroll" ? 8500 : 4200;
  videoState.danmakuLaneUntil[lane] = now + Math.min(ttl, mode === "scroll" ? 1800 : 1100);
  window.setTimeout(() => el.remove(), ttl + 250);
}

function startVideoDanmakuLoop(videoId) {
  const player = $("video-player");
  const layer = $("video-danmaku-layer");
  if (!player || !layer || !videoId) return;
  const tick = () => {
    if (!videoState.current || Number(videoState.current.id || 0) !== Number(videoId)) return;
    const currentMs = Math.max(0, Math.floor(Number(player.currentTime || 0) * 1000));
    if (currentMs < videoState.danmakuFetchFromMs - 2000 || currentMs + 15000 > videoState.danmakuFetchUntilMs) {
      const start = Math.max(0, currentMs - 5000);
      loadVideoDanmakuWindow(videoId, start, start + 65000, { replace: currentMs < videoState.danmakuFetchFromMs - 2000 });
    }
    if (videoState.danmakuEnabled && !player.paused && !player.ended) {
      const due = (videoState.danmakuItems || []).filter((item) => {
        const id = Number(item.id || 0);
        const time = Number(item.time_ms || 0);
        return id > 0 && !videoState.danmakuShown.has(id) && time >= currentMs - 700 && time <= currentMs + 320;
      }).slice(0, 8);
      due.forEach((item) => {
        videoState.danmakuShown.add(Number(item.id || 0));
        spawnVideoDanmakuItem(item);
      });
    }
    videoState.danmakuAnimationId = requestAnimationFrame(tick);
  };
  player.addEventListener("seeked", () => {
    clearVideoDanmakuLayer();
    const start = Math.max(0, Math.floor(Number(player.currentTime || 0) * 1000) - 5000);
    loadVideoDanmakuWindow(videoId, start, start + 65000, { replace: true });
  });
  videoState.danmakuAnimationId = requestAnimationFrame(tick);
}

function bindVideoDanmakuControls(videoId) {
  const player = $("video-player");
  if (!player || !videoId) return;
  const density = $("video-danmaku-density");
  const opacity = $("video-danmaku-opacity");
  const input = $("video-danmaku-input");
  const effect = $("video-danmaku-effect");
  const layer = $("video-danmaku-layer");
  if (layer) layer.style.opacity = String(videoState.danmakuOpacity || 0.92);
  if (density) {
    density.addEventListener("change", () => {
      videoState.danmakuDensity = density.value || "medium";
      clearVideoDanmakuLayer();
    });
  }
  if (opacity) {
    opacity.addEventListener("input", () => {
      videoState.danmakuOpacity = Math.max(0.35, Math.min(1, Number(opacity.value || 92) / 100));
      if (layer) layer.style.opacity = String(videoState.danmakuOpacity);
    });
  }
  if (input) {
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendVideoDanmaku(videoId);
      }
    });
  }
  if (effect) {
    effect.addEventListener("change", updateVideoDanmakuSpecialHint);
    updateVideoDanmakuSpecialHint();
  }
  loadVideoDanmakuWindow(videoId, 0, 60000, { replace: true });
  startVideoDanmakuLoop(videoId);
}

async function sendVideoDanmaku(videoId) {
  const input = $("video-danmaku-input");
  const player = $("video-player");
  const content = String(input?.value || "").trim();
  if (!content) return setVideoDanmakuStatus("請先輸入彈幕內容", true);
  const payload = {
    time_ms: Math.max(0, Math.floor(Number(player?.currentTime || 0) * 1000)),
    content,
    mode: $("video-danmaku-mode")?.value || "scroll",
    color: $("video-danmaku-color")?.value || "#ffffff",
    size: $("video-danmaku-size")?.value || "normal",
    effect: $("video-danmaku-effect")?.value || "none",
    idempotency_key: `video_danmaku:${videoId}:${Date.now()}:${Math.random().toString(16).slice(2)}`,
  };
  try {
    const res = await apiFetch(API + `/videos/${encodeURIComponent(videoId)}/danmaku`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    if (input) input.value = "";
    mergeVideoDanmakuItems([json.danmaku]);
    videoState.danmakuShown.add(Number(json.danmaku?.id || 0));
    spawnVideoDanmakuItem(json.danmaku);
    const paid = Number(json.danmaku?.paid_points || 0);
    const paidText = typeof formatPoints === "function" ? formatPoints(paid) : `${paid} 點`;
    setVideoDanmakuStatus(paid > 0 ? `特製彈幕已送出，已扣 ${paidText}` : "彈幕已送出");
  } catch (err) {
    setVideoDanmakuStatus(err.message || "彈幕送出失敗", true);
  }
}

async function uploadVideoSubtitle(videoId) {
  const input = $("video-subtitle-file");
  const file = input?.files?.[0] || null;
  if (!file) return videoMsg("請先選擇字幕檔", false);
  const form = new FormData();
  form.append("subtitle", file, file.name || "subtitle.srt");
  form.append("label", $("video-subtitle-label")?.value || "");
  form.append("language", $("video-subtitle-language")?.value || "");
  const button = document.querySelector(`[data-video-subtitle-upload="${String(videoId)}"]`);
  if (button) button.disabled = true;
  try {
    const upload = await videoUploadFormWithProgress(`/api/videos/${encodeURIComponent(videoId)}/subtitles`, form);
    const json = upload.json || {};
    if (!upload.ok || !json.ok) throw new Error(json.msg || `HTTP ${upload.status}`);
    videoMsg("字幕已掛載到播放器。", true);
    await openVideoDetail(videoId);
  } catch (err) {
    videoMsg(err.message || "字幕上傳失敗", false);
  } finally {
    if (button) button.disabled = false;
  }
}

function renderVideoDetail(video, comments = [], playback = null) {
  const detail = $("video-detail");
  if (!detail) return;
  resetVideoDanmakuState();
  destroyCurrentVideoPlaybackArtifacts();
  videoState.manualQualitySelection = false;
  videoState.autoQualityFallbackApplied = false;
  videoState.userSeeking = false;
  videoState.lastSeekAt = 0;
  videoState.lastSeekTarget = 0;
  videoState.playbackSessionId += 1;
  const playbackSessionId = videoState.playbackSessionId;
  videoState.current = video;
  videoState.subtitleShiftMs = loadVideoSubtitleShiftMs(video.id);
  showVideoWatchView();
  const videoStatus = String(video.status || "ready");
  let selectedServiceMode = videoSelectedServiceMode(video, playback || {});
  const availablePlaybackModes = new Set(videoStreamingOptions(playback || {}).filter((option) => option.available).map((option) => option.mode));
  if (videoStatus === "processing" && selectedServiceMode === "prepared_hls") {
    if (availablePlaybackModes.has("direct")) selectedServiceMode = "direct";
    else if (availablePlaybackModes.has("realtime_proxy")) selectedServiceMode = "realtime_proxy";
  }
  const processingPlayableViaLowCostMode = videoStatus === "processing" && ["direct", "realtime_proxy"].includes(selectedServiceMode) && availablePlaybackModes.has(selectedServiceMode);
  const videoPlayable = videoStatus === "ready" || processingPlayableViaLowCostMode;
  const processingText = videoStatus === "processing"
    ? (processingPlayableViaLowCostMode
      ? "HLS 正在後台處理；目前先使用已啟用的低成本串流方式播放。"
      : "影音正在後台處理 HLS；你可以先做別的事，進度會顯示在任務中心，處理完成會通知上傳者。")
    : "影音目前不可播放。";
  const playbackSource = videoPlayable
    ? playbackSourceForVideo(video, playback, selectedServiceMode)
    : { mode: "processing", src: "", statusText: processingText };
  const playbackStatus = playback?.status || {};
  const streamStatusText = humanVideoStreamStatus(playback) || String(playback?.stream_warning || "").trim();
  const serviceControl = videoPlayable ? renderVideoStreamingServiceControl(video, playback || {}, selectedServiceMode) : "";
  const qualityControl = playbackSource?.mode && String(playbackSource.mode).startsWith("hls")
    ? renderVideoQualityControl(playback || {})
    : (playbackSource?.mode === "realtime_proxy"
      ? renderVideoRealtimeProxyControl(playback || {})
      : (playback?.mode === "e2ee_stream_v2" ? renderVideoE2eeQualityControl(playback || {}) : ""));
  const hlsStatus = String(playbackStatus.status || playback?.status_text || "").toLowerCase();
  const qualityPolicy = playback?.quality_policy || playbackStatus?.quality_policy || {};
  const hlsProfilePolicy = playbackStatus?.premium_hls_profile_policy || playback?.premium_hls_profile_policy || {};
  const hlsNeedsPrepare = !["ready", "processing", "queued"].includes(hlsStatus);
  const hlsNeedsRebuild = !!(
    playback?.rebuild_recommended
    || playbackStatus?.rebuild_recommended
    || qualityPolicy?.rebuild_recommended
    || qualityPolicy?.profile_drift
    || hlsProfilePolicy?.rebuild_recommended
    || hlsProfilePolicy?.profile_drift
  );
  const canManageHls = !!video.can_edit && !!video.cloud_file_id && playback?.mode !== "e2ee_direct";
  const primaryHlsAction = canManageHls && (hlsNeedsPrepare || hlsNeedsRebuild);
  const streamActions = primaryHlsAction
    ? `
      <div class="drive-file-actions" style="justify-content:flex-start;margin-top:.45rem;">
        <button class="btn btn-sm" type="button" data-video-prepare-stream="${sanitize(video.cloud_file_id || "")}">
          ${hlsNeedsRebuild ? "重新建立 HLS 串流" : "準備 HLS 串流"}
        </button>
        <span class="drive-card-sub">${hlsNeedsRebuild ? "HLS 版本或設定已有變更，建議重建。" : "目前沒有可用的 HLS 版本。"}</span>
      </div>
    `
    : (canManageHls && hlsStatus === "ready" ? `
      <details class="drive-collapsible-panel video-advanced-stream-actions">
        <summary>
          <span>
            <span class="drive-card-title">進階串流維護</span>
            <span class="drive-card-sub">HLS 正常時不需要重建</span>
          </span>
        </summary>
        <div class="drive-collapsible-body">
          <div class="drive-file-actions" style="justify-content:flex-start;">
            <button class="btn btn-sm" type="button" data-video-prepare-stream="${sanitize(video.cloud_file_id || "")}">
              重新建立 HLS 串流
            </button>
            <span class="drive-card-sub">只有字幕/音軌/畫質設定變更或播放異常時才需要使用。</span>
          </div>
        </div>
      </details>
    ` : "");
  const player = !videoPlayable
    ? `<div class="video-processing-notice" role="status">${sanitize(processingText)}</div>`
    : (video.media_type === "audio"
      ? `<audio id="video-player" class="video-player video-audio-player" controls preload="metadata" src="${sanitize(playbackSource.src)}"></audio>`
      : `
        <div class="video-player-shell">
          <video id="video-player" class="video-player" controls playsinline preload="metadata" src="${sanitize(playbackSource.src)}"></video>
          <div id="video-danmaku-layer" class="video-danmaku-layer" aria-hidden="true"></div>
        </div>
      `);
  const rememberedFragment = video.share_requires_fragment_key && video.share_url
    ? getRememberedVideoShareFragment(video.share_url)
    : "";
  const shareState = videoShareStateSummary(video);
  const shareInfo = video.can_edit && video.visibility === "unlisted"
    ? `
      <details class="drive-collapsible-panel" open>
        <summary>
          <span>
            <span class="drive-card-title">分享控制</span>
            <span class="drive-card-sub">${sanitize(shareState.label)}</span>
          </span>
        </summary>
        <div class="drive-collapsible-body">
          <div class="drive-card-sub">${sanitize(video.share_url || "尚未建立分享連結")}</div>
          <div class="drive-card-sub">目前狀態：${sanitize(shareState.label)}</div>
          <div class="drive-card-sub">已觀看次數：${sanitize(String(video.share_link?.access_count ?? 0))}</div>
          <div class="drive-card-sub">${sanitize(shareState.remaining)}</div>
          <div class="drive-card-sub">${video.share_link?.last_accessed_at ? `最後觀看：${sanitize(video.share_link.last_accessed_at)}` : "最後觀看：尚無紀錄"}</div>
          <div class="drive-card-sub">${video.share_link?.password_locked_until ? `分享密碼鎖定到：${sanitize(video.share_link.password_locked_until)}` : "分享密碼狀態：可正常驗證"}</div>
          ${video.share_requires_fragment_key ? `
            <div class="field-help">此影音採 strict E2EE。觀看者需使用完整分享連結；若設定第二層分享密碼，還需要「完整連結 + 分享密碼」。伺服器端不提供轉檔、縮圖或內容掃描。</div>
            <div class="field-help">重新產生此分享時，瀏覽器會要求發布者再次輸入原始 E2EE 密碼；伺服器端不保存原始密碼、raw file key 或 <code>#vk</code> fragment。</div>
            <div class="field-help">${rememberedFragment
              ? "本次登入 session 已保存此分享的片段金鑰，可直接複製完整連結。"
              : "此裝置目前沒有保存片段金鑰；若完整連結遺失，伺服器無法復原，只能重新產生分享。"}
            </div>
            <div class="field-help">資料截斷或 fragment 遺失不會讓伺服器幫你復原分享金鑰；如果遺失，只能重新產生分享。</div>
          ` : `
            <div class="field-help">非 E2EE 分享可使用 HLS 或即時轉封裝；若有設定分享密碼，觀看者需要先解鎖。</div>
          `}
          <div class="drive-card-sub">${video.share_password_required ? "已設定第二層分享密碼" : "未設定第二層分享密碼"}</div>
          <div class="drive-card-sub">${video.share_expires_at ? `到期時間：${sanitize(video.share_expires_at)}` : "到期時間：未限制"}</div>
          <div class="drive-card-sub">${Number(video.share_max_views || 0) > 0 ? `最大觀看次數：${Number(video.share_max_views || 0)}` : "最大觀看次數：不限"}</div>
          <div class="video-share-manage-grid">
            <label>
              <span class="drive-card-sub">更新分享密碼</span>
              <input id="video-share-password-manage" type="password" autocomplete="new-password" placeholder="留空代表不變更" />
            </label>
            <div class="field">
              <span class="drive-card-sub">到期時間</span>
              ${typeof shareExpiryPickerMarkup === "function"
                ? shareExpiryPickerMarkup({ hiddenId: "video-share-expires-at-manage", value: video.share_expires_at || "", help: "用日曆選擇日期；只選日期時預設當天 23:59 失效。" })
                : `<input id="video-share-expires-at-manage" type="datetime-local" value="${sanitize(String(video.share_expires_at || "").slice(0, 16))}" />`}
            </div>
            <label>
              <span class="drive-card-sub">最大觀看次數</span>
              <input id="video-share-max-views-manage" type="number" min="0" step="1" value="${sanitize(String(video.share_max_views || 0))}" />
            </label>
          </div>
          <div class="drive-file-actions" style="justify-content:flex-start;margin-top:.65rem;">
            <button class="btn btn-sm" type="button" id="video-share-save-btn" data-video-share-save="${Number(video.id || 0)}">${video.share_url ? "儲存分享設定" : "建立分享連結"}</button>
            <button class="btn btn-sm" type="button" data-video-share-clear-password="${Number(video.id || 0)}">移除分享密碼</button>
            <button class="btn btn-sm" type="button" data-video-copy-link="${Number(video.id || 0)}">複製完整分享連結</button>
            <button class="btn btn-sm" type="button" data-video-share-regenerate="${Number(video.id || 0)}">重新產生分享</button>
            <button class="btn btn-sm btn-danger" type="button" data-video-share-revoke="${Number(video.id || 0)}">撤銷分享</button>
          </div>
        </div>
      </details>
    `
    : "";
  detail.innerHTML = `
    <div class="video-watch-topbar">
      <button class="btn btn-sm" type="button" id="video-back-btn">← 返回影音列表</button>
      <span class="drive-card-sub">獨立播放頁</span>
    </div>
    <div class="video-watch-layout">
      <div class="video-watch-main">
        ${player}
        <div class="drive-card-sub" id="video-playback-status">
          ${sanitize(playbackSource.statusText || streamStatusText || "")}
        </div>
        <div id="video-stream-debug-slot"></div>
        ${videoPlayable ? renderVideoDanmakuControls(video) : ""}
        ${serviceControl}
        ${qualityControl}
        <div class="drive-file-actions" id="video-playback-action" style="justify-content:flex-start;margin-top:.45rem;"></div>
        ${streamActions}
        ${renderVideoStreamingModeSettings(video, playback || {})}
        ${renderVideoSubtitleControls(video, playback || {})}
        ${shareInfo}
        <div class="drive-card-heading">
          <div>
            <div class="drive-card-title">${sanitize(video.title || "未命名影片")}</div>
            <div class="drive-card-sub video-detail-owner">${userIdentityMarkup(video.owner_user_id, video.owner_username || video.owner_nickname || "使用者", `${formatVideoCount(video.view_count, " 次觀看")} · ${sanitize(videoVisibilityLabel(video.visibility))}`, "video-owner-line", video.owner_avatar_file_id || "")}</div>
            <div class="drive-card-sub">分享 ${formatVideoCount(video.share_count || 0)} · 互動分數 ${formatVideoCount(video.interaction_score || 0)}</div>
          </div>
          <div class="drive-file-actions">
            <button class="btn" type="button" data-video-like="${Number(video.id || 0)}">${video.liked_by_me ? "取消讚" : "👍 按讚"}</button>
            <button class="btn" type="button" data-video-social-share="${Number(video.id || 0)}">分享</button>
            <button class="btn" type="button" data-video-copy-link="${Number(video.id || 0)}">複製連結</button>
          </div>
        </div>
        <details class="drive-collapsible-panel" open>
          <summary>
            <span>
              <span class="drive-card-title">影音描述</span>
              <span class="drive-card-sub">再次點擊才會收合。</span>
            </span>
          </summary>
          <div class="drive-collapsible-body">${sanitize(video.description || "沒有描述")}</div>
        </details>
        <div class="video-actions-row">
          <input type="number" id="video-tip-amount" min="1" step="1" placeholder="投幣點數" />
          <button class="btn btn-primary" type="button" data-video-tip="${Number(video.id || 0)}">🪙 投幣</button>
        </div>
        <details class="drive-collapsible-panel" open>
          <summary>
            <span>
              <span class="drive-card-title">留言</span>
              <span class="drive-card-sub">${formatVideoCount(video.comment_count, " 則")}</span>
            </span>
          </summary>
          <div class="drive-collapsible-body">
            <textarea id="video-comment-content" rows="3" maxlength="1000" placeholder="留下文字留言"></textarea>
            <div class="drive-file-actions" style="justify-content:flex-start;margin:.5rem 0;">
              <button class="btn" type="button" data-video-comment="${Number(video.id || 0)}">送出留言</button>
            </div>
            <div id="video-comments-list">${renderVideoComments(comments)}</div>
          </div>
        </details>
      </div>
      <aside class="video-recommend">
        <div class="drive-card-title">推薦影片</div>
        ${(videoState.videos || []).filter((item) => Number(item.id) !== Number(video.id)).slice(0, 8).map((item) => `
          <button class="video-recommend-item" type="button" data-video-open="${Number(item.id || 0)}">
            <span class="video-recommend-thumb">${item.media_type === "audio" ? "♪" : "▶"}</span>
            <span>
              <strong>${sanitize(item.title || "未命名影片")}</strong>
              <small>${formatVideoCount(item.view_count, " 次觀看")}</small>
            </span>
          </button>
        `).join("") || `<div class="drive-empty">暫無推薦</div>`}
      </aside>
    </div>
  `;
  if (videoPlayable) {
    bindVideoPlayerView(video.id);
    bindVideoPlaybackPreferenceControls(video, playback || {});
    bindVideoSeekProtection($("video-player"));
    bindVideoStreamingServiceControl(video, playback || {});
    bindVideoQualityControl(playback || {});
    bindVideoE2eeQualityControl(video, playback || {}, playbackSessionId);
    bindVideoSubtitleShiftControls(video, playback || {});
    if (video.media_type === "video") bindVideoDanmakuControls(video.id);
    activateVideoPlaybackMode(video, playback || {}, playbackSource, playbackSessionId).catch((err) => {
      if (playbackSessionId !== videoState.playbackSessionId) return;
      const message = err?.message || "影音播放初始化失敗";
      setVideoPlaybackStatus(message, true);
      videoMsg(message, false);
    });
  } else {
    clearVideoPlaybackAction();
  }
}

function videoPlaybackPreferenceKey(name) {
  return `hackme_web.video_playback.${name}`;
}

function loadVideoPlaybackPreference(name, fallback = false) {
  try {
    const value = localStorage.getItem(videoPlaybackPreferenceKey(name));
    if (value === null) return !!fallback;
    return value === "1";
  } catch (_) {
    return !!fallback;
  }
}

function saveVideoPlaybackPreference(name, enabled) {
  const value = !!enabled;
  try {
    localStorage.setItem(videoPlaybackPreferenceKey(name), value ? "1" : "0");
  } catch (_) {}
  return value;
}

function videoPlaybackPreferenceState() {
  return {
    repeat: loadVideoPlaybackPreference("repeat", false),
    background: loadVideoPlaybackPreference("background", false),
  };
}

function currentVideoPlaybackSourceMode() {
  const source = videoState.currentPlaybackSource || {};
  const direct = String(source.mode || source.raw_mode || source.playback_source_mode || "").trim();
  if (direct) return direct;
  try {
    const debug = JSON.parse($("video-stream-debug-output")?.textContent || "{}");
    return String(debug.playback_source_mode || debug.selected_service_mode || "").trim();
  } catch (_) {
    return "";
  }
}

function setVideoMediaSession(video, player, playback = {}) {
  if (!("mediaSession" in navigator) || !player) return;
  try {
    const title = String(video?.title || videoState.current?.title || "影音播放");
    const owner = String(video?.owner_username || video?.owner_display_name || "");
    const artwork = [];
    if (video?.cover_url) {
      artwork.push({ src: video.cover_url, sizes: "512x512", type: "image/jpeg" });
    }
    navigator.mediaSession.metadata = new MediaMetadata({
      title,
      artist: owner || "Hackme",
      album: playback?.media_type === "audio" ? "音訊" : "影音",
      artwork,
    });
    navigator.mediaSession.setActionHandler("play", () => player.play().catch(() => {}));
    navigator.mediaSession.setActionHandler("pause", () => player.pause());
    navigator.mediaSession.setActionHandler("seekbackward", (details) => {
      const offset = Number(details?.seekOffset || 10);
      player.currentTime = Math.max(0, Number(player.currentTime || 0) - offset);
    });
    navigator.mediaSession.setActionHandler("seekforward", (details) => {
      const offset = Number(details?.seekOffset || 10);
      const duration = Number.isFinite(player.duration) ? player.duration : 0;
      const next = Number(player.currentTime || 0) + offset;
      player.currentTime = duration > 0 ? Math.min(duration, next) : next;
    });
    navigator.mediaSession.setActionHandler("seekto", (details) => {
      if (details && Number.isFinite(details.seekTime)) player.currentTime = details.seekTime;
    });
    try {
      if (Number.isFinite(player.duration) && player.duration > 0) {
        navigator.mediaSession.setPositionState({
          duration: player.duration,
          playbackRate: player.playbackRate || 1,
          position: Math.max(0, Math.min(player.duration, Number(player.currentTime || 0))),
        });
      }
    } catch (_) {}
  } catch (_) {}
}

function syncVideoPlaybackPreferenceButtons() {
  const state = videoPlaybackPreferenceState();
  document.querySelectorAll("[data-video-toggle-repeat]").forEach((button) => {
    button.classList.toggle("btn-primary", state.repeat);
    button.setAttribute("aria-pressed", state.repeat ? "true" : "false");
    button.textContent = state.repeat ? "重複播放：開" : "重複播放：關";
  });
  document.querySelectorAll("[data-video-toggle-background]").forEach((button) => {
    button.classList.toggle("btn-primary", state.background);
    button.setAttribute("aria-pressed", state.background ? "true" : "false");
    button.textContent = state.background ? "背景播放：開" : "背景播放：關";
  });
}

function applyVideoPlaybackPreferences(video, playback = {}) {
  const player = $("video-player");
  if (!player) return;
  const state = videoPlaybackPreferenceState();
  player.loop = !!state.repeat && currentVideoPlaybackSourceMode() !== "realtime_proxy";
  player.playsInline = true;
  player.setAttribute("playsinline", "");
  player.setAttribute("webkit-playsinline", "");
  if (state.background) {
    setVideoMediaSession(video, player, playback);
  } else if ("mediaSession" in navigator) {
    try {
      navigator.mediaSession.playbackState = "none";
    } catch (_) {}
  }
  syncVideoPlaybackPreferenceButtons();
}

function bindVideoPlaybackPreferenceControls(video, playback = {}) {
  const player = $("video-player");
  if (!player || player.dataset.videoPlaybackPreferenceBound === "1") {
    applyVideoPlaybackPreferences(video, playback);
    return;
  }
  player.dataset.videoPlaybackPreferenceBound = "1";
  let panel = $("video-playback-preferences");
  if (!panel) {
    panel = document.createElement("div");
    panel.id = "video-playback-preferences";
    panel.className = "drive-file-actions video-playback-preferences";
    panel.style.justifyContent = "flex-start";
    panel.style.margin = ".55rem 0";
    panel.innerHTML = `
      <button class="btn" type="button" data-video-toggle-repeat aria-pressed="false">重複播放：關</button>
      <button class="btn" type="button" data-video-toggle-background aria-pressed="false">背景播放：關</button>
      <span class="drive-card-sub">背景播放會啟用系統媒體控制；螢幕鎖定後是否繼續播放仍取決於瀏覽器與手機系統。</span>
    `;
    const status = $("video-playback-status");
    if (status && status.parentNode) {
      status.parentNode.insertBefore(panel, status.nextSibling);
    } else {
      player.insertAdjacentElement("afterend", panel);
    }
  }
  const repeatButton = panel.querySelector("[data-video-toggle-repeat]");
  const backgroundButton = panel.querySelector("[data-video-toggle-background]");
  if (repeatButton && repeatButton.dataset.bound !== "1") {
    repeatButton.dataset.bound = "1";
    repeatButton.addEventListener("click", () => {
      const next = saveVideoPlaybackPreference("repeat", !loadVideoPlaybackPreference("repeat", false));
      player.loop = next && currentVideoPlaybackSourceMode() !== "realtime_proxy";
      syncVideoPlaybackPreferenceButtons();
      setVideoPlaybackStatus(next ? "已開啟重複播放。" : "已關閉重複播放。", false);
    });
  }
  if (backgroundButton && backgroundButton.dataset.bound !== "1") {
    backgroundButton.dataset.bound = "1";
    backgroundButton.addEventListener("click", () => {
      const next = saveVideoPlaybackPreference("background", !loadVideoPlaybackPreference("background", false));
      applyVideoPlaybackPreferences(video, playback);
      setVideoPlaybackStatus(
        next
          ? "已開啟背景播放輔助；本站不會因頁面隱藏而主動停止播放。"
          : "已關閉背景播放輔助。",
        false
      );
    });
  }
  player.addEventListener("ended", () => {
    if (!loadVideoPlaybackPreference("repeat", false)) return;
    const mode = currentVideoPlaybackSourceMode();
    if (mode === "realtime_proxy") {
      const currentId = Number(video?.id || videoState.current?.id || 0);
      if (!currentId || player.dataset.videoRealtimeRepeatRestarting === "1") return;
      player.dataset.videoRealtimeRepeatRestarting = "1";
      setVideoPlaybackStatus("重複播放：正在重新啟動即時轉封裝。", false);
      openVideoDetail(currentId).finally(() => {
        const nextPlayer = $("video-player");
        if (nextPlayer) {
          nextPlayer.dataset.videoRealtimeRepeatRestarting = "0";
          nextPlayer.play().catch(() => {});
        }
      });
      return;
    }
    try {
      player.currentTime = 0;
      player.play().catch(() => {});
    } catch (_) {}
  });
  player.addEventListener("play", () => {
    if ("mediaSession" in navigator && loadVideoPlaybackPreference("background", false)) {
      try {
        navigator.mediaSession.playbackState = "playing";
      } catch (_) {}
    }
  });
  player.addEventListener("pause", () => {
    if ("mediaSession" in navigator && loadVideoPlaybackPreference("background", false)) {
      try {
        navigator.mediaSession.playbackState = "paused";
      } catch (_) {}
    }
  });
  player.addEventListener("timeupdate", () => {
    if (!("mediaSession" in navigator) || !loadVideoPlaybackPreference("background", false)) return;
    try {
      if (Number.isFinite(player.duration) && player.duration > 0) {
        navigator.mediaSession.setPositionState({
          duration: player.duration,
          playbackRate: player.playbackRate || 1,
          position: Math.max(0, Math.min(player.duration, Number(player.currentTime || 0))),
        });
      }
    } catch (_) {}
  });
  applyVideoPlaybackPreferences(video, playback);
}

function bindVideoPlayerView(videoId) {
  const player = $("video-player");
  if (!player || !videoId) return;
  const key = String(videoId);
  const submitView = async (completed = false) => {
    if (videoState.viewRecordedFor.has(key) && !completed) return;
    videoState.viewRecordedFor.add(key);
    try {
      await apiFetch(API + `/videos/${encodeURIComponent(videoId)}/view`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ watch_seconds: Math.floor(player.currentTime || 0), completed }),
      });
    } catch (_) {
      // View accounting must not interrupt playback.
    }
  };
  let timer = null;
  player.addEventListener("playing", () => {
    if (timer || videoState.viewRecordedFor.has(key)) return;
    timer = setTimeout(() => submitView(false), 6000);
  });
  player.addEventListener("ended", () => submitView(true));
}

async function openVideoDetail(videoId) {
  if (!videoId) return;
  cancelPendingVideoDetailRequest({ advanceNavigation: true });
  const requestGeneration = videoState.detailRequestGeneration;
  const controller = new AbortController();
  videoState.detailAbortController = controller;
  const hash = `#videos/${encodeURIComponent(videoId)}`;
  if (location.hash !== hash) {
    history.pushState(null, "", `${location.pathname}${location.search}${hash}`);
  }
  showVideoWatchView();
  const detail = $("video-detail");
  if (detail) {
    detail.innerHTML = `<div class="drive-empty">影音載入中...</div>`;
  }
  try {
    const res = await apiFetch(API + `/videos/${encodeURIComponent(videoId)}`, {
      credentials: "same-origin",
      signal: controller.signal,
    });
    const json = await res.json().catch(() => ({}));
    if (requestGeneration !== videoState.detailRequestGeneration) return;
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    let playback = null;
    try {
      const playbackRes = await apiFetch(videoPlaybackUrl(json.video), {
        credentials: "same-origin",
        signal: controller.signal,
      });
      const playbackJson = await playbackRes.json().catch(() => ({}));
      if (playbackRes.ok && playbackJson.ok) {
        playback = playbackJson;
      }
    } catch (err) {
      if (err?.name === "AbortError") return;
      playback = null;
    }
    if (requestGeneration !== videoState.detailRequestGeneration) return;
    renderVideoDetail(json.video, json.comments || [], playback);
  } catch (err) {
    if (err?.name === "AbortError") return;
    if (detail) detail.innerHTML = `<div class="drive-empty">${sanitize(err.message || "影音載入失敗")}</div>`;
  } finally {
    if (videoState.detailAbortController === controller) videoState.detailAbortController = null;
  }
}

async function likeVideo(videoId) {
  const liked = !!(videoState.current && videoState.current.liked_by_me);
  try {
    const res = await apiFetch(API + `/videos/${encodeURIComponent(videoId)}/like`, {
      method: liked ? "DELETE" : "POST",
      credentials: "same-origin",
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    await loadVideos(videoState.sort);
    await openVideoDetail(videoId);
  } catch (err) {
    videoMsg(err.message || "按讚操作失敗", false);
  }
}

async function tipVideo(videoId) {
  const amount = Number($("video-tip-amount")?.value || 0);
  if (!Number.isFinite(amount) || amount < 1) return videoMsg("請輸入要投幣的點數", false);
  try {
    const res = await apiFetch(API + `/videos/${encodeURIComponent(videoId)}/tip`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Idempotency-Key": makeVideoIdempotencyKey() },
      body: JSON.stringify({ amount: Math.floor(amount) }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    videoMsg(`投幣成功：${Number(json.tip?.amount_points || amount)} 點`, true);
    await loadVideos(videoState.sort);
    await openVideoDetail(videoId);
  } catch (err) {
    videoMsg(err.message || "投幣失敗", false);
  }
}

async function addVideoComment(videoId) {
  const textarea = $("video-comment-content");
  const content = (textarea?.value || "").trim();
  if (!content) return videoMsg("請輸入留言內容", false);
  try {
    const res = await apiFetch(API + `/videos/${encodeURIComponent(videoId)}/comments`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    if (textarea) textarea.value = "";
    await openVideoDetail(videoId);
  } catch (err) {
    videoMsg(err.message || "留言失敗", false);
  }
}

async function copyVideoLink(videoId, options = {}) {
  const button = options.button || null;
  const video = videoState.current && Number(videoState.current.id || 0) === Number(videoId || 0)
    ? videoState.current
    : (videoState.videos || []).find((item) => Number(item.id || 0) === Number(videoId || 0));
  let url = `${location.origin}${location.pathname}#videos/${encodeURIComponent(videoId)}`;
  if (video?.visibility === "unlisted" && video?.share_url) {
    url = `${location.origin}${video.share_url}`;
    if (video.share_requires_fragment_key) {
      const fragment = getRememberedVideoShareFragment(video.share_url);
      if (!fragment) {
        return videoMsg("此 E2EE 分享連結的本機片段金鑰不可復原；若遺失只能重新產生分享。", false);
      }
      url += `#vk=${fragment}`;
    }
  }
  try {
    await navigator.clipboard.writeText(url);
    videoMsg("連結已複製", true);
    if (typeof showCopyLinkFeedback === "function") showCopyLinkFeedback(button, "已完成複製", true);
  } catch (_) {
    if (typeof showCopyLinkFeedback === "function") showCopyLinkFeedback(button, "請在彈出視窗複製完整連結", false);
    window.prompt("分享連結", url);
  }
}

async function copyVideoShareText(text) {
  const value = String(text || "");
  if (!value) return false;
  if (navigator.clipboard?.writeText && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return true;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-1000px";
  textarea.style.left = "-1000px";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, value.length);
  try {
    return document.execCommand("copy");
  } finally {
    textarea.remove();
  }
}

async function createVideoSocialShare(videoId, options = {}) {
  const button = options.button || null;
  try {
    const res = await apiFetch(API + `/videos/${encodeURIComponent(videoId)}/social-share`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    if (json.video) {
      videoState.current = json.video;
      videoState.videos = (videoState.videos || []).map((item) => Number(item.id) === Number(json.video.id) ? { ...item, ...json.video } : item);
    }
    const url = json.share_link?.url ? `${location.origin}${json.share_link.url}` : `${location.origin}${location.pathname}#videos/${encodeURIComponent(videoId)}`;
    const embedText = json.embed_text || url;
    const copied = await copyVideoShareText(embedText);
    videoMsg(copied ? "分享連結已建立並複製，可貼到貼文中" : "分享連結已建立，請手動複製", true);
    if (typeof showCopyLinkFeedback === "function") {
      showCopyLinkFeedback(button, copied ? "分享碼已複製" : "請手動複製", copied);
    }
    if (!copied) window.prompt("貼到貼文的影音分享碼", embedText);
    if (videoState.current && Number(videoState.current.id || 0) === Number(videoId || 0)) {
      await openVideoDetail(videoId);
    } else {
      renderVideoList();
    }
  } catch (err) {
    videoMsg(err.message || "建立分享失敗", false);
  }
}

async function submitVideoSearch() {
  const query = normalizeVideoSearchQuery($("video-search-input")?.value || "");
  await loadVideos(videoState.sort || "new", { query });
  showVideoBrowseView({ updateHash: true });
}

async function clearVideoSearch() {
  const input = $("video-search-input");
  if (input) input.value = "";
  await loadVideos(videoState.sort || "new", { query: "" });
  showVideoBrowseView({ updateHash: true });
}

function openVideoOverview() {
  if (location.hash !== "#videos") {
    history.pushState(null, "", `${location.pathname}${location.search}#videos`);
  }
  if (typeof switchModuleTab === "function") {
    switchModuleTab("videos");
    return;
  }
  if (videoState.browseLoaded) {
    showVideoBrowseView();
  } else {
    loadVideoPlatform();
  }
}

async function loadVideoPlatform() {
  await Promise.all([loadVideos(videoState.sort), loadVideoPublishFiles()]);
  warnInterruptedVideoE2eeLocalTask();
  const hash = location.hash || "";
  const match = hash.match(/^#videos\/(\d+)$/);
  if (match) {
    openVideoDetail(match[1]);
  } else {
    showVideoBrowseView();
  }
}

function handleVideoHashRoute() {
  const match = (location.hash || "").match(/^#videos\/(\d+)$/);
  if (match) {
    if (!videoState.browseLoaded) {
      loadVideoPlatform();
    } else {
      openVideoDetail(match[1]);
    }
  } else if ((location.hash || "") === "#videos" && $("video-browse-view")) {
    showVideoBrowseView();
  }
}

window.addEventListener("hashchange", handleVideoHashRoute);

document.addEventListener("submit", (event) => {
  if (event.target?.id === "video-search-form") {
    event.preventDefault();
    submitVideoSearch();
  }
});

document.addEventListener("click", (event) => {
  if (event.target.closest("[data-open-user-profile]")) {
    return;
  }
  const open = event.target.closest("[data-video-open]");
  if (open) {
    event.preventDefault();
    openVideoDetail(open.dataset.videoOpen);
    return;
  }
  const sort = event.target.closest("[data-video-sort]");
  if (sort) {
    loadVideos(sort.dataset.videoSort || "new");
    return;
  }
  const like = event.target.closest("[data-video-like]");
  if (like) {
    likeVideo(like.dataset.videoLike);
    return;
  }
  const tip = event.target.closest("[data-video-tip]");
  if (tip) {
    tipVideo(tip.dataset.videoTip);
    return;
  }
  const comment = event.target.closest("[data-video-comment]");
  if (comment) {
    addVideoComment(comment.dataset.videoComment);
    return;
  }
  const danmakuToggle = event.target.closest("[data-video-danmaku-toggle]");
  if (danmakuToggle) {
    videoState.danmakuEnabled = !videoState.danmakuEnabled;
    danmakuToggle.textContent = videoState.danmakuEnabled ? "彈幕開" : "彈幕關";
    if (!videoState.danmakuEnabled) clearVideoDanmakuLayer();
    setVideoDanmakuStatus(videoState.danmakuEnabled ? "彈幕已開啟" : "彈幕已關閉");
    return;
  }
  const danmakuSend = event.target.closest("[data-video-danmaku-send]");
  if (danmakuSend) {
    sendVideoDanmaku(danmakuSend.dataset.videoDanmakuSend);
    return;
  }
  const copy = event.target.closest("[data-video-copy-link]");
  if (copy) {
    copyVideoLink(copy.dataset.videoCopyLink, { button: copy });
    return;
  }
  const socialShare = event.target.closest("[data-video-social-share]");
  if (socialShare) {
    createVideoSocialShare(socialShare.dataset.videoSocialShare, { button: socialShare });
    return;
  }
  const prepare = event.target.closest("[data-video-prepare-stream]");
  if (prepare) {
    prepareVideoStream(prepare.dataset.videoPrepareStream, videoState.current?.id || 0);
    return;
  }
  const saveStreamingModes = event.target.closest("[data-video-streaming-modes-save]");
  if (saveStreamingModes) {
    updateVideoStreamingModes(saveStreamingModes.dataset.videoStreamingModesSave);
    return;
  }
  const subtitleUpload = event.target.closest("[data-video-subtitle-upload]");
  if (subtitleUpload) {
    uploadVideoSubtitle(subtitleUpload.dataset.videoSubtitleUpload);
    return;
  }
  const regenerateShare = event.target.closest("[data-video-share-regenerate]");
  if (regenerateShare) {
    regenerateVideoShareLink(videoState.current);
    return;
  }
  const saveShare = event.target.closest("[data-video-share-save]");
  if (saveShare) {
    saveVideoShareSettings(videoState.current);
    return;
  }
  const clearSharePassword = event.target.closest("[data-video-share-clear-password]");
  if (clearSharePassword) {
    saveVideoShareSettings(videoState.current, { clearPassword: true });
    return;
  }
  const revokeShare = event.target.closest("[data-video-share-revoke]");
  if (revokeShare) {
    revokeVideoShareLink(videoState.current);
    return;
  }
  if (event.target.closest("#video-refresh-btn")) {
    loadVideoPlatform();
    return;
  }
  if (event.target.closest("#video-search-clear")) {
    clearVideoSearch();
    return;
  }
  if (event.target.closest("#video-back-btn")) {
    showVideoBrowseView({ updateHash: true });
    return;
  }
  if (event.target.closest("#video-publish-open-btn")) {
    toggleVideoPublishPanel();
    return;
  }
  if (event.target.closest("#video-publish-cancel-btn")) {
    setVideoPublishPanelVisible(false, { focus: false });
    return;
  }
  const cloudSelectButton = event.target.closest("[data-video-publish-cloud-select]");
  if (cloudSelectButton) {
    applyVideoPublishDriveSelection(cloudSelectButton.dataset.videoPublishCloudSelect || "");
    return;
  }
  const cloudCard = event.target.closest("[data-video-publish-cloud-id]");
  if (cloudCard && !event.target.closest("video,audio,button,input,select,textarea,a")) {
    applyVideoPublishDriveSelection(cloudCard.dataset.videoPublishCloudId || "");
    return;
  }
  if (event.target.closest("#video-publish-btn")) {
    publishVideoFromDrive();
  }
});

document.addEventListener("change", (event) => {
  if (event.target?.id === "video-publish-file") {
    applyVideoPublishDriveSelection(event.target.value || "");
  }
  if (event.target?.id === "video-upload-file") {
    syncVideoPublishStreamingModeForPrivacy();
  }
  if (event.target?.id === "video-upload-privacy-mode") {
    syncVideoPublishStreamingModeForPrivacy();
  }
});
