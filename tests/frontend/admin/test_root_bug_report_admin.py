from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_root_bug_report_admin_is_wired_under_system_management():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    admin_js = (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")
    route_py = (ROOT / "routes" / "bug_reports.py").read_text(encoding="utf-8")

    assert 'id="tab-system-bug-reports"' in index_html
    assert 'id="system-bug-reports-slot"' in index_html
    assert 'id="root-bug-report-list"' in index_html
    assert 'id="root-bug-report-detail"' in index_html
    assert '{ label: "Bug 回報", action: "system:bug-reports" }' in core_js
    assert '"bug-reports"' in admin_js
    assert 'loadRootBugReports();' in admin_js
    assert 'function renderRootBugReportDetail(item)' in admin_js
    assert 'API + "/admin/bug-reports"' in admin_js
    assert 'data-root-bug-report-approve' in admin_js
    assert 'data-root-bug-report-copy-id' in admin_js
    assert 'data-root-bug-report-copy-summary' in admin_js
    assert 'data-root-bug-report-open-announcement' in admin_js
    assert 'data-root-bug-report-publish-announcement' in admin_js
    assert 'id="root-bug-report-review-reward"' in admin_js
    assert 'id="root-bug-report-review-note"' in admin_js
    assert 'id="root-bug-report-announcement-title"' in admin_js
    assert 'id="root-bug-report-announcement-content"' in admin_js
    assert 'function openRootBugReportAnnouncementDraft(reportId)' in admin_js
    assert 'window.prompt("全站公告標題"' not in admin_js
    assert '確定要將這筆 bug 回報發布為全站公告？' not in admin_js
    assert 'detail.classList.add("show");' in admin_js
    assert 'const reviewNote = $("root-bug-report-review-note")?.value || "";' in admin_js
    assert 'reward_points: rewardPoints' in admin_js
    assert 'API + "/community/announcements"' in admin_js
    assert 'tabSystemBugReports.addEventListener("click", () => switchSystemTab("bug-reports"))' in bootstrap_js
    assert 'rootBugReportRefresh.addEventListener("click", loadRootBugReports)' in bootstrap_js
    assert 'if (typeof showBugReportDialog === "function") showBugReportDialog();' in bootstrap_js
    assert 'if (typeof submitBugReport === "function") submitBugReport();' in bootstrap_js
    assert 'if (typeof hideBugReportDialog === "function") hideBugReportDialog();' in bootstrap_js
    assert '"description": item.get("description")' in route_py
    assert '"steps": item.get("steps")' in route_py
    assert '"suggested_reward_points": item.get("suggested_reward_points") or item.get("reward_points")' in route_py
    assert '"user_agent": item.get("request", {}).get("user_agent")' in route_py
    assert '"reward_points" not in data' in route_py
    assert "reward_points = _parse_review_reward_points(data.get(\"reward_points\"), None)" in route_py
    assert 'return json_resp({"ok": False, "msg": "只有 root 可審核 bug 回報"}), 403' in route_py
