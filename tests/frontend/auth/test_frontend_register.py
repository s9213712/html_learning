from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_register_form_surfaces_field_level_errors_without_full_reset():
    auth = (ROOT / "public" / "js" / "40-auth-users.js").read_text(encoding="utf-8")
    bootstrap = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")
    styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    assert "const REGISTER_FIELD_ID_MAP = {" in auth
    assert "function clearRegisterFieldErrors()" in auth
    assert "function markRegisterFieldError(field)" in auth
    assert "function guessRegisterFieldFromMessage(message)" in auth
    assert "function shouldClearRegisterPasswords(field, message)" in auth
    assert "function showRegisterError(message, field = \"\", { focus = true } = {})" in auth
    assert 'showRegisterError(json.msg || "註冊失敗", json.field || "");' in auth
    assert 'if (!pw) { showRegisterError("請輸入密碼", "password"); return; }' in auth
    assert 'if (pw !== pwConfirm) { showRegisterError("兩次密碼輸入不一致", "password_confirm"); return; }' in auth
    assert 'if (field === "password" || field === "password_confirm") return true;' in auth
    assert 'el.addEventListener("input", clearHandler);' in auth
    assert 'if (typeof bindRegisterFieldHelpers === "function") bindRegisterFieldHelpers();' in bootstrap
    assert bootstrap.index('if (typeof bindRegisterFieldHelpers === "function") bindRegisterFieldHelpers();') < bootstrap.index("await loadSiteConfig();")
    assert ".field-error {" in styles
    assert "box-shadow: 0 0 0 1px rgba(255,79,109,.24);" in styles
