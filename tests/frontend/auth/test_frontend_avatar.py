from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_avatar_upload_ui_is_wired():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    users_js = (ROOT / "public" / "js" / "10-users.js").read_text(encoding="utf-8")
    community_js = (ROOT / "public" / "js" / "25-community.js").read_text(encoding="utf-8")
    chat_js = (ROOT / "public" / "js" / "20-chat.js").read_text(encoding="utf-8")
    auth_js = (ROOT / "public" / "js" / "40-auth-users.js").read_text(encoding="utf-8")
    profile_js = (ROOT / "public" / "js" / "58-profile-friends.js").read_text(encoding="utf-8")
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")

    assert 'id="profile-avatar-file"' in index_html
    assert 'id="profile-avatar-upload-btn"' in index_html
    assert 'id="profile-avatar-crop-width"' in index_html
    assert 'id="profile-avatar-crop-zoom-value" for="profile-avatar-crop-zoom"' in index_html
    assert 'id="profile-avatar-crop-zoom" min="0.1" max="10" step="0.01" value="1"' in index_html
    assert 'data-profile-avatar-zoom-step="-0.05"' in index_html
    assert 'data-profile-avatar-zoom-step="0.05"' in index_html
    assert 'id="profile-avatar-crop-rotation-value" for="profile-avatar-crop-rotation"' in index_html
    assert 'id="profile-avatar-crop-rotation" value="0"' in index_html
    assert 'data-profile-avatar-rotate-step="-90"' in index_html
    assert 'data-profile-avatar-rotate-step="90"' in index_html
    zoom_pos = index_html.index('id="profile-avatar-crop-zoom"')
    assert 'max="6"' not in index_html[zoom_pos - 80:zoom_pos + 160]
    assert 'type="number" id="profile-avatar-crop-zoom"' not in index_html
    assert 'type="range" id="profile-avatar-crop-zoom"' in index_html
    assert "async function uploadUserAvatar()" in auth_js
    assert "function selectedUserAvatarFile()" in auth_js
    assert "async function buildCroppedAvatarUpload" in auth_js
    assert "function normalizeAvatarRotation(value)" in auth_js
    assert "els.rotationSteps.forEach((button) => {" in auth_js
    assert 'button.dataset.editAvatarRotateStep || "0"' in auth_js
    assert "syncAvatarRotationControl(avatarCropState.rotation + delta)" in auth_js
    assert "avatarCanvasSourceFromImage(image, normalizedRotation)" in auth_js
    assert "ctx.drawImage(" in auth_js
    assert "canvas.width = 512" in auth_js
    assert 'form.append("avatar_client_cropped", "1")' in auth_js
    assert "const avatarFile = selectedUserAvatarFile();" in auth_js
    assert "if (!Object.keys(payload).length && !avatarFile && !appearanceChanged && !timezoneChanged)" in auth_js
    assert "submitUserAvatarUpload({ reloadUsers: false })" in auth_js
    assert 'apiFetch(API + `/admin/users/${editingUserId}/avatar`' in auth_js
    assert "markUserAvatarUpdated(editingUserId, json.avatar_file_id || \"\")" in auth_js
    assert "if (typeof bindProfileFriendsControls === \"function\") bindProfileFriendsControls();" in bootstrap_js
    assert "function bindProfileAvatarUploaderControls()" in profile_js
    assert "bindProfileAvatarUploaderControls();" in profile_js
    assert "function profileAvatarZoomLabel(value)" in profile_js
    assert "function syncProfileAvatarZoomControl(value = profileAvatarCropState.zoom)" in profile_js
    assert "function profileAvatarRotationLabel(value)" in profile_js
    assert "els.rotationSteps.forEach((button) => {" in profile_js
    assert 'button.dataset.profileAvatarRotateStep || "0"' in profile_js
    assert "syncProfileAvatarRotationControl(profileAvatarCropState.rotation + delta)" in profile_js
    assert "rotation: profileAvatarCropState.rotation" in profile_js
    assert "function avatarUrlForUser(userId, avatarFileId = \"\")" in core_js
    assert "function userAvatarMarkup(userId, username, extraClass = \"\", avatarFileId = \"\")" in core_js
    assert "currentUserAvatarFileId = json.avatar_file_id || \"\";" in core_js
    assert "avatar.innerHTML = currentUser ? userAvatarInnerMarkup(currentUserId, currentUser, currentUserAvatarFileId)" in core_js
    assert "userAvatarMarkup(m.sender_id, m.sender || \"系統\", \"user-avatar-sm\", m.sender_avatar_file_id || \"\")" in core_js
    assert "usernameCell.innerHTML = userIdentityMarkup(u.id, u.username || \"\", u.nickname || \"\", \"user-table-identity\", u.avatar_file_id || \"\")" in users_js
    assert "openPmWithUser(u.username)" in users_js
    assert "chat-message-image-preview" in core_js
    assert "thread.author_avatar_file_id || \"\"" in community_js
    assert "post.author_avatar_file_id || \"\"" in community_js


def test_admin_user_table_shows_online_light():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    users_js = (ROOT / "public" / "js" / "10-users.js").read_text(encoding="utf-8")
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    assert "<th>在線</th>" in index_html
    assert "online-dot" in users_js
    assert "u.is_online" in users_js
    assert ".online-dot.online" in css
