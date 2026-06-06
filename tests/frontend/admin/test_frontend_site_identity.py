from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_root_site_identity_controls_are_wired():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    admin_js = (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
    public_py = (ROOT / "routes" / "public.py").read_text(encoding="utf-8")
    settings_py = (ROOT / "services" / "platform" / "settings.py").read_text(encoding="utf-8")

    assert 'id="auth-site-heading"' in index_html
    assert 'id="auth-site-subtitle"' in index_html
    assert 'id="login-success-title"' in index_html
    assert 'id="sidebar-brand-label"' in index_html
    assert 'id="s-site-name"' in index_html
    assert 'id="s-site-document-title"' in index_html
    assert 'id="s-site-login-heading"' in index_html
    assert 'id="s-site-login-subtitle"' in index_html
    assert 'id="s-site-success-heading"' in index_html
    assert 'id="s-site-success-message"' in index_html

    assert 'const SITE_TEXT_DEFAULTS = {' in core_js
    assert 'function renderSiteTextConfig()' in core_js
    assert 'document.title = title;' in core_js
    assert 'welcomeMsg.textContent = siteTextConfigValue("site_success_message");' in core_js

    assert 'if ($("s-site-name")) $("s-site-name").value = s.site_name || "hackme_web";' in admin_js
    assert 'site_success_message: ($("s-site-success-message")?.value || "").trim() || "歡迎回來！",' in admin_js

    assert '"site_name": "hackme_web"' in settings_py
    assert '"site_success_message": "歡迎回來！"' in settings_py
    assert '"site_document_title": settings.get("site_document_title")' in public_py
    assert 'login_msg = str(settings.get("site_success_heading")' in public_py
