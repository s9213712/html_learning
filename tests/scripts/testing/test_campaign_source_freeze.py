from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import scripts.testing.campaign_source_freeze as source_freeze
from scripts.testing.campaign_source_freeze import GitSourceFreezer, SourceFreezeError


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True)
    return completed.stdout.strip()


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Campaign Test")
    git(root, "config", "user.email", "campaign@example.invalid")
    paths = {
        "server.py": "print('ok')\n",
        "templates/base.html": "<html></html>\n",
        "config/service.toml": "enabled = true\n",
        "config/service.ini": "[service]\n",
        "config/schema.cfg": "schema=1\n",
        "migrations/001.sql": "select 1;\n",
        "requirements.lock": "package==1\n",
        "scripts/testing/harness.py": "pass\n",
    }
    for name, value in paths.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    os.symlink("../config/service.toml", root / "templates" / "service-config")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")
    return root


def ignore_launcher_inputs(root: Path) -> None:
    (root / ".gitignore").write_text(
        "\n".join(
            (
                ".hackme_capacity_defaults.env",
                ".hackme_capacity_defaults.env.old",
                ".hackme_capacity_report.json",
                "ignored-runtime/",
                "",
            )
        ),
        encoding="utf-8",
    )
    git(root, "add", ".gitignore")
    git(root, "commit", "-qm", "ignore local launcher inputs")


def test_h0_has_separate_authority_for_reviewed_ignored_launcher_inputs(tmp_path: Path) -> None:
    root = repo(tmp_path)
    ignore_launcher_inputs(root)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")

    h0 = freezer.capture(label="H0")

    assert h0["verified"] is True
    assert h0["protected_ignored_file_count"] == 2
    assert h0["protected_ignored_present_count"] == 0
    assert h0["protected_ignored_observable"] is True
    policy = h0["protected_ignored_policy"]
    assert policy["policy"] == "explicit_reviewed_list"
    assert policy["broad_ignored_runtime_is_excluded"] is True
    assert [row["path"] for row in policy["paths"]] == list(
        source_freeze.REVIEWED_PROTECTED_IGNORED_PATHS
    )
    assert all(row["git_ignored"] is True for row in policy["paths"])
    manifest_rows = [
        json.loads(line)
        for line in Path(h0["artifacts"]["protected_ignored_manifest"]).read_text(encoding="utf-8").splitlines()
    ]
    assert [row["kind"] for row in manifest_rows] == ["missing", "missing"]
    assert all(
        item["watched"] is True
        for item in h0["runtime_monitor"]["protected_ignored_watch_coverage"].values()
    )


def test_protected_ignored_launcher_input_create_invalidates_runtime_and_h24(tmp_path: Path) -> None:
    root = repo(tmp_path)
    ignore_launcher_inputs(root)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    h0 = freezer.capture(label="H0")
    protected = root / ".hackme_capacity_defaults.env"

    protected.write_text("WORKERS=8\n", encoding="utf-8")
    drift = freezer.lightweight_drift_check()

    assert drift["verified"] is False
    assert drift["status_unchanged"] is True
    assert drift["status"]["blocked_changes"] == []
    change = drift["protected_ignored_changes"][protected.name]
    assert change["authority_class"] == "protected_ignored_launcher_input"
    assert change["actual_kind"] == "file"
    assert change["actual_sha256"] == source_freeze.sha256_file(protected)
    assert change["expected_kind"] == "missing"
    final = freezer.verify_final()
    assert final["verified"] is False
    assert final["comparisons"]["protected_ignored_manifest_digest"] is False
    assert final["comparisons"]["protected_ignored_content_digest"] is False
    assert h0["protected_ignored_content_digest"] != final["h24"]["protected_ignored_content_digest"]


def test_broad_ignored_runtime_stays_excluded_but_protected_ignored_does_not(tmp_path: Path) -> None:
    root = repo(tmp_path)
    ignore_launcher_inputs(root)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    freezer.capture(label="H0")

    ignored_runtime = root / "ignored-runtime"
    ignored_runtime.mkdir()
    (ignored_runtime / "sample.json").write_text('{"runtime": true}\n', encoding="utf-8")
    assert freezer.lightweight_drift_check()["verified"] is True

    protected = root / ".hackme_capacity_report.json"
    protected.write_text('{"workers": 8}\n', encoding="utf-8")
    drift = freezer.lightweight_drift_check()
    assert drift["verified"] is False
    assert protected.name in drift["protected_ignored_changes"]


def test_protected_ignored_launcher_input_content_change_invalidates_runtime(tmp_path: Path) -> None:
    root = repo(tmp_path)
    ignore_launcher_inputs(root)
    protected = root / ".hackme_capacity_report.json"
    protected.write_text('{"workers": 4}\n', encoding="utf-8")
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    h0 = freezer.capture(label="H0")
    before_digest = h0["protected_ignored_content_digest"]

    protected.write_text('{"workers": 8}\n', encoding="utf-8")
    drift = freezer.lightweight_drift_check()

    assert drift["verified"] is False
    assert drift["status_unchanged"] is True
    change = drift["protected_ignored_changes"][protected.name]
    assert change["reason"] == "content_or_type_changed"
    assert change["expected_sha256"] != change["actual_sha256"]
    assert drift["protected_ignored_content_digest"] != before_digest


def test_protected_ignored_launcher_input_delete_invalidates_runtime(tmp_path: Path) -> None:
    root = repo(tmp_path)
    ignore_launcher_inputs(root)
    protected = root / ".hackme_capacity_defaults.env"
    protected.write_text("WORKERS=8\n", encoding="utf-8")
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    freezer.capture(label="H0")

    protected.unlink()
    drift = freezer.lightweight_drift_check()

    assert drift["verified"] is False
    change = drift["protected_ignored_changes"][protected.name]
    assert change["reason"] == "missing"
    assert change["expected_sha256"]
    assert change["actual_sha256"] == ""


def test_protected_ignored_launcher_input_rename_and_symlink_are_detected(tmp_path: Path) -> None:
    rename_base = tmp_path / "rename"
    rename_base.mkdir()
    rename_root = repo(rename_base)
    ignore_launcher_inputs(rename_root)
    protected = rename_root / ".hackme_capacity_defaults.env"
    protected.write_text("WORKERS=8\n", encoding="utf-8")
    rename_freezer = GitSourceFreezer(rename_root, tmp_path / "rename-artifacts")
    rename_freezer.capture(label="H0")

    protected.rename(rename_root / ".hackme_capacity_defaults.env.old")
    renamed = rename_freezer.lightweight_drift_check()
    assert renamed["protected_ignored_changes"][protected.name]["reason"] == "missing"

    symlink_base = tmp_path / "symlink"
    symlink_base.mkdir()
    symlink_root = repo(symlink_base)
    ignore_launcher_inputs(symlink_root)
    symlink_freezer = GitSourceFreezer(symlink_root, tmp_path / "symlink-artifacts")
    symlink_freezer.capture(label="H0")
    os.symlink("server.py", symlink_root / ".hackme_capacity_report.json")

    symlinked = symlink_freezer.lightweight_drift_check()
    change = symlinked["protected_ignored_changes"][".hackme_capacity_report.json"]
    assert change["actual_kind"] == "symlink"
    assert change["actual_sha256"]


def test_h0_fails_closed_when_protected_ignored_parent_watch_is_unobservable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repo(tmp_path)
    ignore_launcher_inputs(root)
    original_add_watch = source_freeze._RuntimeDriftMonitor._add_watch

    def omit_repo_root_watch(monitor: object, path: Path, source: str) -> None:
        if source == "source" and path.resolve() == root.resolve():
            return
        original_add_watch(monitor, path, source)

    monkeypatch.setattr(source_freeze._RuntimeDriftMonitor, "_add_watch", omit_repo_root_watch)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")

    with pytest.raises(SourceFreezeError, match="protected_ignored_observable=False"):
        freezer.capture(label="H0")


def test_freeze_covers_every_tracked_file_type_symlink_and_git_authority(tmp_path: Path) -> None:
    root = repo(tmp_path)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    result = freezer.capture(label="H0")

    assert result["verified"] is True
    assert result["tracked_file_count"] == 9
    assert result["symlink_count"] == 1
    manifest = Path(result["artifacts"]["tracked_manifest"]).read_text(encoding="utf-8")
    for path in ("templates/base.html", "config/service.toml", "config/service.ini", "config/schema.cfg", "migrations/001.sql", "requirements.lock"):
        assert f'"path": "{path}"' in manifest
    assert Path(result["artifacts"]["git_diff_binary"]).read_bytes() == b""
    assert result["git_ls_files_sha256"]
    assert result["git_submodule_status_sha256"]


def test_artifact_root_inside_repository_is_rejected(tmp_path: Path) -> None:
    root = repo(tmp_path)
    with pytest.raises(SourceFreezeError, match="outside the repository"):
        GitSourceFreezer(root, root / "campaign-artifacts")


def test_tracked_source_drift_is_detected_without_full_repo_rehash(tmp_path: Path) -> None:
    root = repo(tmp_path)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    freezer.capture(label="H0")
    (root / "server.py").write_text("print('changed')\n", encoding="utf-8")

    drift = freezer.lightweight_drift_check()

    assert drift["verified"] is False
    assert drift["tracked_changes"]["server.py"]["reason"] == "content_or_type_changed"
    assert drift["status"]["blocked_changes"]


def test_runtime_monitor_hashes_only_the_changed_source_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repo(tmp_path)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    freezer.capture(label="H0")
    assert freezer.lightweight_drift_check()["verified"] is True
    hashed: list[Path] = []
    original_sha256_file = source_freeze.sha256_file

    def counted_sha256(path: Path) -> str:
        hashed.append(path)
        return original_sha256_file(path)

    monkeypatch.setattr(source_freeze, "sha256_file", counted_sha256)
    (root / "server.py").write_text("print('only me')\n", encoding="utf-8")

    drift = freezer.lightweight_drift_check()

    assert drift["verified"] is False
    assert set(hashed) == {root / "server.py"}


def test_untracked_allowlist_is_separate_and_cannot_allow_tracked_edits(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / "local.plan").write_text("reviewed\n", encoding="utf-8")
    with pytest.raises(SourceFreezeError, match="source freeze verification failed"):
        GitSourceFreezer(root, tmp_path / "formal", untracked_allowlist=("*.plan",)).capture(label="H0")

    freezer = GitSourceFreezer(root, tmp_path / "artifacts", untracked_allowlist=("*.plan",))
    result = freezer.capture(label="H0", require_clean=False)
    assert result["verified"] is True
    assert result["git_status_empty"] is False
    assert result["status"]["allowed_untracked"][0]["path"] == "local.plan"
    assert result["untracked_file_count"] == 1
    assert result["untracked_content_digest"]

    (root / "server.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(SourceFreezeError, match="source freeze verification failed"):
        GitSourceFreezer(root, tmp_path / "artifacts2", untracked_allowlist=("*",)).capture(label="H0")


def test_new_untracked_source_invalidates_formal_freeze(tmp_path: Path) -> None:
    root = repo(tmp_path)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    freezer.capture(label="H0")
    (root / "new_source.py").write_text("pass\n", encoding="utf-8")

    drift = freezer.lightweight_drift_check()
    assert drift["verified"] is False
    assert drift["status"]["blocked_changes"] == [{"status": "??", "path": "new_source.py"}]


def test_runtime_rename_and_delete_are_detected_with_changed_path_evidence(tmp_path: Path) -> None:
    root = repo(tmp_path)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    freezer.capture(label="H0")
    (root / "server.py").rename(root / "renamed.py")

    drift = freezer.lightweight_drift_check()

    assert drift["verified"] is False
    assert drift["tracked_changes"]["server.py"]["reason"] == "missing"
    assert drift["untracked_changes"]["renamed.py"]["reason"] == "new_untracked_path"
    changed = json.loads(Path(drift["artifacts"]["changed_path_evidence"]).read_text(encoding="utf-8"))
    assert changed["tracked_changes"]["server.py"]["reason"] == "missing"
    assert changed["untracked_changes"]["renamed.py"]["actual_sha256"]

    second_root = tmp_path / "second"
    second_root.mkdir()
    root2 = repo(second_root)
    freezer2 = GitSourceFreezer(root2, tmp_path / "second-artifacts")
    freezer2.capture(label="H0")
    (root2 / "server.py").unlink()
    deleted = freezer2.lightweight_drift_check()
    assert deleted["tracked_changes"]["server.py"]["reason"] == "missing"


def test_new_directory_is_added_to_recursive_monitor_and_new_file_is_detected(tmp_path: Path) -> None:
    root = repo(tmp_path)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    freezer.capture(label="H0")
    before_watches = freezer._runtime_monitor.health()["watch_count"]
    nested = root / "brand_new" / "nested"
    nested.mkdir(parents=True)
    (nested / "source.py").write_text("pass\n", encoding="utf-8")

    drift = freezer.lightweight_drift_check()

    assert drift["verified"] is False
    assert drift["untracked_changes"]["brand_new/nested/source.py"]["reason"] == "new_untracked_path"
    assert drift["monitor"]["watch_count"] > before_watches


def test_h24_must_match_h0_commit_tree_status_and_submodules(tmp_path: Path) -> None:
    root = repo(tmp_path)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    freezer.capture(label="H0")

    assert freezer.verify_final()["verified"] is True

    (root / "server.py").write_text("changed\n", encoding="utf-8")
    final = freezer.verify_final()
    assert final["verified"] is False
    assert final["comparisons"]["tracked_content_digest"] is False
    assert final["comparisons"]["status_clean"] is False


def test_nonformal_snapshot_may_be_dirty_but_must_remain_byte_identical(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / "server.py").write_text("dirty but frozen\n", encoding="utf-8")
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    assert freezer.capture(label="H0", require_clean=False)["verified"] is True

    assert freezer.lightweight_drift_check()["verified"] is True
    final = freezer.verify_final(require_clean=False)
    assert final["verified"] is True
    assert final["comparisons"]["status_unchanged"] is True
    assert "status_clean" not in final["comparisons"]


def test_nonformal_untracked_content_change_is_detected_even_when_status_is_identical(tmp_path: Path) -> None:
    root = repo(tmp_path)
    local = root / "local.plan"
    local.write_text("aaaa\n", encoding="utf-8")
    freezer = GitSourceFreezer(root, tmp_path / "artifacts", untracked_allowlist=("*.plan",))
    h0 = freezer.capture(label="H0", require_clean=False)

    local.write_text("bbbb\n", encoding="utf-8")
    drift = freezer.lightweight_drift_check()

    assert drift["verified"] is False
    assert drift["status_unchanged"] is True
    assert drift["untracked_changes"]["local.plan"]["reason"] == "content_or_type_changed"
    assert drift["untracked_changes"]["local.plan"]["expected_sha256"] != drift["untracked_changes"]["local.plan"]["actual_sha256"]
    assert freezer.verify_final(require_clean=False)["comparisons"]["untracked_content_digest"] is False
    assert h0["untracked_content_digest"]


def test_untracked_symlink_target_and_metadata_are_frozen(tmp_path: Path) -> None:
    root = repo(tmp_path)
    os.symlink("server.py", root / "source-link")
    freezer = GitSourceFreezer(root, tmp_path / "artifacts", untracked_allowlist=("source-link",))
    result = freezer.capture(label="H0", require_clean=False)
    assert result["untracked_symlink_count"] == 1

    (root / "source-link").unlink()
    os.symlink("README.md", root / "source-link")
    drift = freezer.lightweight_drift_check()
    assert drift["verified"] is False
    assert drift["status_unchanged"] is True
    assert drift["untracked_changes"]["source-link"]["reason"] == "content_or_type_changed"


def test_no_change_runtime_polling_is_bounded_and_does_not_repeat_git_authority_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repo(tmp_path)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    freezer.capture(label="H0")
    authority_calls = 0
    original_authority_snapshot = freezer._authority_snapshot

    def counted_authority_snapshot() -> dict[str, object]:
        nonlocal authority_calls
        authority_calls += 1
        return original_authority_snapshot()

    monkeypatch.setattr(freezer, "_authority_snapshot", counted_authority_snapshot)

    results = [freezer.lightweight_drift_check() for _ in range(25)]
    drift = results[-1]
    assert drift["verified"] is True
    assert drift["monitor"]["mode"] == "inotify"
    assert drift["monitor"]["machine_verified"] is True
    assert drift["monitor"]["watch_add_failures"] == 0
    assert drift["monitor"]["first_reconciliation_completed"] is True
    assert drift["full_git_authority_captured"] is False
    assert drift["poll_evidence_mode"] == "bounded_latest"
    assert drift["comparisons"] == {
        "commit": True,
        "branch": True,
        "status": True,
        "diff": True,
        "ls_files": True,
        "submodules": True,
        "protected_ignored": True,
    }
    runtime_root = Path(drift["artifact_root"])
    assert sorted(path.name for path in runtime_root.iterdir()) == [".monitor_probe", "latest.json"]
    assert not (runtime_root / "incidents").exists()
    assert authority_calls == 1
    persisted = json.loads((runtime_root / "latest.json").read_text(encoding="utf-8"))
    assert persisted["verified"] is True
    assert persisted["artifacts"] == drift["artifacts"]


def test_drift_creates_one_preserved_incident_and_repeated_checks_do_not_expand_artifacts(tmp_path: Path) -> None:
    root = repo(tmp_path)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    freezer.capture(label="H0")
    assert freezer.lightweight_drift_check()["verified"] is True

    (root / "server.py").write_text("print('incident')\n", encoding="utf-8")
    first = freezer.lightweight_drift_check()
    incident_root = Path(first["artifact_root"])
    assert first["verified"] is False
    assert first["incident_evidence_preserved"] is True
    assert first["full_git_authority_captured"] is True
    assert incident_root.parent.name == "incidents"
    for key in ("git_status", "git_diff_binary", "git_ls_files", "git_submodule_status", "changed_path_evidence"):
        assert Path(first["artifacts"][key]).is_file()

    for _ in range(20):
        assert freezer.lightweight_drift_check()["incident_id"] == first["incident_id"]
    assert [path.name for path in incident_root.parent.iterdir()] == [first["incident_id"]]


def test_index_and_staged_diff_authority_are_checked_during_runtime(tmp_path: Path) -> None:
    root = repo(tmp_path)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    freezer.capture(label="H0")
    (root / "staged.py").write_text("pass\n", encoding="utf-8")
    git(root, "add", "staged.py")

    drift = freezer.lightweight_drift_check()
    assert drift["verified"] is False
    assert drift["comparisons"]["status"] is False
    assert drift["comparisons"]["diff"] is False
    assert drift["comparisons"]["ls_files"] is False


def test_commit_change_is_detected_by_git_authority_watch_and_preserved(tmp_path: Path) -> None:
    root = repo(tmp_path)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    baseline = freezer.capture(label="H0")
    git(root, "commit", "--allow-empty", "-qm", "commit drift")

    drift = freezer.lightweight_drift_check()

    assert drift["verified"] is False
    assert drift["authority_event_seen"] is True
    assert drift["comparisons"]["commit"] is False
    assert drift["commit"] != baseline["commit"]
    assert drift["tracked_changes"]["@git_authority"]["reason"] == "git_authority_mutation"
    assert Path(drift["artifacts"]["git_ls_files"]).is_file()


def test_inotify_unavailable_rejects_h0_and_reports_metadata_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def force_fallback(monitor: object) -> None:
        monitor.mode = "metadata_fallback"
        monitor._error("forced inotify outage")

    monkeypatch.setattr(source_freeze._RuntimeDriftMonitor, "_initialize", force_fallback)
    root = repo(tmp_path)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    with pytest.raises(SourceFreezeError, match="protected_ignored_observable=False"):
        freezer.capture(label="H0")

    h0 = json.loads((tmp_path / "artifacts" / "H0" / "source_freeze.json").read_text(encoding="utf-8"))
    assert h0["verified"] is False
    assert h0["protected_ignored_observable"] is False
    assert h0["runtime_monitor"]["mode"] == "metadata_fallback"
    assert h0["runtime_monitor"]["machine_verified"] is False
    assert h0["runtime_monitor"]["formal_eligible"] is False


def test_runtime_monitor_close_is_idempotent_and_releases_file_descriptor(tmp_path: Path) -> None:
    root = repo(tmp_path)
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    freezer.capture(label="H0")
    monitor = freezer._runtime_monitor
    assert monitor is not None and monitor.fd >= 0

    freezer.close()
    freezer.close()

    assert monitor.fd == -1
    assert freezer._runtime_monitor is None


def test_submodule_gitlink_and_recursive_status_are_frozen(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    git(child, "init", "-q")
    git(child, "config", "user.name", "Campaign Test")
    git(child, "config", "user.email", "campaign@example.invalid")
    (child / "child.txt").write_text("one\n", encoding="utf-8")
    git(child, "add", ".")
    git(child, "commit", "-qm", "one")
    (child / "child.txt").write_text("two\n", encoding="utf-8")
    git(child, "commit", "-qam", "two")

    root = repo(tmp_path)
    git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child), "vendor/child")
    git(root, "commit", "-qam", "submodule")
    freezer = GitSourceFreezer(root, tmp_path / "artifacts")
    result = freezer.capture(label="H0")
    manifest = Path(result["artifacts"]["tracked_manifest"]).read_text(encoding="utf-8")
    assert '"kind": "submodule"' in manifest
    assert '"submodule_head":' in manifest

    child_checkout = root / "vendor" / "child"
    (child_checkout / "child.txt").write_text("dirty\n", encoding="utf-8")
    dirty = GitSourceFreezer(root, tmp_path / "dirty-artifacts").capture(label="H0", require_clean=False)
    assert dirty["verified"] is False
    assert dirty["submodule_worktree_changes"]
    git(child_checkout, "checkout", "--", "child.txt")

    git(root / "vendor" / "child", "checkout", "-q", "HEAD^")
    drift = freezer.lightweight_drift_check()
    assert drift["verified"] is False
    assert drift["comparisons"]["submodules"] is False
    assert drift["comparisons"]["status"] is False


def test_load_baseline_rejects_tampered_untracked_manifest(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / "local.plan").write_text("frozen\n", encoding="utf-8")
    original = GitSourceFreezer(root, tmp_path / "artifacts", untracked_allowlist=("*.plan",))
    h0 = original.capture(label="H0", require_clean=False)
    manifest = Path(h0["artifacts"]["untracked_manifest"])
    row = json.loads(manifest.read_text(encoding="utf-8"))
    row["working_sha256"] = "0" * 64
    manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")

    restored = GitSourceFreezer(root, tmp_path / "artifacts", untracked_allowlist=("*.plan",))
    with pytest.raises(SourceFreezeError, match="untracked manifest digest mismatch"):
        restored.load_baseline(Path(h0["artifact_root"]) / "source_freeze.json")


def test_load_baseline_rejects_tampered_protected_ignored_manifest(tmp_path: Path) -> None:
    root = repo(tmp_path)
    ignore_launcher_inputs(root)
    original = GitSourceFreezer(root, tmp_path / "artifacts")
    h0 = original.capture(label="H0")
    manifest = Path(h0["artifacts"]["protected_ignored_manifest"])
    rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    rows[0]["working_sha256"] = "0" * 64
    manifest.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    restored = GitSourceFreezer(root, tmp_path / "artifacts")
    with pytest.raises(SourceFreezeError, match="protected ignored manifest digest mismatch"):
        restored.load_baseline(Path(h0["artifact_root"]) / "source_freeze.json")


def test_separate_campaign_process_can_restore_verified_h0_manifest(tmp_path: Path) -> None:
    root = repo(tmp_path)
    original = GitSourceFreezer(root, tmp_path / "artifacts")
    h0 = original.capture(label="H0")

    restored = GitSourceFreezer(root, tmp_path / "artifacts")
    loaded = restored.load_baseline(Path(h0["artifact_root"]) / "source_freeze.json")

    assert loaded["tracked_content_digest"] == h0["tracked_content_digest"]
    assert restored.lightweight_drift_check()["verified"] is True
