from __future__ import annotations

from scripts.testing import playwright_browser_video_compat as probe


def check(browser: str, viewport: str, *, ok: bool, skipped: bool = False) -> dict:
    return {
        "browser": browser,
        "viewport": viewport,
        "ok": ok,
        "skipped": skipped,
        "skip_reason": "browser_dependency_missing" if skipped else "",
    }


def test_legacy_mode_allows_dependency_skip_when_remaining_check_passes() -> None:
    coverage = probe.evaluate_browser_coverage(
        [
            check("chromium", "desktop", ok=True),
            check("firefox", "desktop", ok=False, skipped=True),
        ],
        requested_browsers=["chromium", "firefox"],
        include_mobile=False,
        require_all_browsers=False,
    )

    assert coverage["ok"] is True
    assert coverage["mode"] == "allow_dependency_skips"
    assert coverage["skipped"] == [
        {"browser": "firefox", "viewport": "desktop", "reason": "browser_dependency_missing"}
    ]


def test_formal_mode_rejects_dependency_skip() -> None:
    coverage = probe.evaluate_browser_coverage(
        [
            check("chromium", "desktop", ok=True),
            check("firefox", "desktop", ok=False, skipped=True),
        ],
        requested_browsers=["chromium", "firefox"],
        include_mobile=False,
        require_all_browsers=True,
    )

    assert coverage["ok"] is False
    assert coverage["mode"] == "require_all_browsers"
    assert coverage["failed"][0]["browser"] == "firefox"


def test_formal_mode_rejects_runnable_browser_launch_failure() -> None:
    failed = check("webkit", "desktop", ok=False)
    failed["exception"] = "Error: browser launch failed"

    coverage = probe.evaluate_browser_coverage(
        [failed],
        requested_browsers=["webkit"],
        include_mobile=False,
        require_all_browsers=True,
    )

    assert coverage["ok"] is False
    assert coverage["skipped"] == []
    assert coverage["failed"] == [
        {
            "browser": "webkit",
            "viewport": "desktop",
            "skipped": False,
            "exception": "Error: browser launch failed",
        }
    ]


def test_formal_mode_requires_every_requested_browser_and_viewport() -> None:
    coverage = probe.evaluate_browser_coverage(
        [
            check("chromium", "desktop", ok=True),
            check("chromium", "mobile", ok=True),
            check("firefox", "desktop", ok=True),
        ],
        requested_browsers=["chromium", "firefox"],
        include_mobile=True,
        require_all_browsers=True,
    )

    assert coverage["ok"] is False
    assert coverage["missing"] == [{"browser": "firefox", "viewport": "mobile"}]


def test_formal_desktop_only_mode_passes_when_all_requested_checks_pass() -> None:
    coverage = probe.evaluate_browser_coverage(
        [
            check("chromium", "desktop", ok=True),
            check("firefox", "desktop", ok=True),
            check("webkit", "desktop", ok=True),
        ],
        requested_browsers=["chromium", "firefox", "webkit"],
        include_mobile=False,
        require_all_browsers=True,
    )

    assert coverage["ok"] is True
    assert coverage["expected_check_count"] == 3
    assert coverage["runnable_check_count"] == 3
    assert coverage["missing"] == []
    assert coverage["skipped"] == []


def test_parser_exposes_require_all_browsers_flag_without_changing_default() -> None:
    parser = probe.build_parser()

    assert parser.parse_args([]).require_all_browsers is False
    assert parser.parse_args(["--require-all-browsers"]).require_all_browsers is True
