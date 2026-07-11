// Album / preview / share block moved out of 35-drive.js for readability.

function albumFileDisplayName(file) {
  return file.display_name || file.original_filename_plain_for_public || file.virtual_path || file.file_id || "image";
}

function absoluteAlbumShareUrl(url) {
  if (!url) return "";
  try {
    return new URL(url, window.location.origin).toString();
  } catch (err) {
    return String(url || "");
  }
}

function albumShareButtonMarkup(album) {
  const albumId = album?.id || "";
  if (!albumId) return "";
  return `<button class="btn btn-sm btn-primary" type="button" data-drive-action="share-album" data-album-id="${sanitize(albumId)}">分享</button>`;
}

async function shareAlbum(albumId) {
  const targetId = albumId || selectedAlbumId || selectedAlbumViewerId || "";
  if (!targetId) {
    alert("請先選擇相簿");
    return;
  }
  try {
    const json = await storageAction(`/storage/albums/${encodeURIComponent(targetId)}`, "PUT", { visibility: "unlisted" });
    const album = json.album || {};
    const shareId = album?.share_link?.id || "";
    selectedAlbumId = album.id || targetId;
    selectedAlbumViewerId = album.id || targetId;
    await loadDriveDashboard();
    await loadAlbumGallery();
    if (typeof switchModuleTab === "function") switchModuleTab("shares");
    if (typeof openShareCenterEditor === "function") {
      await openShareCenterEditor("album", shareId);
    } else if (typeof loadShareCenter === "function") {
      await loadShareCenter();
    }
  } catch (err) {
    alert(err.message || "相簿分享設定開啟失敗");
  }
}

function renderAlbumDetail(album) {
  const card = $("album-detail-card");
  if (!card) return;
  selectedAlbumId = album.id || "";
  card.style.display = "block";
  const title = $("album-detail-title");
  const meta = $("album-detail-meta");
  if (title) title.textContent = album.title || "相簿內容";
  if (meta) {
    meta.innerHTML = `${sanitize(albumVisibilityLabel(album.visibility))} · ${Number((album.files || []).length)} 個檔案 · ${sanitize(album.updated_at || album.created_at || "")}`;
  }
  card.querySelectorAll('[data-drive-action="share-album"]').forEach((button) => {
    button.dataset.albumId = album.id || "";
  });
  if ($("album-edit-title")) $("album-edit-title").value = album.title || "";
  if ($("album-edit-description")) $("album-edit-description").value = album.description || "";
  if ($("album-edit-visibility")) $("album-edit-visibility").value = album.visibility || "private";
}

async function openAlbum(id, options = {}) {
  if (!id) return;
  try {
    await openAlbumViewer(id, options);
  } catch (err) {
    if (!options.quiet) alert(err.message || "相簿讀取失敗");
  }
}

function closeAlbumDetail() {
  selectedAlbumId = "";
  const card = $("album-detail-card");
  if (card) card.style.display = "none";
}

async function saveAlbumDetail() {
  if (!selectedAlbumId) return;
  try {
    const payload = {
      title: $("album-edit-title")?.value || "",
      description: $("album-edit-description")?.value || "",
      visibility: $("album-edit-visibility")?.value || "private",
    };
    const json = await storageAction(`/storage/albums/${encodeURIComponent(selectedAlbumId)}`, "PUT", payload);
    renderAlbumDetail(json.album || {});
    await loadDriveDashboard();
    await loadAlbumGallery();
  } catch (err) { alert(err.message || "相簿儲存失敗"); }
}

async function removeAlbumFile(albumId, albumFileId) {
  try {
    const json = await storageAction(`/storage/albums/${encodeURIComponent(albumId)}/files/${encodeURIComponent(albumFileId)}`, "DELETE");
    renderAlbumDetail(json.album || {});
    await loadDriveDashboard();
  } catch (err) { alert(err.message || "移出相簿失敗"); }
}

function renderAlbumGallery(albums) {
  const list = $("album-gallery-list");
  if (!list) return;
  if (!Array.isArray(albums) || !albums.length) {
    list.innerHTML = `<div class="drive-empty">尚無相簿</div>`;
    return;
  }
  list.innerHTML = albums.map((album) => `
    <div class="drive-gallery-tile">
      <div>
        <strong>${sanitize(album.title || album.id)}</strong>
        <div class="drive-card-sub">${sanitize(albumVisibilityLabel(album.visibility))} · ${Number(album.file_count || 0)} 個檔案</div>
      </div>
      <div class="drive-file-actions">
        ${albumShareButtonMarkup(album)}
        <button class="btn btn-primary" type="button" data-drive-action="open-album-viewer" data-album-id="${sanitize(album.id)}">預覽</button>
        <button class="btn btn-danger" type="button" data-drive-action="delete-album" data-album-id="${sanitize(album.id)}">刪除</button>
      </div>
    </div>
  `).join("");
}

async function loadAlbumGallery() {
  if (!storageAlbumsFeatureEnabled()) {
    renderStorageFeatureDisabled();
    return;
  }
  if (!currentUser || !canAccessModule("privacy_uploads")) return;
  const msg = $("album-gallery-msg");
  try {
    const csrf = await fetchCsrfToken();
    const res = await apiFetch(API + "/storage/albums", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) throw new Error(json.msg || "相簿讀取失敗");
    const albums = json.albums || [];
    storageAlbumsCache = Array.isArray(albums) ? albums : [];
    updateAlbumTargetSelect(storageAlbumsCache);
    renderAlbums(storageAlbumsCache);
    renderAlbumGallery(storageAlbumsCache);
    const activeAlbum = storageAlbumsCache.find((album) => album.id === selectedAlbumViewerId) || storageAlbumsCache[0];
    if (activeAlbum) {
      await openAlbumViewer(activeAlbum.id, { quiet: true });
    } else {
      closeAlbumViewer();
    }
    if (msg) msg.className = "msg";
  } catch (err) {
    if (msg) flash(msg, err.message || "相簿讀取失敗", false);
  }
}

function closeAlbumViewer() {
  selectedAlbumViewerId = "";
  albumPreviewSequence = [];
  albumPreviewIndex = -1;
  clearAlbumThumbObjectUrls();
  const card = $("album-viewer-card");
  if (card) {
    card.open = false;
    card.style.display = "none";
  }
}

function renderAlbumPreviewTile(file) {
  const name = albumFileDisplayName(file);
  const category = driveFileCategory(file);
  const isE2ee = typeof driveFileIsE2ee === "function" && driveFileIsE2ee(file);
  const thumbKey = file.id || file.file_id;
  const canTryPreview = isE2ee || category === "image" || category === "metadata";
  const actionAttrs = canTryPreview
    ? `data-drive-action="album-full-preview" data-file-id="${sanitize(file.file_id)}" data-name="${sanitize(name)}" data-album-sequence="viewer"`
    : "disabled";
  const ariaLabel = canTryPreview ? `全頁檢視 ${name}` : `${name} 無法預覽`;
  return `
    <button class="drive-gallery-tile drive-gallery-photo-tile${canTryPreview ? "" : " is-disabled"}" type="button" ${actionAttrs} aria-label="${sanitize(ariaLabel)}" title="${sanitize(name)}">
      <span class="drive-gallery-thumb ${canTryPreview ? "" : "drive-gallery-thumb-placeholder"}" data-album-thumb-key="${sanitize(thumbKey)}">
        <span>${canTryPreview ? (isE2ee ? "解密預覽" : "讀取預覽") : sanitize(category)}</span>
      </span>
      <span class="drive-gallery-file-info" aria-hidden="true">
        <strong>${sanitize(name)}</strong>
        <span class="drive-card-sub">${formatDriveBytes(file.size_bytes || 0)} · <span data-album-category-key="${sanitize(thumbKey)}">${sanitize(isE2ee ? `${category} · E2EE` : category)}</span></span>
      </span>
    </button>
  `;
}

async function hydrateAlbumViewerThumbnails(files) {
  clearAlbumThumbObjectUrls();
  const previewCandidates = (Array.isArray(files) ? files : []).filter((file) => ["image", "metadata"].includes(driveFileCategory(file)));
  if (!previewCandidates.length) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  for (const file of previewCandidates) {
    const thumbKey = file.id || file.file_id;
    const holder = Array.from(document.querySelectorAll("[data-album-thumb-key]")).find((node) => node.dataset.albumThumbKey === String(thumbKey));
    if (!holder) continue;
    try {
      let blob = await fetchDrivePreviewBlob(file.file_id, csrf);
      if (!String(blob.type || "").toLowerCase().startsWith("image/")) throw new Error("不是圖片預覽");
      const url = URL.createObjectURL(blob);
      albumThumbObjectUrls.push(url);
      holder.innerHTML = `<img src="${url}" alt="${sanitize(albumFileDisplayName(file))}" loading="lazy" />`;
      const categoryLabel = Array.from(document.querySelectorAll("[data-album-category-key]")).find((node) => node.dataset.albumCategoryKey === String(thumbKey));
      if (categoryLabel) categoryLabel.textContent = "image";
    } catch (err) {
      try {
        if (!getDriveE2eeSessionPassphraseCandidates(file.file_id).length) throw err;
        const decrypted = await buildDriveE2eePreview(file.file_id, csrf);
        if (!decrypted || decrypted.preview.category !== "image") throw err;
        const url = URL.createObjectURL(decrypted.blob);
        albumThumbObjectUrls.push(url);
        holder.innerHTML = `<img src="${url}" alt="${sanitize(decrypted.preview.filename || albumFileDisplayName(file))}" loading="lazy" />`;
        const categoryLabel = Array.from(document.querySelectorAll("[data-album-category-key]")).find((node) => node.dataset.albumCategoryKey === String(thumbKey));
        if (categoryLabel) categoryLabel.textContent = "image · E2EE";
      } catch (_) {
        holder.innerHTML = `<span>無法預覽</span>`;
      }
    }
  }
}

async function openAlbumViewer(id, options = {}) {
  if (!id) return;
  selectedAlbumViewerId = id;
  const card = $("album-viewer-card");
  const title = $("album-viewer-title");
  const meta = $("album-viewer-meta");
  const filesEl = $("album-viewer-files");
  if (card) {
    card.style.display = "block";
    card.open = options.openContent !== false;
  }
  if (filesEl) filesEl.innerHTML = `<div class="drive-empty">讀取相簿中...</div>`;
  try {
    const json = await storageAction(`/storage/albums/${encodeURIComponent(id)}`, "GET");
    const album = json.album || {};
    const files = Array.isArray(album.files) ? album.files : [];
    albumPreviewSequence = files.filter((file) => file?.file_id && (typeof driveFileIsImage !== "function" || driveFileIsImage(file)));
    albumPreviewIndex = -1;
    if (title) title.textContent = album.title || "相簿";
    if (meta) {
      meta.innerHTML = `${sanitize(albumVisibilityLabel(album.visibility))} · ${files.length} 個檔案${album.description ? ` · ${sanitize(album.description)}` : ""}`;
    }
    card?.querySelectorAll?.('[data-drive-action="share-album"]').forEach((button) => {
      button.dataset.albumId = album.id || "";
    });
    if (!filesEl) return;
    setAlbumThumbSize(getAlbumThumbSize());
    filesEl.classList.add("album-photo-grid");
    filesEl.innerHTML = files.length ? files.map(renderAlbumPreviewTile).join("") : `<div class="drive-empty">這本相簿還沒有檔案</div>`;
    hydrateAlbumViewerThumbnails(files).catch(() => {});
  } catch (err) {
    if (filesEl) filesEl.innerHTML = `<div class="drive-empty">${sanitize(err.message || "相簿讀取失敗")}</div>`;
  }
}

async function loadDriveDashboard(options = {}) {
  if (!currentUser || !canAccessModule("privacy_uploads")) return;
  if (driveDashboardInFlight) return driveDashboardInFlight;
  const lazy = options && options.lazy === true;
  const navigationScoped = options && options.navigationScoped === true;
  const lifecycleGeneration = driveDashboardLifecycleGeneration;
  const navigationController = navigationScoped ? new AbortController() : null;
  const signal = navigationController?.signal;
  const shouldContinue = () => !navigationScoped || isDriveDashboardNavigationCurrent(lifecycleGeneration);
  if (!shouldContinue()) return;
  const lazyRefreshMs = typeof driveDashboardLazyRefreshMs === "function" ? driveDashboardLazyRefreshMs() : DRIVE_DASHBOARD_LAZY_REFRESH_MS;
  if (lazy && driveDashboardLoadedAt && Date.now() - driveDashboardLoadedAt < lazyRefreshMs) {
    return;
  }
  if (navigationController) driveDashboardNavigationAbortController = navigationController;
  driveDashboardInFlight = (async () => {
    updateDriveE2eePassphraseVisibility();
    const msg = $("drive-msg");
    try {
      await abortableWait(fetchCsrfToken({ force: true }), signal, "Drive dashboard load aborted");
      if (!shouldContinue()) return;
      const csrf = getCsrfToken();
      const res = await apiFetch(API + "/files/security-policy", {
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf || "" },
        signal,
      });
      const json = await res.json().catch(() => ({}));
      if (!shouldContinue()) return;
      if (!json.ok) {
        if (msg) flash(msg, json.msg || "雲端硬碟狀態讀取失敗", false);
        return;
      }
      renderDriveDashboard(json);
      await loadStorageUpgradeOptions({ signal });
      if (!shouldContinue()) return;
      await loadRemoteDownloadCapabilities({ signal });
      if (!shouldContinue()) return;
      if (typeof restoreDriveBackgroundTransfers === "function") {
        await restoreDriveBackgroundTransfers({ signal, shouldContinue });
      } else {
        await restoreRemoteDownloadTasks({ signal });
      }
      if (!shouldContinue()) return;
      await loadDriveFiles(csrf, { signal });
      if (!shouldContinue()) return;
      await loadStorageFiles(csrf, { signal });
      if (!shouldContinue()) return;
      driveDashboardLoadedAt = Date.now();
      if (msg) msg.className = "msg";
    } catch (err) {
      if (err?.name !== "AbortError" && msg) flash(msg, "雲端硬碟狀態讀取失敗", false);
    } finally {
      if (driveDashboardNavigationAbortController === navigationController) {
        driveDashboardNavigationAbortController = null;
      }
      driveDashboardInFlight = null;
    }
  })();
  return driveDashboardInFlight;
}

async function loadStorageUpgradeOptions({ signal } = {}) {
  const card = $("drive-storage-upgrade-card");
  if (!card) return;
  const csrf = getCsrfToken() || await abortableWait(fetchCsrfToken({ force: true }), signal, "Storage upgrade load aborted");
  const res = await apiFetch(API + "/cloud-drive/storage-upgrades", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" },
    signal,
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) {
    renderStorageUpgrade({
      ok: false,
      can_purchase: false,
      message: json.msg || `容量方案讀取失敗（HTTP ${res.status}）`,
      catalog: [],
      active_purchases: [],
    });
    return;
  }
  renderStorageUpgrade(json);
}

function openStorageUpgradePanel() {
  const overlay = $("drive-storage-upgrade-overlay");
  if (!overlay) return;
  overlay.classList.add("show");
  overlay.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  loadStorageUpgradeOptions().catch(() => {});
}

function closeStorageUpgradePanel() {
  const overlay = $("drive-storage-upgrade-overlay");
  if (!overlay) return;
  overlay.classList.remove("show");
  overlay.setAttribute("aria-hidden", "true");
  document.body.classList.remove("modal-open");
}

async function purchaseStorageUpgrade() {
  const msg = $("drive-msg");
  if (!driveStorageUpgradeCanPurchase) {
    if (msg) flash(msg, driveStorageUpgradeMessage || "目前沒有可購買的容量方案", false);
    return;
  }
  if (currentUser === "root") {
    if (msg) flash(msg, "root 依實際磁碟容量控管，不需要購買容量方案", false);
    return;
  }
  const itemKey = $("drive-storage-upgrade-select")?.value || "";
  if (!itemKey) {
    if (msg) flash(msg, "請先選擇容量方案", false);
    return;
  }
  const button = document.querySelector("[data-drive-action='purchase-storage-upgrade']");
  if (button) button.disabled = true;
  if (msg) flash(msg, "正在購買容量...", true);
  try {
    const csrf = getCsrfToken() || await fetchCsrfToken({ force: true });
    const res = await apiFetch(API + "/cloud-drive/storage-upgrades/purchase", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify({ item_key: itemKey }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) {
      if (msg) flash(msg, json.msg || `容量購買失敗（HTTP ${res.status}）`, false);
      return;
    }
    if (msg) flash(msg, "容量已加購，積分已扣除", true);
    renderDriveDashboard({ quota: json.usage });
    await loadStorageUpgradeOptions();
    if (typeof loadEconomyDashboard === "function") {
      loadEconomyDashboard().catch(() => {});
    }
  } catch (err) {
    if (msg) flash(msg, err.message || "容量購買失敗", false);
  } finally {
    if (button) button.disabled = false;
  }
}
