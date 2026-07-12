const CHAT_STICKER_LABELS = {
  smile: "🙂",
  thanks: "🥹",
  ok: "😙",
  wow: "😃",
  cheer: "😚",
  sad: "🥲"
};
let pendingChatAttachments = [];
const chatMessageLatestIdByRoom = {};
const chatMessagePollTicksByRoom = {};

function resetChatAccountState() {
  stopChatPoll();
  pendingChatAttachments = [];
  chatRooms = [];
  selectedChatRoomId = null;
  chatMessageCache = [];
  Object.keys(chatMessageLatestIdByRoom).forEach((key) => delete chatMessageLatestIdByRoom[key]);
  Object.keys(chatMessagePollTicksByRoom).forEach((key) => delete chatMessagePollTicksByRoom[key]);
  renderPendingChatAttachments();
  setChatCreatePanelVisible(false);
  setChatJoinPanelVisible(false);
  const roomList = $("chat-room-list");
  const messages = $("chat-room-messages");
  const attachments = $("chat-attachment-list");
  if (roomList) roomList.innerHTML = "<p style=\"color:var(--muted);\">尚未載入聊天室</p>";
  if (messages) messages.innerHTML = "<p style=\"color:var(--muted);\">尚未選擇聊天室</p>";
  if (attachments) attachments.innerHTML = "";
  syncChatSharedAttachmentPanel([]);
}

document.addEventListener("hackme:account-context-changed", resetChatAccountState);

function chatConfirm(message, options = {}) {
  if (typeof showAppConfirm === "function") return showAppConfirm(message, options);
  return Promise.resolve(window.confirm(message));
}

function chatPrompt(message, options = {}) {
  if (typeof showAppPrompt === "function") return showAppPrompt(message, options);
  return Promise.resolve(window.prompt(message, options.defaultValue || ""));
}

function chatStickerLabel(key, sticker) {
  return (sticker && (sticker.glyph || sticker.label)) || CHAT_STICKER_LABELS[key] || "🙂";
}

function chatAttachmentName(item) {
  return item?.original_filename_plain_for_public || item?.display_name || item?.file_id || "附件";
}

function renderPendingChatAttachments() {
  const list = $("chat-pending-attachment-list");
  if (!list) return;
  if (!pendingChatAttachments.length) {
    list.innerHTML = `<div class="drive-empty">尚未選擇要隨訊息送出的附件</div>`;
    return;
  }
  list.innerHTML = pendingChatAttachments.map((item) => `
    <div class="chat-pending-attachment">
      <span>${sanitize(chatAttachmentName(item))}</span>
      <button class="btn btn-danger chat-sticker-btn" type="button" data-remove-chat-pending-attachment="${sanitize(item.file_id || "")}">刪除</button>
    </div>
  `).join("");
}

function removePendingChatAttachment(fileId) {
  const target = String(fileId || "");
  if (!target) return;
  pendingChatAttachments = pendingChatAttachments.filter((item) => String(item.file_id || "") !== target);
  renderPendingChatAttachments();
}

function handlePendingChatAttachmentClick(event) {
  const btn = event.target?.closest?.("[data-remove-chat-pending-attachment]");
  if (!btn) return;
  event.preventDefault();
  event.stopPropagation();
  removePendingChatAttachment(btn.getAttribute("data-remove-chat-pending-attachment") || "");
}

function addPendingChatAttachment(file) {
  const fileId = file?.file_id || file?.id || "";
  if (!fileId) return;
  if (pendingChatAttachments.some((item) => item.file_id === fileId)) {
    renderPendingChatAttachments();
    return;
  }
  pendingChatAttachments.push({ ...file, file_id: fileId });
  renderPendingChatAttachments();
}

function syncChatSharedAttachmentPanel(refs) {
  const panel = $("chat-shared-attachment-panel");
  const list = $("chat-attachment-list");
  if (!panel || !list) return;
  const hasRows = Array.isArray(refs) ? refs.length > 0 : !!list.querySelector(".drive-file-row");
  const hasError = !Array.isArray(refs) && !!list.textContent.trim();
  panel.hidden = !hasRows && !hasError;
  if (!hasRows && !hasError) panel.open = false;
}

function setChatDialogVisible(panelId, toggleId, visible, focusIds = []) {
  const panel = $(panelId);
  const toggle = $(toggleId);
  const show = !!visible;
  if (panel) {
    panel.hidden = !show;
    panel.classList.toggle("show", show);
    panel.setAttribute("aria-hidden", show ? "false" : "true");
  }
  if (toggle) {
    toggle.setAttribute("aria-expanded", show ? "true" : "false");
  }
  if (show) {
    const target = focusIds.map((id) => $(id)).find(Boolean);
    target?.focus?.();
  }
}

function setChatCreatePanelVisible(visible) {
  if (visible) setChatJoinPanelVisible(false);
  setChatDialogVisible("chat-room-create-panel", "chat-create-room-toggle-btn", visible, ["chat-room-target-user", "chat-room-name"]);
}

function setChatJoinPanelVisible(visible) {
  if (visible) setChatCreatePanelVisible(false);
  setChatDialogVisible("chat-room-join-panel", "chat-join-room-open-btn", visible, ["chat-join-room-id"]);
}

document.addEventListener("click", (event) => {
  const list = $("chat-pending-attachment-list");
  if (list?.contains(event.target)) handlePendingChatAttachmentClick(event);
  if (event.target?.id === "chat-room-create-panel") setChatCreatePanelVisible(false);
  if (event.target?.id === "chat-room-join-panel") setChatJoinPanelVisible(false);
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const createPanel = $("chat-room-create-panel");
  const joinPanel = $("chat-room-join-panel");
  if (createPanel && !createPanel.hidden) {
    setChatCreatePanelVisible(false);
  }
  if (joinPanel && !joinPanel.hidden) {
    setChatJoinPanelVisible(false);
  }
});

function toggleChatCreatePanel() {
  setChatCreatePanelVisible(!!$("chat-room-create-panel")?.hidden);
}

function toggleChatJoinPanel() {
  setChatJoinPanelVisible(!!$("chat-room-join-panel")?.hidden);
}

async function loadChatRooms() {
  const csrf = await fetchCsrfToken();
  try {
    const res = await apiFetch(API + "/chat/rooms", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const raw = await res.text();
    const json = (() => {
      try { return raw ? JSON.parse(raw) : {}; } catch (_err) { return {}; }
    })();
    if (!res.ok || !json.ok) {
      const fallback = (raw || "").split("\n")[0].trim();
      setChatMsg("chat-room-warn", `${res.ok ? "讀取聊天室失敗" : "讀取聊天室失敗（" + res.status + "）"} ${json.msg || fallback || "請稍後再試"}`, false);
      return;
    }
    chatRooms = Array.isArray(json.rooms) ? json.rooms : [];
    renderChatRooms();
    loadChatFriends().catch((err) => {
      setChatMsg("chat-room-warn", `好友清單讀取失敗：${err?.message || "請稍後再試"}`, false);
    });
    if (selectedChatRoomId) {
      const exists = chatRooms.some((r) => r.id === selectedChatRoomId);
      if (!exists) {
        selectedChatRoomId = null;
      }
    }
    if (!selectedChatRoomId && chatRooms.length) {
      await openChatRoom(chatRooms[0].id, true);
    }
    if (!selectedChatRoomId) {
      const roomTitle = $("chat-room-title");
      if (roomTitle) roomTitle.textContent = "請先建立或加入聊天室";
      const memberLabel = $("chat-room-member");
      if (memberLabel) memberLabel.textContent = "";
      const msgs = $("chat-room-messages");
      if (msgs) msgs.innerHTML = "<p style=\"color:var(--muted);\">尚未選擇聊天室</p>";
      const attachments = $("chat-attachment-list");
      if (attachments) attachments.innerHTML = "";
      syncChatSharedAttachmentPanel([]);
    }
  } catch (err) {
    setChatMsg("chat-room-warn", `讀取聊天室失敗：${err?.message || "請稍後再試"}`, false);
  }
}

function renderChatFriends(data) {
  const list = $("chat-friend-list");
  if (!list) return;
  const friends = Array.isArray(data?.friends) ? data.friends : [];
  const incoming = Array.isArray(data?.incoming) ? data.incoming : [];
  const outgoing = Array.isArray(data?.outgoing) ? data.outgoing : [];
  const blocked = Array.isArray(data?.blocked) ? data.blocked : [];
  const rows = [];
  incoming.forEach((item) => {
    rows.push(`
      <div class="chat-friend-row">
        <span>邀請：<strong>${sanitize(item.other_username || "-")}</strong></span>
        <span>
          <button class="btn chat-sticker-btn" type="button" data-friend-review="${item.id}" data-decision="accept">接受</button>
          <button class="btn chat-sticker-btn" type="button" data-friend-review="${item.id}" data-decision="reject">拒絕</button>
        </span>
      </div>
    `);
  });
  friends.forEach((item) => {
    rows.push(`
      <div class="chat-friend-row">
        <strong>${sanitize(item.other_username || "-")}</strong>
        <span>
          <button class="btn chat-sticker-btn" type="button" data-friend-pm="${sanitize(item.other_username || "")}">私訊</button>
          <button class="btn chat-sticker-btn" type="button" data-friend-remove="${item.other_user_id}">解除</button>
          <button class="btn btn-danger chat-sticker-btn" type="button" data-friend-block="${item.other_user_id}">封鎖</button>
        </span>
      </div>
    `);
  });
  outgoing.forEach((item) => {
    rows.push(`<div class="chat-friend-row"><span>等待 ${sanitize(item.other_username || "-")} 回覆</span><span></span></div>`);
  });
  blocked.forEach((item) => {
    rows.push(`
      <div class="chat-friend-row">
        <span>已封鎖：<strong>${sanitize(item.other_username || "-")}</strong></span>
        <span><button class="btn chat-sticker-btn" type="button" data-friend-unblock="${item.other_user_id}">解除封鎖</button></span>
      </div>
    `);
  });
  list.innerHTML = rows.length ? rows.join("") : `<div class="drive-card-sub">尚無好友</div>`;
  list.querySelectorAll("[data-friend-review]").forEach((btn) => {
    btn.addEventListener("click", () => reviewChatFriendRequest(btn.dataset.friendReview, btn.dataset.decision));
  });
  list.querySelectorAll("[data-friend-pm]").forEach((btn) => {
    btn.addEventListener("click", () => openPmWithUser(btn.dataset.friendPm || ""));
  });
  list.querySelectorAll("[data-friend-remove]").forEach((btn) => {
    btn.addEventListener("click", () => removeChatFriend(btn.dataset.friendRemove));
  });
  list.querySelectorAll("[data-friend-block]").forEach((btn) => {
    btn.addEventListener("click", () => blockChatFriend(btn.dataset.friendBlock));
  });
  list.querySelectorAll("[data-friend-unblock]").forEach((btn) => {
    btn.addEventListener("click", () => unblockChatFriend(btn.dataset.friendUnblock));
  });
}

async function loadChatFriends() {
  if (!currentUser) return;
  const csrf = await fetchCsrfToken();
  const res = await apiFetch(API + "/chat/friends", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) renderChatFriends(json);
}

async function addChatFriend() {
  const input = $("chat-friend-username");
  const username = (input?.value || "").trim();
  if (!username) {
    setChatMsg("chat-room-warn", "請輸入好友帳號", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/chat/friends/requests", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ username })
  });
  const json = await res.json().catch(() => ({}));
  setChatMsg("chat-room-warn", json.msg || (json.ok ? "好友邀請已送出" : "好友邀請失敗"), !!json.ok);
  if (json.ok) {
    if (input) input.value = "";
    await loadChatFriends();
  }
}

async function reviewChatFriendRequest(requestId, decision) {
  if (!requestId || !decision) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/chat/friends/requests/${encodeURIComponent(requestId)}/${encodeURIComponent(decision)}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  setChatMsg("chat-room-warn", json.msg || "好友邀請已處理", !!json.ok);
  if (json.ok) await loadChatFriends();
}

async function removeChatFriend(friendUserId) {
  if (!friendUserId) return;
  if (!(await chatConfirm("確定解除好友？解除後需要重新申請才能恢復好友互動。", {
    title: "解除好友",
    confirmLabel: "解除",
    danger: true,
  }))) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/chat/friends/${encodeURIComponent(friendUserId)}`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  setChatMsg("chat-room-warn", json.msg || "好友已更新", !!json.ok);
  if (json.ok) await loadChatFriends();
}

async function blockChatFriend(friendUserId) {
  if (!friendUserId) return;
  if (!(await chatConfirm("確定封鎖這位使用者？封鎖後對方不能再與你私訊、邀請遊戲或送出好友申請。", {
    title: "封鎖使用者",
    confirmLabel: "封鎖",
    danger: true,
  }))) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/chat/friends/${encodeURIComponent(friendUserId)}/block`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  setChatMsg("chat-room-warn", json.msg || "封鎖已更新", !!json.ok);
  if (json.ok) await loadChatFriends();
}

async function unblockChatFriend(friendUserId) {
  if (!friendUserId) return;
  if (!(await chatConfirm("確定解除封鎖？解除後不會自動恢復好友關係。", {
    title: "解除封鎖",
    confirmLabel: "解除封鎖",
  }))) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/chat/friends/${encodeURIComponent(friendUserId)}/block`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  setChatMsg("chat-room-warn", json.msg || "封鎖已更新", !!json.ok);
  if (json.ok) await loadChatFriends();
}

async function openChatRoom(roomId, autoPoll = true) {
  const id = Number(roomId);
  if (!Number.isFinite(id) || id <= 0) return;
  const target = chatRooms.find((r) => Number(r.id) === id);
  if (!target) return;
  selectedChatRoomId = id;
  renderChatRooms();
  const roomTitle = $("chat-room-title");
  if (roomTitle) {
    const lock = target.is_private ? "🔒 " : "";
    roomTitle.textContent = `${lock}${target.name}（#${target.id}）`;
  }
  const member = $("chat-room-member");
  if (member) {
    const passwordState = target.join_password_required ? " · 需密碼" : "";
    const memberCount = target.hide_member_count ? "" : (target.member_count ? ` · ${target.member_count} 人` : "");
    const anonymousState = target.allow_anonymous && !target.is_private ? " · 可匿名" : "";
    member.textContent = `持有者：${target.owner_username || "未知"}${memberCount}${anonymousState}${passwordState}`;
  }
  chatMessageLatestIdByRoom[String(id)] = 0;
  chatMessagePollTicksByRoom[String(id)] = 0;
  await loadChatMessages(id, false, { full: true });
  if (typeof loadContextAttachments === "function") {
    const refs = await loadContextAttachments("group_chat", id, "chat-attachment-list");
    syncChatSharedAttachmentPanel(refs);
  }
  if (autoPoll) startChatPoll();
  const msgInput = $("chat-message-input");
  if (msgInput) {
    try {
      msgInput.focus({ preventScroll: true });
    } catch (_) {
      msgInput.focus();
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    }
  }
}

async function loadChatMessages(roomId, silent = false, options = {}) {
  if (!roomId) return;
  const roomKey = String(roomId);
  chatMessagePollTicksByRoom[roomKey] = (chatMessagePollTicksByRoom[roomKey] || 0) + 1;
  const forceFull = !!options.full || (silent && chatMessagePollTicksByRoom[roomKey] % 12 === 0);
  const lastSeenId = Number(chatMessageLatestIdByRoom[roomKey] || 0);
  const deltaMode = silent && !forceFull && lastSeenId > 0;
  const csrf = await fetchCsrfToken();
  try {
    const url = new URL(API + `/chat/rooms/${roomId}/messages`, window.location.origin);
    url.searchParams.set("limit", deltaMode ? "100" : "50");
    if (deltaMode) url.searchParams.set("after_id", String(lastSeenId));
    if (deltaMode) url.searchParams.set("compact", "1");
    const res = await apiFetch(url.toString(), {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json();
    if (Number(selectedChatRoomId) !== Number(roomId)) return;
    if (!json.ok) {
      if (!silent) {
        setChatMsg("chat-room-warn", json.msg || "讀取訊息失敗", false);
      }
      return;
    }
    if (json.room && json.room.id === roomId) {
      const title = $("chat-room-title");
      if (title) {
        const lock = json.room.is_private ? "🔒 " : "";
        title.textContent = `${lock}${json.room.name}（#${json.room.id}）`;
      }
      const member = $("chat-room-member");
      if (member) {
        const passwordState = json.room.join_password_required ? " · 需密碼" : "";
        const memberCount = json.room.hide_member_count ? "" : (json.room.member_count ? ` · ${json.room.member_count} 人` : "");
        const anonymousState = json.room.allow_anonymous && !json.room.is_private ? " · 可匿名" : "";
        member.textContent = `${memberCount.replace(/^ · /, "")}${anonymousState}${passwordState}`;
      }
    }
    const incoming = Array.isArray(json.messages) ? json.messages : [];
    if (deltaMode && !incoming.length) {
      const warn = $("chat-room-warn");
      if (warn) warn.className = "msg";
      return;
    }
    const nextMessages = deltaMode ? chatMessageCache.concat(incoming).slice(-200) : incoming;
    renderChatMessages(nextMessages);
    const latest = Number(json.cursor?.latest_id || Math.max(0, ...nextMessages.map((item) => Number(item.id || 0))));
    chatMessageLatestIdByRoom[roomKey] = latest || 0;
    const warn = $("chat-room-warn");
    if (warn) warn.className = "msg";
  } catch (e) {
    if (!silent) {
      setChatMsg("chat-room-warn", "讀取訊息失敗", false);
    }
  }
}

async function createChatRoom() {
  const name = ($("chat-room-name")?.value || "").trim();
  const targetUser = ($("chat-room-target-user")?.value || "").trim();
  const inviteUsernames = ($("chat-room-invite-users")?.value || "").trim();
  const joinPassword = ($("chat-room-password")?.value || "");
  const allowAnonymous = !!$("chat-room-allow-anonymous")?.checked && !targetUser;
  const anonymous = allowAnonymous && !!$("chat-room-use-anonymous")?.checked;

  if (!name && !targetUser) {
    setChatMsg("chat-room-warn", "請輸入聊天室名稱或指定對象", false);
    return;
  }
  if (targetUser && targetUser === currentUser) {
    setChatMsg("chat-room-warn", "不能指定自己為對象", false);
    return;
  }

  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/chat/rooms", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({
      name: name || null,
      target_user: targetUser || null,
      invite_usernames: inviteUsernames || null,
      join_password: joinPassword || null,
      allow_anonymous: allowAnonymous,
      anonymous
    })
  });
  const raw = await res.text().catch(() => "");
  const json = (() => {
    try { return raw ? JSON.parse(raw) : {}; } catch (_) { return {}; }
  })();
  if (res.ok && json && json.ok) {
    $("chat-room-name").value = "";
    if ($("chat-room-target-user")) $("chat-room-target-user").value = "";
    if ($("chat-room-invite-users")) $("chat-room-invite-users").value = "";
    if ($("chat-room-password")) $("chat-room-password").value = "";
    if ($("chat-room-allow-anonymous")) $("chat-room-allow-anonymous").checked = false;
    if ($("chat-room-use-anonymous")) $("chat-room-use-anonymous").checked = false;
    setChatCreatePanelVisible(false);
    await loadChatRooms();
    if (json.room && json.room.id) {
      await openChatRoom(json.room.id, true);
      const inviteInfo = json.room.target_username ? `（與 ${sanitize(json.room.target_username)} 的私訊）` : "";
      const forbidden = Array.isArray(json.forbidden) && json.forbidden.length ? `；未加入：${json.forbidden.map((name) => sanitize(name)).join(", ")}（僅限好友）` : "";
      const roomType = json.room.is_private ? "私人訊息聊天室" : "聊天室";
      setChatMsg("chat-room-warn", `${roomType}建立完成${inviteInfo}${forbidden}`, true);
    }
  } else {
    const fallback = (raw || "").split("\n")[0].trim();
    setChatMsg("chat-room-warn", `${res.ok ? "建立聊天室失敗" : "建立聊天室失敗（" + res.status + "）"} ${json.msg || fallback || "請稍後再試"}`, false);
  }
}

async function joinChatRoom() {
  const roomId = Number(($("chat-join-room-id")?.value || "").trim());
  const password = ($("chat-join-password")?.value || "");
  const anonymous = !!$("chat-join-anonymous")?.checked;
  if (!Number.isFinite(roomId) || roomId <= 0) {
    setChatMsg("chat-room-warn", "請輸入有效的聊天室 ID", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/chat/rooms/" + roomId + "/join", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ password, anonymous })
  });
  const raw = await res.text().catch(() => "");
  const json = (() => {
    try { return raw ? JSON.parse(raw) : {}; } catch (_) { return {}; }
  })();
  if (res.ok && json && json.ok) {
    if ($("chat-join-room-id")) $("chat-join-room-id").value = "";
    if ($("chat-join-password")) $("chat-join-password").value = "";
    if ($("chat-join-anonymous")) $("chat-join-anonymous").checked = false;
    setChatJoinPanelVisible(false);
    const roomExists = chatRooms.find((r) => r.id === roomId);
    await loadChatRooms();
    if (roomExists) {
      await openChatRoom(roomId, true);
    } else if (json.room && json.room.id) {
      await openChatRoom(json.room.id, true);
    }
    setChatMsg("chat-room-warn", "已加入聊天室", true);
  } else {
    const fallback = (raw || "").split("\n")[0].trim();
    setChatMsg("chat-room-warn", `${res.ok ? "加入聊天室失敗" : "加入聊天室失敗（" + res.status + "）"} ${json.msg || fallback || "請稍後再試"}`, false);
  }
}

async function inviteChatRoomMembers() {
  if (!selectedChatRoomId) {
    setChatMsg("chat-room-warn", "請先選擇聊天室", false);
    return;
  }
  const input = $("chat-room-invite-more-users");
  const usernames = (input?.value || "").trim();
  if (!usernames) {
    setChatMsg("chat-room-warn", "請輸入要邀請的帳號", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/chat/rooms/${selectedChatRoomId}/invites`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ usernames })
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) {
    if (input) input.value = "";
    const invited = Array.isArray(json.invited) ? json.invited.join(", ") : "";
    const missing = Array.isArray(json.missing) && json.missing.length ? `；找不到：${json.missing.join(", ")}` : "";
    const forbidden = Array.isArray(json.forbidden) && json.forbidden.length ? `；非好友不可邀請：${json.forbidden.join(", ")}` : "";
    setChatMsg("chat-room-warn", `${json.msg || "邀請已送出"}${invited ? `：${invited}` : ""}${missing}${forbidden}`, !forbidden);
    return;
  }
  setChatMsg("chat-room-warn", json.msg || "邀請失敗", false);
}

function exportChatRoom() {
  if (!selectedChatRoomId) {
    setChatMsg("chat-room-warn", "請先選擇聊天室", false);
    return;
  }
  window.location.href = API + `/chat/rooms/${encodeURIComponent(selectedChatRoomId)}/export`;
}

async function sendChatMessage() {
  if (!selectedChatRoomId) {
    setChatMsg("chat-room-warn", "請先選擇聊天室", false);
    return;
  }
  const input = $("chat-message-input");
  const content = (input?.value || "").trim();
  const attachmentFileIds = pendingChatAttachments.map((item) => item.file_id).filter(Boolean);
  if (!content && !attachmentFileIds.length) {
    setChatMsg("chat-room-warn", "訊息或附件不可為空", false);
    return;
  }
  if (content.length > 500) {
    setChatMsg("chat-room-warn", "訊息過長，請少於 500 字", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/chat/rooms/${selectedChatRoomId}/messages`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrf || ""
    },
    body: JSON.stringify({ content, attachment_file_ids: attachmentFileIds, csrf_token: csrf || "" })
  });
  const json = await res.json().catch(() => ({}));
  if (json && json.ok) {
    if (input) input.value = "";
    pendingChatAttachments = [];
    renderPendingChatAttachments();
    setChatMsg("chat-room-warn", "訊息已送出", true);
    await loadChatMessages(selectedChatRoomId, true);
    startChatPoll();
    return;
  }

  const reason = json.reason ? ` [${json.reason}]` : "";
  const suffix = json.violation_count ? `（違規計次：${json.violation_count}）` : "";
  const message = `${json.msg || "發送失敗"}${reason}${suffix}`;
  setChatMsg("chat-room-warn", message, false);
  if (json.warned || json.reason || json.violation_count) {
    if (typeof showAppToast === "function") showAppToast(message, false, { duration: 5200 });
  }
}

async function sendChatSticker(stickerKey) {
  if (!selectedChatRoomId) {
    setChatMsg("chat-room-warn", "請先選擇聊天室", false);
    return;
  }
  if (!CHAT_STICKER_LABELS[stickerKey]) {
    setChatMsg("chat-room-warn", "不支援的表情包", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/chat/rooms/${selectedChatRoomId}/messages`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ message_type: "sticker", sticker_key: stickerKey, csrf_token: csrf || "" })
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) {
    setChatMsg("chat-room-warn", "表情包已送出", true);
    await loadChatMessages(selectedChatRoomId, true);
    startChatPoll();
    return;
  }
  setChatMsg("chat-room-warn", json.msg || "表情包發送失敗", false);
}

function insertChatSticker(stickerKey) {
  const glyph = CHAT_STICKER_LABELS[stickerKey];
  if (!glyph) {
    setChatMsg("chat-room-warn", "不支援的表情符號", false);
    return;
  }
  const input = $("chat-message-input");
  if (!input) return;
  const current = input.value || "";
  const start = Number.isFinite(input.selectionStart) ? input.selectionStart : current.length;
  const end = Number.isFinite(input.selectionEnd) ? input.selectionEnd : start;
  const next = `${current.slice(0, start)}${glyph}${current.slice(end)}`;
  if (next.length > Number(input.maxLength || 500)) {
    setChatMsg("chat-room-warn", "訊息過長，無法再加入表情符號", false);
    input.focus();
    return;
  }
  input.value = next;
  const cursor = start + glyph.length;
  input.setSelectionRange?.(cursor, cursor);
  input.focus();
  setChatMsg("chat-room-warn", "已插入表情符號，編輯完成後再送出", true);
}

async function openPmWithUser(targetUsername) {
  const target = String(targetUsername || "").trim();
  if (!target) return;
  if (target === currentUser) {
    flash($("admin-users-msg") || $("dm-warn") || $("chat-room-warn"), "不能私訊自己", false);
    return;
  }

  if (!canAccessModule("chat")) {
    flash($("admin-users-msg") || $("chat-room-warn"), "聊天功能目前未啟用，請先由 root 在安全中心開啟。", false);
    return;
  }

  switchModuleTab("chat");
  setChatCreatePanelVisible(true);
  const targetInput = $("chat-room-target-user");
  const nameInput = $("chat-room-name");
  if (targetInput) targetInput.value = target;
  if (nameInput) nameInput.value = "";
  await createChatRoom();
}

async function reportChatMessage(messageId) {
  if (!messageId) return;
  const reason = await chatPrompt("請輸入檢舉原因（200 字內）。", {
    title: "檢舉訊息",
    inputLabel: "檢舉原因",
    defaultValue: "違規留言",
    maxLength: 200,
    required: true,
    confirmLabel: "送出檢舉",
  });
  if (reason === null) return;
  const cleanReason = reason.trim();
  if (!cleanReason) {
    setChatMsg("chat-room-warn", "請填寫檢舉原因", false);
    return;
  }
  if (cleanReason.length > 200) {
    setChatMsg("chat-room-warn", "檢舉原因請控制在 200 字以內", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/chat/messages/${messageId}/report`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ reason: cleanReason })
  });
  const json = await res.json().catch(() => ({}));
  setChatMsg("chat-room-warn", json.msg || (json.ok ? "檢舉已送出" : "檢舉失敗"), !!json.ok);
}

function canDeleteChatMessage(message) {
  if (!message || !message.id) return false;
  if (Object.prototype.hasOwnProperty.call(message, "can_delete")) return !!message.can_delete;
  if (message.is_self || String(message.sender || "") === String(currentUser || "")) return true;
  const currentRoom = chatRooms.find((room) => Number(room.id) === Number(selectedChatRoomId));
  if (currentRoom && Number(currentRoom.owner_user_id) === Number(currentUserId)) return true;
  return clientRoleRank(currentRole || "user") >= clientRoleRank("manager");
}

function canDeleteChatRoom(room) {
  if (!room || !room.id) return false;
  if (room.is_official || String(room.name || "") === "官方聊天室") return false;
  if (String(currentUser || "") === "root") return true;
  if (Number(room.owner_user_id) === Number(currentUserId)) return true;
  if (String(room.owner_username || "") === "root") return false;
  return clientRoleRank(currentRole || "user") >= clientRoleRank("manager");
}

async function deleteChatRoom(roomId) {
  if (!roomId) return;
  const room = chatRooms.find((item) => Number(item.id) === Number(roomId));
  const label = room ? `${room.name}（#${room.id}）` : `#${roomId}`;
  if (!(await chatConfirm(`確定要刪除此聊天室 ${label}？歷史資料會保留在資料庫與審計紀錄中。`, {
    title: "刪除聊天室",
    confirmLabel: "刪除",
    danger: true,
  }))) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/chat/rooms/${encodeURIComponent(roomId)}`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) {
    setChatMsg("chat-room-warn", json.msg || "聊天室已刪除", true);
    if (Number(selectedChatRoomId) === Number(roomId)) {
      selectedChatRoomId = null;
      stopChatPoll();
      const memberLabel = $("chat-room-member");
      if (memberLabel) memberLabel.textContent = "";
      const title = $("chat-room-title");
      if (title) title.textContent = "請先建立或加入聊天室";
      const msgs = $("chat-room-messages");
      if (msgs) msgs.innerHTML = "<p style=\"color:var(--muted);\">尚未選擇聊天室</p>";
      const attachments = $("chat-attachment-list");
      if (attachments) attachments.innerHTML = "";
      syncChatSharedAttachmentPanel([]);
    }
    await loadChatRooms();
    return;
  }
  setChatMsg("chat-room-warn", json.msg || "刪除聊天室失敗", false);
}

async function deleteChatMessage(messageId, recall = false) {
  if (!messageId) return;
  if (!(await chatConfirm(recall ? "確定要收回這則留言嗎？" : "確定要刪除此留言嗎？", {
    title: recall ? "收回訊息" : "刪除訊息",
    confirmLabel: recall ? "收回" : "刪除",
    danger: !recall,
  }))) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/chat/messages/${messageId}`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) {
    setChatMsg("chat-room-warn", json.msg || "訊息已刪除", true);
    if (selectedChatRoomId) await loadChatMessages(selectedChatRoomId, true, { full: true });
    return;
  }
  setChatMsg("chat-room-warn", json.msg || "刪除訊息失敗", false);
}

async function editChatMessage(messageId) {
  if (!messageId) return;
  const message = chatMessageCache.find((item) => Number(item.id) === Number(messageId));
  if (!message || !message.can_edit) {
    setChatMsg("chat-room-warn", "這則訊息目前不能編輯", false);
    return;
  }
  const currentContent = String(message.content || "");
  const nextContent = await chatPrompt("編輯訊息（送出 5 分鐘內可修改）。", {
    title: "編輯訊息",
    inputLabel: "訊息內容",
    defaultValue: currentContent,
    multiline: true,
    rows: 4,
    maxLength: 500,
    required: true,
    confirmLabel: "儲存",
  });
  if (nextContent === null) return;
  const clean = nextContent.trim();
  if (!clean) {
    setChatMsg("chat-room-warn", "訊息不可為空", false);
    return;
  }
  if (clean.length > 500) {
    setChatMsg("chat-room-warn", "訊息過長，請少於 500 字", false);
    return;
  }
  if (clean === currentContent) {
    setChatMsg("chat-room-warn", "訊息未變更", true);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/chat/messages/${messageId}`, {
    method: "PUT",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ content: clean })
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) {
    setChatMsg("chat-room-warn", json.msg || "訊息已更新", true);
    if (selectedChatRoomId) await loadChatMessages(selectedChatRoomId, true, { full: true });
    return;
  }
  setChatMsg("chat-room-warn", json.msg || "訊息編輯失敗", false);
}
