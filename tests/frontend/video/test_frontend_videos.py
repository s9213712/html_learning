from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_video_platform_accepts_audio_media_in_ui():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    videos_js = (ROOT / "public" / "js" / "39-videos.js").read_text(encoding="utf-8")
    admin_js = (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
    styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    assert 'accept="video/*,audio/*"' in index_html
    assert 'id="video-cover-file"' in index_html
    assert 'accept="image/*"' in index_html
    assert "發布影片或音樂" in index_html
    assert "雲端硬碟影音" in index_html
    assert 'id="video-publish-open-btn" type="button" aria-expanded="false" aria-controls="video-publish-panel"' in index_html
    assert 'id="video-publish-panel" hidden aria-label="發布影音"' in index_html
    assert 'id="video-publish-cancel-btn"' in index_html
    assert 'id="video-search-form"' in index_html
    assert 'id="video-search-input"' in index_html
    assert 'id="video-search-status"' in index_html
    assert "function isCloudMediaFile" in videos_js
    assert "searchQuery" in videos_js
    assert "normalizeVideoSearchQuery" in videos_js
    assert "syncVideoSearchControls" in videos_js
    assert "submitVideoSearch" in videos_js
    assert "clearVideoSearch" in videos_js
    assert "params.set(\"q\", videoState.searchQuery)" in videos_js
    assert "找不到與" in videos_js
    assert "let videoPendingPublishSelection = null;" in videos_js
    assert "function setVideoPublishPanelVisible" in videos_js
    assert "function toggleVideoPublishPanel" in videos_js
    assert 'toggle.setAttribute("aria-expanded", show ? "true" : "false");' in videos_js
    assert 'event.target.closest("#video-publish-cancel-btn")' in videos_js
    assert "setVideoPublishPanelVisible(false, { focus: false });" in videos_js
    assert "async function openVideoPublishFromDrive(fileId, options = {})" in videos_js
    assert "applyVideoPublishDriveSelection" in videos_js
    assert "請完成標題、可見性、分享與封面設定後發布" in videos_js
    assert 'mime.startsWith("audio/")' in videos_js
    assert '".mp3"' in videos_js
    assert 'video.media_type === "audio"' in videos_js
    assert "<audio" in videos_js
    assert "video-audio-player" in videos_js
    assert "function videoThumbMarkup(video)" in videos_js
    assert "video.cover_url" in videos_js
    assert "direct_stream_allowed" in videos_js
    assert '["server_encrypted", "e2ee"].includes(privacyMode)' in videos_js
    assert "function videoPlaybackUrl(video)" in videos_js
    assert "playback_url" in videos_js
    assert 'form.append("cover", coverFile)' in videos_js
    assert 'form.append("cloud_file_id", payload.cloud_file_id)' in videos_js
    assert "} else if (coverFile) {" in videos_js
    assert "video-thumb-image" in videos_js
    assert "video-thumb-audio" in videos_js
    assert "browserSupportsNativeHls" in videos_js
    assert "loadVideoHlsLibrary" in videos_js
    assert "attachVideoHlsJsPlayer" in videos_js
    assert "function videoStreamingOptions(playback = {})" in videos_js
    assert "video-service-mode-select" in videos_js
    assert "videoRealtimeProxyUrl" in videos_js
    assert "realtime_proxy_url" in videos_js
    assert "Standard 即時轉封裝" in videos_js
    assert "/js/hls.light.min.js?v=20260505-hlsjs" in videos_js
    assert "/js/vendor/hls.light.min.js" not in videos_js
    assert "HLS.js" in videos_js
    assert "HLS 串流" in videos_js
    assert "function videoQualitySizeBytes" in videos_js
    assert "sizeLabel ? `${label} · ${sizeLabel}`" in videos_js
    assert "function bindVideoSeekProtection" in videos_js
    assert "function videoQualityFallbackDeferredForSeek" in videos_js
    assert "正在跳轉到指定時間，暫不自動切換畫質。" in videos_js
    assert "video-danmaku-layer" in videos_js
    assert "function startVideoDanmakuLoop" in videos_js
    assert "function sendVideoDanmaku" in videos_js
    assert "videoDanmakuSpecialPrice" in videos_js
    assert 'id="video-danmaku-effect"' in videos_js
    assert 'id="video-danmaku-size"' in videos_js
    assert "videoState.danmakuShown.add(Number(json.danmaku?.id || 0));" in videos_js
    assert "/danmaku?from_ms=" in videos_js
    assert "data-video-danmaku-send" in videos_js
    assert ".video-danmaku-layer" in styles
    assert "left: 100%" in styles
    assert "--video-danmaku-travel" in styles
    assert ".video-danmaku-effect-rainbow" in styles
    assert "@keyframes video-danmaku-scroll" in styles
    assert "function humanVideoStreamStatus" in videos_js
    assert "data-video-prepare-stream" in videos_js
    assert "prepareVideoStream" in videos_js
    assert "VIDEO_SHARE_FRAGMENT_STORAGE_KEY" in videos_js
    assert "VIDEO_E2EE_STREAM_V2_WORKER_URL" in videos_js
    assert "/js/e2ee-stream-v2-worker.js?v=20260505-e2eev2" in videos_js
    assert "/js/workers/e2ee-stream-v2-worker.js" not in videos_js
    assert "buildVideoE2eeShareEnvelope" in videos_js
    assert "prepareVideoE2eeShareArtifacts" in videos_js
    assert "buildVideoE2eeStreamV2Package" in videos_js
    assert "uploadVideoE2eeStreamV2Package" in videos_js
    assert "buildVideoE2eeDerivativePackages" in videos_js
    assert "uploadVideoE2eeDerivativePackages" in videos_js
    assert "originalCiphertextDigest" in videos_js
    assert "e2ee?.ciphertext_sha256" in videos_js
    derivative_fn = videos_js.split("async function buildVideoE2eeDerivativePackages", 1)[1].split("async function prepareVideoE2eeShareArtifacts", 1)[0]
    assert "await decryptedBlob.arrayBuffer()" not in derivative_fn
    assert "VIDEO_E2EE_LOCAL_TASK_STORAGE_KEY" in videos_js
    assert "hackme_web.video_e2ee_local_task" in videos_js
    assert "video.e2ee_derivatives.client" in videos_js
    assert "warnInterruptedVideoE2eeLocalTask" in videos_js
    assert "beforeunload" in videos_js
    assert "VIDEO_E2EE_DERIVATIVE_TARGET_HEIGHTS" in videos_js
    assert "/e2ee-stream-v2/variants/" in videos_js
    assert "正在瀏覽器端產生 E2EE" in videos_js
    assert "derivative.blob.size >= sourceSize" in videos_js
    assert "renderVideoE2eeQualityControl" in videos_js
    assert "selectedVideoE2eeQualityVariant" in videos_js
    assert "function videoSubtitleUrlWithShift" in videos_js
    assert "data-video-subtitle-shift-step" in videos_js
    assert "shift_ms" in videos_js
    assert "E2EE Streaming v2 密文分段上傳中" in videos_js
    assert "E2EE 省流量版本" in videos_js
    assert "E2EE Streaming v2 manifest 儲存中" in videos_js
    assert 'payload.visibility = "unlisted";' in videos_js
    assert "E2EE 影音對外觀看已改用" in videos_js
    assert "不需要知道原始 E2EE 密碼" in index_html
    assert "上傳完成，伺服器端加密與掃描中；若有選 HLS 才會在後台轉檔" in videos_js
    assert "不預先轉檔（Standard 即時串流）" in index_html
    assert "MP4/WebM 預設不建立 HLS，MKV/AVI/MOV 會自動建議 HLS" in index_html
    assert "function videoPublishShouldPreferPreparedHls" in videos_js
    assert 'if (String(file?.privacy_mode || "").trim().toLowerCase() === "e2ee") return false;' in videos_js
    assert '[".mkv", ".avi", ".mov", ".flv", ".wmv", ".ts", ".mts", ".m2ts"].includes(ext)' in videos_js
    assert "syncVideoPublishStreamingModeForFile(videoPublishFileById(target));" in videos_js
    publish_modes_fn = videos_js.split("function selectedVideoPublishStreamingModes", 1)[1].split("function syncVideoPublishStreamingModeForPrivacy", 1)[0]
    assert 'if (mode === "prepared_hls") return ["prepared_hls"];' in publish_modes_fn
    assert 'if (mode === "realtime_proxy") return ["realtime_proxy"];' in publish_modes_fn
    assert 'return ["realtime_proxy"];' in publish_modes_fn
    assert 'return ["prepared_hls", "realtime_proxy"];' not in publish_modes_fn
    assert 'share_wrapped_file_key_envelope' in videos_js
    assert 'share_expires_at' in videos_js
    assert 'share_max_views' in videos_js
    assert "video_e2ee_derivatives_enabled" in admin_js
    assert "video_e2ee_derivative_heights" in admin_js
    assert "s-video-e2ee-derivatives-enabled" in index_html
    assert "s-video-e2ee-derivative-heights" in index_html
    assert 'remaining_views' in videos_js
    assert 'state_message' in videos_js
    assert 'password_locked_until' in videos_js
    assert 'data-video-share-regenerate' in videos_js
    assert 'data-video-share-revoke' in videos_js
    assert 'data-video-share-save' in videos_js
    assert 'data-video-share-clear-password' in videos_js
    assert 'share_fragment_key' in videos_js
    assert 'getRememberedVideoShareFragment' in videos_js
    assert 'mode === "e2ee_stream_v2"' in videos_js
    assert 'mode === "e2ee_direct"' in videos_js
    assert "fetchVideoE2eeChunkWithRetry" in videos_js
    assert "pruneVideoE2eeChunkCache" in videos_js
    assert "videoE2eeChunkIndexForTime" in videos_js
    assert "正在追上快轉目標" in videos_js
    assert 'setVideoPlaybackActionButton(' in videos_js
    assert '開始 E2EE 播放' in videos_js
    assert '未按下播放前，不會主動要求 E2EE 密碼。' in videos_js
    assert '完整分享連結' in videos_js
    assert '伺服器無法復原，只能重新產生分享' in videos_js
    assert '重新產生此分享時，瀏覽器會要求發布者再次輸入原始 E2EE 密碼' in videos_js
    assert ".video-audio-player" in styles
    assert ".video-quality-control select" in styles
    assert "max-width: 11.5rem" in styles
    assert ".video-thumb-image" in styles
    assert ".video-share-manage-grid" in styles
    assert ".video-search-bar" in styles
    assert ".video-search-status" in styles


def test_video_platform_uses_separate_watch_view_and_mobile_layout():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    videos_js = (ROOT / "public" / "js" / "39-videos.js").read_text(encoding="utf-8")
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")
    styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    assert 'id="video-browse-view"' in index_html
    assert 'id="video-watch-view"' in index_html
    assert "function showVideoBrowseView" in videos_js
    assert "function cancelPendingVideoDetailRequest" in videos_js
    assert "detailRequestGeneration" in videos_js
    assert "navigationGeneration" in videos_js
    assert "Math.min(95, percent * 0.95)" in videos_js
    assert 'stage: "completed_with_warning"' in videos_js
    assert "function showVideoWatchView" in videos_js
    assert "function openVideoOverview" in videos_js
    assert 'history.pushState(null, "", `${location.pathname}${location.search}#videos`);' in videos_js
    assert 'if (typeof openVideoOverview === "function") openVideoOverview();' in bootstrap_js
    assert "/playback" in videos_js
    assert "video-back-btn" in videos_js
    assert "#videos/" in videos_js
    assert '<a class="video-card" href="#videos/${Number(video.id || 0)}" data-video-open=' in videos_js
    assert 'event.preventDefault();' in videos_js
    assert 'class="video-thumb-media"' in videos_js
    assert "#t=0.1" in videos_js
    assert 'share_requires_fragment_key' in videos_js
    assert 'strict E2EE' in videos_js
    assert '正在使用 E2EE Streaming v2' in videos_js
    assert 'MediaSource' in videos_js
    assert 'Web Worker' in videos_js
    assert '分享連結與設定已更新。' in videos_js
    assert '分享連結已撤銷' in videos_js
    assert 'videoShareStateSummary' in videos_js
    assert 'saveVideoShareSettings' in videos_js
    assert '已改用即時轉封裝' in videos_js
    assert 'id="video-playback-action"' in videos_js
    assert "@media (max-width: 720px)" in styles
    assert "#module-videos .admin-tools" in styles
    assert ".video-watch-topbar" in styles
    assert ".video-thumb-media" in styles
    assert "pointer-events: none" in styles
    assert '#video-playback-status[data-state="error"]' in styles


def test_video_share_copy_and_shared_page_guardrails_are_visible_in_ui_code():
    videos_js = (ROOT / "public" / "js" / "39-videos.js").read_text(encoding="utf-8")
    shared_page = (ROOT / "public" / "js" / "shared-video.js").read_text(encoding="utf-8")

    assert 'await navigator.clipboard.writeText(url);' in videos_js
    assert 'videoMsg("連結已複製", true);' in videos_js
    assert 'window.prompt("分享連結", url);' in videos_js
    assert "此 E2EE 分享連結的本機片段金鑰不可復原；若遺失只能重新產生分享。" in videos_js
    assert "AbortController" in shared_page
    assert "setTimeout(() => controller.abort(), 10000);" in shared_page
    assert 'loadSharedVideo().catch((err) => setMsg(err.message || "分享影音載入失敗", true));' in shared_page
    assert "/js/hls.light.min.js?v=20260505-hlsjs" in shared_page
    assert "/js/e2ee-stream-v2-worker.js?v=20260505-e2eev2" in shared_page
    assert "fetchSharedE2eeChunkWithRetry" in shared_page
    assert "pruneSharedE2eeChunkCache" in shared_page
    assert "sharedE2eeChunkIndexForTime" in shared_page
    assert "sharedE2eeQualityOptions" in shared_page
    assert "preferredSharedE2eeQuality" in shared_page
    assert "sharedE2eeFragmentKey" in shared_page
    assert "activeChunkUrlTemplate" in shared_page
    assert "variant.manifest_url" in shared_page
    assert "function subtitleUrlWithShift" in shared_page
    assert "data-subtitle-shift-step" in shared_page
    assert "shared_video_subtitle_shift_ms" in shared_page
    assert "正在追上快轉目標" in shared_page
    assert "function bindSharedSeekProtection" in shared_page
    assert "function sharedQualityFallbackDeferredForSeek" in shared_page
    assert "正在跳轉到指定時間，暫不自動切換畫質。" in shared_page
    assert "function sharedStreamingOptions(playback={})" in shared_page
    assert "shared-service-mode-select" in shared_page
    assert "sharedRealtimeProxyUrl" in shared_page
    assert "Standard 即時轉封裝" in shared_page
    assert "/js/vendor/hls.light.min.js" not in shared_page
    assert "/js/workers/e2ee-stream-v2-worker.js" not in shared_page


def test_shared_video_page_layout_is_viewport_bounded():
    html = (ROOT / "routes" / "videos.py").read_text(encoding="utf-8")

    assert "min-height:100dvh" in html
    assert "radial-gradient(circle at 18% 8%" in html
    assert "backdrop-filter:blur(12px)" in html
    assert "#player-host video" in html
    assert "max-height:min(64dvh, 560px)" in html
    assert "max-height:min(48dvh, calc(100dvh - 210px))" in html
    assert "@media (max-height: 520px) and (orientation: landscape)" in html
    assert 'mimetype="text/html"' in html
    assert 'mimetype="text/html; charset=utf-8"' not in html
