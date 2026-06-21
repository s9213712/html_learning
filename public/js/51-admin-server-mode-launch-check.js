/*
 * Server mode and production launch-check admin module.
 *
 * This file is the single canonical implementation for:
 * - server mode status and switching
 * - production gate / launch-check report UI
 * - internal-test and tester-token management
 *
 * It is loaded after 50-admin.js so it can use shared admin helpers, state,
 * and DOM utilities without duplicating them.
 */

async function loadServerMode() {
  if (!currentUser || currentUser !== "root") return;
  const status = $("server-mode-status");
  if (!$("server-mode-select")) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/root/server-mode", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    if (status) {
      const detail = json.msg || json.error || `HTTP ${res.status}`;
      status.textContent = `伺服器模式讀取失敗：${detail}`;
      status.style.color = "#ff4f6d";
    }
    return;
  }
  const mode = json.mode || {};
  currentServerMode = String(mode.current_mode || "dev_ready").trim().toLowerCase();
  populateSecurityProfiles(json.profiles || securityProfiles, mode.current_mode || "dev_ready");
  updateServerModeLaunchCheckVisibility();
  if (status) {
    const previous = mode.previous_mode ? `，上一個模式：${mode.previous_mode}` : "";
    const snapshot = mode.active_snapshot_id ? `，active snapshot：${mode.active_snapshot_id}` : "";
    const checkpoint = mode.checkpoint_id ? `，checkpoint：${mode.checkpoint_id}` : "";
    const phrase = serverModeConfirmPhrase(mode.current_mode || "dev_ready");
    status.textContent = `目前模式：${mode.current_mode || "dev_ready"}${previous}${snapshot}${checkpoint}。切換此模式需輸入：${phrase}`;
    status.style.color = mode.current_mode === "superweak" || mode.current_mode === "incident_lockdown" ? "#ff4f6d" : "var(--muted)";
  }
  updateServerModeTokenPanels(currentServerMode);
  renderServerModeRequirements(json.production_requirements || {});
  await loadServerModeLogs();
  await loadInternalTestTokenStatus();
  await loadTesterTokens();
}

function serverModeConfirmPhrase(mode) {
  const key = String(mode || "").trim();
  return {
    production: "GO_LIVE",
    preprod: "SWITCH_TO_DEV_READY",
    dev_ready: "SWITCH_TO_DEV_READY",
    test: "SWITCH_TO_TEST",
    internal_test: "SWITCH_TO_INTERNAL_TEST",
    maintenance: "ENTER_MAINTENANCE",
    incident_lockdown: "ENTER_INCIDENT_LOCKDOWN",
    superweak: "ENABLE_SUPERWEAK"
  }[key] || "SWITCH_CUSTOM_MODE";
}

function serverModeSupportsInternalTestToken(mode) {
  return String(mode || "").trim().toLowerCase() === "internal_test";
}

function serverModeSupportsTesterToken(mode) {
  const value = String(mode || "").trim().toLowerCase();
  return value === "test" || value === "internal_test";
}

function updateServerModeTokenPanels(modeOverride = null) {
  const mode = String(modeOverride || $("server-mode-select")?.value || currentServerMode || "dev_ready").trim().toLowerCase();
  const internalPanel = $("server-mode-internal-test-panel");
  const testerPanel = $("server-mode-tester-token-panel");
  const hint = $("server-mode-token-hint");
  const showInternal = serverModeSupportsInternalTestToken(mode);
  const showTester = serverModeSupportsTesterToken(mode);
  if (internalPanel) internalPanel.style.display = showInternal ? "" : "none";
  if (testerPanel) testerPanel.style.display = showTester ? "" : "none";
  if (!hint) return;
  if (showInternal) {
    hint.textContent = "目前模式可管理內測登入 token 與 tester token。";
  } else if (showTester) {
    hint.textContent = "目前模式可管理 tester token。若要開放內測登入 token，請切到 internal_test。";
  } else {
    hint.textContent = "切到 test 或 internal_test 才會顯示 tester token；切到 internal_test 才會顯示內測登入 token。";
  }
  hint.style.color = "var(--muted)";
}

function renderServerModeRequirements(requirements) {
  const host = $("server-mode-requirements");
  if (!host) return;
  const missing = Array.isArray(requirements.missing) ? requirements.missing : [];
  const failed = Array.isArray(requirements.failed) ? requirements.failed : [];
  const required = Array.isArray(requirements.required) ? requirements.required : [];
  host.innerHTML = `
    <div><strong>Production gate</strong>：${requirements.ok ? "已通過" : "未通過"}</div>
    <div>必要報告：${required.join(", ") || "-"}</div>
    <div>缺少：${missing.join(", ") || "無"}；失敗：${failed.join(", ") || "無"}</div>
  `;
}

// ── 上線前檢查分頁（13 份 production gate report）────────────────────────────
//
// Each entry: label / purpose / generator / defaultOutput / tip / shortcut
// shortcut.kind:
//   "ui"      -> jump to the in-app panel that triggers this report
//   "doc"     -> link to the playbook / spec file (CLI-only generators)
const LAUNCH_CHECK_REPORT_META = {
  clean_smoke: {
    label: "Mode v2 乾淨 smoke",
    purpose: "boot path / state-machine / 基線 endpoints 在乾淨狀態下都過。",
    generator: "python3 scripts/security/server_mode/server_mode_v2_clean_smoke.py",
    defaultOutput: "runtime/reports/security/server_mode_v2_clean_smoke_<timestamp>.json|.md",
    tip: "失敗多半是某個 mode 切換邏輯被改壞；先看輸出 JSON 的 first failing step。",
    shortcut: { kind: "doc", label: "playbook §1 No.1", href: "docs/server_mode_v2/03_production_gate_playbook.md" },
  },
  adversarial: {
    label: "Mode v2 對抗測試",
    purpose: "injection / bypass / mode-spoof 等對抗手法都被擋下。",
    generator: "python3 scripts/security/server_mode/server_mode_v2_adversarial.py",
    defaultOutput: "runtime/reports/security/server_mode_v2_adversarial_<timestamp>.json|.md",
    tip: "失敗代表有可繞過的權限漏洞，**不可** 放行 production；先 fix 再上。",
    shortcut: { kind: "doc", label: "playbook §1 No.2", href: "docs/server_mode_v2/03_production_gate_playbook.md" },
  },
  redteam_l2: {
    label: "Mode v2 Red-team L2",
    purpose: "Red-team Level 2 攻擊樹（multi-step exploit）。",
    generator: "python3 scripts/security/server_mode/server_mode_v2_redteam_l2.py",
    defaultOutput: "runtime/reports/security/server_mode_v2_redteam_l2_<timestamp>.json|.md",
    tip: "失敗代表某條 exploit chain 仍可走；找 chain 中第一個能擋的點補強。",
    shortcut: { kind: "doc", label: "playbook §1 No.3", href: "docs/server_mode_v2/03_production_gate_playbook.md" },
  },
  pytest: {
    label: "全專案 pytest",
    purpose: "tests/ 全部 pytest 通過。",
    generator: "pytest tests/ -q",
    defaultOutput: "runtime/reports/security/production_gate/pytest_production_report.json",
    tip: "失敗就跑 pytest -x 找最前面那條紅燈，先修它。",
    shortcut: { kind: "doc", label: "tests/ 目錄", href: "docs/server_mode_v2/03_production_gate_playbook.md" },
  },
  log_chain_verify: {
    label: "Log chain 完整性",
    purpose: "mode_switch_logs + audit chain 雜湊鏈完整無破洞。",
    generator: "GET /api/root/server-mode/logs/verify",
    defaultOutput: "runtime/reports/security/production_gate/log_chain_verify_report.json",
    tip: "失敗代表 audit chain 被改過（極危險）— 立刻進 incident_lockdown 調查。",
    shortcut: { kind: "ui", target: "health", anchor: "server-health-audit", label: "前往健康度 → 審計與檢查" },
  },
  integrity_guard: {
    label: "Integrity Guard 自檢",
    purpose: "IntegrityGuard 自檢無 high-risk finding。",
    generator: "POST /api/root/integrity/rescan ＋ GET /api/root/integrity/report",
    defaultOutput: "runtime/reports/security/production_gate/integrity_guard_report.json",
    tip: "若有 high-risk file diff，請先確認是否為合法升級；不是就走 restore。",
    shortcut: { kind: "ui", target: "integrity", anchor: "integrity-findings", label: "前往 Integrity Guard 分頁" },
  },
  stress: {
    label: "壓力 / 流量壓測",
    purpose: "trading + 一般流量壓測無 OOM / deadlock / 大量錯誤。",
    generator: "python3 scripts/security/pentest/stress_test.py + python3 scripts/security/pentest/trading_stress_pentest.py",
    defaultOutput: "runtime/reports/security/stress_<timestamp>.json|.md ＋ runtime/reports/security/trading_stress_report_<timestamp>.json|.md",
    tip: "失敗多半是 worker 飽和或 race condition；先看 server log 的 spike 時間點。",
    shortcut: { kind: "ui", target: "security", anchor: "security-stress-start-btn", label: "前往總覽 → 壓力測試面板" },
  },
  permission: {
    label: "權限滲透",
    purpose: "role / permission pentest 無越權。",
    generator: "python3 scripts/security/pentest/functional_permission_pentest.py",
    defaultOutput: "runtime/reports/security/functional_permission_pentest_<timestamp>.json|.md",
    tip: "失敗代表某條 API 缺 require_role / require_csrf；補上後重跑。",
    shortcut: { kind: "doc", label: "playbook §1 No.8", href: "docs/server_mode_v2/03_production_gate_playbook.md" },
  },
  functional: {
    label: "全功能 smoke",
    purpose: "全功能流程（登入 / 聊天 / 雲端硬碟 / 交易 / 積分 等）都正常。",
    generator: "bash scripts/security/pentest/run_functional_smoke.sh ＋ python3 tests/security/smoke/smoke_suite.py",
    defaultOutput: "runtime/reports/security/functional_<run_id>/00_FUNCTIONAL_SMOKE.md ＋ raw/、results.tsv、server.out",
    tip: "失敗代表某個使用者流程已壞，可能是最近 commit 副作用。",
    shortcut: { kind: "ui", target: "security", anchor: "security-functional-start-btn", label: "前往總覽 → 全功能測試面板" },
  },
  pentest: {
    label: "安全滲透測試",
    purpose: "session / CSRF / XSS / SQLi 等滲透測試無重大發現。",
    generator: "bash scripts/security/pentest/run_pentest.sh ＋ python3 scripts/security/pentest/session_security_pentest.py",
    defaultOutput: "runtime/reports/security/<run_id>/00_SUMMARY.md ＋ raw/*.json|*.md|*.txt",
    tip: "失敗代表 web layer 有可利用洞，**絕不**可放上線；fix 後重跑。",
    shortcut: { kind: "ui", target: "security", anchor: "security-pentest-start-btn", label: "前往總覽 → 滲透測試面板" },
  },
  snapshot_restore: {
    label: "Snapshot / Restore",
    purpose: "snapshot 建立 + restore 回放 + 一致性驗證全程通過。",
    generator: "pytest tests/snapshots/test_snapshots.py ＋ 手動 1 次 create→restore→verify",
    defaultOutput: "runtime/reports/security/production_gate/snapshot_restore_report.json",
    tip: "失敗代表 disaster recovery 不可靠；不要硬上 production。",
    shortcut: { kind: "ui", target: "settings", anchor: "snapshot-section", label: "前往伺服器設定 → 快照管理" },
  },
  points_chain_consistency: {
    label: "PointsChain 一致性",
    purpose: "PointsChain 雜湊鏈、區塊內 ledger 對應全部一致。",
    generator: "pytest tests/points/test_points_chain.py ＋ services/points_chain.verify_chain()",
    defaultOutput: "runtime/reports/security/production_gate/points_chain_consistency_report.json",
    tip: "失敗等同帳本被污染，**不可** 上線；走 restore + 重新 verify。",
    shortcut: { kind: "ui", target: "settings", anchor: "server-mode-section", label: "前往伺服器設定 → 鏈狀態" },
  },
  cloud_drive_quota_permission: {
    label: "雲端硬碟 quota / 權限",
    purpose: "Cloud Drive quota、上傳權限、共享權限規則都正確。",
    generator: "pytest tests/storage/test_cloud_drive_attachments.py tests/storage/test_storage_albums_schema.py",
    defaultOutput: "runtime/reports/security/production_gate/cloud_drive_quota_permission_report.json",
    tip: "失敗代表 quota 算錯或共享越權；先 fix 再產 report。",
    shortcut: { kind: "ui", target: "settings", anchor: "sec-settings-drive", label: "前往伺服器設定 → 雲端硬碟" },
  },
};

// 其他上線前條件（真正 blocker + 現況提示）— production gate
// 不把「已經先切成 production」或「已手動套 production 設定」當成前置條件。
// production profile 會在 mode switch 成功時自動套用；A 區只保留切換前真的
// 需要先確認的 blocker，外加少量資訊卡。每個 condition 函式拿到
// (sc, requirements) 回傳 {label, value, color, hint?, shortcut?}.
function launchCheckConditionList(sc, requirements) {
  const audit = sc.audit_integrity || {};
  const readiness = sc.readiness || {};
  const anomaly = sc.anomaly || {};
  const mode = (sc.mode || {}).current_mode || "dev_ready";
  const profiles = Array.isArray(sc.profiles) ? sc.profiles : [];
  const productionProfile = profiles.find((item) => item && item.name === "production") || {};
  const productionSettings = productionProfile.settings || {};
  const out = [];

  // Mode posture: 顯示目前在哪個世界，但不要求先成為 production。
  out.push({
    label: "目前 server mode",
    value: mode,
    color: mode === "production" ? "#4caf50" : "#82b1ff",
    hint: mode === "production"
      ? "目前已在 production"
      : "上線前檢查可在非 production 執行；真正切換由 GO_LIVE 完成",
    shortcut: { kind: "ui", target: "settings", anchor: "server-mode-section", label: "前往伺服器設定 → 模式切換" },
  });

  // Production profile 會在成功切 mode 時自動套用，不應列為前置 blocker。
  const productionAutoSummary = [
    productionSettings.server_ssl_enabled ? "HTTPS" : null,
    productionSettings.audit_chain_enabled ? "audit chain" : null,
    productionSettings.ip_blocking_enabled ? "IP 封鎖" : null,
    productionSettings.login_violation_enabled ? "登入暴力鎖" : null,
    productionSettings.rate_limit_violation_enabled ? "rate limit" : null,
    productionSettings.integrity_guard_enabled ? `Integrity Guard${productionSettings.integrity_guard_strict_mode ? " strict" : ""}` : null,
    productionSettings.browser_only_mode_enabled ? "browser-only" : null,
  ].filter(Boolean);
  out.push({
    label: "production profile 自動套用",
    value: productionAutoSummary.length ? productionAutoSummary.join(" / ") : "內建 hardening",
    color: "#82b1ff",
    hint: "這些安全設定會在切換到 production 時自動套用，不是上線前檢查的手動前置條件",
    shortcut: { kind: "ui", target: "settings", anchor: "server-mode-section", label: "前往伺服器設定 → 模式切換" },
  });

  // Audit chain 完整性
  const auditEnabled = audit.enabled !== false;
  const auditOk = !!audit.ok;
  out.push({
    label: "Audit chain 完整性",
    value: auditEnabled ? (auditOk ? "完整" : "異常") : "目前未啟用",
    color: auditEnabled ? (auditOk ? "#4caf50" : "#ff4f6d") : "#82b1ff",
    hint: auditEnabled
      ? (auditOk ? "雜湊鏈無斷點" : "雜湊鏈不完整：先處理 mode_switch_logs / audit 問題再上線")
      : "目前模式未啟用 audit chain；切 production 時會自動開，但若要先演練請在隔離環境做 production gate rehearsal",
    shortcut: { kind: "ui", target: "health", anchor: "server-health-audit", label: "前往健康度 → 審計與檢查" },
  });

  // Readiness
  out.push({
    label: "Readiness 檢查",
    value: readiness.status || "-",
    color: healthStatusColor(readiness.status),
    hint: readiness.status === "ok" ? "Readiness ok" : `Readiness=${readiness.status || "-"}：先解決失敗項`,
    shortcut: { kind: "ui", target: "health", anchor: "server-health-summary", label: "前往健康度 → Readiness" },
  });

  // Anomaly
  const signalCount = Array.isArray(anomaly.signals) ? anomaly.signals.length : 0;
  out.push({
    label: "異常訊號",
    value: signalCount === 0 ? "無" : `${signalCount} 筆`,
    color: signalCount === 0 ? "#4caf50" : (anomaly.status === "critical" ? "#ff4f6d" : "#ffb74d"),
    hint: signalCount === 0 ? "目前無異常訊號" : `先處理 anomaly signals（${signalCount} 筆）再上線`,
    shortcut: { kind: "ui", target: "health", anchor: "server-health-summary", label: "前往健康度 → Anomaly" },
  });

  // 13 reports rollup（同一個 condition 卡，給操作者快速看 B 區概況）
  const total = (requirements.required || []).length;
  const missing = (requirements.missing || []).length;
  const failed = (requirements.failed || []).length;
  const pass = Math.max(0, total - missing - failed);
  out.push({
    label: "13 份 production reports",
    value: `${pass} / ${total}`,
    color: missing === 0 && failed === 0 ? "#4caf50" : "#ff4f6d",
    hint: (missing === 0 && failed === 0) ? "全部通過" : `缺 ${missing} 份、不通過 ${failed} 份；詳見下方 B 區`,
  });

  return out;
}

function jumpToAnchor(anchorId) {
  if (!anchorId) return;
  const el = document.getElementById(anchorId);
  if (el && typeof el.scrollIntoView === "function") {
    setTimeout(() => el.scrollIntoView({ behavior: "smooth", block: "start" }), 150);
  }
}

function launchCheckSetUploadStatus(text, ok = false) {
  const el = $("launch-check-upload-status");
  if (!el) return;
  el.textContent = text || "";
  el.style.color = text ? (ok ? "#4caf50" : "#ff4f6d") : "var(--muted)";
}

function populateLaunchCheckUploadTypes(selected = "") {
  const select = $("launch-check-upload-report-type");
  if (!select) return;
  const current = selected || select.value || "";
  const options = Object.entries(LAUNCH_CHECK_REPORT_META).map(([key, meta]) => {
    const chosen = key === current ? " selected" : "";
    return `<option value="${key}"${chosen}>${meta.label} (${key})</option>`;
  });
  select.innerHTML = options.join("");
}

function openLaunchCheckUpload(reportType = "") {
  populateLaunchCheckUploadTypes(reportType);
  const panel = $("launch-check-upload-panel");
  const sub = $("launch-check-upload-sub");
  const hint = $("launch-check-upload-hint");
  const input = $("launch-check-upload-json");
  const file = $("launch-check-upload-file");
  if (sub) sub.textContent = reportType ? `準備上傳 ${reportType} 的 production report JSON。` : "選擇 report 類型後，可貼上 JSON 或上傳 `.json` 檔。";
  if (hint) {
    const meta = LAUNCH_CHECK_REPORT_META[reportType];
    hint.textContent = meta
      ? `用途：${meta.purpose}｜建議產生方式：${meta.generator}｜注意：必須提供 raw_report + sha256 report_hash + 可驗證 signature，伺服器會重算 hash 並驗簽。`
      : "上傳的 JSON 必須包含 raw_report、sha256 report_hash 與可驗證 signature；未通過驗簽不會計入 production gate。";
  }
  if (input && !input.value.trim()) input.value = reportType ? `{\n  "report_type": "${reportType}",\n  "target_commit": "",\n  "target_branch": "",\n  "server_mode": "preprod",\n  "test_result": "pass",\n  "pass": true,\n  "critical_findings_count": 0,\n  "high_findings_count": 0,\n  "unresolved_findings": [],\n  "tester": "root",\n  "report_hash": "sha256:",\n  "signature": "hmac_sha256:",\n  "key_version": "",\n  "raw_report": {\n    "summary": "fill with the actual signed report body"\n  }\n}` : "";
  if (file) file.value = "";
  launchCheckSetUploadStatus("");
  if (panel) panel.open = true;
  jumpToAnchor("launch-check-upload-panel");
}

async function openLaunchCheckDoc(shortcut) {
  const path = String(shortcut?.href || "").trim();
  if (!path) {
    launchCheckMsg("文件路徑未定義", false);
    return;
  }
  const panel = $("launch-check-doc-panel");
  const sub = $("launch-check-doc-sub");
  const pathEl = $("launch-check-doc-path");
  const content = $("launch-check-doc-content");
  if (sub) sub.textContent = "讀取文件中...";
  if (pathEl) pathEl.textContent = path;
  if (content) content.textContent = "讀取中...";
  if (panel) panel.open = true;
  jumpToAnchor("launch-check-doc-panel");
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(`${API}/root/launch-check/doc?path=${encodeURIComponent(path)}`, {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" },
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    if (sub) sub.textContent = shortcut?.label || json.label || "文件內容";
    if (pathEl) pathEl.textContent = json.path || path;
    if (content) content.textContent = json.content || "（空白文件）";
  } catch (err) {
    if (sub) sub.textContent = "文件讀取失敗";
    if (content) content.textContent = `讀取失敗：${err && err.message ? err.message : "未知錯誤"}`;
    launchCheckMsg(err && err.message ? err.message : "文件讀取失敗", false);
  }
}

function launchCheckShortcutHandler(shortcut) {
  if (!shortcut) return null;
  if (shortcut.kind === "ui") {
    return () => {
      try { switchServerTab(shortcut.target || "security"); } catch (_) {}
      jumpToAnchor(shortcut.anchor);
    };
  }
  if (shortcut.kind === "doc") {
    return () => openLaunchCheckDoc(shortcut);
  }
  return null;
}

function launchCheckConditionMarkup(condition, idx) {
  const escape = (text) => String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const defaultOpen = condition.color === "#ff4f6d" || condition.color === "#ffb74d";
  const shortcutBtn = condition.shortcut
    ? `<button class="btn btn-sm" type="button" data-launch-shortcut="cond-${idx}" style="margin-top:.35rem;font-size:.7rem;padding:.18rem .5rem;">${escape(condition.shortcut.label || "前往")}</button>`
    : "";
  return `
    <details class="drive-collapsible-panel settings-collapse" style="border-left:4px solid ${condition.color};"${defaultOpen ? " open" : ""}>
      <summary>
        <div>
          <div class="drive-card-title">${escape(condition.label)}</div>
          <div class="drive-card-sub">${escape(condition.value)}</div>
        </div>
        <span style="margin-left:auto;font-size:.78rem;color:${condition.color};font-weight:600;">${escape(condition.value)}</span>
      </summary>
      <div class="drive-collapsible-body">
        ${condition.hint ? `<div style="font-size:.78rem;color:var(--muted);">${escape(condition.hint)}</div>` : ""}
        ${shortcutBtn ? `<div style="margin-top:.45rem;">${shortcutBtn}</div>` : ""}
      </div>
    </details>
  `;
}

function launchCheckMsg(text, ok = false) {
  const msg = $("launch-check-msg");
  if (!msg) return;
  msg.textContent = text || "";
  msg.style.color = ok ? "#4caf50" : "#ff4f6d";
}

function renderLaunchCheckReleaseArtifacts(payload = {}, label = "Release Bundle") {
  const sub = $("launch-check-release-sub");
  const summary = $("launch-check-release-summary");
  const details = $("launch-check-release-details");
  const panel = $("launch-check-release-panel");
  const qa = payload.qa_artifacts || payload;
  const qaSummary = qa.summary || {};
  const qaRuns = Array.isArray(qa.qa_runs) ? qa.qa_runs : [];
  const readyText = Object.prototype.hasOwnProperty.call(payload, "ready")
    ? (payload.ready ? "ready" : "not ready")
    : (payload.ok === false ? "failed" : "indexed");
  const bundlePath = payload.bundle_path || payload.latest_bundle_path || "";
  if (panel) panel.open = true;
  if (sub) sub.textContent = `${label}：${readyText}${bundlePath ? ` · ${bundlePath}` : ""}`;
  if (summary) {
    const rows = [
      ["狀態", payload.status || readyText],
      ["Release bundle", bundlePath || "-"],
      ["QA runs", String(qaSummary.qa_run_count ?? qaSummary.run_count ?? qaRuns.length ?? "-")],
      ["QA artifacts", String(qaSummary.artifact_count ?? "-")],
    ];
    summary.innerHTML = rows.map(([name, value]) => `
      <div class="health-summary-card">
        <span>${sanitize(name)}</span>
        <strong>${sanitize(value)}</strong>
      </div>
    `).join("");
  }
  if (details) details.textContent = JSON.stringify(payload, null, 2);
}

async function refreshLaunchCheckQaArtifacts() {
  if (currentUser !== "root") return;
  launchCheckMsg("正在刷新 QA Artifacts...", true);
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/root/qa-artifacts/index?limit=500", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" },
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || json.ok === false) throw new Error(json.msg || `HTTP ${res.status}`);
    renderLaunchCheckReleaseArtifacts(json, "QA Artifacts");
    const count = json.summary?.artifact_count ?? 0;
    const runs = json.summary?.qa_run_count ?? 0;
    launchCheckMsg(`QA Artifacts 已刷新：${count} artifacts，${runs} QA runs`, true);
  } catch (err) {
    launchCheckMsg(`QA Artifacts 刷新失敗：${err && err.message ? err.message : "未知錯誤"}`, false);
  }
}

async function createLaunchCheckReleaseBundle() {
  if (currentUser !== "root") return;
  launchCheckMsg("正在產生 Release Bundle...", true);
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/root/production-release/bundle", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify({ mark_ready: true }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || json.ok === false) throw new Error(json.msg || json.status || `HTTP ${res.status}`);
    renderLaunchCheckReleaseArtifacts(json, "Release Bundle");
    launchCheckMsg(`Release Bundle 已產生：${json.bundle_path || json.status || "完成"}`, true);
    await loadLaunchCheck();
  } catch (err) {
    launchCheckMsg(`Release Bundle 產生失敗：${err && err.message ? err.message : "未知錯誤"}`, false);
  }
}

function launchCheckCardMarkup(reportType, reportRow, missing, failed, idx) {
  const verificationReasonLabel = (reason) => {
    switch (String(reason || "").trim()) {
      case "missing_signature":
        return "unsigned_report";
      case "signature_mismatch":
        return "signature_mismatch";
      case "report_hash_mismatch":
        return "report_hash_mismatch";
      case "invalid_report_json":
        return "invalid_report_json";
      case "invalid_raw_report_json":
        return "invalid_raw_report_json";
      case "report_type_mismatch":
        return "report_type_mismatch";
      case "target_commit_mismatch":
        return "target_commit_mismatch";
      case "target_branch_mismatch":
        return "target_branch_mismatch";
      case "server_mode_mismatch":
        return "server_mode_mismatch";
      case "key_version_mismatch":
        return "key_version_mismatch";
      case "missing_raw_report_json":
        return "missing_raw_report_json";
      default:
        return String(reason || "").trim() || "unknown_verification_error";
    }
  };
  const meta = LAUNCH_CHECK_REPORT_META[reportType] || {
    label: reportType,
    purpose: "（未登錄的 report 類型）",
    generator: "（請查 docs/server_mode_v2/03_production_gate_playbook.md）",
    tip: "—",
  };
  let statusColor = "#4caf50";
  let statusIcon = "✓";
  let statusLabel = "通過";
  let stateNote = "";
  if (missing) {
    statusColor = "#ff4f6d";
    statusIcon = "❌";
    statusLabel = "缺少報告";
    stateNote = "這份報告沒有任何上傳紀錄；請依下方 generator 產生並上傳。";
  } else if (failed) {
    statusColor = "#ff4f6d";
    statusIcon = "❌";
    statusLabel = "最新報告未通過";
    const reasons = [];
    if (!reportRow || !reportRow.pass) reasons.push("test_result 非 pass");
    if (reportRow && Number(reportRow.critical_findings_count || 0) > 0) reasons.push(`critical=${reportRow.critical_findings_count}`);
    if (reportRow && Number(reportRow.high_findings_count || 0) > 0) reasons.push(`high=${reportRow.high_findings_count}`);
    if (reportRow && !reportRow.report_hash) reasons.push("缺 report_hash");
    if (reportRow && reportRow.signature_valid === false) reasons.push(`驗簽失敗(${verificationReasonLabel(reportRow.verification_reason)})`);
    if (reportRow && String(reportRow.trust_level || "").trim() && String(reportRow.trust_level || "").trim() !== "verified") reasons.push(`未驗證報告(${String(reportRow.trust_level || "").trim()})`);
    if (reportRow && reportRow.target_match === false) reasons.push(`目標不符(${verificationReasonLabel(reportRow.target_verification_reason)})`);
    if (reasons.length) stateNote = `失敗原因：${reasons.join("、")}。修完後重跑並上傳新一份。`;
    else stateNote = "請看上傳的 report payload 內容，找出不通過的欄位。";
  } else if (reportRow) {
    stateNote = `最近通過：${reportRow.created_at || "-"}（commit ${String(reportRow.target_commit || "").slice(0, 8) || "-"}）`;
  }
  const escape = (text) => String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const shortcut = meta.shortcut;
  const defaultOpen = !!missing || !!failed;
  const shortcutBtn = shortcut
    ? `<button class="btn btn-sm" type="button" data-launch-shortcut="report-${idx}" style="margin-top:.5rem;font-size:.72rem;padding:.22rem .55rem;">${escape(shortcut.label || (shortcut.kind === "ui" ? "前往 UI" : "開啟 playbook"))}</button>`
    : "";
  const uploadBtn = `<button class="btn btn-sm" type="button" data-launch-upload="${escape(reportType)}" style="margin-top:.5rem;font-size:.72rem;padding:.22rem .55rem;">上傳報告</button>`;
  return `
    <details class="drive-collapsible-panel settings-collapse" style="border-left:4px solid ${statusColor};"${defaultOpen ? " open" : ""}>
      <summary>
        <div>
          <div class="drive-card-title" style="color:${statusColor};">${statusIcon} ${escape(meta.label)}</div>
          <div class="drive-card-sub">${escape(reportType)}${stateNote ? `｜${escape(stateNote)}` : ""}</div>
        </div>
        <span style="margin-left:auto;font-size:.78rem;color:${statusColor};font-weight:600;">${escape(statusLabel)}</span>
      </summary>
      <div class="drive-collapsible-body">
        <div style="color:var(--text);"><strong>用途</strong>：${escape(meta.purpose)}</div>
        <div style="margin-top:.25rem;"><strong>產生方式</strong>：<code style="font-size:.7rem;">${escape(meta.generator)}</code></div>
        ${meta.defaultOutput ? `<div style="margin-top:.25rem;"><strong>預設放置</strong>：<code style="font-size:.7rem;">${escape(meta.defaultOutput)}</code></div>` : ""}
        <div style="margin-top:.25rem;"><strong>失敗對策</strong>：${escape(meta.tip)}</div>
        ${stateNote ? `<div style="margin-top:.4rem;color:${statusColor};">⤷ ${escape(stateNote)}</div>` : ""}
        <div style="display:flex;gap:.45rem;flex-wrap:wrap;align-items:center;">
          ${shortcutBtn}
          ${uploadBtn}
        </div>
      </div>
    </details>
  `;
}

async function submitLaunchCheckReportUpload() {
  const typeSelect = $("launch-check-upload-report-type");
  const textarea = $("launch-check-upload-json");
  const reportType = String(typeSelect?.value || "").trim();
  if (!reportType) {
    launchCheckSetUploadStatus("請先選擇 report 類型", false);
    return;
  }
  let payload;
  try {
    payload = JSON.parse(textarea?.value || "{}");
  } catch (err) {
    launchCheckSetUploadStatus(`JSON 解析失敗：${err.message || "格式錯誤"}`, false);
    return;
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    launchCheckSetUploadStatus("Report JSON 必須是 object", false);
    return;
  }
  if (payload.raw_report == null) {
    launchCheckSetUploadStatus("缺少 raw_report；伺服器需要重算 hash 並驗證簽章", false);
    return;
  }
  if (!String(payload.report_hash || "").startsWith("sha256:")) {
    launchCheckSetUploadStatus("report_hash 必須是 sha256:<64 hex>", false);
    return;
  }
  if (!String(payload.signature || "").startsWith("hmac_sha256:")) {
    launchCheckSetUploadStatus("signature 必須是 hmac_sha256:<hex>", false);
    return;
  }
  payload.report_type = reportType;
  launchCheckSetUploadStatus("上傳中...", true);
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/root/production-report/upload", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf || "",
      },
      body: JSON.stringify(payload),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.ok) throw new Error(json.msg || `HTTP ${res.status}`);
    launchCheckSetUploadStatus(`上傳成功：${json.report_id || reportType}`, true);
    launchCheckMsg(`已上傳 ${reportType} 報告`, true);
    await loadLaunchCheck();
  } catch (err) {
    const message = err && err.message ? err.message : "上傳失敗";
    launchCheckSetUploadStatus(message, false);
    launchCheckMsg(message, false);
  }
}

async function loadLaunchCheck() {
  const list = $("launch-check-list");
  const conditions = $("launch-check-conditions");
  const overall = $("launch-check-overall");
  const summary = $("launch-check-summary");
  if (!list || currentUser !== "root") return;
  list.innerHTML = `<div class="drive-empty">讀取上線前檢查中…</div>`;
  if (conditions) conditions.innerHTML = "";
  if (overall) {
    overall.textContent = "讀取中…";
    overall.style.color = "var(--muted)";
  }
  if (summary) summary.textContent = "";
  launchCheckMsg("");
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const headers = { "X-CSRF-Token": csrf || "" };
    // Pull both endpoints in parallel — we need security center for the
    // settings / readiness / anomaly status lights, and requirements
    // for the 13-report rollup.
    const [reqRes, scRes] = await Promise.all([
      apiFetch(API + "/root/server-mode/requirements", { credentials: "same-origin", headers }),
      apiFetch(API + "/admin/security-center", { credentials: "same-origin", headers }),
    ]);
    const reqJson = await reqRes.json().catch(() => ({}));
    const scJson = await scRes.json().catch(() => ({}));
    const failures = [];
    if (!reqRes.ok || (typeof reqJson.ok === "boolean" && !reqJson.ok && !Array.isArray(reqJson.required))) {
      failures.push(`requirements: ${reqJson.msg || `HTTP ${reqRes.status}`}`);
    }
    if (!scRes.ok || (typeof scJson.ok === "boolean" && !scJson.ok)) {
      failures.push(`security-center: ${scJson.msg || `HTTP ${scRes.status}`}`);
    }
    if (failures.length) {
      throw new Error(failures.join("；"));
    }
    const sc = (scJson.ok && scJson.security_center) ? scJson.security_center : {};

    const required = Array.isArray(reqJson.required) ? reqJson.required : [];
    const missing = new Set(Array.isArray(reqJson.missing) ? reqJson.missing : []);
    const failed = new Set(Array.isArray(reqJson.failed) ? reqJson.failed : []);
    const reports = reqJson.reports && typeof reqJson.reports === "object" ? reqJson.reports : {};
    const passingReports = required.filter((key) => !missing.has(key) && !failed.has(key)).length;

    // ── A. 其他上線前條件（status lights） ───────────────────────────
    const conditionList = launchCheckConditionList(sc, reqJson);
    const conditionsRedCount = conditionList.filter((c) => c.color === "#ff4f6d").length;
    const conditionsYellowCount = conditionList.filter((c) => c.color === "#ffb74d").length;
    if (conditions) {
      conditions.innerHTML = conditionList
        .map((cond, idx) => launchCheckConditionMarkup(cond, idx))
        .join("");
      conditionList.forEach((cond, idx) => {
        if (!cond.shortcut) return;
        const btn = conditions.querySelector(`[data-launch-shortcut="cond-${idx}"]`);
        const handler = launchCheckShortcutHandler(cond.shortcut);
        if (btn && handler) btn.addEventListener("click", handler);
      });
    }

    // ── B. 13 reports cards + shortcuts ─────────────────────────────
    if (overall) {
      const allReportsPassed = reqJson.ok === true && missing.size === 0 && failed.size === 0;
      const allConditionsPassed = conditionsRedCount === 0;
      const allGreen = allReportsPassed && allConditionsPassed;
      const parts = [];
      if (allGreen) {
        overall.textContent = `✓ 條件全綠 + 13 份報告全通過，可進 production`;
      } else {
        const fragments = [];
        if (!allConditionsPassed) fragments.push(`A 區紅燈 ${conditionsRedCount}`);
        if (conditionsYellowCount) fragments.push(`A 區黃燈 ${conditionsYellowCount}`);
        if (!allReportsPassed) fragments.push(`B 區 ${passingReports}/${required.length}`);
        overall.textContent = `❌ ${fragments.join(" · ")} — 還不能進 production`;
      }
      overall.style.color = allGreen ? "#4caf50" : "#ff4f6d";
    }
    if (summary) {
      const parts = [];
      if (missing.size) parts.push(`B 區缺 ${missing.size} 份`);
      if (failed.size) parts.push(`B 區不通過 ${failed.size} 份`);
      if (conditionsRedCount) parts.push(`A 區 ${conditionsRedCount} 紅`);
      if (conditionsYellowCount) parts.push(`A 區 ${conditionsYellowCount} 黃`);
      summary.textContent = parts.join("、");
    }
    if (!required.length) {
      list.innerHTML = `<div class="drive-empty">沒有定義 production gate report — 請檢查 services/snapshots/schema.py:PRODUCTION_REQUIRED_REPORT_TYPES</div>`;
      return;
    }
    list.innerHTML = required
      .map((reportType, idx) => launchCheckCardMarkup(reportType, reports[reportType] || null, missing.has(reportType), failed.has(reportType), idx))
      .join("");
    required.forEach((reportType, idx) => {
      const meta = LAUNCH_CHECK_REPORT_META[reportType];
      if (!meta || !meta.shortcut) return;
      const btn = list.querySelector(`[data-launch-shortcut="report-${idx}"]`);
      const handler = launchCheckShortcutHandler(meta.shortcut);
      if (btn && handler) btn.addEventListener("click", handler);
    });
    required.forEach((reportType) => {
      const btn = list.querySelector(`[data-launch-upload="${reportType}"]`);
      if (btn) btn.addEventListener("click", () => openLaunchCheckUpload(reportType));
    });
  } catch (err) {
    if (overall) {
      overall.textContent = "讀取失敗";
      overall.style.color = "#ff4f6d";
    }
    list.innerHTML = `<div class="drive-empty">上線前檢查讀取失敗：${err && err.message ? err.message : "未知錯誤"}</div>`;
    launchCheckMsg(err && err.message ? err.message : "未知錯誤", false);
  }
}

async function loadServerModeLogs() {
  const host = $("server-mode-logs");
  if (!host || currentUser !== "root") return;
  try {
    const res = await apiFetch(API + "/root/server-mode/logs?limit=5", { credentials: "same-origin" });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) throw new Error(json.msg || "讀取失敗");
    const logs = Array.isArray(json.logs) ? json.logs : [];
    host.innerHTML = `
      <div><strong>最近模式切換</strong></div>
      ${logs.length ? logs.map((row) => `
        <div>${row.created_at || "-"}：${row.from_mode || "-"} → ${row.to_mode || "-"} · ${row.success ? "成功" : "失敗"} · ${row.reason || ""}</div>
      `).join("") : "<div>尚無紀錄</div>"}
    `;
  } catch (err) {
    host.textContent = `模式切換紀錄讀取失敗：${err.message || "未知錯誤"}`;
  }
}

async function loadInternalTestTokenStatus() {
  if (currentUser !== "root") return;
  const status = $("internal-test-token-status");
  if (!status) return;
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/admin/access-controls", {
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf || "" }
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) throw new Error(json.msg || "讀取失敗");
    const access = json.access_controls || {};
    const configured = !!access.internal_test_token_configured;
    const expired = !!access.internal_test_token_expired;
    const expires = access.internal_test_token_expires_at || "-";
    const boundId = Number(access.internal_test_token_user_id || 0);
    const boundUser = String(access.internal_test_token_username || "").trim();
    const boundText = boundId ? `，綁定帳號：${boundUser || `user #${boundId}`}` : "，尚未綁定帳號";
    status.textContent = configured ? `已設定，${expired ? "已過期" : "有效"}，到期：${expires}${boundText}` : "尚未設定內測 token";
    status.style.color = configured && !expired ? "#4caf50" : "var(--muted)";
  } catch (err) {
    status.textContent = err.message || "內測 token 狀態讀取失敗";
    status.style.color = "#ff4f6d";
  }
}

function testerTokenMsg(text, ok = true) {
  const msg = $("tester-token-msg");
  if (!msg) return;
  msg.textContent = text || "";
  msg.style.color = ok ? "#4caf50" : "#ff4f6d";
}

function testerTokenListMarkup(tokens) {
  if (!Array.isArray(tokens) || !tokens.length) return `<div class="drive-empty">尚無 Server Mode v2 tester token</div>`;
  return tokens.map((testerEntry) => {
    const revoked = !!testerEntry.revoked_at;
    const routes = Array.isArray(testerEntry.allowed_routes) ? testerEntry.allowed_routes.join(", ") : "-";
    return `<div class="drive-file-row">
      <div>
        <strong>${sanitize(testerEntry.id || "-")}</strong>
        <div class="drive-card-sub">user #${sanitize(String(testerEntry.tester_user_id || "-"))} · 到期 ${sanitize(testerEntry.expires_at || "-")} · ${revoked ? "已撤銷" : "有效"}</div>
        <div class="drive-card-sub">routes: ${sanitize(routes || "-")} · rpm ${sanitize(String(testerEntry.max_requests_per_minute || "-"))}</div>
      </div>
      ${revoked ? "" : `<button class="btn" type="button" data-revoke-tester-entry="${sanitize(testerEntry.id || "")}">撤銷</button>`}
    </div>`;
  }).join("");
}

async function loadTesterTokens() {
  if (currentUser !== "root" || !$("tester-token-list")) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/root/tester-token/list", {
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf || "" }
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    testerTokenMsg(json.msg || "tester token 清單讀取失敗", false);
    return;
  }
  const list = $("tester-token-list");
  list.innerHTML = testerTokenListMarkup(json.tokens || []);
  list.querySelectorAll("[data-revoke-tester-entry]").forEach((btn) => {
    btn.addEventListener("click", () => revokeTesterToken(btn.dataset.revokeTesterEntry || ""));
  });
}

async function createTesterToken() {
  if (currentUser !== "root") return;
  const userId = Number($("tester-token-user-id")?.value || 0);
  const expiresAt = $("tester-token-expires-at")?.value || "";
  if (!userId || !expiresAt) {
    testerTokenMsg("請填測試員 user id 與到期時間", false);
    return;
  }
  const routes = ($("tester-token-routes")?.value || "/api/tester")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/root/tester-token/create", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({
      tester_user_id: userId,
      expires_at: expiresAt,
      allowed_routes: routes,
      max_requests_per_minute: Number($("tester-token-rpm")?.value || 60),
      can_modify_own_role: !!$("tester-token-can-role")?.checked,
      can_modify_own_points: !!$("tester-token-can-points")?.checked,
      can_run_security_tests: !!$("tester-token-can-security")?.checked
    })
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    testerTokenMsg(json.msg || "建立 tester token 失敗", false);
    return;
  }
  const wrap = $("tester-token-created-wrap");
  const out = $("tester-token-created");
  const usage = $("tester-token-usage-wrap");
  if (wrap) wrap.style.display = "block";
  if (usage) usage.style.display = "block";
  if (out) {
    out.value = json.token || "";
    out.focus();
    out.select();
  }
  testerTokenMsg(`tester token 已建立：${json.token_id || "-"}`, true);
  await loadTesterTokens();
}

async function revokeTesterToken(tokenId) {
  if (currentUser !== "root" || !tokenId) return;
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/root/tester-token/revoke", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ token_id: tokenId, reason: "root revoked from UI" })
  });
  const json = await res.json().catch(() => ({}));
  testerTokenMsg(json.ok ? "tester token 已撤銷" : (json.msg || "撤銷失敗"), !!json.ok);
  await loadTesterTokens();
}

async function rotateInternalTestToken() {
  const confirmText = $("internal-test-token-confirm")?.value || "";
  const targetUserId = parseInt($("internal-test-token-user-id")?.value || "0", 10);
  const targetUsername = ($("internal-test-token-username")?.value || "").trim();
  const msg = $("internal-test-token-msg");
  if (confirmText !== "ROTATE_INTERNAL_TEST_TOKEN") {
    if (msg) flash(msg, "確認字串必須等於 ROTATE_INTERNAL_TEST_TOKEN", false);
    return;
  }
  if (!targetUserId && !targetUsername) {
    if (msg) flash(msg, "請至少填一個綁定帳號（user id 或帳號名稱）", false);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const ttl = parseInt($("internal-test-token-ttl")?.value || "1440", 10);
  const res = await apiFetch(API + "/admin/access-controls/internal-test-token", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({
      confirm: confirmText,
      ttl_minutes: ttl,
      target_user_id: targetUserId || null,
      target_username: targetUsername || "",
    })
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    if (msg) flash(msg, json.msg || "產生內測 token 失敗", false);
    return;
  }
  const outWrap = $("internal-test-token-output-wrap");
  const out = $("internal-test-token-output");
  const usage = $("internal-test-token-usage-wrap");
  if (outWrap) outWrap.style.display = "block";
  if (usage) usage.style.display = "block";
  if (out) {
    out.value = json.token || "";
    out.focus();
    out.select();
  }
  if ($("internal-test-token-confirm")) $("internal-test-token-confirm").value = "";
  if (msg) flash(msg, `內測 token 已產生，綁定 ${json.target_username || (json.target_user_id ? `user #${json.target_user_id}` : "指定帳號")}，到期：${json.expires_at || "-"}`, true);
  await loadInternalTestTokenStatus();
}

async function applyServerMode() {
  const target = $("server-mode-select")?.value || "dev_ready";
  const confirmText = $("server-mode-confirm")?.value || "";
  const notes = $("server-mode-notes")?.value || "";
  const expectedConfirm = serverModeConfirmPhrase(target);
  if (confirmText !== expectedConfirm) {
    alert(`切換到 ${target} 必須在確認欄輸入 ${expectedConfirm}`);
    return;
  }
  await fetchCsrfToken({ force: true });
  const csrf = getCsrfToken();
  const res = await apiFetch(API + "/root/server-mode/switch", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    body: JSON.stringify({ mode: target, confirm: confirmText, notes })
  });
  const json = await res.json().catch(() => ({}));
  const status = $("server-mode-status");
  if (status) {
    status.textContent = json.ok ? "伺服器模式已更新" : (json.msg || "伺服器模式更新失敗");
    status.style.color = json.ok ? "#4caf50" : "#ff4f6d";
  }
  if (json.ok) {
    if (json.profile) {
      applySecurityProfileDataToInputs(json.profile, "s");
      applySecurityProfileDataToInputs(json.profile, "sc");
    }
    if ($("server-mode-confirm")) $("server-mode-confirm").value = "";
    await loadServerMode();
    await loadSecurityCenter();
    await loadSettings();
  }
}
