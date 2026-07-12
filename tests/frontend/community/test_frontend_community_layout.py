from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_community_composers_are_button_opened_not_permanent():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    community_js = (ROOT / "public" / "js" / "25-community.js").read_text(encoding="utf-8")
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")

    assert 'id="community-announcement-open-btn"' in index_html
    assert 'id="tab-module-announcements"' in index_html
    assert 'id="module-announcements"' in index_html
    assert 'id="community-announcement-cancel-btn"' in index_html
    assert index_html.index('id="module-announcements"') < index_html.index('id="module-community"')
    assert 'id="community-board-request-open-btn"' in index_html
    assert 'id="community-board-request-cancel-btn"' in index_html
    assert 'id="community-thread-create-open-btn"' in index_html
    assert 'id="community-thread-create-cancel-btn"' in index_html
    assert 'id="community-thread-media-input"' in index_html
    assert 'data-community-media-picker="community-thread-content"' in index_html
    assert 'id="community-reply-media-input"' in index_html
    assert 'data-community-media-picker="community-reply-content"' in index_html
    assert 'id="community-moderator-open-btn" type="button" style="display:none;"' in index_html
    assert 'id="community-moderator-manager" class="community-panel" style="display:none;margin-bottom:.75rem;"' in index_html
    assert '<select id="community-moderator-user-id">' in index_html
    assert "輸入要設定為版主的帳號 ID" not in index_html
    assert 'id="community-tools-panel" class="community-tools-drawer" style="display:none;"' in index_html
    assert 'id="community-review-area" class="community-review-area" style="display:none;"' in index_html
    assert 'id="community-board-stage"' in index_html
    assert 'id="community-thread-stage" class="community-stage" style="display:none;"' in index_html
    assert 'id="community-detail-stage" class="community-stage" style="display:none;"' in index_html
    assert 'id="community-board-request-panel" style="display:none;"' in index_html
    assert 'id="community-thread-creator" style="display:none;"' in index_html
    assert 'id="community-mod-can-pin-threads"' in index_html
    assert 'id="community-thread-sticky-toggle"' in index_html
    assert "/js/25-community.js?v=__ASSET_VERSION__" in index_html

    assert "communityAnnouncementEditorOpen = false" in community_js
    assert "communityAnnouncementEditingId = null" in community_js
    assert "communityBoardRequestOpen = false" in community_js
    assert "communityThreadCreatorOpen = false" in community_js
    assert "communityModeratorManagerOpen = false" in community_js
    assert "toggleCommunityModeratorManager" in community_js
    assert 'openBtn.textContent = communityModeratorManagerOpen ? "隱藏版主設定" : "版主設定"' in community_js
    assert "function renderCommunityModeratorUserOptions" in community_js
    assert "async function loadCommunityModeratorCandidates" in community_js
    assert "請先從下拉選單選擇版主帳號" in community_js
    assert 'communityMode = "boards"' in community_js
    assert "function canOpenCommunityReviewMode()" in community_js
    assert "function resetCommunityReviewState()" in community_js
    assert 'communityMode = nextMode === "review" && !canOpenCommunityReviewMode() ? "boards" : nextMode' in community_js
    assert "resetCommunityReviewState();" in community_js
    assert 'requiresCommunityReview: true' in core_js
    assert "function canShowSidebarSubitem(sub)" in core_js
    assert 'if (typeof canOpenCommunityReviewMode === "function") return canOpenCommunityReviewMode();' in core_js
    assert 'subButton.style.display = visible && canShowSidebarSubitem(sub) ? "" : "none";' in core_js
    assert "reactToCommunityThread" in community_js
    assert "data-edit-announcement" in community_js
    assert "function editAnnouncement" in community_js
    assert "function resetCommunityAnnouncementEditor" in community_js
    assert "communityAnnouncementsEtag" in community_js
    assert 'headers["If-None-Match"] = communityAnnouncementsEtag' in community_js
    assert 'submitBtn.textContent = communityAnnouncementEditingId ? "更新公告" : "發布公告"' in community_js
    assert 'method: isEditing ? "PUT" : "POST"' in community_js
    assert '`/community/announcements/${editingId}`' in community_js
    assert "rewardCommunityThread" in community_js
    assert "penalizeCommunityPost" in community_js
    assert "can_pin_threads" in community_js
    assert "toggleCommunityThreadSticky" in community_js
    assert "data-load-more-community-posts" in community_js
    assert "appendPosts: true" in community_js
    assert "COMMUNITY_INLINE_MEDIA_TOKEN_RE" in community_js
    assert "function uploadCommunityInlineMedia" in community_js
    assert "function insertCommunityInlineMediaToken" in community_js
    assert "/storage/share-links" in community_js
    assert "community-media:${kind}:${token}" in community_js
    assert "openCommunityInlineMediaPicker(btn)" in bootstrap_js
    assert "uploadCommunityInlineMedia(input)" in bootstrap_js
    assert 'flash($("community-msg"), json.msg || "公告讀取失敗", false);' in community_js
    assert 'flash($("community-category-msg"), json.msg || "分類讀取失敗", false);' in community_js
    assert 'flash($("community-msg"), json.msg || "討論區清單讀取失敗", false);' in community_js
    assert 'flash($("community-msg"), json.msg || "待審核討論區讀取失敗", false);' in community_js
    assert "async function loadCommunityThreadReviews(options = {})" in community_js
    assert "const quiet = !!options.quiet;" in community_js
    assert 'if (!quiet) flash($("community-msg"), json.msg || "待審核主題讀取失敗", false);' in community_js
    assert "loadCommunityThreadReviews({ quiet: true })" in community_js
    assert 'switchModuleTab("announcements")' in bootstrap_js
    assert "toggleCommunityAnnouncementEditor(true)" in bootstrap_js
    assert "toggleCommunityBoardRequest(true)" in bootstrap_js
    assert "toggleCommunityThreadCreator(true)" in bootstrap_js
    assert "toggleCommunityModeratorManager()" in bootstrap_js
    assert "toggleCommunityThreadSticky" in bootstrap_js
