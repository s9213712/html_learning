'use strict';

const NOTIFICATION_POLL_MS = 60000;
const NOTIFICATION_INITIAL_DELAY_MS = 10000;

let notificationInitialPollTimer = null;
let notificationPollBusy = false;

function notificationPollMs() {
  if (typeof configRefreshSeconds === "function") {
    return Math.round(configRefreshSeconds("notification_poll_seconds", NOTIFICATION_POLL_MS / 1000, 5, 600) * 1000);
  }
  return NOTIFICATION_POLL_MS;
}

function setNotificationBadge(count) {
  const badge = $("notification-badge");
  if (!badge) return;
  const n = Math.max(0, parseInt(count || 0, 10));
  badge.textContent = n > 99 ? "99+" : String(n);
  badge.style.display = n > 0 ? "inline-flex" : "none";
  const readAll = $("notification-read-all");
  if (readAll) {
    readAll.disabled = n <= 0;
    readAll.textContent = n > 0 ? `全部已讀 (${n})` : "全部已讀";
  }
}

function setNotificationInlineError(message) {
  const clean = String(message || "通知操作失敗，請稍後再試。").trim();
  const list = $("notification-list");
  if (notificationsOpen && list) {
    const row = document.createElement("div");
    row.className = "notification-item notification-inline-error";
    row.setAttribute("role", "alert");
    row.textContent = clean;
    list.prepend(row);
  }
  if (typeof showAppToast === "function") showAppToast(clean, false);
}

function notificationMetadata(item = {}) {
  return item && item.metadata && typeof item.metadata === "object" ? item.metadata : {};
}

function notificationDisputeReplyAction(item = {}) {
  const metadata = notificationMetadata(item);
  if (currentUser === "root") return "";
  if (String(metadata.action || "") !== "points_chain_dispute_reply") return "";
  const disputeUuid = String(metadata.dispute_uuid || "").trim();
  if (!disputeUuid) return "";
  return `
    <div class="notification-actions-row">
      <button class="btn btn-primary" type="button"
        data-notification-dispute-reply="${sanitize(disputeUuid)}"
        data-notification-dispute-tx="${sanitize(metadata.tx_hash || "")}"
        data-notification-dispute-from="${sanitize(metadata.from_wallet_address || "")}"
        data-notification-dispute-to="${sanitize(metadata.to_wallet_address || "")}"
        data-notification-dispute-amount="${sanitize(String(metadata.claimed_amount_points || 0))}"
        data-notification-dispute-branch="${sanitize(metadata.chain_branch || "main")}">To 回覆</button>
    </div>
  `;
}

function renderNotifications(items, unreadCount) {
  setNotificationBadge(unreadCount);
  const list = $("notification-list");
  if (!list) return;
  const notifications = Array.isArray(items) ? items : [];
  if (!notifications.length) {
    list.innerHTML = "<p style='color:var(--muted);'>目前沒有通知</p>";
    return;
  }
  list.innerHTML = notifications.map((item) => {
    const cls = item.is_read ? "notification-item" : "notification-item unread";
    const readAction = item.is_read
      ? ""
      : `<button class="btn" type="button" data-notification-read="${item.id}" style="width:auto;padding:.35rem .55rem;font-size:.72rem;">已讀</button>`;
    const dismissAction = `<button class="btn" type="button" data-notification-dismiss="${item.id}" style="width:auto;padding:.35rem .55rem;font-size:.72rem;">隱藏</button>`;
    const link = item.link
      ? `<div class="notification-meta">連結：${sanitize(item.link)}</div>`
      : "";
    const disputeReplyAction = notificationDisputeReplyAction(item);
    const severity = item.severity && item.severity !== "info" ? ` · ${sanitize(item.severity)}` : "";
    return `
      <div class="${cls}">
        <div class="notification-title">
          <span>${sanitize(item.title || "通知")}</span>
          <span class="notification-actions">${readAction}${dismissAction}</span>
        </div>
        <div class="notification-body">${sanitize(item.body || "")}</div>
        <div class="notification-meta">${sanitize(formatChatTime(item.created_at || ""))} · ${sanitize(item.type || "system")}${severity}</div>
        ${link}
        ${disputeReplyAction}
      </div>
    `;
  }).join("");
  list.querySelectorAll("button[data-notification-read]").forEach((btn) => {
    btn.addEventListener("click", () => markNotificationRead(parseInt(btn.getAttribute("data-notification-read"), 10)));
  });
  list.querySelectorAll("button[data-notification-dismiss]").forEach((btn) => {
    btn.addEventListener("click", () => dismissNotification(parseInt(btn.getAttribute("data-notification-dismiss"), 10)));
  });
  list.querySelectorAll("button[data-notification-dispute-reply]").forEach((btn) => {
    btn.addEventListener("click", () => openNotificationDisputeReply(btn));
  });
}

async function openNotificationDisputeReply(btn) {
  if (!btn) return;
  if (currentUser === "root") {
    setNotificationInlineError("root 帳號不使用匿名地址疑義回覆流程；官方錢包事故請改走官方治理。");
    return;
  }
  const disputeUuid = btn.getAttribute("data-notification-dispute-reply") || "";
  if (!disputeUuid) {
    setNotificationInlineError("通知缺少疑義交易案件編號。");
    return;
  }
  try {
    if (typeof switchModuleTab === "function") switchModuleTab("economy");
    if (typeof setEconomyActivePage === "function") setEconomyActivePage("transactions");
    if (typeof loadEconomyDashboard === "function") await loadEconomyDashboard();
    if (typeof loadEconomyTransactions === "function") await loadEconomyTransactions();
    if (typeof loadEconomyTransactionDisputes === "function") await loadEconomyTransactionDisputes({ silent: true });
    if (typeof replyEconomyTransactionDispute !== "function") {
      throw new Error("疑義交易回覆模組尚未載入，請重新整理頁面後再試。");
    }
    await replyEconomyTransactionDispute({
      disputeUuid,
      txHash: btn.getAttribute("data-notification-dispute-tx") || "",
      from: btn.getAttribute("data-notification-dispute-from") || "",
      to: btn.getAttribute("data-notification-dispute-to") || "",
      amount: btn.getAttribute("data-notification-dispute-amount") || "0",
      chainBranch: btn.getAttribute("data-notification-dispute-branch") || "main",
    });
  } catch (err) {
    setNotificationInlineError(err?.message || "疑義交易回覆開啟失敗。");
  }
}

function shouldRunNotificationPoll({ force = false } = {}) {
  if (!currentUser) return false;
  if (force) return true;
  return !document.hidden;
}

function clearNotificationInitialPoll() {
  if (!notificationInitialPollTimer) return;
  clearTimeout(notificationInitialPollTimer);
  notificationInitialPollTimer = null;
}

function scheduleNotificationInitialPoll(delayMs = NOTIFICATION_INITIAL_DELAY_MS) {
  clearNotificationInitialPoll();
  notificationInitialPollTimer = setTimeout(() => {
    notificationInitialPollTimer = null;
    loadNotifications().catch((err) => reportFrontendFailure("notification-initial-poll", err));
  }, Math.max(0, delayMs));
}

async function loadNotifications(options = {}) {
  const force = Boolean(options.force);
  if (!shouldRunNotificationPoll({ force })) return;
  if (notificationPollBusy) return;
  notificationPollBusy = true;
  try {
    const csrf = await fetchCsrfToken();
    const loadList = notificationsOpen || force;
    const requestOptions = {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    };
    const res = loadList
      ? await apiFetch(API + "/notifications?limit=20", requestOptions)
      : await apiFetch(API + "/notifications/unread-count", requestOptions);
    const json = await res.json().catch(() => ({}));
    if (!json.ok) {
      reportFrontendFailure("notification-load", new Error(json.msg || `HTTP ${res.status}`));
      if (res.status === 503) setNotificationBadge(0);
      const list = $("notification-list");
      if (notificationsOpen && list) {
        list.innerHTML = `<p style="color:#ffb74d;">${sanitize(json.msg || "通知讀取失敗，請稍後重試。")}</p>`;
      }
      return;
    }
    if (!loadList) {
      setNotificationBadge(json.unread_count);
      return;
    }
    renderNotifications(json.notifications, json.unread_count);
  } catch (err) {
    reportFrontendFailure("notification-load", err);
    const list = $("notification-list");
    if (notificationsOpen && list) {
      list.innerHTML = "<p style='color:#ffb74d;'>通知讀取失敗，請稍後重試。</p>";
    }
  } finally {
    notificationPollBusy = false;
  }
}

async function markNotificationRead(notificationId) {
  if (!currentUser || !notificationId) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/notifications/${notificationId}/read`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    setNotificationInlineError(json.msg || "通知已讀更新失敗");
    return;
  }
  await loadNotifications({ force: true });
}

async function markAllNotificationsRead() {
  if (!currentUser) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/notifications/read-all", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    setNotificationInlineError(json.msg || "全部已讀失敗");
    return;
  }
  await loadNotifications({ force: true });
}

async function dismissNotification(notificationId) {
  if (!currentUser || !notificationId) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + `/notifications/${notificationId}/dismiss`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    setNotificationInlineError(json.msg || "通知隱藏失敗");
    return;
  }
  await loadNotifications({ force: true });
}

function toggleNotificationPanel() {
  const panel = $("notification-panel");
  if (!panel) return;
  notificationsOpen = !notificationsOpen;
  panel.classList.toggle("show", notificationsOpen);
  if (notificationsOpen) loadNotifications({ force: true });
}

function closeNotificationPanel() {
  notificationsOpen = false;
  const panel = $("notification-panel");
  if (panel) panel.classList.remove("show");
}

function startNotificationPoll() {
  stopNotificationPoll();
  if (!currentUser) return;
  scheduleNotificationInitialPoll();
  notificationPollTimer = setInterval(() => {
    loadNotifications().catch((err) => reportFrontendFailure("notification-poll", err));
  }, notificationPollMs());
}

function stopNotificationPoll() {
  clearNotificationInitialPoll();
  if (notificationPollTimer) {
    clearInterval(notificationPollTimer);
    notificationPollTimer = null;
  }
  closeNotificationPanel();
  setNotificationBadge(0);
}

function restartNotificationPoll() {
  clearNotificationInitialPoll();
  if (notificationPollTimer) {
    clearInterval(notificationPollTimer);
    notificationPollTimer = null;
  }
  if (!currentUser) return;
  scheduleNotificationInitialPoll();
  notificationPollTimer = setInterval(() => {
    loadNotifications().catch((err) => reportFrontendFailure("notification-restart-poll", err));
  }, notificationPollMs());
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden || !currentUser || !notificationPollTimer) return;
  scheduleNotificationInitialPoll(1500);
});

function resetNotificationAccountState() {
  stopNotificationPoll();
  notificationPollBusy = false;
  const list = $("notification-list");
  if (list) list.innerHTML = "<p style='color:var(--muted);'>尚未載入通知</p>";
}

document.addEventListener("hackme:account-context-changed", resetNotificationAccountState);
