from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_profile_friends_panel_is_wired_as_user_module():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    admin_js = (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")
    profile_js = (ROOT / "public" / "js" / "58-profile-friends.js").read_text(encoding="utf-8")
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    chat_pos = index_html.index('id="tab-module-chat"')
    profile_pos = index_html.index('id="tab-module-profile"')
    announcements_pos = index_html.index('id="tab-module-announcements"')
    assert announcements_pos < chat_pos < profile_pos
    assert 'id="sidebar-user-card" role="button" tabindex="0"' in index_html
    assert 'id="module-profile"' in index_html
    assert 'data-profile-tab="home"' in index_html
    assert 'data-profile-tab="edit"' in index_html
    assert 'data-profile-tab="friends"' in index_html
    assert 'id="s-module-profile-min-role"' in index_html
    assert 'id="profile-avatar-cloud-file"' in index_html
    assert 'id="profile-avatar-cloud-use"' in index_html
    assert 'id="profile-avatar-crop-zoom" min="0.5" max="6"' in index_html
    assert 'id="profile-edit-avatar-size" min="100" max="220" step="5" value="140"' in index_html
    assert 'data-profile-avatar-size="220">最大</button>' in index_html
    assert '/styles.css?v=20260602-profile-avatar-large' in index_html
    assert 'id="profile-edit-display-timezone"' in index_html
    assert 'id="profile-quick-customize-card"' in index_html
    assert 'id="profile-appearance-save-btn"' in index_html
    assert 'id="profile-edit-template"' in index_html
    assert 'id="profile-edit-accent"' in index_html
    assert 'id="profile-edit-density"' in index_html
    assert 'id="edit-user-display-timezone"' in index_html
    assert 'id="edit-user-preferences-section"' in index_html
    assert "跟隨瀏覽器" in index_html
    assert "/js/58-profile-friends.js" in index_html
    assert 'tabId: "tab-module-profile"' in core_js
    assert 'action: "profile:appearance"' in core_js
    assert 'action: "profile:friends"' in core_js
    assert 'switchModuleTab("profile")' in core_js
    assert 'currentModuleTab === "profile"' in core_js
    assert 'canAccessProfile' in admin_js
    assert 'module_profile_min_role' in admin_js
    assert 'modProfile.classList.toggle("active", normTab === "profile")' in admin_js
    assert 'loadProfilePanel()' in admin_js
    assert 'tabModuleProfile.addEventListener("click", () => switchModuleTab("profile"))' in bootstrap_js
    assert 'openMyProfilePanel("edit")' in bootstrap_js
    assert 'bindProfileFriendsControls()' in bootstrap_js
    assert '/users/me/profile' in profile_js
    assert '/friends/add-by-code' in profile_js
    assert '/friends/request' in profile_js
    assert '/users/target-options' in profile_js
    assert '["chat-room-target-user", "pm"]' in profile_js
    assert '["drive-share-account", "cloud_drive_share"]' in profile_js
    assert 'target-options-personal' in index_html
    assert '一般用戶限好友' in index_html
    users_js = (ROOT / "public" / "js" / "10-users.js").read_text(encoding="utf-8")
    assert "u.is_friend || canAdministrativePm" in users_js
    assert 'async function copyTextToClipboard(text)' in profile_js
    assert 'await navigator.clipboard.writeText(value)' in profile_js
    assert 'textarea.value = value' in profile_js
    assert 'button.textContent = "已複製"' in profile_js
    assert 'showActionFeedback(button || document.activeElement, "好友代碼已複製"' in profile_js
    assert "profileAvatarCloudFileIsUsable" in profile_js
    assert "cloud_file_id" in profile_js
    assert "/cloud-drive/files?user_id=" in profile_js
    assert "/preview/content" in profile_js
    assert "display_timezone" in profile_js
    assert "profile_template" in profile_js
    assert "profile_accent" in profile_js
    assert "profile_density" in profile_js
    assert "profile_style: collectProfileStyleFromForm()" in profile_js
    assert 'avatar_size: "140"' in profile_js
    assert 'const MAX_PROFILE_AVATAR_SIZE = 220' in profile_js
    assert 'numeric <= 90' in profile_js
    assert "function previewProfileAppearanceFromForm()" in profile_js
    assert "function applyProfilePresentation(profile)" in profile_js
    assert "setUserDisplayTimezone" in profile_js
    auth_js = (ROOT / "public" / "js" / "40-auth-users.js").read_text(encoding="utf-8")
    assert "saveUserDisplayTimezoneSetting" in auth_js
    assert 'API + "/users/me/profile"' in auth_js
    assert "selectedUserDisplayTimezone" in auth_js
    assert "openProfileAvatarPreview(profilePanelCache)" in profile_js
    assert "function updateProfileTabVisibility()" in profile_js
    assert 'btn.hidden = hidden;' in profile_js
    assert 'btn.style.display = hidden ? "none" : "";' in profile_js
    assert '!currentProfileIsViewingSelf && ["edit", "friends"].includes(requested)' in profile_js
    assert '.profile-tabs [hidden]' in css
    assert "const PROFILE_AVATAR_CROPPER_MIN_ZOOM = 0.5" in profile_js
    assert "function profileAvatarMinimumZoom(metrics)" in profile_js
    assert "buildCroppedAvatarUpload(image, crop" in profile_js
    assert 'form.append("avatar_client_cropped", "1")' in profile_js
    assert "profile-avatar-preview-overlay" in profile_js
    assert '.profile-friend-columns' in css
    assert '.profile-avatar-cloud-row' in css
    assert ".profile-avatar-preview-frame" in css
    assert ".profile-summary.profile-template-creator" in css
    assert "grid-template-columns: minmax(150px, 180px) minmax(0, 1fr);" in css
    assert "var(--profile-avatar-custom-size, 140px)" in css
    assert ".profile-summary.profile-template-gallery" in css
    assert ".profile-summary.profile-accent-ocean" in css
    assert ".profile-summary.profile-accent-violet" in css
    assert ".profile-quick-customize" in css
    assert '.sidebar-user-card:hover' in css
    assert "USER_DISPLAY_TIMEZONE_STORAGE_KEY" in core_js
    assert "function getUserDisplayTimezone" in core_js
    assert "timeZone: displayTimezone" in core_js
