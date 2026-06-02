from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_admin_storage_maintenance_routes_are_bounded():
    routes = (ROOT / "routes" / "file_sections" / "admin_storage_routes.py").read_text(encoding="utf-8")
    files_routes = (ROOT / "routes" / "files.py").read_text(encoding="utf-8")

    assert "SELECT * FROM users ORDER BY id ASC LIMIT ? OFFSET ?" in routes
    assert '"has_more": has_more' in routes
    assert "SELECT id FROM users ORDER BY id ASC" not in routes
    assert "SELECT DISTINCT owner_user_id FROM storage_files" in routes
    assert "SELECT * FROM announcement_attachment_requests ORDER BY created_at DESC LIMIT ? OFFSET ?" in files_routes
    assert "SELECT * FROM announcement_attachment_requests ORDER BY created_at DESC\").fetchall()" not in files_routes


def test_cloud_drive_preview_ui_is_wired():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    drive_js = ((ROOT / "public" / "js" / "35-drive.js").read_text(encoding="utf-8") + "\n" + (ROOT / "public" / "js" / "35-drive-preview-share.js").read_text(encoding="utf-8"))
    shared_file_js = (ROOT / "public" / "js" / "shared-file.js").read_text(encoding="utf-8")
    share_preview_routes = (ROOT / "routes" / "file_sections" / "share_preview_routes.py").read_text(encoding="utf-8")
    server_py = (ROOT / "server.py").read_text(encoding="utf-8")

    assert 'id="drive-preview-card"' in index_html
    assert 'id="drive-preview-panel"' in index_html
    assert 'class="drive-file-preview-layout"' in index_html
    assert "手機點預覽會直接開小視窗" in index_html
    assert "async function previewDriveFile(fileId, options = {})" in drive_js
    assert "function shouldOpenDriveFullscreen(fileId" in drive_js
    assert "function isDriveMobilePreviewViewport()" in drive_js
    assert 'window.matchMedia("(max-width: 720px)")' in drive_js
    assert "if (!options.inlinePreview && isDriveMobilePreviewViewport()) return true;" in drive_js
    auth_js = (ROOT / "public" / "js" / "40-auth-users.js").read_text(encoding="utf-8")
    assert "promptDriveGlobalE2eePassphraseOnLogin" not in auth_js
    assert "promptDriveGlobalE2eePassphraseOnLogin" not in drive_js
    assert "driveGlobalE2eePassphrase" not in drive_js
    assert "全域 E2EE 密碼" not in drive_js
    assert "DRIVE_FULLSCREEN_PREVIEW_MS" in drive_js
    assert "/preview/content" in drive_js
    assert "drive-preview-archive" in drive_js
    assert "drive-preview-text" in drive_js
    assert "function renderDriveArchiveEntries(entries)" in drive_js
    assert 'class="drive-archive-list"' in drive_js
    assert 'class="drive-archive-entry"' in drive_js
    assert 'class="drive-archive-kind"' in drive_js
    assert 'class="drive-archive-entry-meta"' in drive_js
    assert "壓縮後" in drive_js
    assert '".7z", ".rar", ".tar", ".gz"' in drive_js
    assert "closeDrivePreview()" in drive_js
    assert "async function previewDriveE2eeFile(fileId)" in drive_js
    assert "decryptDriveE2eeFileForSession" in drive_js
    assert "function normalizeDrivePreviewBlobMime(blob, expectedMime = \"\")" in drive_js
    assert "new Blob([blob], { type: targetMime })" in drive_js
    assert "function drivePreviewUsesDirectStream(preview)" in drive_js
    assert 'return category === "audio" || category === "video" || category === "pdf";' in drive_js
    assert "function drivePreviewHasReadyHls(preview)" in drive_js
    assert "function drivePreviewServiceOptions(preview)" in drive_js
    assert "drive-service-mode-control" in drive_js
    assert "drivePreviewRealtimeProxyUrl" in drive_js
    assert "Standard · 即時轉封裝" in drive_js
    assert "function drivePreviewSubtitles(preview)" in drive_js
    assert "function syncDrivePreviewSubtitleTracks(player, preview, fileId = \"\")" in drive_js
    assert "data-drive-preview-subtitle" in drive_js
    assert "data-drive-subtitle-shift-step" in drive_js
    assert "driveSubtitleUrlWithShift" in drive_js
    assert "shift_ms" in drive_js
    assert "function attachDriveHlsPreview(fileId, preview" in drive_js
    assert "DRIVE_HLS_JS_URL" in drive_js
    assert "/cloud-drive/files/${encodeURIComponent(fileId)}/hls/master.m3u8" in drive_js
    assert "driveHlsPlayerMarkup(preview" in drive_js
    assert "sharedFileSubtitleUrlWithShift" in shared_file_js
    assert "data-shared-file-subtitle-shift-step" in shared_file_js
    assert "/api/storage/shared/<token>/hls/subtitles/<subtitle_name>.vtt" in share_preview_routes
    assert "async function resolveDrivePreviewMediaUrl(fileId, csrf, preview" in drive_js
    assert 'return drivePreviewContentUrl(fileId);' in drive_js
    assert "function renderDrivePdfPreview(url, title, { encrypted = false } = {})" in drive_js
    assert '這份 PDF 已在瀏覽器解密。若內嵌檢視器無法開啟，請改用新分頁或直接下載。' in drive_js
    assert '若瀏覽器內建 PDF 檢視器未載入，請改用新分頁開啟或直接下載。' in drive_js
    assert '<iframe src="${url}" title="${safeTitle}" loading="lazy"></iframe>' in drive_js
    assert '在新分頁開啟 PDF' in drive_js
    assert '下載 PDF' in drive_js
    assert "driveE2eeSessionPassphrases" in drive_js
    assert "driveE2eeRecentSessionPassphrases" in drive_js
    assert "clearDriveE2eeSessionPassphrases" in (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    assert "return previewAlbumFileFullscreen(fileId, options.fileName || \"\")" in drive_js
    assert 'preview.category === "video"' in drive_js
    assert 'preview.category === "audio"' in drive_js
    assert "function driveDirectPlayerMarkup(fileId, preview, url" in drive_js
    assert "function driveRealtimeProxyPlayerMarkup(fileId, preview" in drive_js
    assert "attachDrivePlainMediaPreview(fileId, preview" in drive_js
    assert 'preview.category === "pdf"' in drive_js
    assert 'preview.category === "image"' in drive_js
    assert '"img-src":     "\'self\' data: blob:"' in server_py
    assert '"media-src":   "\'self\' blob:"' in server_py
    assert '"worker-src":  "\'self\' blob:"' in server_py
    assert '"frame-src":   "\'self\' blob:"' in server_py
    assert '"object-src":  "\'none\'"' in server_py
    styles_css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    assert ".drive-preview-panel object," in styles_css
    assert ".album-full-preview-body object," in styles_css
    assert ".drive-pdf-preview {" in styles_css
    assert ".drive-pdf-preview iframe {" in styles_css
    assert ".drive-archive-list {" in styles_css
    assert ".drive-archive-entry {" in styles_css
    assert ".drive-archive-kind {" in styles_css


def test_filemanager_and_albummanager_ui_are_wired():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    drive_js = ((ROOT / "public" / "js" / "35-drive.js").read_text(encoding="utf-8") + "\n" + (ROOT / "public" / "js" / "35-drive-preview-share.js").read_text(encoding="utf-8"))
    shared_file_js = (ROOT / "public" / "js" / "shared-file.js").read_text(encoding="utf-8")
    share_preview_routes = (ROOT / "routes" / "file_sections" / "share_preview_routes.py").read_text(encoding="utf-8")
    admin_js = (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")

    assert 'id="storage-upload-file"' in index_html
    assert 'id="storage-folder-upload-btn"' in index_html
    assert 'id="drive-remote-download-btn"' in index_html
    assert 'id="drive-remote-torrent-inline-btn"' in index_html
    assert 'id="storage-upload-folder"' in index_html
    assert 'id="drive-capacity-visual"' in index_html
    assert 'id="drive-capacity-percent-label"' in index_html
    assert 'id="drive-capacity-note"' in index_html
    assert "drive-capacity-charge" not in index_html
    assert 'id="drive-upload-client-scan-report"' not in index_html
    assert 'id="drive-upload-mode-client-scan-report"' not in index_html
    assert "附上本機掃描回報" not in index_html
    assert "webkitdirectory" in index_html
    assert 'id="storage-folder-path"' in index_html
    assert 'id="storage-browser-list"' in index_html
    assert 'id="storage-organize-path"' in index_html
    assert 'id="storage-file-list"' not in index_html
    assert 'id="storage-trash-list"' in index_html
    assert 'id="drive-section-tabs"' in index_html
    assert 'data-drive-page-tab="files"' in index_html
    assert 'data-drive-page-tab="capacity"' in index_html
    assert 'id="drive-files-page"' in index_html
    assert 'id="drive-capacity-page"' in index_html
    assert 'id="album-create-title"' in index_html
    assert 'id="album-create-description"' in index_html
    assert 'id="album-create-share-password"' not in index_html
    assert 'id="album-edit-share-password"' not in index_html
    assert 'id="album-edit-clear-share-password"' not in index_html
    assert 'id="album-picker-select"' in index_html
    assert 'id="album-smart-strategy"' in index_html
    assert 'data-drive-action="smart-organize-albums"' in index_html
    assert 'id="album-list"' in index_html
    assert 'id="album-detail-card"' in index_html
    assert 'id="album-file-list"' not in index_html
    assert "不列出，持連結可看" in index_html
    assert "async function uploadStorageFile()" in drive_js
    assert "async function uploadStorageFolder()" in drive_js
    assert "let driveLatestQuota = null;" in drive_js
    assert "function renderDriveCapacityGauge" in drive_js
    assert "function setDriveActivePage(page = \"files\")" in drive_js
    assert "function bindDriveSectionTabs()" in drive_js
    assert "data-drive-page-panel" in drive_js
    assert '"--drive-capacity-level"' in drive_js
    assert "zeroQuota" in drive_js
    assert "function formatDriveSpeed" in drive_js
    assert "speed_bytes_per_sec" in drive_js
    assert "function drivePostUploadProcessingMessage(mode)" in drive_js
    assert "伺服器端加密、儲存與掃描中" in drive_js
    assert "伺服器儲存密文與掃描中" in drive_js
    assert "瀏覽器端加密中" in drive_js
    assert "加密完成，開始上傳密文" in drive_js
    assert "DRIVE_RESUMABLE_UPLOAD_THRESHOLD_BYTES" in drive_js
    assert "async function uploadDriveBlobResumable" in drive_js
    assert '"/cloud-drive/resumable-upload/start"' in drive_js
    assert '"/cloud-drive/resumable-upload/sessions?limit=20"' in drive_js
    assert "completeDriveResumableUpload" in drive_js
    assert '"resumable_uploading"' in drive_js
    assert '"waiting_resume"' in drive_js
    assert "function restoreDriveBackgroundTransfers()" in drive_js
    assert "function applyResumableUploadSessionToTransfer" in drive_js
    assert 'data-drive-action="cancel-resumable-upload"' in drive_js
    assert "function syncDriveCsrfFromCookie()" in drive_js
    assert "async function currentDriveCsrfToken" in drive_js
    assert 'readCookie("csrf_token")' in drive_js
    assert 'setCsrfToken(latestCookieToken)' in drive_js
    assert 'result.status === 403 && result.json?.error === "csrf_invalid"' in drive_js
    assert "const retryCsrf = await currentDriveCsrfToken({ force: true });" in drive_js
    assert "if (isDriveE2eeMode(privacyMode)) return \"\";" in drive_js
    assert "if (!isDriveE2eeMode(options.privacyMode) && shouldUseDriveResumableUpload(uploadBlob))" not in drive_js
    assert "if (shouldUseDriveResumableUpload(uploadBlob))" in drive_js
    assert "progressTotalBytes = Math.max(0, progressTotalBytes + uploadDisplayBytes - fileSize);" in drive_js
    assert "async function ensureDriveUploadQuota()" in drive_js
    assert "function driveUploadQuotaError" in drive_js
    assert "async function preflightDriveUploadSize" in drive_js
    assert "超過雲端硬碟容量上限" in drive_js
    assert "await preflightDriveUploadSize(file.size" in drive_js
    assert "await preflightDriveUploadSize(totalBytes" in drive_js
    assert "async function smartOrganizeAlbums()" in drive_js
    assert 'storageAction("/storage/albums/smart-organize", "POST"' in drive_js
    assert 'action === "smart-organize-albums"' in drive_js
    assert "function openStorageFolderUploadPicker()" in drive_js
    assert "function storageUploadRelativePath(file)" in drive_js
    assert "file?.webkitRelativePath" in drive_js
    assert 'form.append("virtual_path", virtualPath)' in drive_js
    assert "async function createStorageFolder()" in drive_js
    assert "async function organizeSelectedStorageFile()" in drive_js
    assert "async function renameStorageFile(id, currentPath, currentName = \"\")" in drive_js
    assert "async function moveStorageFileFromRow(id, currentPath)" in drive_js
    assert "async function renameStorageFolder(path, currentName = \"\")" in drive_js
    assert "async function moveCloudFileToStorage(fileId, name)" in drive_js
    assert "function driveFileIsMedia(file)" in drive_js
    assert 'data-drive-action="publish-to-video"' in drive_js
    assert "分享到影音" in drive_js
    assert "openDriveFileInVideoPublish(fileId, name)" in drive_js
    assert 'id="drive-share-overlay"' in index_html
    assert 'id="drive-share-storage-file-id"' in index_html
    assert 'id="drive-share-scope"' in index_html
    assert 'data-drive-action="share-cloud-file"' in drive_js
    assert 'data-drive-action="create-share-link"' in index_html
    assert "async function createDriveShareLink()" in drive_js
    assert "wrapped_file_key_envelope" in drive_js
    assert "DRIVE_SHARE_FRAGMENT_STORAGE_KEY" in drive_js
    assert "function rememberDriveShareFragment" in drive_js
    assert "function getRememberedDriveShareFragment" in drive_js
    assert "function driveShareUrlHasFragmentKey" in drive_js
    assert "function driveShareUrlWithRememberedFragment" in drive_js
    assert "data-drive-share-copy-status" in drive_js
    assert "copyBtn.dataset.shareUrl = shareUrl" in drive_js
    assert 'copyBtn.dataset.shareRequiresFragment = requiresFragment ? "1" : "0";' in drive_js
    assert 'setDriveShareCopyStatus("連結已複製"' in drive_js
    assert 'flash(msg, err?.message || "分享連結建立失敗", false)' in drive_js
    assert 'flash(msg.message || "分享連結建立失敗", false)' not in drive_js
    assert "payload.storage_file_id = storageFileId" in drive_js
    assert 'data-drive-action="share-storage-folder"' in drive_js
    assert "async function shareStorageFolder(path, name = \"\")" in drive_js
    assert 'visibility: "unlisted"' in drive_js
    assert 'openShareCenterEditor("album", shareId)' in drive_js
    assert 'switchModuleTab("shares")' in drive_js
    assert "sharedFileDownload" in shared_file_js
    assert "sharedFilePreview" in shared_file_js
    assert "sharedFileRenderPreviewMetadata" in shared_file_js
    assert "sharedFileRenderBlobPreview" in shared_file_js
    assert "function sharedFileIsServerEncryptedVideoProcessing(file)" in shared_file_js
    assert "function sharedFileHasReadyHls(file)" in shared_file_js
    assert "function sharedFileServiceOptions(file)" in shared_file_js
    assert "shared-file-service-mode-select" in shared_file_js
    assert "sharedFileRealtimeProxyUrl" in shared_file_js
    assert "Standard · 即時轉封裝" in shared_file_js
    assert "async function sharedFileRenderHlsPreview(file)" in shared_file_js
    assert "SHARED_FILE_HLS_JS_URL" in shared_file_js
    assert "HLS 串流準備中" in shared_file_js
    assert "完成前不觸發主程序整檔解密預覽" in shared_file_js
    assert "function sharedFileShowProgress(title" in shared_file_js
    assert "async function sharedFileFetchBlobWithProgress" in shared_file_js
    assert "正在伺服器端解密並傳輸原始檔預覽" in shared_file_js
    assert "正在下載 E2EE 密文以供預覽" in shared_file_js
    assert "正在瀏覽器端解密 E2EE 預覽" in shared_file_js
    assert "preview_content_url" in shared_file_js
    assert "/api/storage/shared/" in shared_file_js
    assert 'id="shared-file-login-link"' in share_preview_routes
    assert "前往登入" in share_preview_routes
    assert ".shared-file-progress" in share_preview_routes
    assert "function sharedFileSetLoginRequired(required)" in shared_file_js
    assert 'reason === "login_required"' in shared_file_js
    assert "return_to=" in shared_file_js
    assert "sharedFileDecryptBlob" in shared_file_js
    assert "async function moveStorageFolder()" in drive_js
    assert "async function createAlbum()" in drive_js
    assert "async function openAlbum(id" in drive_js
    assert "await openAlbumViewer(id, options);" in drive_js
    assert "async function saveAlbumDetail()" in drive_js
    assert "function albumShareButtonMarkup(album)" in drive_js
    assert "async function shareAlbum(albumId)" in drive_js
    assert 'data-drive-action="share-album"' in drive_js
    assert 'data-drive-action="copy-album-share-link"' not in drive_js
    assert "share_url" in drive_js
    assert 'storageAction(`/storage/albums/${encodeURIComponent(targetId)}`, "PUT", { visibility: "unlisted" })' in drive_js
    assert 'openShareCenterEditor("album", shareId)' in drive_js
    assert "async function removeAlbumFile(albumId, albumFileId)" in drive_js
    assert "請輸入相簿 id" not in drive_js
    assert 'id="storage-breadcrumb"' in index_html
    assert 'id="storage-selection-label"' in index_html
    assert 'data-drive-action="open-storage-folder"' in drive_js
    assert 'data-drive-action="rename-storage-folder"' in drive_js
    assert 'data-drive-action="rename-storage-file"' in drive_js
    assert 'data-drive-action="move-storage-file"' in drive_js
    assert 'data-drive-action="move-cloud-to-storage"' in drive_js
    assert 'data-drive-action="folder-to-album"' in drive_js
    assert 'item.status === "failed"' in drive_js
    assert "下載失敗" in drive_js
    assert 'data-drive-action="dismiss-transfer"' in drive_js
    assert 'data-drive-action="pause-remote-download"' in drive_js
    assert 'data-drive-action="resume-remote-download"' in drive_js
    assert 'data-drive-action="cancel-remote-download"' in drive_js
    assert "dismissRemoteDownloadTask" in drive_js
    assert "pauseRemoteDownloadTask" in drive_js
    assert "resumeRemoteDownloadTask" in drive_js
    assert "cancelRemoteDownloadTask" in drive_js
    assert "/cloud-drive/remote-download/tasks/${encodeURIComponent(taskId)}/${action}" in drive_js
    assert "DRIVE_TRANSFER_FAILED_VISIBLE_MS" in drive_js
    assert "DRIVE_REMOTE_STATUS_RETRY_LIMIT" in drive_js
    assert "consecutiveStatusErrors" in drive_js
    assert "狀態暫時讀取失敗，正在重試" in drive_js
    assert 'setTimeout(() => dismissRemoteDownloadTask(task.id, transferId)' not in drive_js
    assert "findDriveTransferRowIdForTask" in drive_js
    assert "async function createAlbumFromFolder(path, name = \"\")" in drive_js
    assert 'storageAction("/storage/folders/album", "POST"' in drive_js
    assert 'storageAction("/storage/folders/trash", "POST"' in drive_js
    assert "操作失敗（HTTP ${res.status}）" in drive_js
    assert 'data-drive-action="edit-text" data-file-id="${sanitize(file.id)}">編輯文字</button>' not in drive_js
    assert 'id="storage-organize-btn"' not in index_html
    assert "loadStorageFiles(csrf)" in drive_js
    assert 'storageUploadBtn.addEventListener("click", openStorageUploadPicker)' in bootstrap_js
    assert 'storageFolderUploadBtn.addEventListener("click", openStorageFolderUploadPicker)' in bootstrap_js
    assert 'storageUploadFile.addEventListener("change", uploadStorageFile)' in bootstrap_js
    assert 'storageUploadFolder.addEventListener("change", uploadStorageFolder)' in bootstrap_js
    assert 'driveRemoteDownloadBtn.addEventListener("click", promptRemoteDriveDownloadUrl)' in bootstrap_js
    assert 'driveRemoteTorrentFile.addEventListener("change", () => startRemoteDriveDownload({ source: "torrent"' in bootstrap_js
    assert 'storageFolderCreateBtn.addEventListener("click", createStorageFolder)' in bootstrap_js
    assert 'storageFolderMoveBtn.addEventListener("click", moveStorageFolder)' in bootstrap_js
    assert 'albumCreateBtn.addEventListener("click", createAlbum)' in bootstrap_js
    assert 'id="s-cloud-drive-global-capacity-limit-mb"' in index_html
    assert "cloud_drive_global_capacity_limit_mb" in admin_js
    assert "全用戶容量上限" in admin_js


def test_album_viewer_has_dedicated_module():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    drive_js = ((ROOT / "public" / "js" / "35-drive.js").read_text(encoding="utf-8") + "\n" + (ROOT / "public" / "js" / "35-drive-preview-share.js").read_text(encoding="utf-8"))
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    admin_js = (
        (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    )
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")

    assert 'id="tab-module-albums"' in index_html
    assert 'id="module-albums"' in index_html
    assert 'id="app-sidebar"' in index_html
    assert 'id="sidebar-toggle"' in index_html
    assert 'id="album-gallery-list"' in index_html
    assert 'id="album-viewer-card"' in index_html
    assert 'id="album-thumb-size"' in index_html
    assert 'id="album-full-preview-overlay"' in index_html
    assert 'class="drive-collapsible-panel" id="album-management-panel"' in index_html
    assert 'class="drive-collapsible-panel album-viewer-panel" id="album-viewer-card"' in index_html
    assert 'data-drive-action="album-preview-prev"' in index_html
    assert 'data-drive-action="album-preview-next"' in index_html
    assert 'src="/js/35-drive.js' in index_html
    assert 'href="/styles.css' in index_html
    assert 'src="/js/00-core.js' in index_html
    assert 'src="/js/40-auth-users.js' in index_html
    assert 'src="/js/50-admin.js' in index_html
    assert 'id="root-storage-user-select"' in index_html
    assert 'id="root-storage-save-btn"' in index_html
    assert 'id="root-storage-users"' in index_html
    assert 'id="drive-root-admin-tab"' in index_html
    assert 'id="drive-root-admin-page"' in index_html
    assert "雲端硬碟 > Root 管理" in index_html
    assert "function loadRootStorageUsers" in admin_js
    assert "function saveRootStorageOverride" in admin_js
    assert "function saveDriveRootStorageSettings" in admin_js
    assert '"/root/storage/users"' in admin_js
    assert 'rootStorageSave.addEventListener("click", saveRootStorageOverride)' in bootstrap_js
    album_module_html = index_html.split('id="module-albums"', 1)[1].split('id="module-comfyui"', 1)[0]
    assert "onclick=" not in album_module_html
    assert "onclick=" not in drive_js
    assert "data-drive-action" in drive_js
    assert "function drivePreviewContentUrl(fileId)" in drive_js
    assert "function driveFileIsImage(file)" in drive_js
    assert "let albumPreviewSequence = []" in drive_js
    assert "function setAlbumPreviewSequence" in drive_js
    assert "function stepAlbumPreview(direction)" in drive_js
    assert "event.key === \"ArrowLeft\"" in drive_js
    assert "event.key === \"ArrowRight\"" in drive_js
    assert "function renderAttachmentFileSelects" in drive_js
    assert "async function ensureAttachmentFileOptionsLoaded" in drive_js
    assert "請先從下拉選單選擇雲端檔案" in drive_js
    assert "function openChatAttachmentPicker()" in drive_js
    assert "function attachmentStoragePath(file, prefix = \"attachment\")" in drive_js
    assert 'joinStoragePath("/attachments", uniqueName)' in drive_js
    assert 'form.append("virtual_path", attachmentStoragePath(selectedFile, contextType || "attachment"))' in drive_js
    assert 'form.append("display_name", selectedFile.name || "attachment.bin")' in drive_js
    assert 'canRemoveContextAttachment(ref)' in drive_js
    assert "const removeButton = canRemove" in drive_js
    assert 'data-drive-action="delete-context-attachment"' in drive_js
    assert "async function deleteContextAttachment" in drive_js
    assert "/cloud-drive/refs/${encodeURIComponent(refId)}/delete" in drive_js
    assert "附件編號讀取失敗" in drive_js
    assert "loadChatMessages(selectedChatRoomId" in drive_js
    assert 'id="chat-attachment-existing-file-id"' in index_html
    assert 'id="chat-attachment-pick-btn"' in index_html
    assert 'id="chat-attachment-upload-btn"' not in index_html
    assert 'id="chat-attachment-existing-btn"' not in index_html
    assert 'form.append("virtual_path", attachmentStoragePath(selectedFile, "chat"))' in drive_js
    assert 'form.append("virtual_path", attachmentStoragePath(selectedFile, "announcement"))' in drive_js
    assert "dm-attachment-existing-file-id" in drive_js
    assert 'id="announcement-attachment-existing-file-id"' in index_html
    assert 'placeholder="file_id"' not in index_html
    assert "chat-message-image-preview" in drive_js
    assert "driveTransferRows" in drive_js
    assert "xhrUploadWithProgress" in drive_js
    assert "data-folder-path" in drive_js
    assert "function storageFolderRowPathFromEventTarget(target)" in drive_js
    assert 'document.addEventListener("dblclick", (event) => {' in drive_js
    assert 'target.closest(".drive-file-actions")' in drive_js
    assert 'openStorageFolder(folderPath).catch((err) => alert(err.message || "開啟資料夾失敗"))' in drive_js
    assert "/cloud-drive/remote-download/tasks" in drive_js
    assert "async function restoreRemoteDownloadTasks()" in drive_js
    assert "resumeRemoteDownloadTaskPolling(task)" in drive_js
    assert "function classifyRemoteDownloadInput(rawUrl" in drive_js
    assert "torrentUrlsAsBt" in drive_js
    assert "function promptRemoteDriveDownloadUrl()" in drive_js
    assert "function openRemoteTorrentPicker()" in drive_js
    assert "magnet link 或 .torrent URL" in drive_js
    assert "download_mode: effectiveMode" in drive_js
    assert 'source: "torrent-url"' in drive_js
    assert 'id="drive-remote-torrent-file"' in index_html
    assert 'id="drive-remote-torrent-btn"' in index_html
    assert "/cloud-drive/remote-download/torrent-tasks" in drive_js
    assert "FormData" in drive_js
    assert "async function loadAlbumGallery()" in drive_js
    assert "async function openAlbumViewer(id" in drive_js
    assert "async function fetchDrivePreviewBlob(fileId, csrf)" in drive_js
    assert "async function previewAlbumFileFullscreen(fileId" in drive_js
    assert 'data-drive-action="album-full-preview"' in drive_js
    assert 'data-album-sequence="viewer"' in drive_js
    assert "drive-gallery-photo-tile" in drive_js
    assert "filesEl.classList.add(\"album-photo-grid\")" in drive_js
    assert "card.open = options.openContent !== false;" in drive_js
    assert "const ariaLabel = canTryPreview ? `全頁檢視 ${name}` : `${name} 無法預覽`;" in drive_js
    assert 'data-album-sequence="viewer">預覽</button>' not in drive_js
    assert 'data-storage-file-id="${sanitize(file.storage_file_id)}">下載</button>' not in drive_js
    assert "closeAlbumFullPreview" in drive_js
    assert "hydrateAlbumViewerThumbnails" in drive_js
    assert "let blob = await fetchDrivePreviewBlob(file.file_id, csrf);" in drive_js
    assert "if (!getDriveE2eeSessionPassphraseCandidates(file.file_id).length) throw err;" in drive_js
    assert "buildDriveE2eePreview(file.file_id, csrf)" in drive_js
    assert "const blob = await fetchDrivePreviewContent(file.file_id, csrf);" not in drive_js
    assert 'data-drive-action="add-cloud-to-album"' in drive_js
    assert 'tabModuleAlbums.style.display = (canAccessModule("privacy_uploads") && isFeatureEnabledForUi("feature_storage_albums_enabled", false)) ? "" : "none"' in core_js
    assert "SIDEBAR_MENU_CONFIG" in core_js
    assert '{ label: "相簿", action: "module:albums" }' not in core_js
    assert 'featureKey: "feature_storage_albums_enabled"' in core_js
    assert "SIDEBAR_ICON_PATHS" in core_js
    assert "sidebar-footer" in index_html
    assert "sidebar-current-user" in index_html
    assert "sidebar-current-level" in index_html
    assert "sidebar-points" in index_html
    assert "sidebar-violations" in index_html
    assert "sidebar-server-version" in index_html
    assert "app-action-bar" in index_html
    assert 'id="session-countdown-label"' in index_html
    assert "member_level_label" in core_js
    assert "特殊階級" in core_js
    assert "RESET_RUNTIME_STATE" in index_html
    assert 'id="security-profile-load-current-btn"' in index_html
    assert 'id="security-mode-profile-preview"' in index_html
    assert 'id="server-mode-profile-preview"' in index_html
    assert 'id="server-mode-token-hint"' in index_html
    assert 'id="server-mode-internal-test-panel" style="display:none;"' in index_html
    assert 'id="server-mode-tester-token-panel" style="display:none;"' in index_html
    assert 'id="internal-test-token-usage-wrap" class="security-profile-preview" style="display:none;"' in index_html
    assert 'id="tester-token-usage-wrap" class="security-profile-preview" style="display:none;"' in index_html
    assert "這顆 token 只綁定指定帳號" in index_html
    assert 'id="internal-test-token-user-id"' in index_html
    assert 'id="internal-test-token-username"' in index_html
    assert "這不是登入 token，不能拿去填 <code>/api/login</code>" in index_html
    assert "loadCurrentSecurityProfileDraft" in admin_js
    assert "renderSecurityProfilePreview" in admin_js
    assert "function applySecurityProfileToInputs" in admin_js
    assert "function applySecurityProfileDataToInputs" in admin_js
    assert "function previewSecurityProfileSelection" in admin_js
    assert "function bindSecurityProfileSelect" in admin_js
    assert "function updateServerModeTokenPanels(modeOverride = null)" in admin_js
    assert 'const usage = $("internal-test-token-usage-wrap");' in admin_js
    assert 'const usage = $("tester-token-usage-wrap");' in admin_js
    assert '"feature_audit_log_enabled"' in admin_js
    assert '"feature_economy_enabled"' in admin_js
    assert '"feature_experiments_enabled"' in admin_js
    assert "FEATURE_SETTING_GROUPS" in admin_js
    assert "FEATURE_SERVICE_BUNDLES" in admin_js
    assert '"all-enabled"' in admin_js
    assert '"ops-minimum"' in admin_js
    assert '"minimum-ops"' in admin_js
    assert '"raspberry-lite"' in admin_js
    assert '"safe-community"' in admin_js
    assert '"creator-media"' in admin_js
    assert '"points-chain-rc1"' in admin_js
    assert '"exchange-ops"' in admin_js
    assert '"low-resource"' in admin_js
    assert "全開" in admin_js
    assert "維運骨架" in admin_js
    assert "最低維運" in admin_js
    assert "Raspberry 套餐" in admin_js
    assert "輕量主機預設" in admin_js
    assert "renderFeatureSwitchGroups" in admin_js
    assert "setFeatureGroupState" in admin_js
    assert "bundle.replace === true" in admin_js
    assert "feature-bundle-toolbar" in index_html
    assert "feature-bundle-select" in index_html
    assert "feature-bundle-apply" in index_html
    assert "feature-bundle-preview" in index_html
    assert "feature-switch-groups" in index_html
    assert "feature-advisory-list" in index_html
    assert "先選功能套餐看摘要" in index_html
    assert "請先選擇功能套餐" in admin_js
    assert "category: \"低資源\"" in admin_js
    assert 'id="sc-feature-audit-log-enabled"' in index_html
    assert 'id="sc-feature-economy-enabled"' in index_html
    assert 'previewSecurityProfileSelection("security-mode-select", "security-mode-profile-preview", "sc")' in bootstrap_js
    assert 'previewSecurityProfileSelection("server-mode-select", "server-mode-profile-preview", "s")' in bootstrap_js
    assert 'bindSecurityProfileSelect("security-mode-select", "security-mode-profile-preview", "sc")' in admin_js
    assert 'bindSecurityProfileSelect("server-mode-select", "server-mode-profile-preview", "s")' in admin_js
    assert "按套用才會寫入伺服器" in admin_js
    assert "await loadSettings();" in admin_js
    assert "populateProfileSelect(\"server-mode-select\"" in admin_js
    assert 'updateServerModeTokenPanels(serverModeSelect.value)' in bootstrap_js
    assert 'confirm: "RESET_RUNTIME_STATE"' in admin_js
    assert '"RUN_RESET"' not in admin_js
    assert "icon-action-btn" in index_html
    assert "server-connection-light" not in index_html
    assert "startClock" not in core_js
    assert "SIDEBAR_COLLAPSED_STORAGE_KEY" in core_js
    assert "function sidebarCollapsedStorageKey()" in core_js
    assert "localStorage.setItem(sidebarCollapsedStorageKey()" in core_js
    assert "data-sidebar-action" in core_js
    assert 'switchModuleTab("albums")' in bootstrap_js
    assert "sidebarToggle.addEventListener" in bootstrap_js
    assert 'normTab === "albums"' in admin_js
    assert "renderStorageFeatureDisabled" in drive_js
    assert ".drive-collapsible-panel" in (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    styles_css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    assert ".drive-capacity-liquid::before" in styles_css
    assert "@keyframes drive-capacity-wave" in styles_css
    assert ".drive-capacity-charge" not in styles_css
    assert ".drive-section-tabs" in styles_css
    assert ".drive-subpage.active" in styles_css
    assert ".settings-feature-advisory" in (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    assert ".album-preview-nav" in (ROOT / "public" / "styles.css").read_text(encoding="utf-8")


def test_album_preview_category_uses_storage_name_before_uploaded_metadata():
    drive_js = ((ROOT / "public" / "js" / "35-drive.js").read_text(encoding="utf-8") + "\n" + (ROOT / "public" / "js" / "35-drive-preview-share.js").read_text(encoding="utf-8"))

    assert 'const name = file?.display_name || file?.virtual_path || file?.original_filename_plain_for_public || file?.storage_path || "";' in drive_js
    assert 'const canTryPreview = isE2ee || category === "image" || category === "metadata";' in drive_js
    assert '["image", "metadata"].includes(driveFileCategory(file))' in drive_js
    assert 'startsWith("image/")' in drive_js


def test_album_gallery_layout_wraps_long_filenames():
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    drive_js = ((ROOT / "public" / "js" / "35-drive.js").read_text(encoding="utf-8") + "\n" + (ROOT / "public" / "js" / "35-drive-preview-share.js").read_text(encoding="utf-8"))

    assert 'class="drive-gallery-file-info"' in drive_js
    assert ".drive-gallery-tile {" in css
    assert ".drive-gallery-grid.album-photo-grid {" in css
    assert ".drive-gallery-photo-tile {" in css
    assert ".drive-gallery-photo-tile:hover .drive-gallery-thumb" in css
    assert "transform: scale(1.055);" in css
    assert ".album-thumb-small .drive-gallery-photo-tile," in css
    assert "overflow: hidden;" in css
    assert ".drive-gallery-tile strong" in css
    assert "overflow-wrap: anywhere;" in css
    assert "word-break: break-word;" in css


def test_cloud_drive_toolbar_buttons_wrap_on_mobile():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    assert 'data-drive-action="open-text-document-modal">新增文檔</button>' in index_html
    assert "#module-drive .drive-card-heading > .drive-file-actions" in css
    assert "grid-template-columns: repeat(auto-fit, minmax(6.8rem, 1fr));" in css
    assert "white-space: normal;" in css
    assert "overflow-wrap: anywhere;" in css
    assert "text-align: center;" in css


def test_cloud_drive_privacy_modes_use_human_labels():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    drive_js = ((ROOT / "public" / "js" / "35-drive.js").read_text(encoding="utf-8") + "\n" + (ROOT / "public" / "js" / "35-drive-preview-share.js").read_text(encoding="utf-8"))
    platform_js = (ROOT / "public" / "js" / "57-platform-centers.js").read_text(encoding="utf-8")

    assert 'value="standard_plain">一般檔案（可掃毒、可預覽、可分享）' in index_html
    assert 'value="server_encrypted">伺服器端加密（磁碟密文、下載明文）' in index_html
    assert 'value="e2ee">端到端加密（站方無法讀取）' in index_html
    assert "三種模式怎麼選" in index_html
    assert "非 E2EE 會讓伺服器取得明文" in index_html
    assert "E2EE 上傳時附本機掃描回報" not in index_html
    assert "新增文檔" in index_html
    assert 'data-drive-action="create-text-document"' in index_html
    assert "virtual_path: joinStoragePath(currentStoragePath, filename)" in drive_js
    assert "drivePrivacyModeLabel(file.privacy_mode)" in drive_js
    assert "DRIVE_PRIVACY_MODE_COMPARISON" in drive_js
    assert "伺服器端加密" in drive_js
    assert "站方無法讀取" in drive_js
    assert "driveRenderTextPreview" in drive_js
    assert "driveHighlightCode" in drive_js
    assert "需密碼預覽" in drive_js
    assert "解密預覽" in drive_js
    assert "isDriveE2eeServerPreviewError" in drive_js
    assert "return previewDriveE2eeFile(fileId);" in drive_js
    assert "root 上限：全用戶容量設定（磁碟總容量 95%）" in drive_js
    assert "root_global_capacity_limit_mb" in drive_js
    assert "manager 上限：1 GB" in drive_js
    assert "warning_active" in drive_js
    assert "function shareCenterLinkUrl" in platform_js
    assert "requires_fragment_key" in platform_js
    assert "driveShareUrlWithRememberedFragment" in platform_js
    assert "data-share-missing-fragment" in platform_js
    assert "分享連結缺少 E2EE 片段金鑰" in platform_js


def test_cloud_drive_storage_upgrade_ui_is_wired():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    drive_js = ((ROOT / "public" / "js" / "35-drive.js").read_text(encoding="utf-8") + "\n" + (ROOT / "public" / "js" / "35-drive-preview-share.js").read_text(encoding="utf-8"))
    routes_py = (ROOT / "routes" / "files.py").read_text(encoding="utf-8")
    upload_security_py = (ROOT / "services" / "security" / "upload_security.py").read_text(encoding="utf-8")

    assert 'id="drive-storage-upgrade-card"' in index_html
    assert 'id="drive-storage-upgrade-select"' in index_html
    assert 'data-drive-action="purchase-storage-upgrade"' in index_html
    assert "function renderStorageUpgrade" in drive_js
    assert 'currentUser === "root"' in drive_js
    assert "root 不需要購買容量方案" in drive_js
    assert "root 依實際磁碟容量控管，不需要購買容量方案" in drive_js
    assert "let driveStorageUpgradeCanPurchase = false;" in drive_js
    assert "button.disabled = !driveStorageUpgradeCanPurchase || !driveStorageUpgradeCatalog.length;" in drive_js
    assert "正在購買容量..." in drive_js
    assert "async function loadStorageUpgradeOptions" in drive_js
    assert "async function purchaseStorageUpgrade" in drive_js
    assert "/cloud-drive/storage-upgrades" in drive_js
    assert "/cloud-drive/storage-upgrades/purchase" in drive_js
    assert 'if (action === "purchase-storage-upgrade") return purchaseStorageUpgrade();' in drive_js
    assert '@app.route("/api/cloud-drive/storage-upgrades", methods=["GET"])' in routes_py
    assert '@app.route("/api/cloud-drive/storage-upgrades/purchase", methods=["POST"])' in routes_py
    assert "root 不需要用積分購買容量" in routes_py
    assert "purchased_extra_bytes" in upload_security_py
    assert "+storage_purchase" in upload_security_py


def test_core_api_fetch_refreshes_csrf_once():
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")

    assert "async function apiFetch" in core_js
    assert 'payload.error !== "csrf_invalid"' in core_js
    assert "fetchCsrfToken({ force: true })" in core_js
    assert "const retried = await apiFetch(url, { ...options, credentials: opts.credentials, headers: retryHeaders }, false);" in core_js
    assert "return retried;" in core_js
    assert 'headers.set("X-CSRF-Token", await fetchCsrfToken());' in core_js
    assert "BroadcastChannel" in core_js


def test_cloud_drive_e2ee_upload_prepares_required_crypto_fields():
    drive_js = ((ROOT / "public" / "js" / "35-drive.js").read_text(encoding="utf-8") + "\n" + (ROOT / "public" / "js" / "35-drive-preview-share.js").read_text(encoding="utf-8"))

    assert "async function prepareDriveE2eeUpload(file, passphrase)" in drive_js
    assert "includeClientScanReport" not in drive_js
    assert 'form.append("client_scan_report"' not in drive_js
    assert "window.crypto.subtle.generateKey" in drive_js
    assert "deriveDriveE2eePassphraseKey" in drive_js
    assert "PBKDF2" in drive_js
    assert "encrypted_file_key" in drive_js
    assert "function driveEncryptedUploadFields" in drive_js
    assert "encrypted_file_key: encrypted.encrypted_file_key" in drive_js
    assert "encrypted_metadata: encrypted.encrypted_metadata" in drive_js
    assert "ciphertext_sha256: encrypted.ciphertext_sha256" in drive_js
    assert "encryption_algorithm: encrypted.encryption_algorithm" in drive_js
    assert "encryption_version: encrypted.encryption_version" in drive_js
    assert "nonce: encrypted.nonce" in drive_js
    assert 'form.append("file", uploadBlob, uploadFilename)' in drive_js
    assert "appendDriveUploadFields(form, uploadFields)" in drive_js
    assert 'const originalName = file.name || "未命名檔案";' in drive_js
    assert "filename: originalName" in drive_js
    assert "vault.bin" not in drive_js
    assert "browser_passphrase_pbkdf2_v2" in drive_js
    assert "localStorage.getItem(DRIVE_E2EE" not in drive_js
    assert "此瀏覽器不支援端到端加密上傳" in drive_js


def test_cloud_drive_e2ee_download_decrypts_in_browser():
    drive_js = ((ROOT / "public" / "js" / "35-drive.js").read_text(encoding="utf-8") + "\n" + (ROOT / "public" / "js" / "35-drive-preview-share.js").read_text(encoding="utf-8"))

    assert "async function unwrapDriveFileKey(encryptedFileKey, passphrase)" in drive_js
    assert "async function decryptDriveE2eeBlob(blob, e2ee, passphrase)" in drive_js
    assert "/e2ee-key" in drive_js
    assert "/preview/content" in drive_js
    assert "askDriveE2eePassphrase" in drive_js
    assert "getDriveE2eeSessionPassphrase" in drive_js
    assert "function rememberDriveE2eeSessionPassphrase(fileId, passphrase)" in drive_js
    assert "function getRememberedDriveE2eeSessionPassphrase(fileId)" in drive_js
    assert "function rememberDriveE2eeRecentSessionPassphrase(passphrase)" in drive_js
    assert "function getDriveE2eeSessionPassphraseCandidates(fileId)" in drive_js
    assert "driveE2eeRecentSessionPassphrases.forEach(addCandidate);" in drive_js
    assert "rememberDriveE2eeRecentSessionPassphrase(passphrase);" in drive_js
    assert "const candidates = getDriveE2eeSessionPassphraseCandidates(fileId);" in drive_js
    assert "for (const passphrase of candidates)" in drive_js
    assert "{ promptOnMiss = true }" in drive_js
    assert "if (!promptOnMiss && !candidates.length)" in drive_js
    assert "DRIVE_E2EE_PREVIEW_NO_RECENT_PASSWORD" in drive_js
    assert "DRIVE_E2EE_PREVIEW_DECRYPT_FAILED" in drive_js
    assert "正在使用最近輸入過的 E2EE 密碼嘗試預覽" in drive_js
    assert "等待 E2EE 密碼並在瀏覽器解密中" not in drive_js
    assert "const passphrase = await getDriveE2eeSessionPassphrase(fileId, promptText, { force: true });" in drive_js
    assert "const decrypted = await decryptDriveE2eeBlob(blob, keyJson.e2ee, passphrase);" in drive_js
    assert "rememberDriveE2eeSessionPassphrase(fileId, passphrase);" in drive_js
    assert "if (!getDriveE2eeSessionPassphraseCandidates(file.file_id).length) throw err;" in drive_js
    assert "buildDriveE2eePreview(file.file_id, csrf)" in drive_js
    assert "image · E2EE" in drive_js
    assert "outputBlob = decrypted.blob" in drive_js
    assert "name = decrypted.filename || name" in drive_js
    assert "伺服器無法重設或找回此密碼" in drive_js


def test_share_link_copy_buttons_have_clipboard_fallback():
    """Issue #176 / #177 regression guard.

    `copyDriveShareUrl` (drive) and `copyVideoLink` (videos) call
    `navigator.clipboard.writeText`, which is undefined in non-secure
    contexts (HTTP). The fallback MUST give the user a way to manually
    select+copy the URL — not just flash a toast that disappears."""
    drive_js = ((ROOT / "public" / "js" / "35-drive.js").read_text(encoding="utf-8") + "\n" + (ROOT / "public" / "js" / "35-drive-preview-share.js").read_text(encoding="utf-8"))
    video_js = (ROOT / "public" / "js" / "39-videos.js").read_text(encoding="utf-8")

    # Drive: prompt-based fallback is OK (user can select+copy).
    assert "async function copyDriveShareUrl(url, options = {})" in drive_js
    assert "navigator.clipboard.writeText(shareUrl)" in drive_js
    assert 'setDriveShareCopyStatus("連結已複製"' in drive_js
    assert "分享連結缺少 E2EE 片段金鑰" in drive_js
    assert 'window.prompt("分享連結"' in drive_js, (
        "drive copyDriveShareUrl must offer a window.prompt fallback so the "
        "URL is selectable when navigator.clipboard is unavailable"
    )

    # Videos: assert copyVideoLink has a fallback that lets the user
    # actually copy the URL (window.prompt OR a persistent visible element).
    assert "async function copyVideoLink(videoId, options = {})" in video_js
    assert "navigator.clipboard.writeText(url)" in video_js
    has_prompt_fallback = "window.prompt" in video_js
    has_input_fallback = (
        "select()" in video_js and "execCommand" in video_js
    )
    assert has_prompt_fallback or has_input_fallback, (
        "copyVideoLink fallback must offer a way for the user to manually "
        "select and copy the URL when navigator.clipboard is unavailable. "
        "videoMsg(url, true) alone is a transient toast and not selectable. "
        "See issue #176."
    )
