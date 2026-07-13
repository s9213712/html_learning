let adminUserActionMenusBound = false;

function closeAdminUserActionMenus(except = null) {
  document.querySelectorAll(".admin-user-actions.open").forEach((wrap) => {
    if (wrap === except) return;
    wrap.classList.remove("open");
    const toggle = wrap.querySelector(".admin-user-action-toggle");
    if (toggle) toggle.setAttribute("aria-expanded", "false");
    const menu = wrap.querySelector(".admin-user-action-menu");
    if (menu) menu.setAttribute("aria-hidden", "true");
  });
}

function ensureAdminUserActionMenuListeners() {
  if (adminUserActionMenusBound) return;
  adminUserActionMenusBound = true;
  document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    if (!event.target.closest(".admin-user-actions")) closeAdminUserActionMenus();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeAdminUserActionMenus();
  });
}

function buildAdminUserActionMenu(actionButtons, user) {
  const actions = document.createElement("div");
  actions.className = "action admin-user-actions";
  if (!actionButtons.length) return actions;

  ensureAdminUserActionMenuListeners();
  const menuId = `admin-user-action-menu-${user?.id || "unknown"}`;
  const toggle = document.createElement("button");
  toggle.className = "btn btn-primary admin-user-action-toggle";
  toggle.type = "button";
  toggle.textContent = "操作";
  toggle.setAttribute("aria-label", `${user?.username || "用戶"} 的可用操作`);
  toggle.setAttribute("aria-haspopup", "menu");
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-controls", menuId);

  const menu = document.createElement("div");
  menu.className = "admin-user-action-menu";
  menu.id = menuId;
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-hidden", "true");
  menu.addEventListener("click", (event) => event.stopPropagation());
  actionButtons.forEach((btn) => menu.appendChild(btn));

  toggle.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    const willOpen = !actions.classList.contains("open");
    closeAdminUserActionMenus(actions);
    actions.classList.toggle("open", willOpen);
    toggle.setAttribute("aria-expanded", String(willOpen));
    menu.setAttribute("aria-hidden", String(!willOpen));
  });

  actions.appendChild(toggle);
  actions.appendChild(menu);
  return actions;
}

function renderUsers() {
  const tbody = $("user-table")?.querySelector("tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  const allowedPendingIds = new Set();
  for (const u of users) {
    const blocked = u.blocked_until && new Date(u.blocked_until) > new Date();
    const isBlocked = blocked;
    const isSelf = String(u.username || "") === String(currentUser || "");
    const canReviewPending = (currentRole === "manager" || currentRole === "super_admin") && u.status === "pending" && !isSelf;
    if (canReviewPending) {
      allowedPendingIds.add(String(u.id));
    }
    const actionButtons = [];
    if (canReviewPending) {
      const approveBtn = document.createElement("button");
      approveBtn.className = "btn btn-primary";
      approveBtn.type = "button";
      approveBtn.textContent = "核准";
      approveBtn.addEventListener("click", () => reviewRegistration(u.id, "approve"));
      actionButtons.push(approveBtn);

      const rejectBtn = document.createElement("button");
      rejectBtn.className = "btn";
      rejectBtn.type = "button";
      rejectBtn.textContent = "駁回";
      rejectBtn.style.color = "#ff8a80";
      rejectBtn.addEventListener("click", () => reviewRegistration(u.id, "reject"));
      actionButtons.push(rejectBtn);
    }
    if (currentRole === "manager" || currentRole === "super_admin") {
      if (u.role !== "super_admin" && !isSelf) {
        const blockBtn = document.createElement("button");
        blockBtn.className = "btn btn-primary";
        blockBtn.type = "button";
        blockBtn.textContent = isBlocked ? "解除封鎖" : "封鎖";
        blockBtn.dataset.userId = String(u.id);
        blockBtn.addEventListener("click", () => toggleBlock(u.id, isBlocked));
        actionButtons.push(blockBtn);
      }
    }
    if ((currentRole === "manager" || currentRole === "super_admin") && u.role === "user" && !isSelf) {
      const levelWrap = document.createElement("span");
      levelWrap.style.display = "inline-flex";
      levelWrap.style.gap = ".25rem";
      levelWrap.style.alignItems = "center";
      const levelSelect = document.createElement("select");
      levelSelect.id = `member-level-select-${u.id}`;
      levelSelect.setAttribute("aria-label", `${u.username || "用戶"} 的會員等級`);
      levelSelect.style.maxWidth = "105px";
      const levelOptions = currentRole === "super_admin"
        ? ["newbie", "normal", "trusted", "vip", "restricted", "suspended"]
        : ["newbie", "normal", "trusted", "vip"];
      levelOptions.forEach((level) => {
        const opt = document.createElement("option");
        opt.value = level;
        opt.textContent = level;
        if ((u.effective_level || u.base_level || u.member_level || "normal") === level) opt.selected = true;
        levelSelect.appendChild(opt);
      });
      const levelBtn = document.createElement("button");
      levelBtn.className = "btn";
      levelBtn.type = "button";
      levelBtn.textContent = "套用等級";
      levelBtn.style.color = "#82b1ff";
      levelBtn.addEventListener("click", () => updateUserMemberLevel(u.id, u.username));
      levelWrap.appendChild(levelSelect);
      levelWrap.appendChild(levelBtn);
      actionButtons.push(levelWrap);
    }
    // Promote button (super_admin only: user -> manager)
    if (currentRole === "super_admin" && u.role === "user" && !isSelf) {
      const promoteBtn = document.createElement("button");
      promoteBtn.className = "btn";
      promoteBtn.type = "button";
      promoteBtn.textContent = "升級";
      promoteBtn.title = "升級為管理者";
      promoteBtn.style.color = "#82b1ff";
      promoteBtn.addEventListener("click", () => promoteUser(u.id, u.username));
      actionButtons.push(promoteBtn);
    }
    // Demote button (super_admin only: manager→user, user→delete)
    if (currentRole === "super_admin" && u.role === "manager" && !isSelf) {
      const demBtn = document.createElement("button");
      demBtn.className = "btn";
      demBtn.type = "button";
      demBtn.textContent = "⬇ 降級";
      demBtn.style.color = "#ff8a80";
      demBtn.addEventListener("click", () => demoteUser(u.id, u.username, u.role));
      actionButtons.push(demBtn);
    }
    // Violation controls (manager/super_admin)
    if ((currentRole === "manager" || currentRole === "super_admin") && u.role !== "super_admin" && !isSelf) {
      const violCount = u.violation_count || 0;
      const violBtn = document.createElement("button");
      violBtn.className = "btn";
      violBtn.type = "button";
      violBtn.textContent = `⚠ ${violCount}`;
      violBtn.style.color = violCount > 0 ? "#ff4f6d" : "#888";
      violBtn.addEventListener("click", () => addViolation(u.id));
      actionButtons.push(violBtn);
      if (currentRole === "super_admin") {
        const detailBtn = document.createElement("button");
        detailBtn.className = "btn";
        detailBtn.type = "button";
        detailBtn.textContent = "明細";
        detailBtn.title = "查看違規原因";
        detailBtn.style.color = "#82b1ff";
        detailBtn.addEventListener("click", () => {
          switchAdminTab("violations");
          loadViolations(0, u.username);
        });
        actionButtons.push(detailBtn);
      }
      const governanceBtn = document.createElement("button");
      governanceBtn.className = "btn";
      governanceBtn.type = "button";
      governanceBtn.textContent = "治理";
      governanceBtn.title = "建立治理提案";
      governanceBtn.style.color = "#ffb74d";
      governanceBtn.addEventListener("click", () => openGovernanceProposalForUser(u.id, u.username));
      actionButtons.push(governanceBtn);
      if (violCount > 0) {
        const resetBtn = document.createElement("button");
        resetBtn.className = "btn";
        resetBtn.type = "button";
        resetBtn.textContent = "↺";
        resetBtn.title = "歸零違規";
        resetBtn.style.color = "#4caf50";
        resetBtn.addEventListener("click", () => resetViolations(u.id));
        actionButtons.push(resetBtn);
      }
    }
    if (canManageUsers || isSelf) {
      const editBtn = document.createElement("button");
      editBtn.className = "btn btn-primary";
      editBtn.type = "button";
      editBtn.textContent = isSelf ? "我的資料" : "修改";
      editBtn.addEventListener("click", () => editUser(u.id));
      editBtn.classList.add("action-edit-user");
      actionButtons.push(editBtn);
    }
    const canAdministrativePm = currentRole === "manager" || currentRole === "super_admin";
    // Normal users can PM accepted friends; managers/root may PM from account management for governance work.
    if (!isSelf && (u.is_friend || canAdministrativePm)) {
      const pmBtn = document.createElement("button");
      pmBtn.className = "btn";
      pmBtn.type = "button";
      pmBtn.textContent = "💬 私訊";
      pmBtn.style.color = "#82b1ff";
      pmBtn.title = `傳送私人訊息給 ${u.username}`;
      pmBtn.addEventListener("click", () => openPmWithUser(u.username));
      actionButtons.push(pmBtn);
    }
    if (canManageUsers && !isSelf) {
      const delBtn = document.createElement("button");
      delBtn.className = "btn btn-danger";
      delBtn.type = "button";
      delBtn.textContent = "刪除";
      delBtn.addEventListener("click", () => removeUser(u.id));
      delBtn.classList.add("action-remove-user");
      actionButtons.push(delBtn);
    }
    const tr = document.createElement("tr");
    if (isBlocked) tr.style.opacity = "0.5";
    const appendTextCell = (value, label) => {
      const td = document.createElement("td");
      td.dataset.label = label;
      td.textContent = value == null ? "" : String(value);
      tr.appendChild(td);
      return td;
    };
    const selectCell = document.createElement("td");
    selectCell.dataset.label = "選取";
    if (canReviewPending) {
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = selectedPendingUserIds.has(String(u.id));
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) selectedPendingUserIds.add(String(u.id));
        else selectedPendingUserIds.delete(String(u.id));
        updatePendingSelectionUi();
      });
      selectCell.appendChild(checkbox);
    } else {
      selectCell.textContent = "—";
      selectCell.style.color = "var(--muted)";
    }
    const actions = buildAdminUserActionMenu(actionButtons, u);
    appendTextCell(u.id, "ID");
    const onlineCell = document.createElement("td");
    onlineCell.dataset.label = "在線";
    const onlineDot = document.createElement("span");
    onlineDot.className = `online-dot ${u.is_online ? "online" : "offline"}`;
    onlineDot.title = u.is_online
      ? `在線 · ${u.active_session_count || 1} 個 session`
      : (u.online_last_seen ? `離線 · 最後活動 ${u.online_last_seen}` : "離線");
    onlineCell.appendChild(onlineDot);
    tr.appendChild(onlineCell);
    const usernameCell = document.createElement("td");
    usernameCell.dataset.label = "帳號";
    usernameCell.innerHTML = userIdentityMarkup(u.id, u.username || "", u.nickname || "", "user-table-identity", u.avatar_file_id || "");
    const relationBadges = [];
    if (u.is_friend) relationBadges.push('<span class="profile-official-badge">好友</span>');
    if (u.is_official) relationBadges.push('<span class="profile-official-badge">官方/管理者</span>');
    if (relationBadges.length) usernameCell.insertAdjacentHTML("beforeend", `<div class="user-target-badges">${relationBadges.join("")}</div>`);
    if ((currentRole === "manager" || currentRole === "super_admin") && u.username !== "root" && u.official_hot_wallet_address) {
      const walletLine = document.createElement("div");
      walletLine.className = "user-official-hot-wallet-line";
      walletLine.style.fontFamily = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
      walletLine.style.fontSize = ".72rem";
      walletLine.style.color = "var(--muted)";
      walletLine.style.marginTop = ".2rem";
      walletLine.style.wordBreak = "break-all";
      const hotBalance = Number(u.official_hot_wallet_balance || 0);
      const hotFrozen = Number(u.official_hot_wallet_frozen || 0);
      const hotPending = Number(u.official_hot_wallet_pending_outgoing || 0);
      const hotDeposit = String(u.official_hot_wallet_deposit_address || "").trim();
      const hotParts = [`站內託管錢包：${u.official_hot_wallet_address}`, `餘額 ${hotBalance} 點`];
      if (hotFrozen) hotParts.push(`凍結 ${hotFrozen} 點`);
      if (hotPending) hotParts.push(`待出金 ${hotPending} 點`);
      if (hotDeposit) hotParts.push(`橋接 pc1：${hotDeposit}`);
      walletLine.textContent = hotParts.join(" · ");
      usernameCell.appendChild(walletLine);
    }
    tr.appendChild(usernameCell);
    appendTextCell(u.nickname || "", "暱稱");
    appendTextCell(u.real_name || "", "真實姓名");
    appendTextCell(u.role_label || u.role || "", "角色");
    appendTextCell(u.member_level_label || `${u.effective_level || u.member_level || "-"}${u.base_level && u.base_level !== u.effective_level ? ` (${u.base_level})` : ""}`, "會員等級");
    const statusCell = document.createElement("td");
    statusCell.dataset.label = "狀態";
    const statusSpan = document.createElement("span");
    statusSpan.textContent = "正常";
    statusSpan.style.color = "#4caf50";
    if (u.status === "pending") {
      statusSpan.textContent = "待審核";
      statusSpan.style.color = "#ffb74d";
    } else if (u.status === "deleted") {
      statusSpan.textContent = "已刪除";
      statusSpan.style.color = "#9e9e9e";
    } else if (u.status === "rejected") {
      statusSpan.textContent = "已駁回";
      statusSpan.style.color = "#ff4f6d";
    } else if (u.status === "inactive") {
      statusSpan.textContent = "停用";
      statusSpan.style.color = "#9e9e9e";
    }
    if (isBlocked) {
      statusSpan.textContent = "封鎖中";
      statusSpan.style.color = "#ff4f6d";
    }
    statusCell.appendChild(statusSpan);
    tr.appendChild(statusCell);
    const violationCell = document.createElement("td");
    violationCell.dataset.label = "違規";
    const violationCount = u.violation_count || 0;
    if (violationCount > 0) {
      const violationSpan = document.createElement("span");
      violationSpan.textContent = String(violationCount);
      violationSpan.style.color = "#ff4f6d";
      violationSpan.style.fontWeight = "bold";
      violationCell.appendChild(violationSpan);
    } else {
      violationCell.textContent = "0";
    }
    tr.appendChild(violationCell);
    tr.insertBefore(selectCell, tr.firstChild);
    const actionCell = document.createElement("td");
    actionCell.dataset.label = "行為";
    actionCell.appendChild(actions);
    tr.appendChild(actionCell);
    tbody.appendChild(tr);
  }
  bindAvatarFallbacks(tbody);
  selectedPendingUserIds = new Set([...selectedPendingUserIds].filter((id) => allowedPendingIds.has(id)));
  updatePendingSelectionUi();
  renderAdminUsersPagination();
  // Role quota info
  const managerCount = Number(adminUsersRoleCounts.manager ?? users.filter(u => u.role === "manager").length);
  const superAdminCount = Number(adminUsersRoleCounts.super_admin ?? users.filter(u => u.role === "super_admin").length);
  const infoEl = $("role-limit-info");
  if (infoEl) {
    infoEl.textContent = `管理者 ${managerCount}/5 · 超級管理者 ${superAdminCount}/1`;
    infoEl.style.color = managerCount >= 5 ? "#ff4f6d" : "#888";
  }
}

function adminUsersMsgEl() {
  return $("admin-users-msg") || $("li-msg");
}

function updatePendingSelectionUi() {
  const count = selectedPendingUserIds.size;
  const info = $("pending-selection-info");
  if (info) info.textContent = `已選 ${count} 筆待審核`;
  const approveBtn = $("admin-bulk-approve");
  const rejectBtn = $("admin-bulk-reject");
  if (approveBtn) approveBtn.disabled = count === 0;
  if (rejectBtn) rejectBtn.disabled = count === 0;
}

function renderAdminUsersPagination() {
  const info = $("admin-users-page-info");
  const prev = $("admin-users-prev");
  const next = $("admin-users-next");
  const size = $("admin-users-page-size");
  const page = Number(adminUsersPagination?.page || adminUsersPage || 1);
  const totalPages = Math.max(1, Number(adminUsersPagination?.total_pages || 1));
  const total = Number(adminUsersPagination?.total || users.length || 0);
  const pageSize = Number(adminUsersPagination?.page_size || adminUsersPageSize || 25);
  if (info) {
    const query = String(adminUsersPagination?.q || "").trim();
    info.textContent = `第 ${page} / ${totalPages} 頁 · ${total} 筆 · 每頁 ${pageSize} · ID 由小到大${query ? ` · 搜尋：${query}` : ""}`;
  }
  if (prev) prev.disabled = page <= 1;
  if (next) next.disabled = page >= totalPages;
  if (size && String(size.value || "") !== String(pageSize)) size.value = String(pageSize);
}

async function loadUsers(page = adminUsersPage) {
  if (!currentUser) return;
  if (!["manager","super_admin"].includes(currentRole)) return;
  const csrf = await fetchCsrfToken();
  try {
    const requestedPage = Math.max(1, Number(page || 1));
    const sizeEl = $("admin-users-page-size");
    const requestedSize = Math.max(1, Math.min(100, Number(sizeEl?.value || adminUsersPageSize || 25)));
    adminUsersPage = requestedPage;
    adminUsersPageSize = requestedSize;
    const query = String($("admin-user-search")?.value || "").trim();
    const params = new URLSearchParams({
      page: String(requestedPage),
      page_size: String(requestedSize),
      sort: "id",
      order: "asc",
    });
    if (query) params.set("q", query);
    const res = await apiFetch(API + "/admin/users?" + params.toString(), {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json();
    if (!json.ok) {
      flash(adminUsersMsgEl(), json.msg || "會員清單讀取失敗", false);
      return;
    }
    users = Array.isArray(json.users) ? json.users : [];
    adminUsersPagination = json.pagination || {
      page: requestedPage,
      page_size: requestedSize,
      total: users.length,
      total_pages: 1,
      sort: "id",
      order: "asc",
      q: query,
    };
    adminUsersPage = Number(adminUsersPagination.page || requestedPage);
    adminUsersPageSize = Number(adminUsersPagination.page_size || requestedSize);
    adminUsersRoleCounts = json.role_counts || {};
    canManageUsers = !!json.can_manage;
    renderUsers();
    if (typeof renderAdminNoticeTargetOptions === "function") renderAdminNoticeTargetOptions();
  } catch (err) {
    flash(adminUsersMsgEl(), err.message || "會員清單讀取失敗", false);
  }
}

function resetAdminUsersAccountState() {
  closeAdminUserActionMenus();
  users = [];
  adminUsersPage = 1;
  adminUsersPagination = { page: 1, page_size: adminUsersPageSize, total: 0, total_pages: 1, sort: "id", order: "asc", q: "" };
  adminUsersRoleCounts = {};
  selectedPendingUserIds.clear();
  const tbody = $("user-table")?.querySelector("tbody");
  if (tbody) tbody.replaceChildren();
  const search = $("admin-user-search");
  if (search) search.value = "";
  const info = $("admin-users-page-info");
  if (info) info.textContent = "";
  const msg = adminUsersMsgEl();
  if (msg) {
    msg.textContent = "";
    msg.className = "msg";
  }
}

document.addEventListener("hackme:account-context-changed", resetAdminUsersAccountState);
