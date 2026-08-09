from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[3]


def _block(css, selector):
    start = css.index(selector)
    open_brace = css.index("{", start)
    close_brace = css.index("}", open_brace)
    return css[open_brace + 1:close_brace]


def _rule_block(css, selector):
    pattern = re.compile(rf"^\s*{re.escape(selector)}\s*\{{(?P<body>.*?)^\s*\}}", re.MULTILINE | re.DOTALL)
    match = pattern.search(css)
    assert match, f"{selector} rule not found"
    return match.group("body")


def test_global_buttons_are_content_sized_and_wrap_long_labels():
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    btn = _rule_block(css, ".btn")

    assert "width: auto;" in btn
    assert "max-width: 100%;" in btn
    assert "display: inline-flex;" in btn
    assert "white-space: normal;" in btn
    assert "overflow-wrap: anywhere;" in btn
    assert "font-size: .84rem;" in btn
    assert "padding: .5rem .78rem;" in btn


def test_toolbar_and_tabs_do_not_force_oversized_buttons():
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    toolbar_btn = _block(css, ".admin-toolbar .btn")
    assert "width: auto;" in toolbar_btn
    assert "flex: 0 1 auto;" in toolbar_btn

    tabs = _block(css, ".tabs:not(.sidebar-nav)")
    assert "flex-wrap: wrap;" in tabs

    tab = _rule_block(css, ".tab")
    assert "flex: 1 1 7rem;" in tab
    assert "white-space: normal;" in tab
    assert "overflow-wrap: anywhere;" in tab


def test_small_button_class_uses_compact_dimensions():
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    small = _block(css, ".btn-sm,")

    assert "width: auto;" in small
    assert "min-height: 1.95rem;" in small
    assert "font-size: .76rem;" in small
    assert "overflow-wrap: anywhere;" in small


def test_pw_toggle_meets_wcag_touch_target_size():
    """Issue #168 regression — .pw-toggle was padding:.2rem (≈16x16),
    below the 24x24 WCAG 2.5.5 minimum and far below the 44x44 target.
    """
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    pw = _block(css, ".pw-toggle {")
    assert "min-width: 2.75rem;" in pw, ".pw-toggle min-width must stay at the 44px touch target baseline"
    assert "min-height: 2.75rem;" in pw, ".pw-toggle min-height must stay at the 44px touch target baseline"
    assert "padding: .5rem;" in pw, ".pw-toggle padding bumped to .5rem"
    assert "place-items: center;" in pw

    # Focus outline for keyboard accessibility
    focus = _block(css, ".pw-toggle:focus-visible")
    assert "outline: 2px solid var(--accent);" in focus


def test_chat_action_buttons_meet_wcag_touch_target_size():
    """Issue #168 regression — .chat-report-btn / .chat-delete-btn were
    padding:.2rem .45rem; font-size:.68rem — about 16x16 effective.
    """
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    chat = _block(css, ".chat-report-btn,")
    assert "min-height: 2.4rem;" in chat
    assert "min-width: 2.4rem;" in chat
    assert "padding: .45rem .65rem;" in chat
    assert "font-size: .75rem;" in chat

    focus = _block(css, ".chat-report-btn:focus-visible,")
    assert "outline: 2px solid var(--accent);" in focus


def test_all_mobile_form_controls_meet_44px_touch_target():
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    mobile_targets = css.split(
        "/* Keep every mobile control at the shared 44px accessibility target. */",
        1,
    )[1]

    assert "@media (max-width: 768px)" in mobile_targets
    assert "min-height: 44px !important;" in mobile_targets
    assert "min-width: 44px !important;" in mobile_targets
    assert 'input[type="checkbox"]' in mobile_targets
    assert 'input[type="radio"]' in mobile_targets


def test_root_quick_settings_button_meets_touch_target_size():
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    root_settings = _block(css, ".root-module-settings-btn {")
    assert "width: 2.75rem;" in root_settings
    assert "height: 2.75rem;" in root_settings
    assert "min-width: 2.75rem;" in root_settings
    assert "min-height: 2.75rem;" in root_settings


def test_muted_color_passes_wcag_aa_contrast_on_dark_bg():
    """Issue #168 regression — --muted #8888aa on --bg #0f0f1a was
    contrast 4.0:1, below WCAG AA's 4.5:1 for small text.
    The fix raises it to #a8b5d4 (~5.0:1).
    """
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    # Match `--muted: #...;` allowing leading whitespace
    pattern = re.compile(r"--muted:\s*(#[0-9a-fA-F]{6})\s*;")
    match = pattern.search(css)
    assert match, "--muted CSS variable not found"
    assert match.group(1).lower() == "#a8b5d4", (
        f"--muted regressed to {match.group(1)!r}; should be #a8b5d4 "
        "(see issue #168). Run a contrast checker against --bg #0f0f1a "
        "if changing — must be >= 4.5:1 for WCAG AA."
    )


def test_form_inputs_pin_to_16px_on_touch_devices_no_ios_zoom():
    """Issue #175 regression — iOS Safari auto-zooms when a focused
    form control's font-size < 16px. The fix is a `@media (hover: none)
    and (pointer: coarse)` rule pinning input/select/textarea to 1rem.
    """
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    # The exact media query from the fix
    assert "@media (hover: none) and (pointer: coarse)" in css, (
        "issue #175 fix relies on `@media (hover: none) and (pointer: coarse)` — "
        "if removed, iOS Safari auto-zooms again on every form field"
    )
    # Find the rule body and assert it pins font-size to 1rem
    pattern = re.compile(
        r"@media \(hover: none\) and \(pointer: coarse\) \{\s*"
        r"input:not\(\[type=\"checkbox\"\]\):not\(\[type=\"radio\"\]\),\s*"
        r"select,\s*"
        r"textarea \{\s*"
        r"font-size: 1rem;",
        re.DOTALL,
    )
    assert pattern.search(css), (
        "iOS auto-zoom mitigation rule body does not match expected shape — "
        "see issue #175 for the exact contract"
    )
