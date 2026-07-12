let communityBoards = [];
let communityCategories = [];
let communityAnnouncements = [];
let communityAnnouncementsEtag = "";
let communityBoardReviews = [];
let communityThreadReviews = [];
let selectedCommunityBoardId = null;
let selectedCommunityThreadId = null;
let communityThreads = [];
let selectedCommunityBoard = null;
let selectedCommunityThread = null;
let selectedCommunityPosts = [];
let selectedCommunityPostPageInfo = {};
let communityBoardQuery = "";
let communityThreadQuery = "";
let communityThreadPage = 0;
let communityThreadLimit = 10;
let communityThreadTotal = 0;
let communityAnnouncementEditorOpen = false;
let communityAnnouncementEditingId = null;
let communityBoardRequestOpen = false;
let communityCategoryManagerOpen = false;
let communityThreadCreatorOpen = false;
let communityToolsOpen = false;
let communityMode = "boards";
let canReviewCommunityThreads = false;
let communityBoardModerators = [];
let communityModeratorManagerOpen = false;
let communityModeratorCandidates = [];
let communityModeratorCandidatesLoadedAt = 0;
let communityAccountGeneration = 0;

function communityOperationContext() {
  return {
    generation: communityAccountGeneration,
    scope: typeof getCurrentAccountStorageScope === "function"
      ? getCurrentAccountStorageScope()
      : String(currentUserId || currentUser || "anonymous"),
  };
}

function communityOperationIsCurrent(operation) {
  if (!operation || Number(operation.generation) !== communityAccountGeneration) return false;
  const scope = typeof getCurrentAccountStorageScope === "function"
    ? getCurrentAccountStorageScope()
    : String(currentUserId || currentUser || "anonymous");
  return operation.scope === scope;
}

function communityAssertOperationCurrent(operation) {
  if (communityOperationIsCurrent(operation)) return;
  const err = new Error("Account context changed");
  err.name = "AbortError";
  throw err;
}

const COMMUNITY_MODERATOR_PERMISSIONS = [
  ["can_review_threads", "審核主題"],
  ["can_pin_threads", "置頂主題"],
  ["can_pin_posts", "置頂留言"],
  ["can_lock_threads", "鎖定主題"],
  ["can_edit_posts", "編輯留言"],
  ["can_delete_posts", "刪除主題或留言"],
  ["can_reward_authors", "獎勵作者"],
  ["can_penalize_posts", "懲處違規留言"],
];

const COMMUNITY_MODERATOR_PRESETS = {
  full: {
    can_review_threads: true,
    can_pin_threads: true,
    can_pin_posts: true,
    can_lock_threads: true,
    can_edit_posts: true,
    can_delete_posts: true,
    can_reward_authors: true,
    can_penalize_posts: true,
  },
  review: {
    can_review_threads: true,
    can_pin_threads: false,
    can_pin_posts: false,
    can_lock_threads: false,
    can_edit_posts: false,
    can_delete_posts: false,
    can_reward_authors: false,
    can_penalize_posts: false,
  },
  content: {
    can_review_threads: true,
    can_pin_threads: true,
    can_pin_posts: true,
    can_lock_threads: true,
    can_edit_posts: true,
    can_delete_posts: false,
    can_reward_authors: true,
    can_penalize_posts: false,
  },
  discipline: {
    can_review_threads: true,
    can_pin_threads: false,
    can_pin_posts: false,
    can_lock_threads: true,
    can_edit_posts: false,
    can_delete_posts: true,
    can_reward_authors: false,
    can_penalize_posts: true,
  },
};

const COMMUNITY_INLINE_MEDIA_TOKEN_RE = /\[\[community-media:(image|video|audio|file):([A-Za-z0-9_-]+)(?:\|([^\]\n]{0,160}))?\]\]/g;
const COMMUNITY_LEGACY_COMFYUI_IMAGE_TOKEN_RE = /\[\[comfyui-image:([A-Za-z0-9_-]+)\]\]/g;
const COMMUNITY_INLINE_MEDIA_ACCEPTED_EXTENSIONS = {
  image: new Set(["png", "jpg", "jpeg", "webp", "gif"]),
  video: new Set(["mp4", "webm", "ogg", "ogv", "m4v", "mov"]),
  audio: new Set(["mp3", "m4a", "aac", "ogg", "oga", "wav", "webm"]),
};
const COMMUNITY_ANY_INLINE_MEDIA_TOKEN_RE = /\[\[comfyui-image:([A-Za-z0-9_-]+)\]\]|\[\[community-media:(image|video|audio|file):([A-Za-z0-9_-]+)(?:\|([^\]\n]{0,160}))?\]\]|\[\[video-share:([A-Za-z0-9_-]+)(?:\|([^\]\n]{0,160}))?\]\]/g;

function canOpenCommunityReviewMode() {
  return currentRole === "manager" || currentRole === "super_admin" || canReviewCommunityThreads;
}

function resetCommunityReviewState() {
  communityBoardReviews = [];
  communityThreadReviews = [];
  canReviewCommunityThreads = false;
  const reviewBtn = $("community-review-tab-btn");
  const reviewArea = $("community-review-area");
  const boardPanel = $("community-board-review-panel");
  const threadPanel = $("community-thread-review-panel");
  if (reviewBtn) {
    reviewBtn.style.display = "none";
    reviewBtn.classList.remove("active");
    reviewBtn.textContent = "審核";
  }
  if (reviewArea) reviewArea.style.display = "none";
  if (boardPanel) boardPanel.style.display = "none";
  if (threadPanel) threadPanel.style.display = "none";
  if (typeof syncSidebarMenuVisibility === "function") syncSidebarMenuVisibility();
}

function switchCommunityMode(mode) {
  const nextMode = mode || "boards";
  communityMode = nextMode === "review" && !canOpenCommunityReviewMode() ? "boards" : nextMode;
  const reviewArea = $("community-review-area");
  const mainArea = $("community-main-area");
  const reviewBtn = $("community-review-tab-btn");
  if (reviewArea) reviewArea.style.display = communityMode === "review" ? "block" : "none";
  if (mainArea) mainArea.style.display = communityMode === "review" ? "none" : "block";
  if (reviewBtn) reviewBtn.classList.toggle("active", communityMode === "review");
  if (reviewBtn) reviewBtn.textContent = communityMode === "review" ? "返回討論區" : "審核";
  if (communityMode === "review") {
    renderCommunityBoardReviews();
    renderCommunityThreadReviews();
  } else {
    renderCommunityStage();
  }
}

function toggleCommunityTools(forceOpen = null) {
  communityToolsOpen = forceOpen === null ? !communityToolsOpen : !!forceOpen;
  const panel = $("community-tools-panel");
  const btn = $("community-tools-toggle-btn");
  if (panel) panel.style.display = communityToolsOpen ? "block" : "none";
  if (btn) btn.classList.toggle("active", communityToolsOpen);
}

function renderCommunityStage() {
  const boardStage = $("community-board-stage");
  const threadStage = $("community-thread-stage");
  const detailStage = $("community-detail-stage");
  const breadcrumb = $("community-breadcrumb");
  const stage = selectedCommunityThreadId ? "detail" : (selectedCommunityBoardId ? "threads" : "boards");
  if (boardStage) boardStage.style.display = stage === "boards" ? "block" : "none";
  if (threadStage) threadStage.style.display = stage === "threads" ? "block" : "none";
  if (detailStage) detailStage.style.display = stage === "detail" ? "block" : "none";
  if (breadcrumb) {
    const parts = ["討論區列表"];
    if (selectedCommunityBoard) parts.push(selectedCommunityBoard.title || "主題列表");
    if (selectedCommunityThread) parts.push(selectedCommunityThread.title || "主題內容");
    breadcrumb.textContent = parts.join(" / ");
  }
}

function showCommunityBoardStage() {
  selectedCommunityBoardId = null;
  selectedCommunityBoard = null;
  selectedCommunityThreadId = null;
  selectedCommunityThread = null;
  selectedCommunityPosts = [];
  selectedCommunityPostPageInfo = {};
  communityThreads = [];
  communityBoardModerators = [];
  communityThreadCreatorOpen = false;
  communityModeratorManagerOpen = false;
  renderCommunityBoards();
  renderCommunityThreads(null);
  renderCommunityModerators();
  renderCommunityThreadDetail(null, []);
  switchCommunityMode("boards");
}

function showCommunityThreadStage() {
  selectedCommunityThreadId = null;
  selectedCommunityThread = null;
  selectedCommunityPosts = [];
  selectedCommunityPostPageInfo = {};
  communityModeratorManagerOpen = false;
  renderCommunityThreads(selectedCommunityBoard);
  renderCommunityThreadDetail(null, []);
  switchCommunityMode("boards");
}

function communityStatusLabel(status) {
  if (status === "approved") return "已開放";
  if (status === "pending") return "待審核";
  if (status === "rejected") return "已駁回";
  return status || "未知";
}

function communityVisibilityLabel(visibility) {
  if (visibility === "public") return "公開";
  if (visibility === "unlisted") return "不公開列表";
  if (visibility === "private") return "私人";
  return visibility || "公開";
}

function canManageCommunity() {
  return currentRole === "manager" || currentRole === "super_admin";
}

function canDeleteCommunityItem(authorUserId, ownerUserId) {
  if (canManageCommunity()) return true;
  return Number(currentUserId) === Number(authorUserId) || Number(currentUserId) === Number(ownerUserId);
}

function moderatorPermissionInputId(key) {
  return "community-mod-" + key.replaceAll("_", "-");
}

function moderatorPermissionPayload() {
  const payload = {};
  COMMUNITY_MODERATOR_PERMISSIONS.forEach(([key]) => {
    payload[key] = !!$(moderatorPermissionInputId(key))?.checked;
  });
  return payload;
}

function communityModeratorCandidateLabel(user) {
  const id = user?.id || "";
  const username = user?.username || "unknown";
  const role = username === "root" ? "root" : (user?.role || "user");
  const status = user?.status || "-";
  const level = user?.effective_level || user?.member_level || "";
  return `${username} (#${id}) · ${role} · ${status}${level ? " · " + level : ""}`;
}

function renderCommunityModeratorUserOptions(selectedValue = "") {
  const select = $("community-moderator-user-id");
  if (!select) return;
  const previous = String(selectedValue || select.value || "");
  const rows = communityModeratorCandidates.length
    ? communityModeratorCandidates
    : (Array.isArray(users) ? users : []);
  if (!rows.length) {
    select.innerHTML = `<option value="">沒有可選擇的會員</option>`;
    return;
  }
  select.innerHTML = `<option value="">請選擇版主帳號</option>` + rows.map((user) => `
    <option value="${sanitize(String(user.id || ""))}">${sanitize(communityModeratorCandidateLabel(user))}</option>
  `).join("");
  if (previous && rows.some((user) => String(user.id || "") === previous)) {
    select.value = previous;
  }
}

async function loadCommunityModeratorCandidates({ force = false } = {}) {
  if (!currentUser || !canManageCommunity()) return [];
  const fresh = communityModeratorCandidatesLoadedAt && Date.now() - communityModeratorCandidatesLoadedAt < 30000;
  if (!force && fresh) {
    renderCommunityModeratorUserOptions();
    return communityModeratorCandidates;
  }
  if (Array.isArray(users) && users.length && !force) {
    communityModeratorCandidates = users;
    communityModeratorCandidatesLoadedAt = Date.now();
    renderCommunityModeratorUserOptions();
    return communityModeratorCandidates;
  }
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/admin/users", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) {
    const select = $("community-moderator-user-id");
    if (select) select.innerHTML = `<option value="">會員清單讀取失敗</option>`;
    return [];
  }
  communityModeratorCandidates = Array.isArray(json.users) ? json.users : [];
  communityModeratorCandidatesLoadedAt = Date.now();
  if (Array.isArray(json.users)) users = json.users;
  renderCommunityModeratorUserOptions();
  return communityModeratorCandidates;
}

function applyModeratorPreset(presetName) {
  const preset = COMMUNITY_MODERATOR_PRESETS[presetName] || COMMUNITY_MODERATOR_PRESETS.full;
  COMMUNITY_MODERATOR_PERMISSIONS.forEach(([key]) => {
    const input = $(moderatorPermissionInputId(key));
    if (input) input.checked = !!preset[key];
  });
}

function fillModeratorForm(moderator) {
  if (!moderator) return;
  communityModeratorManagerOpen = true;
  renderCommunityModerators();
  const userId = $("community-moderator-user-id");
  renderCommunityModeratorUserOptions(moderator.user_id || "");
  if (userId) userId.value = moderator.user_id || "";
  COMMUNITY_MODERATOR_PERMISSIONS.forEach(([key]) => {
    const input = $(moderatorPermissionInputId(key));
    if (input) input.checked = !!moderator[key];
  });
  const preset = $("community-moderator-preset");
  if (preset) preset.value = "full";
}

function toggleCommunityModeratorManager(forceOpen = null) {
  communityModeratorManagerOpen = forceOpen === null ? !communityModeratorManagerOpen : !!forceOpen;
  renderCommunityModerators();
}

function renderCommunityModerators() {
  const panel = $("community-moderator-manager");
  const list = $("community-moderator-list");
  const openBtn = $("community-moderator-open-btn");
  const canShow = selectedCommunityBoardId && canManageCommunity();
  if (openBtn) {
    openBtn.style.display = canShow ? "" : "none";
    openBtn.classList.toggle("active", canShow && communityModeratorManagerOpen);
    openBtn.textContent = communityModeratorManagerOpen ? "隱藏版主設定" : "版主設定";
  }
  if (panel) panel.style.display = canShow && communityModeratorManagerOpen ? "block" : "none";
  if (canShow && communityModeratorManagerOpen) {
    loadCommunityModeratorCandidates().catch((err) => {
      reportFrontendFailure("community-moderator-candidates", err);
      const select = $("community-moderator-user-id");
      if (select) select.innerHTML = `<option value="">會員清單讀取失敗</option>`;
    });
  }
  if (!list || !canManageCommunity()) return;
  if (!selectedCommunityBoardId) {
    list.innerHTML = "<p style='color:var(--muted);'>請先選擇討論區</p>";
    return;
  }
  if (!communityBoardModerators.length) {
    list.innerHTML = "<p style='color:var(--muted);'>尚未設定版主。每個討論區至少需要一位版主。</p>";
    return;
  }
  list.innerHTML = communityBoardModerators.map((moderator) => {
    const allowed = COMMUNITY_MODERATOR_PERMISSIONS
      .filter(([key]) => moderator[key])
      .map(([, label]) => label)
      .join("、") || "無權限";
    return `
      <div class="community-card">
        <div class="community-card-head">
          <strong>${sanitize(moderator.username || "")}</strong>
          <span class="community-badge approved">會員 #${sanitize(String(moderator.user_id || ""))}</span>
        </div>
        <div class="community-meta">權限：${sanitize(allowed)}</div>
        <div class="community-actions">
          <button class="btn community-mini-btn" type="button" data-edit-board-moderator="${moderator.user_id}">修改權限</button>
          <button class="btn community-mini-btn" type="button" data-delete-board-moderator="${moderator.user_id}">移除版主</button>
        </div>
      </div>
    `;
  }).join("");
  list.querySelectorAll("button[data-edit-board-moderator]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const userId = parseInt(btn.getAttribute("data-edit-board-moderator"), 10);
      fillModeratorForm(communityBoardModerators.find((item) => Number(item.user_id) === Number(userId)));
    });
  });
  list.querySelectorAll("button[data-delete-board-moderator]").forEach((btn) => {
    btn.addEventListener("click", () => deleteCommunityModerator(parseInt(btn.getAttribute("data-delete-board-moderator"), 10)));
  });
}

function renderCommunityCategories() {
  const panel = $("community-category-manager-panel");
  const list = $("community-category-list");
  const select = $("community-board-category");
  const openBtn = $("community-category-manager-open-btn");
  if (openBtn) openBtn.style.display = canManageCommunity() ? "" : "none";
  if (panel) panel.style.display = canManageCommunity() && communityCategoryManagerOpen ? "block" : "none";
  if (select) {
    const activeCategories = communityCategories.filter((item) => item.is_active !== false);
    const options = activeCategories.map((item) => (
      `<option value="${item.id}">${sanitize(item.name || "")}</option>`
    ));
    if (!options.length) options.push('<option value="">一般討論（預設）</option>');
    select.innerHTML = options.join("");
  }
  if (!list || !canManageCommunity()) return;
  if (!communityCategories.length) {
    list.innerHTML = "<p style='color:var(--muted);'>目前沒有討論分類</p>";
    return;
  }
  list.innerHTML = communityCategories.map((item) => `
    <div class="community-card">
      <div class="community-card-head">
        <strong>${sanitize(item.name || "")}</strong>
        <span class="community-badge ${item.is_active ? "approved" : "rejected"}">${item.is_active ? "啟用" : "停用"}</span>
      </div>
      <div class="community-meta">排序 ${item.sort_order ?? 100} · 版面 ${item.board_count || 0}</div>
      <div class="community-body">${sanitize(item.description || "")}</div>
    </div>
  `).join("");
}

function communityReactionButton(post, value, label) {
  const active = Number(post.user_reaction || 0) === Number(value);
  return `<button class="btn community-mini-btn${active ? " active" : ""}" type="button" data-community-reaction="${post.id}" data-reaction-value="${value}">${label} ${value === 1 ? (post.like_count || 0) : (post.dislike_count || 0)}</button>`;
}

function communityThreadReactionButton(thread, value, label) {
  const active = Number(thread?.user_reaction || 0) === Number(value);
  return `<button class="btn community-mini-btn${active ? " active" : ""}" type="button" data-community-thread-reaction="${thread.id}" data-reaction-value="${value}">${label} ${value === 1 ? (thread.like_count || 0) : (thread.dislike_count || 0)}</button>`;
}

function communityPlainContent(content) {
  return String(content || "")
    .replace(/\n?\[\[comfyui-image:[A-Za-z0-9_-]+\]\]\n?/g, "\n")
    .replace(/\n?\[\[community-media:(?:image|video|audio|file):[A-Za-z0-9_-]+(?:\|[^\]\n]{0,160})?\]\]\n?/g, "\n")
    .replace(/\n?\[\[video-share:[A-Za-z0-9_-]+(?:\|[^\]\n]{0,160})?\]\]\n?/g, "\n")
    .trim();
}

function communityPreviewContentUrl(fileId) {
  return `${API}/cloud-drive/files/${encodeURIComponent(fileId)}/preview/content`;
}

function renderCommunityBody(content) {
  const raw = String(content || "");
  COMMUNITY_ANY_INLINE_MEDIA_TOKEN_RE.lastIndex = 0;
  const hasInlineTokens = COMMUNITY_ANY_INLINE_MEDIA_TOKEN_RE.test(raw);
  COMMUNITY_ANY_INLINE_MEDIA_TOKEN_RE.lastIndex = 0;
  if (typeof markdownToSafeHtml === "function" && !hasInlineTokens) {
    return `<div class="community-body markdown-rendered">${markdownToSafeHtml(raw)}</div>`;
  }
  let lastIndex = 0;
  const parts = [];
  raw.replace(COMMUNITY_ANY_INLINE_MEDIA_TOKEN_RE, (match, comfyuiFileId, kind, token, name, videoToken, videoName, offset) => {
    parts.push(sanitize(raw.slice(lastIndex, offset)));
    if (comfyuiFileId) {
      parts.push(`<div class="community-share-image"><img src="${sanitize(communityPreviewContentUrl(comfyuiFileId))}" alt="ComfyUI shared image" loading="lazy" /></div>`);
    } else if (videoToken) {
      parts.push(communityVideoShareHtml(videoToken, videoName));
    } else {
      parts.push(communityInlineMediaHtml(kind, token, name));
    }
    lastIndex = offset + match.length;
    return match;
  });
  parts.push(sanitize(raw.slice(lastIndex)));
  return `<div class="community-body">${parts.join("")}</div>`;
}

function communityVideoShareHtml(token, name = "") {
  const safeToken = String(token || "").match(/^[A-Za-z0-9_-]+$/) ? String(token) : "";
  if (!safeToken) return "";
  const safeName = sanitize(name || "影音分享");
  const pageUrl = `/shared/videos/${encodeURIComponent(safeToken)}`;
  return `
    <figure class="community-inline-media community-inline-video-share">
      <a href="${sanitize(pageUrl)}" target="_blank" rel="noopener noreferrer">
        <strong>${safeName}</strong>
        <span>站內影音分享</span>
      </a>
    </figure>
  `;
}

function communityInlineMediaHtml(kind, token, name = "") {
  const mediaKind = ["image", "video", "audio", "file"].includes(kind) ? kind : "file";
  const safeToken = String(token || "").match(/^[A-Za-z0-9_-]+$/) ? String(token) : "";
  if (!safeToken) return "";
  const safeName = sanitize(name || "討論區媒體");
  const contentUrl = `${API}/storage/shared/${encodeURIComponent(safeToken)}/preview/content`;
  const pageUrl = `/shared/files/${encodeURIComponent(safeToken)}`;
  if (mediaKind === "image") {
    return `<figure class="community-inline-media community-inline-media-image"><img src="${sanitize(contentUrl)}" alt="${safeName}" loading="lazy" /><figcaption>${safeName}</figcaption></figure>`;
  }
  if (mediaKind === "video") {
    return `<figure class="community-inline-media community-inline-media-video"><video src="${sanitize(contentUrl)}" controls preload="metadata"></video><figcaption>${safeName}</figcaption></figure>`;
  }
  if (mediaKind === "audio") {
    return `<figure class="community-inline-media community-inline-media-audio"><audio src="${sanitize(contentUrl)}" controls preload="metadata"></audio><figcaption>${safeName}</figcaption></figure>`;
  }
  return `<figure class="community-inline-media community-inline-media-file"><a href="${sanitize(pageUrl)}" target="_blank" rel="noopener noreferrer">${safeName}</a></figure>`;
}

function communityInlineMediaKind(file) {
  const type = String(file?.type || "").toLowerCase();
  const ext = String(file?.name || "").split(".").pop().toLowerCase();
  if (type.startsWith("image/") && type !== "image/svg+xml") return "image";
  if (type.startsWith("video/")) return "video";
  if (type.startsWith("audio/")) return "audio";
  if (COMMUNITY_INLINE_MEDIA_ACCEPTED_EXTENSIONS.image.has(ext)) return "image";
  if (COMMUNITY_INLINE_MEDIA_ACCEPTED_EXTENSIONS.video.has(ext)) return "video";
  if (COMMUNITY_INLINE_MEDIA_ACCEPTED_EXTENSIONS.audio.has(ext)) return "audio";
  return "";
}

function communityInlineMediaSafeName(file) {
  return String(file?.name || "media")
    .replace(/[\[\]\|\r\n]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 120) || "media";
}

function communityInlineMediaVirtualPath(file) {
  const safeName = communityInlineMediaSafeName(file);
  return `/Community/Inline/${Date.now()}-${safeName}`;
}

function insertCommunityInlineMediaToken(textarea, tokenText) {
  if (!textarea || !tokenText) return;
  const start = Number.isFinite(textarea.selectionStart) ? textarea.selectionStart : textarea.value.length;
  const end = Number.isFinite(textarea.selectionEnd) ? textarea.selectionEnd : start;
  const before = textarea.value.slice(0, start);
  const after = textarea.value.slice(end);
  const prefix = before && !before.endsWith("\n") ? "\n" : "";
  const suffix = after && !after.startsWith("\n") ? "\n" : "";
  const insert = `${prefix}${tokenText}\n${suffix}`;
  textarea.value = before + insert + after;
  const cursor = before.length + insert.length;
  textarea.focus();
  textarea.setSelectionRange(cursor, cursor);
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

function setCommunityMediaButtonsBusy(targetTextareaId, busy) {
  document.querySelectorAll("[data-community-media-picker]").forEach((btn) => {
    if (String(btn.dataset.communityMediaPicker || "") !== String(targetTextareaId || "")) return;
    if (!btn.dataset.originalLabel) btn.dataset.originalLabel = btn.textContent || "插入圖片/影音";
    btn.disabled = !!busy;
    btn.textContent = busy ? "上傳中..." : (btn.dataset.originalLabel || "插入圖片/影音");
  });
}

function openCommunityInlineMediaPicker(button) {
  const textareaId = button?.dataset?.communityMediaPicker || "";
  const inputId = button?.dataset?.communityMediaInput || "";
  const input = $(inputId);
  const textarea = $(textareaId);
  if (!input || !textarea) {
    flash($("community-msg"), "找不到討論區編輯器", false);
    return;
  }
  input.dataset.communityMediaTarget = textareaId;
  input.value = "";
  input.click();
}

async function uploadCommunityInlineMediaFile(file, textarea, csrf, operation = communityOperationContext()) {
  communityAssertOperationCurrent(operation);
  const kind = communityInlineMediaKind(file);
  if (!kind) throw new Error(`「${file?.name || "檔案"}」不是可內嵌的圖片、影片或音訊格式`);
  if (typeof preflightDriveUploadSize === "function") {
    const ok = await preflightDriveUploadSize(file.size, `討論區媒體「${file.name || "media"}」`);
    communityAssertOperationCurrent(operation);
    if (!ok) throw new Error("檔案大小超出目前可上傳限制");
  }
  const filename = communityInlineMediaSafeName(file);
  const virtualPath = communityInlineMediaVirtualPath(file);
  let uploadJson = null;
  if (
    typeof shouldUseDriveResumableUpload === "function"
    && typeof uploadDriveBlobResumable === "function"
    && shouldUseDriveResumableUpload(file)
  ) {
    const transferId = typeof addDriveTransferRow === "function"
      ? addDriveTransferRow({
        kind: "resumable_upload",
        name: filename,
        loaded_bytes: 0,
        total_bytes: file.size,
        progress_percent: 0,
        msg: "討論區媒體等待分段上傳",
      })
      : "";
    uploadJson = await uploadDriveBlobResumable({
      blob: file,
      sourceFile: file,
      filename,
      mimeType: file.type || "application/octet-stream",
      privacyMode: "standard_plain",
      target: "cloud_drive",
      virtualPath,
      transferId,
      csrf,
      label: filename,
    });
    communityAssertOperationCurrent(operation);
  } else if (typeof xhrUploadWithProgress === "function") {
    const form = new FormData();
    form.append("file", file, filename);
    form.append("privacy_mode", "standard_plain");
    form.append("virtual_path", virtualPath);
    const upload = await xhrUploadWithProgress(`${API}/cloud-drive/upload`, form, csrf, null);
    communityAssertOperationCurrent(operation);
    uploadJson = upload.json || {};
    if (upload.status < 200 || upload.status >= 300 || !uploadJson.ok) {
      throw new Error(uploadJson.msg || `討論區媒體上傳失敗（HTTP ${upload.status}）`);
    }
  } else {
    const form = new FormData();
    form.append("file", file, filename);
    form.append("privacy_mode", "standard_plain");
    form.append("virtual_path", virtualPath);
    const upload = await apiFetch(`${API}/cloud-drive/upload`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" },
      body: form,
    });
    uploadJson = await upload.json().catch(() => ({}));
    communityAssertOperationCurrent(operation);
    if (!upload.ok || !uploadJson.ok) throw new Error(uploadJson.msg || "討論區媒體上傳失敗");
  }
  const fileId = uploadJson?.file?.file_id || uploadJson?.file?.id || "";
  if (!fileId) throw new Error("討論區媒體上傳完成，但伺服器未回傳 file_id");
  const shareRes = await apiFetch(`${API}/storage/share-links`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrf || "",
    },
    body: JSON.stringify({
      file_id: fileId,
      can_preview: true,
      access_scope: "link",
      max_views: 0,
    }),
  });
  const shareJson = await shareRes.json().catch(() => ({}));
  communityAssertOperationCurrent(operation);
  if (!shareRes.ok || !shareJson.ok) throw new Error(shareJson.msg || "討論區媒體分享連結建立失敗");
  const token = shareJson?.share_link?.token || "";
  if (!token) throw new Error("討論區媒體分享連結缺少 token");
  insertCommunityInlineMediaToken(textarea, `[[community-media:${kind}:${token}|${filename}]]`);
  return { fileId, token, kind, filename };
}

async function uploadCommunityInlineMedia(input) {
  const operation = communityOperationContext();
  const textarea = $(input?.dataset?.communityMediaTarget || "");
  const files = Array.from(input?.files || []);
  if (!textarea || !files.length) return;
  setCommunityMediaButtonsBusy(textarea.id, true);
  let inserted = 0;
  try {
    await fetchCsrfToken({ force: true });
    communityAssertOperationCurrent(operation);
    const csrf = getCsrfToken() || "";
    for (const file of files) {
      communityAssertOperationCurrent(operation);
      try {
        await uploadCommunityInlineMediaFile(file, textarea, csrf, operation);
        communityAssertOperationCurrent(operation);
        inserted += 1;
      } catch (err) {
        if (!communityOperationIsCurrent(operation) || err?.name === "AbortError") throw err;
        flash($("community-msg"), err.message || "討論區媒體插入失敗", false);
      }
    }
    if (inserted > 0) {
      flash($("community-msg"), `已插入 ${inserted} 個討論區媒體`, true);
      if (typeof loadDriveDashboard === "function") {
        loadDriveDashboard().catch((err) => reportFrontendFailure("community-inline-media-drive-refresh", err));
      }
    }
  } catch (err) {
    if (communityOperationIsCurrent(operation) && err?.name !== "AbortError") {
      flash($("community-msg"), err?.message || "討論區媒體插入失敗", false);
    }
  } finally {
    input.value = "";
    if (communityOperationIsCurrent(operation)) setCommunityMediaButtonsBusy(textarea.id, false);
  }
}

function renderCommunityAnnouncements() {
  const list = $("community-announcement-list");
  const editor = $("community-announcement-editor");
  const openBtn = $("community-announcement-open-btn");
  const submitBtn = $("community-announcement-submit");
  const canPublish = currentRole === "manager" || currentRole === "super_admin";
  if (openBtn) openBtn.style.display = canPublish && !communityAnnouncementEditorOpen ? "" : "none";
  if (editor) editor.style.display = canPublish && communityAnnouncementEditorOpen ? "block" : "none";
  if (submitBtn) submitBtn.textContent = communityAnnouncementEditingId ? "更新公告" : "發布公告";
  if (!list) return;
  if (!communityAnnouncements.length) {
    list.innerHTML = "<p style='color:var(--muted);'>目前沒有公告</p>";
    return;
  }
  list.innerHTML = communityAnnouncements.map((item) => `
    <div class="community-card">
      <div class="community-card-head">
        <strong>${item.is_pinned ? "📌 " : ""}${sanitize(item.title || "")}</strong>
        ${(currentRole === "manager" || currentRole === "super_admin") ? `
          <div style="display:flex;gap:.35rem;flex-wrap:wrap;justify-content:flex-end;">
            <button class="btn community-mini-btn" type="button" data-edit-announcement="${item.id}">編輯</button>
            <button class="btn community-mini-btn" type="button" data-del-announcement="${item.id}">刪除</button>
          </div>
        ` : ""}
      </div>
      <div class="community-meta">${sanitize(item.author_username || "")} · ${sanitize(formatChatTime(item.created_at || ""))}${item.updated_at && item.updated_at !== item.created_at ? ` · 更新於 ${sanitize(formatChatTime(item.updated_at || ""))}` : ""}</div>
      <div class="community-body markdown-rendered">${markdownToSafeHtml(item.content || "")}</div>
    </div>
  `).join("");
  list.querySelectorAll("button[data-edit-announcement]").forEach((btn) => {
    btn.addEventListener("click", () => editAnnouncement(parseInt(btn.getAttribute("data-edit-announcement"), 10)));
  });
  list.querySelectorAll("button[data-del-announcement]").forEach((btn) => {
    btn.addEventListener("click", () => deleteAnnouncement(parseInt(btn.getAttribute("data-del-announcement"), 10)));
  });
}

function resetCommunityAnnouncementEditor() {
  communityAnnouncementEditingId = null;
  if ($("community-announcement-title")) $("community-announcement-title").value = "";
  if ($("community-announcement-content")) $("community-announcement-content").value = "";
  if ($("community-announcement-pinned")) $("community-announcement-pinned").checked = false;
}

function editAnnouncement(id) {
  const item = communityAnnouncements.find((row) => Number(row?.id) === Number(id));
  if (!item) {
    flash($("community-msg"), "找不到要編輯的公告", false);
    return;
  }
  communityAnnouncementEditingId = item.id;
  if ($("community-announcement-title")) $("community-announcement-title").value = item.title || "";
  if ($("community-announcement-content")) $("community-announcement-content").value = item.content || "";
  if ($("community-announcement-pinned")) $("community-announcement-pinned").checked = !!item.is_pinned;
  toggleCommunityAnnouncementEditor(true);
}

function renderCommunityBoardReviews() {
  const panel = $("community-board-review-panel");
  const list = $("community-board-review-list");
  const reviewBtn = $("community-review-tab-btn");
  const canReview = currentRole === "manager" || currentRole === "super_admin";
  if (reviewBtn) reviewBtn.style.display = (canReview || canReviewCommunityThreads) ? "" : "none";
  if (typeof syncSidebarMenuVisibility === "function") syncSidebarMenuVisibility();
  if (panel) panel.style.display = canReview && communityMode === "review" ? "block" : "none";
  if (!list || !canReview) return;
  if (!communityBoardReviews.length) {
    list.innerHTML = "<p style='color:var(--muted);'>目前沒有待審核討論區</p>";
    return;
  }
  list.innerHTML = communityBoardReviews.map((item) => `
    <div class="community-card">
      <div class="community-card-head">
        <strong>${sanitize(item.title || "")}</strong>
        <span class="community-badge pending">${communityStatusLabel(item.status)}</span>
      </div>
      <div class="community-meta">分類：${sanitize(item.category?.name || "未分類")} · 申請者：${sanitize(item.owner_username || "")} · ${sanitize(formatChatTime(item.created_at || ""))}</div>
      <div class="community-body">${sanitize(item.description || "")}</div>
      <div class="community-actions">
        <button class="btn btn-primary" type="button" data-board-review="${item.id}" data-board-action="approve">核准</button>
        <button class="btn" type="button" data-board-review="${item.id}" data-board-action="reject">駁回</button>
      </div>
    </div>
  `).join("");
  list.querySelectorAll("button[data-board-review]").forEach((btn) => {
    btn.addEventListener("click", () => reviewCommunityBoard(
      parseInt(btn.getAttribute("data-board-review"), 10),
      btn.getAttribute("data-board-action")
    ));
  });
}

function renderCommunityThreadReviews() {
  const panel = $("community-thread-review-panel");
  const list = $("community-thread-review-list");
  const reviewBtn = $("community-review-tab-btn");
  const canReview = currentRole === "manager" || currentRole === "super_admin" || canReviewCommunityThreads;
  if (reviewBtn) reviewBtn.style.display = canReview ? "" : "none";
  if (typeof syncSidebarMenuVisibility === "function") syncSidebarMenuVisibility();
  if (panel) panel.style.display = canReview && communityMode === "review" ? "block" : "none";
  if (!list || !canReview) return;
  if (!communityThreadReviews.length) {
    list.innerHTML = "<p style='color:var(--muted);'>目前沒有待審核主題</p>";
    return;
  }
  list.innerHTML = communityThreadReviews.map((item) => `
    <div class="community-card">
      <div class="community-card-head">
        <strong>${sanitize(item.title || "")}</strong>
        <span class="community-badge pending">${communityStatusLabel(item.status)}</span>
      </div>
      <div class="community-meta">作者：${sanitize(item.author_username || "")} · ${sanitize(formatChatTime(item.created_at || ""))}</div>
      <div class="community-body">${sanitize(item.content || "")}</div>
      <div class="community-actions">
        <button class="btn btn-primary" type="button" data-thread-review="${item.id}" data-thread-action="approve">核准公開</button>
        <button class="btn" type="button" data-thread-review="${item.id}" data-thread-action="reject">駁回</button>
      </div>
    </div>
  `).join("");
  list.querySelectorAll("button[data-thread-review]").forEach((btn) => {
    btn.addEventListener("click", () => reviewCommunityThread(
      parseInt(btn.getAttribute("data-thread-review"), 10),
      btn.getAttribute("data-thread-action")
    ));
  });
}

function renderCommunityBoards() {
  const list = $("community-board-list");
  if (!list) return;
  const query = communityBoardQuery.trim().toLowerCase();
  const visibleBoards = communityBoards.filter((board) => {
    if (!query) return true;
    return [board.title, board.description, board.rules, board.owner_username, board.category?.name].some((v) => String(v || "").toLowerCase().includes(query));
  });
  if (!visibleBoards.length) {
    list.innerHTML = "<p style='color:var(--muted);'>目前沒有討論區</p>";
    return;
  }
  list.innerHTML = visibleBoards.map((board) => `
    <button class="community-board-item${Number(selectedCommunityBoardId) === Number(board.id) ? " active" : ""}" type="button" data-open-board="${board.id}">
      <div class="community-card-head">
        <strong>${sanitize(board.title || "")}</strong>
        <span class="community-badge ${sanitize(board.status || "")}">${communityStatusLabel(board.status)}</span>
      </div>
      <div class="community-meta">分類：${sanitize(board.category?.name || "未分類")} · 維護：${sanitize((board.moderators || []).join("、") || board.owner_username || "未設定")} · ${communityVisibilityLabel(board.visibility)}${board.is_active === false ? " · 停用" : ""}</div>
      <div class="community-body">${sanitize(board.description || "")}</div>
      <div class="community-meta">主題 ${board.thread_count || 0} · 留言 ${board.post_count || 0}</div>
    </button>
  `).join("");
  list.querySelectorAll("button[data-open-board]").forEach((btn) => {
    btn.addEventListener("click", () => {
      communityThreadPage = 0;
      openCommunityBoard(parseInt(btn.getAttribute("data-open-board"), 10));
    });
  });
  renderCommunityStage();
}

function renderCommunityThreads(board) {
  const heading = $("community-board-heading");
  const meta = $("community-board-meta");
  const list = $("community-thread-list");
  const creator = $("community-thread-creator");
  const createOpenBtn = $("community-thread-create-open-btn");
  const rulesView = $("community-board-rules-view");
  const pageInfo = $("community-thread-page-info");
  const prevBtn = $("community-thread-prev");
  const nextBtn = $("community-thread-next");
  const submitBtn = $("community-thread-submit");
  if (heading) heading.textContent = board ? board.title : "請先選擇討論區";
  if (meta) {
    meta.textContent = board
      ? `${board.category?.name || "未分類"} · 維護：${(board.moderators || []).join("、") || board.owner_username || "-"} · ${communityStatusLabel(board.status)} · ${communityVisibilityLabel(board.visibility)}${board.is_active === false ? " · 停用" : ""}${board.review_note ? ` · ${board.review_note}` : ""}`
      : "";
  }
  if (rulesView) {
    if (board && board.rules) {
      rulesView.style.display = "block";
      rulesView.textContent = `版規：${board.rules}`;
    } else {
      rulesView.style.display = "none";
      rulesView.textContent = "";
    }
  }
  if (pageInfo) pageInfo.textContent = board ? `第 ${communityThreadPage + 1} 頁` : "第 1 頁";
  if (prevBtn) prevBtn.disabled = communityThreadPage <= 0;
  if (nextBtn) nextBtn.disabled = ((communityThreadPage + 1) * communityThreadLimit) >= communityThreadTotal;
  const canCreateThread = board && board.status === "approved";
  if (createOpenBtn) createOpenBtn.style.display = canCreateThread && !communityThreadCreatorOpen ? "" : "none";
  if (creator) creator.style.display = canCreateThread && communityThreadCreatorOpen ? "block" : "none";
  if (submitBtn) submitBtn.textContent = (currentRole === "manager" || currentRole === "super_admin") ? "發布主題" : "送審主題";
  renderCommunityModerators();
  if (!list) return;
  if (!board) {
    list.innerHTML = "<p style='color:var(--muted);'>請先選擇左側討論區</p>";
    return;
  }
  if (!communityThreads.length) {
    list.innerHTML = "<p style='color:var(--muted);'>這個討論區尚無主題</p>";
    return;
  }
  list.innerHTML = communityThreads.map((thread) => `
    <button class="community-thread-item${Number(selectedCommunityThreadId) === Number(thread.id) ? " active" : ""}" type="button" data-open-thread="${thread.id}">
      <strong>${thread.is_sticky ? "置頂 · " : ""}${sanitize(thread.title || "")}</strong>
      <div class="community-meta">${userIdentityMarkup(thread.author_user_id, thread.author_username || "", formatChatTime(thread.created_at || ""), "community-author-line", thread.author_avatar_file_id || "")}</div>
      <div class="community-meta">${communityStatusLabel(thread.status)}${thread.review_note ? ` · ${sanitize(thread.review_note)}` : ""}</div>
      <div class="community-body">${sanitize(communityPlainContent(thread.content || "").slice(0, 140))}${communityPlainContent(thread.content || "").length > 140 ? "..." : ""}</div>
      <div class="community-meta">回覆 ${thread.reply_count || 0}</div>
    </button>
  `).join("");
  list.querySelectorAll("button[data-open-thread]").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      if (event.target.closest("[data-open-user-profile]")) return;
      openCommunityThread(parseInt(btn.getAttribute("data-open-thread"), 10));
    });
  });
  bindAvatarFallbacks(list);
  renderCommunityStage();
}

function renderCommunityThreadDetail(thread, posts, postPageInfo = {}) {
  const heading = $("community-thread-heading");
  const detail = $("community-thread-detail");
  const replyBox = $("community-reply-box");
  const lockTools = $("community-thread-lock-tools");
  const lockToggle = $("community-thread-lock-toggle");
  const stickyToggle = $("community-thread-sticky-toggle");
  const deleteThreadBtn = $("community-thread-delete-btn");
  const canModerateThread = !!thread?.can_moderate || canManageCommunity();
  const modPerms = thread?.moderator_permissions || {};
  const canPinThread = canManageCommunity() || !!modPerms.can_pin_threads;
  const canLockThread = canManageCommunity() || !!modPerms.can_lock_threads;
  const canPinPost = canManageCommunity() || !!modPerms.can_pin_posts;
  const canRewardThread = canManageCommunity() || !!modPerms.can_reward_authors;
  const canPenalizePost = canManageCommunity() || !!modPerms.can_penalize_posts;
  if (heading) heading.textContent = thread ? thread.title : "主題內容";
  if (replyBox) replyBox.style.display = thread && thread.board_status === "approved" && thread.status === "approved" && !thread.is_locked ? "block" : "none";
  if (lockTools) lockTools.style.display = thread && (canModerateThread || canDeleteCommunityItem(thread.author_user_id)) ? "flex" : "none";
  if (lockToggle && thread) lockToggle.textContent = thread.is_locked ? "解除鎖定" : "鎖定主題";
  if (stickyToggle && thread) stickyToggle.textContent = thread.is_sticky ? "取消置頂" : "置頂主題";
  if (lockToggle) lockToggle.style.display = thread && canModerateThread && canLockThread ? "" : "none";
  if (stickyToggle) stickyToggle.style.display = thread && canModerateThread && canPinThread ? "" : "none";
  if (deleteThreadBtn) deleteThreadBtn.style.display = thread && (canModerateThread || canDeleteCommunityItem(thread.author_user_id)) ? "" : "none";
  if (!detail) return;
  if (!thread) {
    detail.innerHTML = "<p style='color:var(--muted);'>請選擇主題以查看內容與留言</p>";
    return;
  }
  const replies = Array.isArray(posts) && posts.length
    ? posts.map((post) => `
        <div class="community-card${post.is_hidden ? " community-hidden-post" : ""}">
          <div class="community-card-head">
            <div class="community-meta">${post.is_pinned ? "置頂留言 · " : ""}${userIdentityMarkup(post.author_user_id, post.author_username || "", `${formatChatTime(post.created_at || "")}${post.is_hidden ? ` · 已隱藏：${post.hidden_reason || ""}` : ""}`, "community-author-line", post.author_avatar_file_id || "")}</div>
            <div style="display:flex;gap:.35rem;flex-wrap:wrap;justify-content:flex-end;">
              ${canModerateThread && canPinPost ? `<button class="btn community-mini-btn" type="button" data-pin-community-post="${post.id}" data-pinned="${post.is_pinned ? "1" : "0"}">${post.is_pinned ? "取消置頂" : "置頂留言"}</button>` : ""}
              ${(canModerateThread || canDeleteCommunityItem(post.author_user_id, thread.author_user_id)) ? `<button class="btn community-mini-btn" type="button" data-delete-community-post="${post.id}">刪除</button>` : ""}
              ${canModerateThread && canPenalizePost ? `<button class="btn community-mini-btn" type="button" data-penalty-community-post="${post.id}">懲處</button>` : ""}
            </div>
          </div>
          ${renderCommunityBody(post.content || "")}
          <div class="community-actions" style="margin-top:.45rem;">
            ${communityReactionButton(post, 1, "讚")}
            ${communityReactionButton(post, -1, "倒讚")}
          </div>
        </div>
      `).join("")
    : "<p style='color:var(--muted);'>尚無留言</p>";
  const totalPosts = Number(postPageInfo.posts_total ?? posts.length ?? 0);
  const shownPosts = Array.isArray(posts) ? posts.length : 0;
  const nextPostPage = Number(postPageInfo.posts_page || 0) + 1;
  const postBoundedNote = postPageInfo.posts_has_more
    ? `<div class="community-meta" style="display:flex;align-items:center;gap:.45rem;flex-wrap:wrap;">
        <span>目前顯示 ${shownPosts} / ${totalPosts} 則留言</span>
        <button class="btn community-mini-btn" type="button" data-load-more-community-posts="${nextPostPage}">載入更多留言</button>
      </div>`
    : "";
  detail.innerHTML = `
    <div class="community-card">
      <div class="community-meta">${thread.is_sticky ? "置頂主題 · " : ""}${userIdentityMarkup(thread.author_user_id, thread.author_username || "", `${formatChatTime(thread.created_at || "")} · ${thread.board_title || ""}${thread.is_locked ? " · 已鎖定" : ""}`, "community-author-line", thread.author_avatar_file_id || "")}</div>
      <div class="community-meta">${communityStatusLabel(thread.status)}${thread.review_note ? ` · ${sanitize(thread.review_note)}` : ""}</div>
      ${renderCommunityBody(thread.content || "")}
      <div class="community-actions" style="margin-top:.45rem;">
        ${communityThreadReactionButton(thread, 1, "讚")}
        ${communityThreadReactionButton(thread, -1, "倒讚")}
        ${canModerateThread && canRewardThread ? `<button class="btn community-mini-btn" type="button" data-reward-community-thread="${thread.id}">獎勵作者</button>` : ""}
      </div>
    </div>
    <div class="mini-title" style="margin:.8rem 0 .45rem;">留言區</div>
    ${replies}
    ${postBoundedNote}
  `;
  detail.querySelectorAll("button[data-delete-community-post]").forEach((btn) => {
    btn.addEventListener("click", () => deleteCommunityPost(parseInt(btn.getAttribute("data-delete-community-post"), 10)));
  });
  bindAvatarFallbacks(detail);
  detail.querySelectorAll("button[data-pin-community-post]").forEach((btn) => {
    btn.addEventListener("click", () => toggleCommunityPostPin(
      parseInt(btn.getAttribute("data-pin-community-post"), 10),
      btn.getAttribute("data-pinned") !== "1"
    ));
  });
  detail.querySelectorAll("button[data-penalty-community-post]").forEach((btn) => {
    btn.addEventListener("click", () => penalizeCommunityPost(parseInt(btn.getAttribute("data-penalty-community-post"), 10)));
  });
  detail.querySelectorAll("button[data-community-reaction]").forEach((btn) => {
    btn.addEventListener("click", () => reactToCommunityPost(
      parseInt(btn.getAttribute("data-community-reaction"), 10),
      parseInt(btn.getAttribute("data-reaction-value"), 10)
    ));
  });
  detail.querySelectorAll("button[data-community-thread-reaction]").forEach((btn) => {
    btn.addEventListener("click", () => reactToCommunityThread(
      parseInt(btn.getAttribute("data-community-thread-reaction"), 10),
      parseInt(btn.getAttribute("data-reaction-value"), 10)
    ));
  });
  detail.querySelectorAll("button[data-reward-community-thread]").forEach((btn) => {
    btn.addEventListener("click", () => rewardCommunityThread(parseInt(btn.getAttribute("data-reward-community-thread"), 10)));
  });
  detail.querySelectorAll("button[data-load-more-community-posts]").forEach((btn) => {
    btn.addEventListener("click", () => openCommunityThread(
      thread.id,
      { postPage: parseInt(btn.getAttribute("data-load-more-community-posts"), 10) || 0, appendPosts: true }
    ));
  });
  renderCommunityStage();
}

async function loadAnnouncements() {
  await fetchCsrfToken({ force: true });
  const headers = { "X-CSRF-Token": getCsrfToken() || "" };
  if (communityAnnouncementsEtag) headers["If-None-Match"] = communityAnnouncementsEtag;
  const res = await apiFetch(API + "/community/announcements", {
    credentials: "same-origin",
    headers
  });
  if (res.status === 304) {
    renderCommunityAnnouncements();
    return;
  }
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    flash($("community-msg"), json.msg || "公告讀取失敗", false);
    return;
  }
  communityAnnouncementsEtag = res.headers.get("ETag") || "";
  communityAnnouncements = Array.isArray(json.announcements) ? json.announcements : [];
  renderCommunityAnnouncements();
}

async function publishAnnouncement() {
  await fetchCsrfToken({ force: true });
  const editingId = Number(communityAnnouncementEditingId || 0);
  const isEditing = editingId > 0;
  const res = await apiFetch(API + (isEditing ? `/community/announcements/${editingId}` : "/community/announcements"), {
    method: isEditing ? "PUT" : "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
    body: JSON.stringify({
      title: $("community-announcement-title")?.value || "",
      content: $("community-announcement-content")?.value || "",
      is_pinned: !!$("community-announcement-pinned")?.checked
    })
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-msg"), json.msg || (isEditing ? "公告更新失敗" : "公告發布失敗"), !!json.ok);
  if (json.ok) {
    resetCommunityAnnouncementEditor();
    communityAnnouncementEditorOpen = false;
    renderCommunityAnnouncements();
    await loadAnnouncements();
  }
}

async function deleteAnnouncement(id) {
  if (!confirm("確定要刪除這則公告？")) return;
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/announcements/" + id, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-msg"), json.msg || "公告刪除失敗", !!json.ok);
  if (json.ok) await loadAnnouncements();
}

async function loadCommunityCategories() {
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/categories", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    flash($("community-category-msg"), json.msg || "分類讀取失敗", false);
    return;
  }
  communityCategories = Array.isArray(json.categories) ? json.categories : [];
  renderCommunityCategories();
}

async function createCommunityCategory() {
  if (!canManageCommunity()) return;
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/categories", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
    body: JSON.stringify({
      name: $("community-category-name")?.value || "",
      description: $("community-category-description")?.value || "",
      sort_order: $("community-category-sort")?.value || 100,
      is_active: true
    })
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-category-msg"), json.msg || "分類建立失敗", !!json.ok);
  if (json.ok) {
    if ($("community-category-name")) $("community-category-name").value = "";
    if ($("community-category-description")) $("community-category-description").value = "";
    if ($("community-category-sort")) $("community-category-sort").value = "100";
    await loadCommunityCategories();
  }
}

async function loadCommunityBoards() {
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/boards", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    flash($("community-msg"), json.msg || "討論區清單讀取失敗", false);
    return;
  }
  communityBoards = Array.isArray(json.boards) ? json.boards : [];
  const requestPanel = $("community-board-request-panel");
  if (requestPanel) requestPanel.style.display = communityBoardRequestOpen ? "block" : "none";
  renderCommunityBoards();
  selectedCommunityBoard = communityBoards.find((item) => Number(item.id) === Number(selectedCommunityBoardId)) || null;
  renderCommunityThreads(selectedCommunityBoard);
}

async function requestCommunityBoard() {
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/boards", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
    body: JSON.stringify({
      category_id: $("community-board-category")?.value || null,
      title: $("community-board-title")?.value || "",
      description: $("community-board-description")?.value || "",
      rules: $("community-board-rules")?.value || "",
      visibility: $("community-board-visibility")?.value || "public",
      sort_order: $("community-board-sort")?.value || 100
    })
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-board-request-msg"), json.msg || "申請送出失敗", !!json.ok);
  if (json.ok) {
    if ($("community-board-title")) $("community-board-title").value = "";
    if ($("community-board-description")) $("community-board-description").value = "";
    if ($("community-board-rules")) $("community-board-rules").value = "";
    if ($("community-board-visibility")) $("community-board-visibility").value = "public";
    if ($("community-board-sort")) $("community-board-sort").value = "100";
    communityBoardRequestOpen = false;
    const requestPanel = $("community-board-request-panel");
    if (requestPanel) requestPanel.style.display = "none";
    await loadCommunityBoards();
    if (currentRole === "manager" || currentRole === "super_admin") await loadCommunityBoardReviews();
  }
}

async function loadCommunityModerators(boardId = selectedCommunityBoardId) {
  if (!boardId || !canManageCommunity()) {
    communityBoardModerators = [];
    renderCommunityModerators();
    return;
  }
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/boards/" + boardId + "/moderators", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    communityBoardModerators = [];
    flash($("community-moderator-msg"), json.msg || "版主清單讀取失敗", false);
    renderCommunityModerators();
    return;
  }
  communityBoardModerators = Array.isArray(json.moderators) ? json.moderators : [];
  renderCommunityModerators();
}

async function saveCommunityModerator() {
  if (!selectedCommunityBoardId || !canManageCommunity()) return;
  await loadCommunityModeratorCandidates();
  const userId = parseInt($("community-moderator-user-id")?.value || "", 10);
  if (!userId) {
    flash($("community-moderator-msg"), "請先從下拉選單選擇版主帳號", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/boards/" + selectedCommunityBoardId + "/moderators", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
    body: JSON.stringify({ user_id: userId, ...moderatorPermissionPayload() })
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-moderator-msg"), json.msg || "版主設定儲存失敗", !!json.ok);
  if (json.ok) {
    await Promise.all([loadCommunityModerators(selectedCommunityBoardId), loadCommunityBoards()]);
  }
}

async function deleteCommunityModerator(userId) {
  if (!selectedCommunityBoardId || !userId || !canManageCommunity()) return;
  if (!confirm("確定要移除此版主？")) return;
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/boards/" + selectedCommunityBoardId + "/moderators/" + userId, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-moderator-msg"), json.msg || "版主移除失敗", !!json.ok);
  if (json.ok) {
    await Promise.all([loadCommunityModerators(selectedCommunityBoardId), loadCommunityBoards()]);
  }
}

async function loadCommunityBoardReviews() {
  if (!(currentRole === "manager" || currentRole === "super_admin")) return;
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/boards/reviews", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    flash($("community-msg"), json.msg || "待審核討論區讀取失敗", false);
    return;
  }
  communityBoardReviews = Array.isArray(json.items) ? json.items : [];
  renderCommunityBoardReviews();
}

async function loadCommunityThreadReviews(options = {}) {
  const quiet = !!options.quiet;
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/threads/reviews", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    canReviewCommunityThreads = false;
    communityThreadReviews = [];
    renderCommunityThreadReviews();
    if (!quiet) flash($("community-msg"), json.msg || "待審核主題讀取失敗", false);
    return;
  }
  canReviewCommunityThreads = json.can_review !== false;
  communityThreadReviews = Array.isArray(json.items) ? json.items : [];
  renderCommunityThreadReviews();
}

async function reviewCommunityBoard(boardId, action) {
  await fetchCsrfToken({ force: true });
  const note = prompt(action === "approve" ? "核准備註（可留空）" : "駁回原因（可留空）", "") || "";
  const res = await apiFetch(API + "/community/boards/" + boardId + "/review", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
    body: JSON.stringify({ action, note })
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-msg"), json.msg || "審核失敗", !!json.ok);
  if (json.ok) {
    await Promise.all([loadCommunityBoardReviews(), loadCommunityBoards()]);
  }
}

async function reviewCommunityThread(threadId, action) {
  await fetchCsrfToken({ force: true });
  const note = prompt(action === "approve" ? "核准備註（可留空）" : "駁回原因（可留空）", "") || "";
  const res = await apiFetch(API + "/community/threads/" + threadId + "/review", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
    body: JSON.stringify({ action, note })
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-msg"), json.msg || "主題審核失敗", !!json.ok);
  if (json.ok) {
    await Promise.all([loadCommunityThreadReviews(), loadCommunityBoards()]);
    if (selectedCommunityBoardId) await openCommunityBoard(selectedCommunityBoardId);
  }
}

async function openCommunityBoard(boardId, preserveThread = false) {
  const previousBoardId = selectedCommunityBoardId;
  selectedCommunityBoardId = boardId;
  if (Number(previousBoardId) !== Number(boardId)) {
    communityModeratorManagerOpen = false;
  }
  if (!preserveThread) {
    selectedCommunityThreadId = null;
    selectedCommunityThread = null;
    selectedCommunityPosts = [];
    selectedCommunityPostPageInfo = {};
    communityThreadCreatorOpen = false;
  }
  await fetchCsrfToken({ force: true });
  const q = encodeURIComponent(communityThreadQuery || "");
  const res = await apiFetch(API + "/community/boards/" + boardId + "/threads?page=" + communityThreadPage + "&limit=" + communityThreadLimit + "&q=" + q, {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    flash($("community-msg"), json.msg || "討論區讀取失敗", false);
    return;
  }
  const board = json.board || null;
  if (board) board.can_moderate = !!json.can_moderate;
  selectedCommunityBoard = board;
  communityThreads = Array.isArray(json.threads) ? json.threads : [];
  communityThreadTotal = Number(json.total || 0);
  communityThreadPage = Number(json.page || 0);
  if (canManageCommunity()) await loadCommunityModerators(boardId);
  renderCommunityBoards();
  renderCommunityThreads(board);
  if (!preserveThread) renderCommunityThreadDetail(null, []);
}

async function createCommunityThread() {
  if (!selectedCommunityBoardId) {
    flash($("community-msg"), "請先選擇討論區", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/boards/" + selectedCommunityBoardId + "/threads", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
    body: JSON.stringify({
      title: $("community-thread-title")?.value || "",
      content: $("community-thread-content")?.value || ""
    })
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-msg"), json.msg || "主題建立失敗", !!json.ok);
  if (json.ok) {
    if ($("community-thread-title")) $("community-thread-title").value = "";
    if ($("community-thread-content")) $("community-thread-content").value = "";
    communityThreadCreatorOpen = false;
    await openCommunityBoard(selectedCommunityBoardId);
    if (currentRole !== "manager" && currentRole !== "super_admin") {
      flash($("community-msg"), json.msg || "主題已送審", true);
    } else {
      await loadCommunityThreadReviews();
    }
  }
}

async function openCommunityThread(threadId, options = {}) {
  const postPage = Math.max(0, Number(options.postPage || 0));
  const appendPosts = !!options.appendPosts && Number(selectedCommunityThreadId) === Number(threadId);
  selectedCommunityThreadId = threadId;
  await fetchCsrfToken({ force: true });
  const url = new URL(API + "/community/threads/" + threadId, window.location.origin);
  url.searchParams.set("posts_page", String(postPage));
  url.searchParams.set("posts_limit", "100");
  const res = await apiFetch(url.toString(), {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    flash($("community-msg"), json.msg || "主題讀取失敗", false);
    return;
  }
  selectedCommunityThread = json.thread || null;
  const incomingPosts = Array.isArray(json.posts) ? json.posts : [];
  if (appendPosts) {
    const seen = new Set(selectedCommunityPosts.map((post) => Number(post.id)));
    selectedCommunityPosts = selectedCommunityPosts.concat(incomingPosts.filter((post) => !seen.has(Number(post.id))));
  } else {
    selectedCommunityPosts = incomingPosts;
  }
  selectedCommunityPostPageInfo = json;
  renderCommunityThreads(selectedCommunityBoard || communityBoards.find((item) => Number(item.id) === Number(selectedCommunityBoardId)) || null);
  renderCommunityThreadDetail(selectedCommunityThread, selectedCommunityPosts, selectedCommunityPostPageInfo);
}

async function replyCommunityThread() {
  if (!selectedCommunityThreadId) {
    flash($("community-msg"), "請先選擇主題", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/threads/" + selectedCommunityThreadId + "/posts", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
    body: JSON.stringify({
      content: $("community-reply-content")?.value || ""
    })
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-msg"), json.msg || "留言送出失敗", !!json.ok);
  if (json.ok) {
    if ($("community-reply-content")) $("community-reply-content").value = "";
    await openCommunityBoard(selectedCommunityBoardId, true);
    await openCommunityThread(selectedCommunityThreadId);
  }
}

async function deleteCommunityThread() {
  if (!selectedCommunityThreadId) return;
  if (!confirm("確定要刪除此主題？回覆也會一併刪除。")) return;
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/threads/" + selectedCommunityThreadId, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-msg"), json.msg || "主題刪除失敗", !!json.ok);
  if (json.ok) {
    selectedCommunityThreadId = null;
    selectedCommunityThread = null;
    selectedCommunityPosts = [];
    selectedCommunityPostPageInfo = {};
    renderCommunityThreadDetail(null, []);
    if (selectedCommunityBoardId) await openCommunityBoard(selectedCommunityBoardId);
  }
}

async function deleteCommunityPost(postId) {
  if (!postId) return;
  if (!confirm("確定要刪除此留言？")) return;
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/posts/" + postId, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": getCsrfToken() || "" }
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-msg"), json.msg || "留言刪除失敗", !!json.ok);
  if (json.ok && selectedCommunityThreadId) {
    if (selectedCommunityBoardId) await openCommunityBoard(selectedCommunityBoardId, true);
    await openCommunityThread(selectedCommunityThreadId);
  }
}

async function toggleCommunityPostPin(postId, pinned) {
  if (!postId) return;
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/posts/" + postId + "/pin", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
    body: JSON.stringify({ pinned })
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-msg"), json.msg || "留言置頂更新失敗", !!json.ok);
  if (json.ok && selectedCommunityThreadId) {
    await openCommunityThread(selectedCommunityThreadId);
  }
}

async function reactToCommunityPost(postId, value) {
  if (!postId) return;
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/posts/" + postId + "/reaction", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
    body: JSON.stringify({ value })
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) {
    if (json.auto_hidden) {
      flash($("community-msg"), "留言倒讚過多，已自動隱藏並送 root 審核", false);
    }
    if (selectedCommunityBoardId) await openCommunityBoard(selectedCommunityBoardId, true);
    if (selectedCommunityThreadId) await openCommunityThread(selectedCommunityThreadId);
    return;
  }
  flash($("community-msg"), json.msg || "反應更新失敗", false);
}

async function reactToCommunityThread(threadId, value) {
  if (!threadId) return;
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/threads/" + threadId + "/reaction", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
    body: JSON.stringify({ value })
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) {
    if (selectedCommunityBoardId) await openCommunityBoard(selectedCommunityBoardId, true);
    if (selectedCommunityThreadId) await openCommunityThread(selectedCommunityThreadId);
    return;
  }
  flash($("community-msg"), json.msg || "主題反應更新失敗", false);
}

async function rewardCommunityThread(threadId) {
  if (!threadId) return;
  const pointsRaw = prompt("獎勵點數（1-50）", "1");
  if (pointsRaw === null) return;
  const reason = prompt("獎勵理由", "優質主題貢獻") || "優質主題貢獻";
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/threads/" + threadId + "/reward", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
    body: JSON.stringify({ points: Number(pointsRaw) || 1, reason })
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-msg"), json.msg || "獎勵失敗", !!json.ok);
  if (json.ok && selectedCommunityThreadId) await openCommunityThread(selectedCommunityThreadId);
}

async function penalizeCommunityPost(postId) {
  if (!postId) return;
  const pointsRaw = prompt("懲處點數（1-10）", "1");
  if (pointsRaw === null) return;
  const reason = prompt("懲處原因", "討論區違規留言") || "討論區違規留言";
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/posts/" + postId + "/penalty", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
    body: JSON.stringify({ points: Number(pointsRaw) || 1, reason })
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-msg"), json.msg || "懲處失敗", !!json.ok);
  if (json.ok && selectedCommunityThreadId) await openCommunityThread(selectedCommunityThreadId);
}

async function toggleCommunityThreadLock() {
  if (!selectedCommunityThreadId || !selectedCommunityThread) return;
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/threads/" + selectedCommunityThreadId + "/lock", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
    body: JSON.stringify({ locked: !selectedCommunityThread.is_locked })
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-msg"), json.msg || "主題狀態更新失敗", !!json.ok);
  if (json.ok) {
    await openCommunityBoard(selectedCommunityBoardId, true);
    await openCommunityThread(selectedCommunityThreadId);
  }
}

async function toggleCommunityThreadSticky() {
  if (!selectedCommunityThreadId || !selectedCommunityThread) return;
  await fetchCsrfToken({ force: true });
  const res = await apiFetch(API + "/community/threads/" + selectedCommunityThreadId + "/sticky", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() || "" },
    body: JSON.stringify({ sticky: !selectedCommunityThread.is_sticky })
  });
  const json = await res.json().catch(() => ({}));
  flash($("community-msg"), json.msg || "主題置頂更新失敗", !!json.ok);
  if (json.ok) {
    await openCommunityBoard(selectedCommunityBoardId, true);
    await openCommunityThread(selectedCommunityThreadId);
  }
}

async function loadCommunityHome() {
  resetCommunityReviewState();
  switchCommunityMode("boards");
  await Promise.all([
    loadCommunityCategories(),
    loadCommunityBoards(),
    (currentRole === "manager" || currentRole === "super_admin") ? loadCommunityBoardReviews() : Promise.resolve(),
    loadCommunityThreadReviews({ quiet: true }),
  ]);
}

function toggleCommunityAnnouncementEditor(forceOpen = null) {
  communityAnnouncementEditorOpen = forceOpen === null ? !communityAnnouncementEditorOpen : !!forceOpen;
  if (!communityAnnouncementEditorOpen) resetCommunityAnnouncementEditor();
  renderCommunityAnnouncements();
}

function toggleCommunityBoardRequest(forceOpen = null) {
  communityBoardRequestOpen = forceOpen === null ? !communityBoardRequestOpen : !!forceOpen;
  if (communityBoardRequestOpen) toggleCommunityTools(true);
  const panel = $("community-board-request-panel");
  if (panel) panel.style.display = communityBoardRequestOpen ? "block" : "none";
}

function toggleCommunityCategoryManager(forceOpen = null) {
  communityCategoryManagerOpen = forceOpen === null ? !communityCategoryManagerOpen : !!forceOpen;
  if (communityCategoryManagerOpen) toggleCommunityTools(true);
  renderCommunityCategories();
}

function toggleCommunityThreadCreator(forceOpen = null) {
  communityThreadCreatorOpen = forceOpen === null ? !communityThreadCreatorOpen : !!forceOpen;
  renderCommunityThreads(selectedCommunityBoard);
}

function resetCommunityAccountState() {
  communityAccountGeneration += 1;
  communityBoards = [];
  communityCategories = [];
  communityAnnouncements = [];
  communityAnnouncementsEtag = "";
  selectedCommunityBoardId = null;
  selectedCommunityThreadId = null;
  communityThreads = [];
  selectedCommunityBoard = null;
  selectedCommunityThread = null;
  selectedCommunityPosts = [];
  selectedCommunityPostPageInfo = {};
  communityBoardModerators = [];
  communityModeratorCandidates = [];
  communityModeratorCandidatesLoadedAt = 0;
  communityThreadPage = 0;
  communityThreadTotal = 0;
  communityBoardQuery = "";
  communityThreadQuery = "";
  communityAnnouncementEditorOpen = false;
  communityBoardRequestOpen = false;
  communityCategoryManagerOpen = false;
  communityThreadCreatorOpen = false;
  communityModeratorManagerOpen = false;
  resetCommunityAnnouncementEditor();
  resetCommunityReviewState();
  toggleCommunityTools(false);
  showCommunityBoardStage();
  renderCommunityAnnouncements();
  renderCommunityCategories();
  [
    "community-thread-title",
    "community-thread-content",
    "community-reply-content",
    "community-thread-media-input",
    "community-reply-media-input",
    "community-board-title",
    "community-board-description",
    "community-board-rules",
    "community-category-name",
    "community-category-description",
    "community-moderator-user-id",
  ].forEach((id) => {
    const input = $(id);
    if (input && "value" in input) input.value = "";
  });
}

document.addEventListener("hackme:account-context-changed", resetCommunityAccountState);
