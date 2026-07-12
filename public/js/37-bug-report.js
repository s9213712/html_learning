'use strict';

let bugReportCloseTimer = null;

function showBugReportDialog() {
  if (bugReportCloseTimer) clearTimeout(bugReportCloseTimer);
  bugReportCloseTimer = null;
  const overlay = $("bug-report-overlay");
  if (!overlay) return;
  const page = window.location.pathname + window.location.hash;
  const title = $("bug-report-title");
  const device = $("bug-report-device");
  if (device && !device.dataset.touched) {
    device.value = window.matchMedia?.("(max-width: 720px)")?.matches ? "mobile" : "desktop";
  }
  if (title && !title.value) title.value = "";
  const msg = $("bug-report-msg");
  if (msg) msg.className = "msg";
  overlay.classList.add("show");
  if (title) title.focus();
  overlay.dataset.page = page;
}

function hideBugReportDialog() {
  const overlay = $("bug-report-overlay");
  if (overlay) overlay.classList.remove("show");
}

function setBugReportMsg(text, ok) {
  const el = $("bug-report-msg");
  if (!el) return;
  el.textContent = text || "";
  el.className = text ? "msg show " + (ok ? "ok" : "err") : "msg";
}

async function submitBugReport() {
  const payload = {
    severity: $("bug-report-severity")?.value || "medium",
    device: $("bug-report-device")?.value || "unknown",
    feature: $("bug-report-feature")?.value || "other",
    title: $("bug-report-title")?.value.trim() || "",
    description: $("bug-report-description")?.value.trim() || "",
    steps: $("bug-report-steps")?.value.trim() || "",
    expected: $("bug-report-expected")?.value.trim() || "",
    actual: $("bug-report-actual")?.value.trim() || "",
    page: $("bug-report-overlay")?.dataset.page || (window.location.pathname + window.location.hash)
  };
  if (!payload.title || !payload.description) {
    setBugReportMsg("請填寫標題與問題描述", false);
    return;
  }
  try {
    await fetchCsrfToken({ force: true });
    const csrf = getCsrfToken();
    const res = await apiFetch(API + "/bug-reports", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
      body: JSON.stringify(payload)
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) {
      setBugReportMsg(json.msg || "Bug 回報失敗", false);
      return;
    }
    ["bug-report-title", "bug-report-description", "bug-report-steps", "bug-report-expected", "bug-report-actual"].forEach((id) => {
      const el = $(id);
      if (el) el.value = "";
    });
    setBugReportMsg(`已建立回報：${json.report_id}`, true);
    bugReportCloseTimer = setTimeout(() => {
      bugReportCloseTimer = null;
      hideBugReportDialog();
    }, 900);
  } catch (err) {
    if (err?.name === "AbortError") return;
    reportFrontendFailure("bug-report-submit", err);
    setBugReportMsg(err?.message || "Bug 回報失敗，請稍後再試。", false);
  }
}

function resetBugReportAccountState() {
  if (bugReportCloseTimer) clearTimeout(bugReportCloseTimer);
  bugReportCloseTimer = null;
  hideBugReportDialog();
  [
    "bug-report-title", "bug-report-description", "bug-report-steps",
    "bug-report-expected", "bug-report-actual",
  ].forEach((id) => {
    const el = $(id);
    if (el) el.value = "";
  });
  const device = $("bug-report-device");
  if (device) delete device.dataset.touched;
  const overlay = $("bug-report-overlay");
  if (overlay) delete overlay.dataset.page;
  setBugReportMsg("", true);
}

document.addEventListener("hackme:account-context-changed", resetBugReportAccountState);
