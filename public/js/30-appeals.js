function appealCountdownText(totalSeconds) {
  const h = Math.max(0, Math.floor(totalSeconds / 3600));
  const m = Math.max(0, Math.floor((totalSeconds % 3600) / 60));
  const s = Math.max(0, totalSeconds % 60);
  return `${h} 小時 ${m} 分 ${s} 秒`;
}

function violationFineStatusLabel(status) {
  return {
    pending: "待繳",
    overdue: "逾期",
    paid: "已繳",
    waived: "已豁免",
    cancelled: "已取消",
  }[String(status || "")] || String(status || "-");
}

function violationFineStatusColor(status) {
  return {
    pending: "#ffb74d",
    overdue: "#ff4f6d",
    paid: "#4caf50",
    waived: "#82b1ff",
    cancelled: "var(--muted)",
  }[String(status || "")] || "var(--muted)";
}

function renderUserFineAppealStatus(appeals, fineUuid) {
  const appeal = (Array.isArray(appeals) ? appeals : []).find((row) => String(row.fine_uuid || "") === String(fineUuid || ""));
  if (!appeal) return "";
  const statusText = { pending: "罰單申覆待審", approved: "罰單申覆已核准", rejected: "罰單申覆已駁回" };
  const color = appeal.status === "approved" ? "#4caf50" : appeal.status === "rejected" ? "#ff4f6d" : "#ffb74d";
  return `<div style="color:${color};font-size:.74rem;">${statusText[appeal.status] || appeal.status}${appeal.review_note ? ` · ${sanitize(appeal.review_note)}` : ""}</div>`;
}

function renderUserViolationFines(fines, fineAppeals) {
  const host = $("appeal-fines");
  if (!host) return;
  const rows = Array.isArray(fines) ? fines : [];
  if (!rows.length) {
    host.innerHTML = "";
    return;
  }
  host.innerHTML = `
    <div style="color:var(--muted);font-size:.75rem;margin:.2rem 0;">罰單與解鎖付款</div>
    ${rows.map((fine) => {
      const status = String(fine.status || "");
      const payable = !!fine.is_payable;
      const labels = Array.isArray(fine.restriction_feature_labels) ? fine.restriction_feature_labels.join("、") : "";
      const baseAmount = Number(fine.amount_points || 0);
      const interest = Number(fine.overdue_interest_points || 0);
      const dueAmount = Number(fine.amount_due_points || baseAmount + interest);
      return `
        <div style="border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:.65rem;margin-bottom:.55rem;background:rgba(255,255,255,.035);word-break:break-all;">
          <div style="display:flex;gap:.45rem;align-items:center;flex-wrap:wrap;">
            <strong>罰單 ${sanitize(String(fine.fine_uuid || "").slice(0, 18))}</strong>
            <span style="color:${violationFineStatusColor(status)};">${violationFineStatusLabel(status)}</span>
            <span>${dueAmount.toLocaleString()} 點</span>
          </div>
          ${interest > 0 ? `<div style="color:#ff8a80;font-size:.74rem;">本金 ${baseAmount.toLocaleString()} 點 · 逾期附加費 ${interest.toLocaleString()} 點</div>` : ""}
          <div style="color:#ffcc80;margin-top:.25rem;">原因：${sanitize(fine.reason || "")}</div>
          <div style="color:var(--muted);font-size:.74rem;">到期：${sanitize(fine.due_at || "-")} · 逾期限制：${sanitize(labels || "-")}</div>
          ${renderUserFineAppealStatus(fineAppeals, fine.fine_uuid)}
          ${payable ? `
            <div style="display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.5rem;">
              <button class="btn btn-primary" type="button" data-fine-pay="${sanitize(fine.fine_uuid)}" data-fine-amount="${dueAmount}">繳納並解鎖</button>
              <button class="btn" type="button" data-fine-appeal="${sanitize(fine.fine_uuid)}">申覆此罰單</button>
            </div>` : ""}
        </div>
      `;
    }).join("")}
  `;
  host.querySelectorAll("button[data-fine-pay]").forEach((btn) => {
    btn.addEventListener("click", () => payViolationFine(btn.getAttribute("data-fine-pay"), Number(btn.getAttribute("data-fine-amount") || 0)));
  });
  host.querySelectorAll("button[data-fine-appeal]").forEach((btn) => {
    btn.addEventListener("click", () => appealViolationFine(btn.getAttribute("data-fine-appeal")));
  });
}

function renderUserFeatureRestrictions(restrictions) {
  const host = $("appeal-restrictions");
  if (!host) return;
  const rows = Array.isArray(restrictions) ? restrictions : [];
  if (!rows.length) {
    host.textContent = "";
    return;
  }
  const labels = rows.map((row) => row.feature_label || row.feature_key || "-").join("、");
  host.innerHTML = `<span style="color:#ff8a80;">目前因罰單逾期限制：${sanitize(labels)}</span>`;
}

async function loadUserAppeals() {
  const wrap = $("user-appeal-wrap");
  if (!wrap || !currentUser || !canAccessModule("appeals")) return;
  const csrf = await fetchCsrfToken();
  const res = await apiFetch(API + "/appeals", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    const summaryEl = $("appeal-summary");
    if (summaryEl) summaryEl.textContent = json.msg || "申覆資料讀取失敗";
    return;
  }

  userAppeals = Array.isArray(json.appeals) ? json.appeals : [];
  userViolationFines = Array.isArray(json.violation_fines) ? json.violation_fines : [];
  userFeatureRestrictions = Array.isArray(json.feature_restrictions) ? json.feature_restrictions : [];
  const allViolations = Array.isArray(json.violations) ? json.violations : [];
  const governanceNotices = allViolations.filter(v => v.is_governance_notice);
  const violations = allViolations.filter(v => !v.is_governance_notice);
  const activeViolations = violations.filter(v => !v.is_resolved);
  const resolvedViolations = violations.filter(v => v.is_resolved);
  const summaryEl = $("appeal-summary");
  if (summaryEl) {
    const appealableCount = activeViolations.filter(v => v.can_appeal).length;
    const currentPoints = parseInt(json.violation_count || 0, 10);
    const openFineCount = userViolationFines.filter((fine) => fine.is_payable).length;
    if (!activeViolations.length && !openFineCount) {
      summaryEl.textContent = "目前無可申覆違規或待處理罰單";
    } else if (appealableCount > 0) {
      summaryEl.textContent = `目前違規點數 ${currentPoints}，共有 ${activeViolations.length} 筆有效違規，其中 ${appealableCount} 筆仍可逐條申覆，待處理罰單 ${openFineCount} 筆`;
    } else {
      summaryEl.textContent = `目前違規點數 ${currentPoints}，共有 ${activeViolations.length} 筆有效違規，待處理罰單 ${openFineCount} 筆`;
    }
  }
  renderUserFeatureRestrictions(userFeatureRestrictions);
  renderUserViolationFines(userViolationFines, json.violation_fine_appeals || []);

  const listEl = $("appeal-entries");
  if (!listEl) return;
  const statusText = {
    pending: "待審",
    approved: "已核准",
    rejected: "駁回"
  };
  function renderAppealViolation(v) {
    const appeal = v.appeal || null;
    const status = appeal ? appeal.status : "";
    const color = status === "approved" ? "#4caf50" : status === "rejected" ? "#ff4f6d" : "#ffb74d";
    const remaining = parseInt(v.remaining_seconds || 0, 10);
    const canAppeal = !!v.can_appeal;
    const appealStatus = appeal
      ? `<div style="color:${color};">申覆狀態：${statusText[status] || status}${appeal.review_note ? ` · 備註：${sanitize(appeal.review_note || "")}` : ""}</div>`
      : canAppeal
        ? `<div style="color:#82b1ff;">剩餘申覆時間：${appealCountdownText(remaining)}</div>`
        : `<div style="color:var(--muted);">不可申覆或已超過 24 小時</div>`;
    const controls = canAppeal
      ? `<textarea data-appeal-reason="${v.id}" rows="2" maxlength="200" placeholder="針對違規 #${v.id} 填寫申覆原因" style="margin-top:.45rem;"></textarea>
         <button class="btn btn-primary" type="button" data-appeal-submit="${v.id}" style="margin-top:.35rem;width:auto;padding:.45rem .75rem;">提交這筆申覆</button>`
      : "";
    return `
      <div style="border-bottom:1px solid #222;padding:.55rem .25rem;word-break:break-all;">
        <div><strong>違規 #${v.id}</strong> · ${sanitize(v.created_at || "")} · 懲罰 ${v.points || 0} 點</div>
        <div style="color:#ff8a80;">違規原因：${sanitize(v.reason || "")}</div>
        ${appealStatus}
        ${controls}
      </div>
    `;
  }
  function renderGovernanceNotice(v) {
    const appeal = v.appeal || null;
    const status = appeal ? appeal.status : "";
    const color = status === "approved" ? "#4caf50" : status === "rejected" ? "#ff4f6d" : "#ffb74d";
    const remaining = parseInt(v.remaining_seconds || 0, 10);
    const canAppeal = !!v.can_appeal;
    const appealStatus = appeal
      ? `<div style="color:${color};">申覆狀態：${statusText[status] || status}${appeal.review_note ? ` · 備註：${sanitize(appeal.review_note || "")}` : ""}</div>`
      : canAppeal
        ? `<div style="color:#82b1ff;">剩餘申覆時間：${appealCountdownText(remaining)}</div>`
        : `<div style="color:var(--muted);">不可申覆或已超過申覆期限</div>`;
    const controls = canAppeal
      ? `<textarea data-appeal-reason="${v.id}" rows="2" maxlength="200" placeholder="填寫申覆理由" style="margin-top:.45rem;"></textarea>
         <button class="btn btn-primary" type="button" data-appeal-submit="${v.id}" style="margin-top:.35rem;width:auto;padding:.45rem .75rem;">提交申覆</button>`
      : "";
    return `
      <div style="border-bottom:1px solid #222;padding:.55rem .25rem;word-break:break-all;">
        <div><strong>管理通知 #${v.id}</strong> · ${sanitize(v.created_at || "")}</div>
        <div style="color:#ffcc80;">${sanitize(v.reason || "")}</div>
        ${appealStatus}
        ${controls}
      </div>
    `;
  }
  const activeHtml = activeViolations.length
    ? `<div style="color:var(--muted);font-size:.75rem;margin:.25rem 0;">目前有效違規</div>${activeViolations.map(renderAppealViolation).join("")}`
    : "<p style='color:var(--muted);'>目前無有效違規</p>";
  const historyHtml = resolvedViolations.length
    ? `<div style="color:var(--muted);font-size:.75rem;margin:.75rem 0 .25rem;">已撤銷歷史</div>${resolvedViolations.map(renderAppealViolation).join("")}`
    : "";
  const governanceHtml = governanceNotices.length
    ? `<div style="color:var(--muted);font-size:.75rem;margin:1.25rem 0 .25rem;">會員權益變更通知（不計入違規點數）</div>${governanceNotices.map(renderGovernanceNotice).join("")}`
    : "";
  if (!violations.length && !governanceNotices.length) {
    listEl.innerHTML = "<p style='color:var(--muted);'>尚無違規或治理通知記錄</p>";
  } else {
    listEl.innerHTML = activeHtml + historyHtml + governanceHtml;
  }
  listEl.querySelectorAll("button[data-appeal-submit]").forEach((btn) => {
    btn.addEventListener("click", () => submitAppeal(parseInt(btn.getAttribute("data-appeal-submit"), 10)));
  });
}

async function payViolationFine(fineUuid, amount) {
  const fineId = String(fineUuid || "").trim();
  if (!fineId) {
    flash($("appeal-msg"), "找不到罰單編號", false);
    return;
  }
  const defaultWallet = typeof readEconomyDefaultSpendWalletAddress === "function" ? readEconomyDefaultSpendWalletAddress() : "";
  const sourceWallet = prompt("付款錢包地址；可留空使用主錢包，冷錢包會要求本機簽署。", defaultWallet || "");
  if (sourceWallet === null) return;
  const requestUuid = typeof economyRequestId === "function"
    ? economyRequestId("violation_fine")
    : (crypto?.randomUUID ? crypto.randomUUID() : `violation_fine:${Date.now()}`);
  let signature = "";
  try {
    if (sourceWallet && typeof economyBuildServiceFeeSignature === "function") {
      signature = await economyBuildServiceFeeSignature({
        source: sourceWallet,
        itemKey: "violation_fine",
        quantity: 1,
        amount: Number(amount || 0),
        requestUuid,
        referenceType: "violation_fine",
        referenceId: fineId,
      });
    }
  } catch (err) {
    flash($("appeal-msg"), err?.message || "罰款付款簽署失敗，未送出", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/violation-fines/" + encodeURIComponent(fineId) + "/pay", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({
      source_wallet_address: String(sourceWallet || "").trim(),
      request_uuid: requestUuid,
      signature,
    }),
  });
  const json = await res.json().catch(() => ({}));
  if (json.ok) {
    flash($("appeal-msg"), json.msg || "罰款已繳納，逾期限制已解除", true);
    await loadUserAppeals();
    if (typeof loadEconomyDashboard === "function") loadEconomyDashboard();
  } else {
    flash($("appeal-msg"), json.msg || "罰款付款失敗", false);
  }
}

async function appealViolationFine(fineUuid) {
  const fineId = String(fineUuid || "").trim();
  if (!fineId) return;
  const reason = prompt("請輸入罰單申覆理由（例如：違規已撤銷、罰單金額錯誤、已完成補正）", "");
  if (reason === null) return;
  const clean = String(reason || "").trim();
  if (clean.length < 8) {
    flash($("appeal-msg"), "罰單申覆理由至少 8 字", false);
    return;
  }
  if (clean.length > 500) {
    flash($("appeal-msg"), "罰單申覆理由請控制在 500 字內", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/violation-fines/" + encodeURIComponent(fineId) + "/appeal", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ reason: clean }),
  });
  const json = await res.json().catch(() => ({}));
  flash($("appeal-msg"), json.msg || (json.ok ? "罰單申覆已提交" : "罰單申覆提交失敗"), !!json.ok);
  if (json.ok) await loadUserAppeals();
}

async function submitAppeal(violationId) {
  const reasonEl = document.querySelector(`textarea[data-appeal-reason="${violationId}"]`);
  const reason = (reasonEl?.value || "").trim();
  if (!reason) {
    flash($("appeal-msg"), "請填寫申覆原因", false);
    return;
  }
  if (reason.length > 200) {
    flash($("appeal-msg"), "申覆原因請控制在 200 字以內", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/appeals", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ violation_id: violationId, reason })
  });
  const json = await res.json().catch(() => ({}));
  if (json && json.ok) {
    if (reasonEl) reasonEl.value = "";
    flash($("appeal-msg"), json.msg || "申覆已提交", true);
    await loadUserAppeals();
  } else {
    flash($("appeal-msg"), json.msg || "提交失敗", false);
  }
}

async function loadAdminAppeals(page = 0, status = null) {
  if (!currentUser || currentRole !== "super_admin" || !canAccessModule("appeals")) return;
  const targetStatus = status || adminAppealStatus;
  adminAppealStatus = targetStatus;
  const targetPage = Math.max(1, parseInt(page || 1, 10));
  const csrf = await fetchCsrfToken();
  const res = await apiFetch(API + "/admin/appeals?status=" + encodeURIComponent(targetStatus) + "&page=" + targetPage + "&limit=20", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    const message = json.msg || "申覆清單讀取失敗";
    flash($("appeal-msg"), message, false);
    const list = $("admin-appeal-list");
    if (list) {
      list.innerHTML = `<p style='color:var(--red);text-align:center;padding:1rem;'>${sanitize(message)}</p>`;
    }
    if ($("admin-appeals-total")) $("admin-appeals-total").textContent = "0";
    return;
  }

  adminAppealPage = targetPage;
  adminAppeals = Array.isArray(json.items) ? json.items : [];
  if ($("admin-appeals-total")) $("admin-appeals-total").textContent = json.total || 0;

  const list = $("admin-appeal-list");
  if (!list) return;
  const allowedAppealIds = new Set();
  if (!adminAppeals.length) {
    list.innerHTML = "<p style='color:var(--muted);'>目前沒有符合條件的申覆</p>";
  } else {
    list.innerHTML = adminAppeals.map(a => {
      const selectable = a.status === "pending";
      if (selectable) allowedAppealIds.add(String(a.id));
      const checked = selectable && selectedAppealIds.has(String(a.id)) ? "checked" : "";
      const statusColor = a.status === "pending" ? "#ffb74d" : a.status === "approved" ? "#4caf50" : "#ff4f6d";
      const action = a.status === "pending"
        ? `<div style=\"margin-top:.4rem;display:flex;gap:.4rem;\">
            <button class=\"btn\" data-appeal-action=\"approve\" data-appeal-id=\"${a.id}\" style=\"background:#1f9d57;color:#fff;border:1px solid #1f9d57;\">核准撤銷</button>
            <button class=\"btn\" data-appeal-action=\"reject\" data-appeal-id=\"${a.id}\" style=\"background:#ff5252;color:#fff;border:1px solid #ff5252;\">維持處分</button>
          </div>`
        : "";
      return `
        <div style="border-bottom:1px solid #222;padding:.45rem .25rem;word-break:break-all;">
          <div style="display:flex;align-items:center;gap:.5rem;">
            ${selectable ? `<input type="checkbox" data-appeal-check="${a.id}" ${checked} />` : `<span style="color:var(--muted);">—</span>`}
            <strong>${sanitize(a.username || "")}</strong> · 違規 #${a.latest_violation_id || "-"}
          </div>
          <div style=\"color:${statusColor};font-size:.75rem;\">${a.status}</div>
          <div style=\"color:#aaa;font-size:.7rem;\">時間：${sanitize(a.created_at || "")} · 懲罰：${a.penalty_points || 0} 點</div>
          <div>原因：${sanitize(a.reason || "")}</div>
          ${a.review_note ? `<div style=\"color:#bbb;\">備註：${sanitize(a.review_note || "")}</div>` : ""}
          <div>${action}</div>
        </div>
      `;
    }).join("");
    list.querySelectorAll("button[data-appeal-id]").forEach((btn) => {
      const appealId = btn.getAttribute("data-appeal-id");
      const action = btn.getAttribute("data-appeal-action");
      btn.addEventListener("click", () => reviewAppeal(appealId, action));
    });
    list.querySelectorAll("input[data-appeal-check]").forEach((box) => {
      box.addEventListener("change", () => {
        const id = box.getAttribute("data-appeal-check");
        if (box.checked) selectedAppealIds.add(String(id));
        else selectedAppealIds.delete(String(id));
        updateAppealSelectionUi();
      });
    });
  }
  selectedAppealIds = new Set([...selectedAppealIds].filter((id) => allowedAppealIds.has(id)));
  updateAppealSelectionUi();

  if ($("admin-appeals-prev")) $("admin-appeals-prev").disabled = targetPage <= 1;
  if ($("admin-appeals-next")) $("admin-appeals-next").disabled = (targetPage * 20) >= (json.total || 0);
}

function updateAppealSelectionUi() {
  const count = selectedAppealIds.size;
  const info = $("appeal-selection-info");
  if (info) info.textContent = `已選 ${count} 筆申覆`;
  const approveBtn = $("admin-appeals-bulk-approve");
  const rejectBtn = $("admin-appeals-bulk-reject");
  if (approveBtn) approveBtn.disabled = count === 0;
  if (rejectBtn) rejectBtn.disabled = count === 0;
}

async function reviewAppeal(appealId, action) {
  if (!currentUser || currentRole !== "super_admin") return;
  const note = prompt("審核備註（非必填）", "");
  if (note === null) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/appeals/" + parseInt(appealId, 10) + "/review", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ action, note: (note || "").trim() })
  });
  const json = await res.json().catch(() => ({}));
  if (json && json.ok) {
    await Promise.all([loadAdminAppeals(adminAppealPage, adminAppealStatus), loadUsers()]);
  } else {
    alert(json.msg || "審核失敗");
  }
}

async function bulkReviewAppeals(action) {
  if (!currentUser || currentRole !== "super_admin") return;
  const ids = [...selectedAppealIds];
  if (!ids.length) return;
  const label = action === "approve" ? "核准撤銷" : "維持處分";
  if (!confirm(`確定要${label}這 ${ids.length} 筆申覆？`)) return;
  const note = prompt("批次審核備註（非必填）", "");
  if (note === null) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  let success = 0;
  let failed = 0;
  for (const appealId of ids) {
    try {
      const res = await apiFetch(API + "/admin/appeals/" + parseInt(appealId, 10) + "/review", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
        body: JSON.stringify({ action, note: (note || "").trim() })
      });
      const json = await res.json().catch(() => ({}));
      if (json && json.ok) success += 1;
      else failed += 1;
    } catch (_) {
      failed += 1;
    }
  }
  selectedAppealIds.clear();
  await Promise.all([loadAdminAppeals(adminAppealPage, adminAppealStatus), loadUsers()]);
  flash($("li-msg"), failed === 0 ? `${label}完成，共 ${success} 筆` : `${label}完成 ${success} 筆，失敗 ${failed} 筆`, failed === 0);
}

async function loadAdminReports(page = 0, status = null) {
  if (!currentUser || currentRole !== "super_admin") return;
  const targetStatus = status || adminReportStatus;
  adminReportStatus = targetStatus;
  const targetPage = Math.max(0, parseInt(page || 0, 10));
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/admin/message-reports?status=" + encodeURIComponent(targetStatus) + "&page=" + targetPage + "&limit=30", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    const message = json.msg || "訊息檢舉清單讀取失敗";
    flash($("appeal-msg"), message, false);
    const list = $("admin-report-list");
    if (list) {
      list.innerHTML = `<p style='color:var(--red);text-align:center;padding:1rem;'>${sanitize(message)}</p>`;
    }
    if ($("admin-reports-total")) $("admin-reports-total").textContent = "0";
    return;
  }

  adminReportPage = targetPage;
  adminReports = Array.isArray(json.items) ? json.items : [];
  if ($("admin-reports-total")) $("admin-reports-total").textContent = json.total || 0;
  const list = $("admin-report-list");
  if (!list) return;
  const allowedReportIds = new Set();
  if (!adminReports.length) {
    list.innerHTML = "<p style='color:var(--muted);'>目前沒有符合條件的訊息檢舉</p>";
  } else {
    list.innerHTML = adminReports.map(r => {
      const selectable = r.status === "pending";
      const reportKey = `${r.kind || "chat"}:${r.id}`;
      const sourceLabel = r.kind === "community_post" ? "社群留言" : "聊天室訊息";
      if (selectable) allowedReportIds.add(reportKey);
      const checked = selectable && selectedReportIds.has(reportKey) ? "checked" : "";
      const action = r.status === "pending"
        ? `<div style="margin-top:.4rem;display:flex;gap:.4rem;">
            <button class="btn" data-report-action="approve" data-report-id="${r.id}" data-report-kind="${r.kind || "chat"}" style="background:#1f9d57;color:#fff;border:1px solid #1f9d57;">核准計點</button>
            <button class="btn" data-report-action="reject" data-report-id="${r.id}" data-report-kind="${r.kind || "chat"}" style="background:#ff5252;color:#fff;border:1px solid #ff5252;">駁回</button>
          </div>`
        : "";
      return `
        <div style="border-bottom:1px solid #222;padding:.45rem .25rem;word-break:break-all;">
          <div style="display:flex;align-items:center;gap:.5rem;">
            ${selectable ? `<input type="checkbox" data-report-check="${reportKey}" ${checked} />` : `<span style="color:var(--muted);">—</span>`}
            <strong>${sourceLabel}檢舉 #${r.id}</strong> · 內容 #${r.message_id} · ${r.kind === "community_post" ? "thread" : "room"} #${r.room_id}
          </div>
          <div style="color:#aaa;font-size:.7rem;">${sanitize(r.created_at || "")} · 檢舉者：${sanitize(r.reporter_username || "")} · 被檢舉：${sanitize(r.reported_username || "")}</div>
          <div style="color:#ff8a80;">訊息：${sanitize(r.content || "")}</div>
          <div>檢舉原因：${sanitize(r.reason || "")}</div>
          ${r.review_note ? `<div style="color:#bbb;">備註：${sanitize(r.review_note || "")}</div>` : ""}
          ${action}
        </div>
      `;
    }).join("");
    list.querySelectorAll("button[data-report-id]").forEach((btn) => {
      btn.addEventListener("click", () => reviewMessageReport(
        parseInt(btn.getAttribute("data-report-id"), 10),
        btn.getAttribute("data-report-action"),
        btn.getAttribute("data-report-kind") || "chat"
      ));
    });
    list.querySelectorAll("input[data-report-check]").forEach((box) => {
      box.addEventListener("change", () => {
        const id = box.getAttribute("data-report-check");
        if (box.checked) selectedReportIds.add(id);
        else selectedReportIds.delete(id);
        updateReportSelectionUi();
      });
    });
  }
  selectedReportIds = new Set([...selectedReportIds].filter((id) => allowedReportIds.has(id)));
  updateReportSelectionUi();
  if ($("admin-reports-prev")) $("admin-reports-prev").disabled = targetPage <= 0;
  if ($("admin-reports-next")) $("admin-reports-next").disabled = ((targetPage + 1) * 30) >= (json.total || 0);
}

function updateReportSelectionUi() {
  const count = selectedReportIds.size;
  const info = $("report-selection-info");
  if (info) info.textContent = `已選 ${count} 筆檢舉`;
  const approveBtn = $("admin-reports-bulk-approve");
  const rejectBtn = $("admin-reports-bulk-reject");
  if (approveBtn) approveBtn.disabled = count === 0;
  if (rejectBtn) rejectBtn.disabled = count === 0;
}

async function reviewMessageReport(reportId, action, kind = "chat") {
  if (!currentUser || currentRole !== "super_admin") return;
  const note = prompt("審核備註（非必填）", "");
  if (note === null) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const path = kind === "community_post"
    ? "/admin/community-post-reports/" + reportId + "/review"
    : "/admin/message-reports/" + reportId + "/review";
  const res = await apiFetch(API + path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ action, note: (note || "").trim() })
  });
  const json = await res.json().catch(() => ({}));
  if (json && json.ok) {
    await Promise.all([loadAdminReports(adminReportPage, adminReportStatus), loadUsers()]);
  } else {
    alert(json.msg || "審核失敗");
  }
}

async function bulkReviewMessageReports(action) {
  if (!currentUser || currentRole !== "super_admin") return;
  const ids = [...selectedReportIds];
  if (!ids.length) return;
  const label = action === "approve" ? "核准計點" : "駁回";
  if (!confirm(`確定要${label}這 ${ids.length} 筆訊息檢舉？`)) return;
  const note = prompt("批次審核備註（非必填）", "");
  if (note === null) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  let success = 0;
  let failed = 0;
  for (const reportId of ids) {
    try {
      const [kind, rawId] = String(reportId).split(":");
      const path = kind === "community_post"
        ? "/admin/community-post-reports/" + parseInt(rawId, 10) + "/review"
        : "/admin/message-reports/" + parseInt(rawId, 10) + "/review";
      const res = await apiFetch(API + path, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
        body: JSON.stringify({ action, note: (note || "").trim() })
      });
      const json = await res.json().catch(() => ({}));
      if (json && json.ok) success += 1;
      else failed += 1;
    } catch (_) {
      failed += 1;
    }
  }
  selectedReportIds.clear();
  await Promise.all([loadAdminReports(adminReportPage, adminReportStatus), loadUsers()]);
  flash($("li-msg"), failed === 0 ? `${label}完成，共 ${success} 筆` : `${label}完成 ${success} 筆，失敗 ${failed} 筆`, failed === 0);
}

function resetAppealsAccountState() {
  userAppeals = [];
  userViolationFines = [];
  userFeatureRestrictions = [];
  adminAppeals = [];
  adminAppealPage = 1;
  adminAppealStatus = "pending";
  adminReports = [];
  adminReportPage = 0;
  adminReportStatus = "pending";
  selectedAppealIds.clear();
  selectedReportIds.clear();
  [
    "appeal-fines", "appeal-restrictions", "appeal-summary", "appeal-entries",
    "admin-appeal-list", "admin-report-list", "appeal-selection-info", "report-selection-info",
  ].forEach((id) => {
    const el = $(id);
    if (el) el.replaceChildren();
  });
  ["admin-appeals-total", "admin-reports-total"].forEach((id) => {
    const el = $(id);
    if (el) el.textContent = "0";
  });
  const msg = $("appeal-msg");
  if (msg) {
    msg.textContent = "";
    msg.className = "msg";
  }
}

document.addEventListener("hackme:account-context-changed", resetAppealsAccountState);
