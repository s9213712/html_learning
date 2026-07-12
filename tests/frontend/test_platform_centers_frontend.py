from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_platform_center_frontend_surfaces_are_wired():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    admin_js = (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
    platform_js = (ROOT / "public" / "js" / "57-platform-centers.js").read_text(encoding="utf-8")

    assert 'id="tab-module-jobs"' in index_html
    assert 'id="module-jobs"' in index_html
    assert 'data-job-center-view="general"' in index_html
    assert 'data-job-center-view="trading"' in index_html
    assert 'id="tab-module-shares"' in index_html
    assert 'id="module-shares"' in index_html
    assert 'id="share-center-events"' in index_html
    assert 'data-share-center-tab="links"' in index_html
    assert 'data-share-center-tab="videos"' in index_html
    assert 'id="video-manage-list"' in index_html
    assert 'id="video-manage-platform-fee"' in index_html
    assert 'id="trading-asset-admin-risk"' in index_html
    assert "/js/57-platform-centers.js" in index_html
    assert 'module: "jobs"' in core_js
    assert 'label: "公告", group: "公告"' in core_js
    assert 'label: "聊天", group: "社交"' in core_js
    assert 'label: "個人面板",\n    group: "社交"' in core_js
    assert 'label: "社群",\n    group: "社交"' in core_js
    assert '{ label: "公告", action: "module:announcements" }' not in core_js
    assert 'label: "雲端硬碟",\n    group: "功能"' in core_js
    assert 'label: "影音", group: "功能"' in core_js
    assert 'label: "遊戲區", group: "功能"' in core_js
    assert 'label: "AI 產圖", group: "功能"' in core_js
    assert 'label: "積分錢包",\n    group: "帳務"' in core_js
    assert 'label: "積分交易所",\n    group: "帳務"' in core_js
    assert 'label: "分享管理", group: "管理"' in core_js
    assert 'label: "申覆", group: "管理"' in core_js
    assert 'label: "實驗區", group: "實驗區"' in core_js
    assert 'label: "分享管理", action: "module:shares", moduleKey: "shares"' not in core_js
    assert 'label: "遊戲 / AI"' not in core_js
    assert 'label: "任務中心", group: "管理"' in core_js
    assert 'tabModuleShares.addEventListener("click", () => switchModuleTab("shares"))' in (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")
    assert 'switchModuleTab("jobs")' in admin_js or 'normTab === "jobs"' in admin_js
    assert 'modShares.classList.toggle("active", normTab === "shares")' in admin_js
    assert 'mShares.classList.toggle("active", normTab === "shares")' in admin_js
    assert 'function loadJobCenter' in platform_js
    assert 'function startJobCenterPolling' in platform_js
    assert 'if (!quiet) {\n      platformCenterSetMsg("job-center-msg", `${viewText}：已同步' in platform_js
    assert 'if (accountGeneration === platformCenterAccountGeneration && err?.name !== "AbortError" && !quiet) {' in platform_js
    assert 'platformCenterSetMsg("job-center-msg", "任務中心讀取失敗，請稍後再試。", false);' in platform_js
    assert 'JOB_CENTER_POLL_INTERVAL_MS = 3000' in platform_js
    assert 'hydrateJobCenterLiveProgress' in platform_js
    assert 'document.addEventListener("hackme:module-changed"' in platform_js
    assert 'loadDriveTaskCenterJobs({ csrf })' in platform_js
    assert 'mergePlatformJobCenterJobs([...jobs, ...driveJobs])' in platform_js
    assert 'isLowSignalJobCenterNoise' in platform_js
    assert 'function isTradingBackgroundJob' in platform_js
    assert '已隱藏 ${summary.hiddenCount} 筆已完成上傳' in platform_js
    assert 'loadShareCenter()' in platform_js
    assert 'loadVideoManageCenter()' in platform_js
    assert 'function renderVideoManageCenter' in platform_js
    assert '/videos/manage?limit=120' in platform_js
    assert 'data-video-manage-share-open="${sanitize(id)}"' in platform_js
    assert 'async function openManagedVideoShareSettings(videoId)' in platform_js
    assert '已切到分享管理，請在這裡調整分享選項。' in platform_js
    assert '/boost' in platform_js
    assert 'data-video-manage-save' in platform_js
    assert 'data-video-manage-delete' in platform_js
    assert 'data-video-manage-boost' in platform_js
    assert 'loadTradingAssetOverview({ quiet: false })' in platform_js
    assert 'platformConfirm("確定要取消這個任務？"' in platform_js
    assert 'platformConfirm("確定要取消這個下載任務？"' in platform_js
    assert 'data-job-dismiss="${sanitize(job.job_uuid)}"${remoteDismissAttr}' in platform_js
    assert 'data-job-remote-download-task="${sanitize(remoteTaskId)}"' in platform_js
    assert 'async function dismissJobCenterJob(jobUuid, options = {})' in platform_js
    assert '/jobs/${encodeURIComponent(jobUuid)}' in platform_js
    assert '/cloud-drive/remote-download/tasks/${encodeURIComponent(remoteTaskId)}' in platform_js
    assert '任務更新失敗：${err?.message || err || "請稍後重試"}' in platform_js
    assert 'data-job-remote-action="pause"' in platform_js
    assert 'data-job-remote-action="resume"' in platform_js
    assert 'updateJobCenterRemoteDownloadTask' in platform_js
    assert '/cloud-drive/remote-download/tasks/${encodeURIComponent(taskId)}/${action}' in platform_js
    assert 'paused: "已暫停"' in platform_js
    assert 'parsed.origin === location.origin' in platform_js
    assert 'loadShareCenterEvents' in platform_js
    assert '/access-events' in platform_js
    assert 'function closeShareCenterEvents()' in platform_js
    assert 'function renderShareCenterEventsPanel' in platform_js
    assert 'data-share-events-close' in platform_js
    assert 'closeShareCenterEvents();' in platform_js
    assert 'function formatShareCenterCountdown(ms)' in platform_js
    assert 'function shareCenterEffectiveStatus' in platform_js
    assert 'shareCenterEffectiveStatus(s, now) === "active"' in platform_js
    assert 'data-share-countdown-status="${sanitize(effectiveStatus)}"' in platform_js
    assert 'setTimeout(rerenderShareCenter, 0)' in platform_js
    assert '倒數計時：${formatShareCenterCountdown' in platform_js
    assert 'data-share-countdown-until' in platform_js
    assert 'data-share-edit="${sanitize(key)}"' in platform_js
    assert 'data-share-edit-save="${sanitize(key)}"' in platform_js
    assert 'function shareCenterCanEdit' in platform_js
    assert '["active", "expired", "view_limit_reached", "password_locked"]' in platform_js
    assert '重新分享設定' in platform_js
    assert 'function shareCenterReactivateHint' in platform_js
    assert 'data-share-edit-reset-access-count' in platform_js
    assert 'payload.reset_access_count' in platform_js
    assert 'type === "file" || type === "album" || type === "video"' in platform_js
    assert 'share.share_type === "file" || share.share_type === "album" || share.share_type === "video"' in platform_js
    assert 'async function openShareCenterEditor(type, id)' in platform_js
    assert 'async function saveShareCenterOptions(key)' in platform_js
    assert 'method: "PUT"' in platform_js
    assert '/shares/${encodeURIComponent(share.share_type)}/${encodeURIComponent(share.id)}' in platform_js
    assert 'scheduleShareCenterCountdowns()' in platform_js
    assert 'setInterval(updateShareCenterCountdowns, 1000)' in platform_js
    assert 'const keepJobProgressDetail = status === "running" && !asset.error_message && (jobDetail || jobStage);' in platform_js
    assert 'stage: keepJobProgressDetail ? (jobStage || asset.status || job.stage)' in platform_js
    assert 'currentUser === "root"' in platform_js
    assert '&all=1' not in platform_js
    assert '/shares?limit=120"' in platform_js
    assert 'share-center-countdown' in platform_js
    assert '.share-center-countdown' in (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    assert 'const timeLabel = isOpenEvent ? "開啟時間" : "時間"' in platform_js
    assert 'IP 來源：${sanitize(ip)}' in platform_js
    assert 'event.source_ip || event.ip' in platform_js
    assert '.share-center-events-header' in (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    assert '.share-center-event-row' in (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    assert '.video-manage-row' in (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    assert '/admin/trading/asset-overview' in platform_js
    assert '交易資產總覽讀取失敗' in platform_js
    drive_js = (ROOT / "public" / "js" / "35-drive.js").read_text(encoding="utf-8")
    assert 'function loadDriveTaskCenterJobs' in drive_js
    assert 'driveRemoteTaskToJobCenterJob' in drive_js
    assert 'driveTransferToJobCenterJob' in drive_js
    assert 'live_status_source: "遠端下載"' in drive_js
    assert 'source_ref: json.file?.file_id ? `cloud_file:${json.file.file_id}` : ""' in drive_js
