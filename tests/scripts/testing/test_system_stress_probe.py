import json
from pathlib import Path

from scripts.testing.points_chain_destructive_stress import chain_seed_path
from scripts.testing.operational_soak_probe import (
    MIN_SIGNOFF_SECONDS,
    SentinelStats,
    aggregate_resource_evidence,
    aggregate_rounds,
    sanitized_command,
    validate_run_policy,
)
from scripts.testing.system_stress_probe import (
    OperationBudget,
    Stats,
    resolve_session_pool_size,
    rotation_operation_account,
    run_operation,
)


ROOT = Path(__file__).resolve().parents[3]


def test_expected_503_does_not_count_as_server_busy():
    stats = Stats()

    stats.record("optional_api", status=503, elapsed_ms=1.0, ok=True, body_sample="")
    summary = stats.summary()

    assert summary["server_busy_503"] == 0
    assert summary["transport_or_5xx_failures"] == 0
    assert summary["ops"]["optional_api"]["expected_503"] == 1


def test_feature_disabled_503_does_not_count_as_server_failure():
    stats = Stats()
    body = json.dumps({"ok": False, "feature": "video", "feature_label": "影音"})

    stats.record("video_watch", status=503, elapsed_ms=1.0, ok=False, error=body, body_sample=body)
    summary = stats.summary()

    assert summary["server_busy_503"] == 0
    assert summary["transport_or_5xx_failures"] == 0
    assert summary["ops"]["video_watch"]["feature_disabled_503"] == 1


def test_truncated_feature_disabled_503_does_not_count_as_server_failure():
    stats = Stats()
    body = '{"feature":"feature_videos_enabled","feature_description":"影音若搭配雲端硬碟'

    stats.record("video_list", status=503, elapsed_ms=1.0, ok=False, error=body, body_sample=body)
    summary = stats.summary()

    assert summary["server_busy_503"] == 0
    assert summary["transport_or_5xx_failures"] == 0
    assert summary["ops"]["video_list"]["feature_disabled_503"] == 1


def test_server_busy_503_counts_as_server_failure():
    stats = Stats()
    body = json.dumps({"ok": False, "error": "server_busy"})

    stats.record(
        "upload",
        status=503,
        elapsed_ms=1.0,
        ok=False,
        error=body,
        body_sample=body,
        backpressure_rejected=True,
    )
    summary = stats.summary()

    assert summary["server_busy_503"] == 1
    assert summary["transport_or_5xx_failures"] == 1
    assert summary["ops"]["upload"]["server_busy_503"] == 1


def test_server_busy_body_without_rejection_proof_is_a_hard_failure():
    stats = Stats()
    body = json.dumps({"ok": False, "error": "server_busy"})

    stats.record("mutation", status=503, elapsed_ms=1.0, ok=False, error=body, body_sample=body)
    summary = stats.summary()

    assert summary["server_busy_503"] == 0
    assert summary["hard_failures_excluding_controlled_503"] == 1
    assert summary["ops"]["mutation"]["unexpected_503"] == 1


def test_defensive_latency_is_separated_from_ordinary_latency():
    stats = Stats()

    stats.record("bad_login", status=401, elapsed_ms=10_000.0, ok=True)
    stats.record("drive_list", status=200, elapsed_ms=120.0, ok=True)
    summary = stats.summary()

    assert summary["overall_latency"]["p99_ms"] == 10000.0
    assert summary["ordinary_latency"]["p99_ms"] == 120.0
    assert "bad_login" in summary["ordinary_latency"]["excluded_ops"]


def test_stats_preserves_true_per_account_operation_coverage():
    stats = Stats()

    stats.record("drive_list", status=200, elapsed_ms=10, ok=True, account="opsim-a")
    stats.record("points_wallet", status=200, elapsed_ms=12, ok=True, account="opsim-b")
    stats.record("points_wallet", status=500, elapsed_ms=15, ok=False, account="opsim-b")
    summary = stats.summary()

    assert summary["accounts"]["opsim-a"]["operations"] == {"drive_list": 1}
    assert summary["accounts"]["opsim-a"]["successful_operations"] == {"drive_list": 1}
    assert summary["accounts"]["opsim-b"]["successful_operations"] == {"points_wallet": 1}
    assert summary["ops"]["points_wallet"]["successful_2xx"] == 1
    assert summary["accounts"]["opsim-b"]["total_ops"] == 2
    assert summary["accounts"]["opsim-b"]["failed_ops"] == 1


def test_rotation_assigns_every_operation_to_every_account_before_repeating():
    operations = ["drive", "video", "trading"]
    accounts = ["alice", "bob"]

    assignments = [rotation_operation_account(index, operations, accounts) for index in range(6)]

    assert assignments == [
        ("drive", "alice"),
        ("video", "alice"),
        ("trading", "alice"),
        ("drive", "bob"),
        ("video", "bob"),
        ("trading", "bob"),
    ]


def test_system_stress_clone_mode_uses_each_accounts_own_authenticated_seed():
    script = (ROOT / "scripts" / "testing" / "system_stress_probe.py").read_text(encoding="utf-8")

    assert "account_seeds.get(username)" in script
    assert "client.clone_auth_from(account_seed)" in script
    assert "client.clone_auth_from(seed_client)" not in script
    assert 'choices=["random", "rotation"]' in script
    assert '"missing_accounts"' in script
    assert 'os.environ.get("HACKME_STRESS_ACCOUNTS", "")' in script
    assert 'os.environ.get("HACKME_STRESS_TEST_PASSWORD", "")' in script
    assert "HACKME_STRESS_ACCOUNTS/--accounts or" in script
    assert '"test:test,test2:test2,test3:test3"' not in script
    assert '"server_busy_rate_above_configured_limit"' in script
    assert '"ordinary_p95_above_configured_limit"' in script
    assert '"--max-ordinary-p95-ms"' in script


def test_bt_reject_uses_rejected_torrent_url_instead_of_creating_magnet_task():
    calls = []

    class FakeClient:
        def request(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"ok": True, "status": 400, "elapsed_ms": 1.0, "op": args[0]}

    result = run_operation(
        "bt_reject",
        FakeClient(),
        {},
        OperationBudget({"bt_reject": 1}),
        1,
    )

    assert result["ok"] is True
    args, kwargs = calls[0]
    assert args[:3] == ("bt_reject", "POST", "/api/cloud-drive/remote-download/tasks")
    assert kwargs["json"] == {"url": "http://127.0.0.1/blocked.torrent", "download_mode": "bt"}
    assert 202 not in kwargs["expected"]


def test_auto_login_session_pool_is_capped_to_account_count():
    size, mode = resolve_session_pool_size(
        requested=0,
        session_mode="login",
        account_count=3,
        concurrency=24,
        logical_users=100,
    )

    assert size == 3
    assert mode == "auto_login_account_capped"


def test_explicit_session_pool_is_respected_for_login_limit_probes():
    size, mode = resolve_session_pool_size(
        requested=96,
        session_mode="login",
        account_count=3,
        concurrency=24,
        logical_users=100,
    )

    assert size == 96
    assert mode == "explicit"


def test_long_needle_probe_orchestrates_economy_private_chain_and_full_feature():
    script = (ROOT / "scripts" / "testing" / "long_needle_simulation_probe.py").read_text(encoding="utf-8")
    index = (ROOT / "scripts" / "INDEX.md").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "long-needle-simulation.yml").read_text(encoding="utf-8")
    workflow_template = (ROOT / "scripts" / "testing" / "long-needle-simulation.workflow.yml").read_text(encoding="utf-8")

    assert "points_chain_destructive_stress.py" in script
    assert "system_stress_probe.py" in script
    assert "economy_private_chain" in script
    assert "full_feature" in script
    assert "--direct-transfer-ops" in script
    assert "--allow-server-busy" in script
    assert "provision_probe_accounts" in script
    assert '"--operation-mode"' in script
    assert '"--require-all-accounts"' in script
    assert '"--require-operation-coverage"' in script
    assert "test2:test2" not in script
    assert "long_needle_simulation" in script
    assert "scripts/testing/long_needle_simulation_probe.py" in index
    assert workflow == workflow_template
    assert "schedule:" in workflow
    assert "PROFILE=\"medium\"" in workflow
    assert "python scripts/testing/long_needle_simulation_probe.py" in workflow
    assert "actions/upload-artifact@v4" in workflow


def test_operational_soak_aggregates_all_accounts_and_operations():
    rounds = [
        {
            "ok": True,
            "registered_operations": ["drive", "points"],
            "account_operation_counts": {"alice": 2, "bob": 2},
            "summary": {
                "total_ops": 4,
                "hard_failures_excluding_503": 0,
                "server_busy_503": 1,
                "ops": {"drive": {}, "points": {}},
            },
        },
        {
            "ok": False,
            "degraded_reasons": ["server_busy_rate_above_configured_limit"],
            "registered_operations": ["drive", "points", "profile"],
            "account_operation_counts": {"alice": 1},
            "summary": {
                "total_ops": 1,
                "hard_failures_excluding_503": 1,
                "server_busy_503": 0,
                "ops": {"profile": {}},
            },
        },
    ]

    summary = aggregate_rounds(rounds, ["alice", "bob", "carol"])

    assert summary["total_ops"] == 5
    assert summary["hard_failures"] == 1
    assert summary["server_busy_rate"] == 0.2
    assert summary["missing_operations"] == []
    assert summary["account_operation_counts"] == {"alice": 3, "bob": 2, "carol": 0}
    assert summary["accounts_without_operations"] == ["carol"]
    assert "drive_upload" in summary["operations_without_success"]
    assert set(summary["account_success_gaps"]) == {"alice", "bob", "carol"}
    assert len(summary["round_failures"]) == 1


def test_operational_soak_requires_eight_hours_for_signoff_and_redacts_commands():
    command = sanitized_command([
        "python3",
        "probe.py",
        "--root-password",
        "secret-root",
        "--accounts",
        "alice:secret-user,bob:secret-user",
        "--account-password=secret-account",
        "--manager-password",
        "secret-manager",
    ])

    assert MIN_SIGNOFF_SECONDS == 28_800
    assert command[-1] == "[redacted]"
    assert "secret-root" not in command
    assert "secret-user" not in " ".join(command)
    assert "secret-account" not in " ".join(command)
    assert "secret-manager" not in " ".join(command)


def test_operational_soak_uses_reentrant_auth_lock_and_full_rotation_contract():
    script = (ROOT / "scripts" / "testing" / "operational_soak_probe.py").read_text(encoding="utf-8")

    assert "threading.RLock()" in script
    assert '"--operation-mode", "rotation"' in script
    assert '"--require-all-accounts"' in script
    assert '"--require-operation-coverage"' in script
    assert "operations_without_success" in script
    assert "account_success_gaps" in script
    assert "OPERATION_COVERAGE" in script
    assert "production_signoff_eligible" in script
    assert "MIN_SIGNOFF_SECONDS = 8 * 60 * 60" in script
    assert "atomic_write_json(checkpoint_path" in script
    assert "source_harness_hashes = harness_hashes()" in script
    assert "detected_harness_drift = harness_drift(source_harness_hashes)" in script
    assert '"HACKME_STRESS_ACCOUNTS": account_spec' in script
    assert '"HACKME_POINTS_STRESS_ROOT_PASSWORD": args.root_password' in script
    assert '"--accounts", account_spec' not in script
    assert '"--root-password", args.root_password' not in script
    assert 'parser.add_argument("--max-sentinel-p95-ms"' in script


def test_operational_soak_aggregates_server_rss_and_database_evidence():
    rounds = [{
        "resource_monitor": {
            "sample_count": 2,
            "monitored_rss_max_mb": 125.5,
            "monitored_pid_count_max": 3,
            "monitored_pids_seen": [10, 11, 12],
            "mem_available_min_mb": 2048.0,
            "runtime_disk_free_min_mb": 1000.0,
            "first_sample": {"monitored_rss_mb": 100.0},
            "last_sample": {"monitored_rss_mb": 120.0},
            "db_peak": {"main": {"max_db_mb": 4.0, "max_wal_mb": 1.0, "last": {"db_bytes": 4_000_000}}},
        },
    }]

    evidence = aggregate_resource_evidence(rounds, "10, 11")

    assert evidence["server_pids"] == ["10", "11"]
    assert evidence["monitored_rss_first_mb"] == 100.0
    assert evidence["monitored_rss_last_mb"] == 120.0
    assert evidence["monitored_rss_max_mb"] == 125.5
    assert evidence["monitored_pid_count_max"] == 3
    assert evidence["monitored_pids_seen"] == [10, 11, 12]
    assert evidence["db_peak"]["main"]["max_db_mb"] == 4.0


def test_operational_soak_restricts_destructive_targets_and_artifacts_to_tmp(tmp_path):
    validate_run_policy("http://127.0.0.1:5000", tmp_path, owns_target=False)  # ci-safety: fixture-only
    validate_run_policy("https://staging.example.test", tmp_path, owns_target=True)

    try:
        validate_run_policy("https://staging.example.test", tmp_path, owns_target=False)
    except ValueError as exc:
        assert "--i-own-this-target" in str(exc)
    else:
        raise AssertionError("remote destructive target should require ownership acknowledgement")

    non_tmp_runtime = Path.home() / ".hackme-operational-soak-source-runtime-test"
    try:
        validate_run_policy("http://127.0.0.1:5000", non_tmp_runtime, owns_target=False)  # ci-safety: fixture-only
    except ValueError as exc:
        assert "under /tmp" in str(exc)
    else:
        raise AssertionError("source-repo runtime should be rejected")


def test_operational_sentinel_treats_requirements_not_ready_as_business_state():
    stats = SentinelStats()

    stats.record(
        "root",
        "/api/root/server-mode/requirements",
        {"status": 200, "ok": False, "elapsed_ms": 12, "body": {"missing": ["pytest"]}},
    )
    stats.record(
        "root",
        "/api/admin/security-center",
        {"status": 200, "ok": False, "elapsed_ms": 15, "body": {"msg": "broken"}},
    )
    summary = stats.summary()

    requirements = summary["checks"]["root:/api/root/server-mode/requirements"]
    assert requirements["body_not_ready"] == 1
    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["path"] == "/api/admin/security-center"


def test_operational_sentinel_tracks_controlled_busy_without_hard_failure():
    stats = SentinelStats()

    stats.record(
        "manager",
        "/api/community/boards",
        {
            "status": 503,
            "ok": False,
            "elapsed_ms": 20,
            "backpressure_rejected": True,
            "body": {"ok": False, "error": "server_busy", "msg": "目前是流量高峰"},
        },
    )
    summary = stats.summary()

    assert summary["errors"] == []
    assert summary["server_busy"] == 1
    assert summary["server_busy_rate"] == 1.0


def test_points_chain_stress_uses_explicit_finality_sweep_job():
    script = (ROOT / "scripts" / "testing" / "points_chain_destructive_stress.py").read_text(encoding="utf-8")

    assert "def run_finality_sweep_job" in script
    assert '"/api/root/points/finality-sweep"' in script
    assert "root_finalize_transfers" in script
    assert "root_observe_transfers_after_finality_sweep" in script
    assert "compact=1&sweep=0" in script


def test_points_chain_stress_finds_chain_seed_under_runtime_secrets(tmp_path):
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    expected = secret_dir / ".chain_seed"
    expected.write_text("seed", encoding="utf-8")

    assert chain_seed_path(str(tmp_path)) == expected


def test_bad_login_operation_treats_auth_rejection_as_expected():
    class FakeLoginClient:
        base_url = "https://127.0.0.1:0"
        timeout = 1

        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, *, name="login", expected=None):
            return {"ok": 401 in set(expected or []), "status": 401, "elapsed_ms": 1.0, "op": name}

    import scripts.testing.system_stress_probe as probe

    original_client = probe.Client
    try:
        probe.Client = FakeLoginClient
        result = run_operation(
            "bad_login",
            FakeLoginClient(),
            {},
            OperationBudget({"bad_login": 1}),
            1,
        )
    finally:
        probe.Client = original_client

    assert result["op"] == "bad_login"
    assert result["status"] == 401
    assert result["ok"] is True
