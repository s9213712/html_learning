import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_main_app_has_mobile_responsive_overrides():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    # Match any cache-bust version of styles.css — the actual mobile
    # behavior we test is in the CSS body below, not the version stamp.
    assert re.search(r"/styles\.css\?v=", index_html)
    assert "Mobile ergonomics pass" in css
    assert "@media (max-width: 860px)" in css
    assert "@media (max-width: 720px)" in css
    assert "-webkit-text-size-adjust: 100%;" in css
    assert ".app-action-bar" in css
    assert "left: .45rem;" in css
    assert "right: .45rem;" in css
    assert ".sidebar-nav.tabs" in css
    assert "overflow-x: auto;" in css
    assert "Mobile uses the same config-driven sidebar as desktop" in css
    assert "body.sidebar-collapsed .app-sidebar" in css
    assert "width: calc(100vw - 3.55rem);" in css
    assert "body.sidebar-collapsed .sidebar-nav .tab" in css
    assert "width: 2.75rem;" in css
    assert "min-height: 2.75rem;" in css
    assert "body.sidebar-collapsed .sidebar-icon-svg" in css
    assert "collapseSidebarAfterMobileNavigation" in (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    assert ".settings-option-grid" in css
    assert "grid-template-columns: 1fr !important;" in css
    assert ".drive-file-row" in css
    assert ".table-scroll-wrap" in css
    assert ".health-hero" in css
    assert ".health-row-value" in css
    assert "white-space: normal;" in css
    assert ".system-resource-board-header" in css
    assert ".system-resource-gauges" in css
    assert ".server-env-kv-grid" in css
    assert "overscroll-behavior-inline: contain;" in css
    assert re.search(
        r'id="module-system"[\s\S]*?class="tabs system-operation-tabs"[\s\S]*?id="tab-system-health"',
        index_html,
    )
    assert ".tabs.system-operation-tabs" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    assert "overflow-x: visible;" in css
    assert ".trading-indicator-controls" in css
    assert ".trading-bot-tabs" in css
    assert ".chess-board" in css
    assert '<div class="table-scroll-wrap">' in index_html
    assert '<div class="admin-toolbar" style="display:flex;gap:.5rem;align-items:center;">' not in index_html
    assert '<div class="admin-toolbar" style="display:flex;gap:.5rem;align-items:center;grid-template-columns:auto auto auto; margin-bottom:.65rem;">' not in index_html
    assert '<div class="admin-toolbar" style="grid-template-columns:1fr;margin-bottom:0;">' not in index_html
    assert '<div class="admin-toolbar" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin-bottom:.65rem;">' not in index_html
    assert '<div class="admin-toolbar" style="grid-template-columns:repeat(2,1fr);margin-bottom:.65rem;">' not in index_html
    assert "min-width: 680px;" in css


def test_workflow_editor_has_mobile_responsive_overrides():
    css = (ROOT / "public" / "trading-workflow-editor.css").read_text(encoding="utf-8")

    assert "@media (max-width: 720px)" in css
    assert ".top-actions" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    assert ".tool-grid" in css
    assert "max-height: 40dvh;" in css
    assert ".flow" in css
    assert ".logic-node" in css
    assert "@media (max-width: 460px)" in css
    assert ".graph-panel { overflow: auto; }" in css
    assert "touch-action: pan-x pan-y;" in css
    assert "min-width: 1450px;" not in css
    assert "min-width: 980px;" not in css
    assert "min-width: 100%;" in css


def test_root_accounts_tabs_and_user_actions_stay_in_mobile_viewport():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    users_js = (ROOT / "public" / "js" / "10-users.js").read_text(encoding="utf-8")

    accounts_section = index_html.split('id="module-accounts"', 1)[1].split('id="module-system"', 1)[0]
    assert 'class="tabs accounts-operation-tabs"' in accounts_section
    assert 'id="tab-violations"' in accounts_section
    assert 'id="tab-reports"' in accounts_section

    responsive_accounts = css.split(
        "/* Root account management keeps navigation and row actions in the viewport.",
        1,
    )[1]
    assert "@media (max-width: 768px)" in responsive_accounts
    assert "#module-accounts .accounts-operation-tabs" in responsive_accounts
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in responsive_accounts
    assert "overflow-x: visible;" in responsive_accounts
    assert "#sec-users .user-table" in responsive_accounts
    assert "min-width: 0;" in responsive_accounts
    assert "#sec-users .user-table td::before" in responsive_accounts
    assert "content: attr(data-label);" in responsive_accounts
    assert "#sec-users .user-table .admin-user-action-toggle" in responsive_accounts
    assert "#sec-users .user-table .admin-user-actions.open .admin-user-action-menu" in responsive_accounts

    for label in ("選取", "ID", "在線", "帳號", "暱稱", "真實姓名", "角色", "會員等級", "狀態", "違規", "行為"):
        assert f'"{label}"' in users_js
