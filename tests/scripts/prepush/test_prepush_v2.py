import os
import re
import subprocess
import time
from pathlib import Path

from scripts.prepush import utils
from scripts.prepush.checks import (
    cleanup_check,
    forbidden_paths_check,
    frontend_check,
    local_path_check,
    git_clean_check,
    markdown_links_check,
    pytest_quick_check,
    release_check,
    scripts_index_check,
    secrets_check,
)
from scripts.prepush.context import PrepushContext
from scripts.prepush.result import FAIL, SKIP


ROOT = Path(__file__).resolve().parents[3]


def make_ctx(tmp_path, **kwargs):
    return PrepushContext.build(repo_root=tmp_path, mode=kwargs.pop("mode", "quick"), is_ci=kwargs.pop("is_ci", False), **kwargs)


def test_path_sanitizer_does_not_output_local_home():
    home = str(Path.home()).replace("\\", "/").rstrip("/")
    sanitized = utils.sanitize_path(f"{home}/hackme_web/runtime/database.db")
    assert home not in sanitized
    assert "<LOCAL_HOME_PATH>" in sanitized


def test_local_path_leak_reports_pattern_not_raw_line(tmp_path):
    path = tmp_path / "docs.md"
    path.write_text("dev path: /mnt/d/share/ComfyUI\n", encoding="utf-8")
    findings = local_path_check.scan_line("docs.md", path.read_text(encoding="utf-8"), 1)
    assert findings == [{"file": "docs.md", "line": 1, "pattern": "WSL_DRIVE_PATH"}]


def test_markdown_link_check_reports_missing_relative_link(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "README.md").write_text("# root\n", encoding="utf-8")
    (tmp_path / "SECURITY.md").write_text("# security\n", encoding="utf-8")
    (docs / "00_START_HERE.md").write_text("[missing](MISSING.md)\n", encoding="utf-8")

    ctx = make_ctx(tmp_path)
    result = markdown_links_check.run(ctx)

    assert result.status == FAIL
    assert result.name == "markdown links"
    assert result.details == [{"file": "docs/00_START_HERE.md", "line": 1, "link": "MISSING.md"}]


def test_markdown_link_check_accepts_correct_relative_links_and_ignores_code(tmp_path):
    docs = tmp_path / "docs"
    agents = docs / "AGENTS"
    security = docs / "security"
    agents.mkdir(parents=True)
    security.mkdir(parents=True)

    (tmp_path / "README.md").write_text("[docs](docs/README.md)\n", encoding="utf-8")
    (tmp_path / "SECURITY.md").write_text("# security\n", encoding="utf-8")
    (docs / "README.md").write_text(
        "[quickstart](01_DEPLOY_QUICKSTART.md)\n"
        "```md\n"
        "[ignored](BROKEN.md)\n"
        "```\n",
        encoding="utf-8",
    )
    (docs / "01_DEPLOY_QUICKSTART.md").write_text("# quickstart\n", encoding="utf-8")
    (agents / "QA_MISSION_FOR_AGENTS.md").write_text("[qa](../11_QA_TESTING.md)\n", encoding="utf-8")
    (docs / "11_QA_TESTING.md").write_text("[external](https://example.com)\n", encoding="utf-8")
    (security / "PRE_RELEASE_CHECKLIST.md").write_text("# release\n", encoding="utf-8")

    ctx = make_ctx(tmp_path)
    result = markdown_links_check.run(ctx)

    assert result.status != FAIL


def test_scripts_index_check_requires_registered_security_scripts(tmp_path):
    script_dir = tmp_path / "scripts" / "security" / "pentest"
    script_dir.mkdir(parents=True)
    script = script_dir / "new_probe.py"
    script.write_text("print('probe')\n", encoding="utf-8")
    index = tmp_path / "scripts" / "INDEX.md"
    index.write_text("# Scripts Index\n", encoding="utf-8")

    ctx = make_ctx(tmp_path)
    result = scripts_index_check.run(ctx)

    assert result.status == FAIL
    assert result.name == "scripts index"
    assert result.details == [{"script": "scripts/security/pentest/new_probe.py"}]

    index.write_text("`scripts/security/pentest/new_probe.py` | QA | Probe | stdout | Probe failed |\n", encoding="utf-8")
    assert scripts_index_check.run(ctx).status != FAIL


def test_scripts_index_check_requires_registered_user_facing_game_and_trading_scripts(tmp_path):
    game_dir = tmp_path / "scripts" / "games"
    trading_dir = tmp_path / "scripts" / "trading" / "probes"
    game_dir.mkdir(parents=True)
    trading_dir.mkdir(parents=True)
    (game_dir / "new_trainer.py").write_text("print('train')\n", encoding="utf-8")
    (trading_dir / "new_probe.py").write_text("print('probe')\n", encoding="utf-8")
    index = tmp_path / "scripts" / "INDEX.md"
    index.write_text("# Scripts Index\n", encoding="utf-8")

    ctx = make_ctx(tmp_path)
    result = scripts_index_check.run(ctx)

    assert result.status == FAIL
    assert result.name == "scripts index"
    assert result.details == [
        {"script": "scripts/games/new_trainer.py"},
        {"script": "scripts/trading/probes/new_probe.py"},
    ]

    index.write_text(
        "`scripts/games/new_trainer.py` | Games | Train | runtime/reports/games | Training failed |\n"
        "`scripts/trading/probes/new_probe.py` | Trading | Probe | stdout | Probe failed |\n",
        encoding="utf-8",
    )
    assert scripts_index_check.run(ctx).status != FAIL


def test_git_clean_check_allows_decorative_separator_comments(monkeypatch, tmp_path):
    target = tmp_path / "public" / "js" / "app.js"
    target.parent.mkdir(parents=True)
    target.write_text("// =====================================================================\n", encoding="utf-8")

    ctx = PrepushContext(repo_root=tmp_path, changed_files=["public/js/app.js"], staged_files=[])

    class CleanDiff:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(utils, "run_command", lambda *args, **kwargs: CleanDiff())

    assert git_clean_check.run(ctx).status != FAIL


def test_git_clean_check_flags_real_conflict_markers(monkeypatch, tmp_path):
    target = tmp_path / "public" / "js" / "app.js"
    target.parent.mkdir(parents=True)
    target.write_text("  <<<<<<< HEAD\n", encoding="utf-8")

    ctx = PrepushContext(repo_root=tmp_path, changed_files=["public/js/app.js"], staged_files=[])

    class CleanDiff:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(utils, "run_command", lambda *args, **kwargs: CleanDiff())

    result = git_clean_check.run(ctx)
    assert result.status == FAIL
    assert result.details == [{"file": "public/js/app.js", "line": 1, "problem": "conflict marker"}]


def test_gitkeep_is_not_forbidden_runtime_artifact():
    assert forbidden_paths_check.is_forbidden("runtime/.gitkeep") is False
    assert forbidden_paths_check.is_forbidden("runtime/storage/.gitkeep") is False


def test_db_log_storage_report_artifacts_are_forbidden():
    assert forbidden_paths_check.is_forbidden("anchors/audit_head.jsonl")
    assert forbidden_paths_check.is_forbidden("chats/room_1.jsonl")
    assert forbidden_paths_check.is_forbidden("database/database.db")
    assert forbidden_paths_check.is_forbidden("logs/server.log")
    assert forbidden_paths_check.is_forbidden("storage/u1/file.bin")
    assert forbidden_paths_check.is_forbidden("reports/bugs/bug.md")
    assert forbidden_paths_check.is_forbidden("security/audit_exports/server_mode/run.json")
    assert forbidden_paths_check.is_forbidden("docs/WEBCHAT/AGENT_SKILL_PROPOSAL.md:Zone.Identifier")


def test_services_storage_package_is_not_treated_as_runtime_storage():
    assert forbidden_paths_check.is_forbidden("services/storage/__init__.py") is False
    assert forbidden_paths_check.is_forbidden("services/storage/cloud_drive.py") is False


def test_secret_scanner_allows_fake_examples_and_redacts_real_secret():
    fake = 'password="fake example changeme"'
    real = "api_key=sk-abcdefghijklmnopqrstuvwxyz123456"
    assert not secrets_check.scan_text("docs/example.md", fake)
    findings = secrets_check.scan_text("config.py", real)
    assert findings
    evidence = findings[0]["evidence"]
    assert "REDACTED" in evidence
    assert "abcdefghijklmnopqrstuvwxyz" not in evidence


def test_release_id_missing_from_docs_fails(tmp_path):
    service = tmp_path / "services"
    docs = tmp_path / "docs"
    service.mkdir()
    docs.mkdir()
    (service / "release_info.py").write_text('APP_RELEASE_ID = "2026.01.01-test"\n', encoding="utf-8")
    (tmp_path / "README.md").write_text("old", encoding="utf-8")
    (docs / "README.zh-TW.md").write_text("old", encoding="utf-8")
    (docs / "For_developer.md").write_text("old", encoding="utf-8")
    (docs / "UPDATE_SUMMARY.md").write_text("old", encoding="utf-8")
    ctx = make_ctx(tmp_path)
    result = release_check.run(ctx)
    assert result.status == FAIL


def test_update_summary_has_explicit_release_id_line_for_hook_bump():
    summary = (ROOT / "docs" / "UPDATE_SUMMARY.md").read_text(encoding="utf-8")
    assert "Release ID: `" in summary


def test_quick_pytest_targets_cover_new_feature_regressions():
    expected = {
        "tests/scripts/prepush/test_prepush_v2.py",
        "tests/frontend/admin/test_frontend_account_admin.py",
        "tests/account/sessions/test_account_sessions.py",
        "tests/users/test_sanction_notices.py",
        "tests/trading/core/test_trading_engine.py",
        "tests/services/test_management_plane.py",
        "tests/regressions/test_security_issue_regressions.py",
    }
    assert expected.issubset(set(pytest_quick_check.QUICK_TESTS))


def test_quick_pytest_timeout_budget_matches_current_hook_scope():
    assert pytest_quick_check.QUICK_PYTEST_TIMEOUT_SECONDS >= 180


def test_ci_job_timeout_covers_quick_pytest_and_gate_overhead():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    match = re.search(r"^\s*timeout-minutes:\s*(\d+)\s*$", workflow, re.MULTILINE)

    assert match is not None
    assert int(match.group(1)) * 60 >= pytest_quick_check.QUICK_PYTEST_TIMEOUT_SECONDS + 600


def test_ci_context_is_noninteractive_for_clean(tmp_path):
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "x.pyc").write_bytes(b"cache")
    removed, candidates = cleanup_check.clean_repo_caches(yes=False, root=tmp_path, tracked=set(), is_ci=True)
    assert removed == 0
    assert candidates
    assert cache.exists()


def test_clean_keeps_gitkeep_while_removing_cache_file(tmp_path):
    cache_dir = tmp_path / "__pycache__"
    cache_dir.mkdir()
    gitkeep = cache_dir / ".gitkeep"
    gitkeep.write_text("", encoding="utf-8")
    pyc = cache_dir / "module.pyc"
    pyc.write_bytes(b"cache")

    removed, _ = cleanup_check.clean_repo_caches(yes=True, root=tmp_path, tracked=set())

    assert removed == 1
    assert gitkeep.exists()
    assert cache_dir.exists()
    assert not pyc.exists()


def test_clean_removes_zone_identifier_sidecar(tmp_path):
    sidecar = tmp_path / "docs" / "WEBCHAT" / "AGENT_SKILL_PROPOSAL.md:Zone.Identifier"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("[ZoneTransfer]\nZoneId=3\n", encoding="utf-8")

    removed, candidates = cleanup_check.clean_repo_caches(yes=True, root=tmp_path, tracked=set())

    assert removed == 1
    assert sidecar in candidates
    assert not sidecar.exists()


def test_clean_does_not_delete_runtime_or_user_data_dirs(tmp_path):
    protected_paths = [
        tmp_path / "database" / "database.db",
        tmp_path / "runtime" / "logs" / "server.log",
        tmp_path / "runtime" / "storage" / "user.bin",
        tmp_path / "runtime" / "reports" / "summary.md",
        tmp_path / "security" / "reports" / "scan.json",
        tmp_path / "runtime" / "reports" / "bugs" / "bug.md",
        tmp_path / "runtime" / "cert.pem",
        tmp_path / "runtime" / "key.pem",
        tmp_path / "runtime" / ".csrfkey",
        tmp_path / "runtime" / ".integrity_key",
        tmp_path / "runtime" / ".chain_seed",
        tmp_path / "runtime" / "integrity_manifest.json",
    ]
    for path in protected_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("do not delete", encoding="utf-8")
    protected_cache = tmp_path / "runtime" / "storage" / "__pycache__" / "x.pyc"
    protected_cache.parent.mkdir(parents=True, exist_ok=True)
    protected_cache.write_bytes(b"cache")

    cleanup_check.clean_repo_caches(yes=True, root=tmp_path, tracked=set())

    for path in protected_paths:
        assert path.exists(), path
    assert protected_cache.exists()


def test_clean_runtime_removes_repo_runtime_root(tmp_path):
    runtime_root = tmp_path / "runtime"
    (runtime_root / "database").mkdir(parents=True)
    (runtime_root / "database" / "database.db").write_text("db", encoding="utf-8")

    removed, candidates = cleanup_check.clean_repo_runtime(yes=True, root=tmp_path, tracked=set())

    assert removed == 1
    assert candidates == [runtime_root]
    assert not runtime_root.exists()


def test_clean_runtime_skips_tracked_runtime_placeholder(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    gitkeep = runtime_root / ".gitkeep"
    gitkeep.write_text("", encoding="utf-8")

    removed, candidates = cleanup_check.clean_repo_runtime(
        yes=True,
        root=tmp_path,
        tracked={"runtime/.gitkeep"},
    )

    assert removed == 0
    assert candidates == []
    assert runtime_root.exists()


def test_clean_does_not_delete_tracked_files_except_untracked_cache(tmp_path):
    tracked_file = tmp_path / "build" / "artifact.txt"
    tracked_file.parent.mkdir()
    tracked_file.write_text("tracked", encoding="utf-8")
    cache_file = tmp_path / "build" / "artifact.pyc"
    cache_file.write_bytes(b"cache")

    candidates = cleanup_check.collect_repo_cache_candidates(root=tmp_path, tracked={"build/artifact.txt"})
    assert tracked_file not in candidates
    assert cache_file in candidates


def test_clean_temp_keeps_latest_two_temp_roots(tmp_path):
    roots = []
    for index in range(5):
        path = tmp_path / f"html_learning_prepush_{index}"
        path.mkdir()
        stamp = time.time() + index
        os.utime(path, (stamp, stamp))
        roots.append(path)

    removed, _ = cleanup_check.clean_temp_roots(tmp_root=tmp_path, keep_latest=2, yes=True)

    assert removed == 3
    assert roots[3].exists()
    assert roots[4].exists()
    assert not roots[0].exists()
    assert not roots[1].exists()
    assert not roots[2].exists()


def test_ci_runtime_cleanup_success_removes_failure_keeps(tmp_path):
    success_root = tmp_path / "html_learning_prepush_success"
    failure_root = tmp_path / "html_learning_prepush_failure"
    success_root.mkdir()
    failure_root.mkdir()

    assert cleanup_check.cleanup_current_runtime(success_root, success=True, ci=True, keep_temp=False) == "removed"
    assert not success_root.exists()

    assert cleanup_check.cleanup_current_runtime(failure_root, success=False, ci=True, keep_temp=False) == "kept"
    assert failure_root.exists()


def test_frontend_node_missing_local_skip(monkeypatch):
    monkeypatch.setattr(utils, "tool_exists", lambda name: False)
    ctx = PrepushContext.build(repo_root=ROOT, mode="quick", is_ci=False)
    result = frontend_check.run(ctx)
    assert result.status == SKIP


def test_gitleaks_missing_ci_fails(monkeypatch):
    monkeypatch.delenv("ALLOW_MISSING_GITLEAKS", raising=False)
    monkeypatch.setattr(utils, "tool_exists", lambda name: False)
    ctx = PrepushContext.build(repo_root=ROOT, mode="quick", is_ci=True)
    result = secrets_check.run(ctx)
    assert result.status == FAIL


def test_gitleaks_timeout_budget_is_bounded_and_configurable(monkeypatch):
    monkeypatch.delenv("PREPUSH_GITLEAKS_TIMEOUT_SECONDS", raising=False)
    assert secrets_check.gitleaks_timeout_seconds() == 300
    monkeypatch.setenv("PREPUSH_GITLEAKS_TIMEOUT_SECONDS", "2")
    assert secrets_check.gitleaks_timeout_seconds() == 30
    monkeypatch.setenv("PREPUSH_GITLEAKS_TIMEOUT_SECONDS", "9999")
    assert secrets_check.gitleaks_timeout_seconds() == 1800
    monkeypatch.setenv("PREPUSH_GITLEAKS_TIMEOUT_SECONDS", "invalid")
    assert secrets_check.gitleaks_timeout_seconds() == 300


def test_gitleaks_timeout_returns_actionable_failure(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path, is_ci=True)
    monkeypatch.setattr(utils, "tool_exists", lambda name: name == "gitleaks")
    monkeypatch.setattr(
        utils,
        "run_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))),
    )
    monkeypatch.setenv("PREPUSH_GITLEAKS_TIMEOUT_SECONDS", "31")

    result = secrets_check.run(ctx)

    assert result.status == FAIL
    assert result.name == "gitleaks scan"
    assert "timed out after 31 seconds" in result.message
    assert "PREPUSH_GITLEAKS_TIMEOUT_SECONDS" in result.remediation


def test_subprocess_timeout_is_reported():
    try:
        utils.run_command(["python3", "-c", "import time; time.sleep(2)"], cwd=ROOT, timeout=1)
    except Exception as exc:
        assert "timed out" in str(exc).lower()
    else:
        raise AssertionError("timeout was not enforced")
