import json
import os
import sys
import threading
import time
from pathlib import Path

from scripts.testing.points_chain_destructive_stress import (
    chain_seed_path,
    create_or_get_user,
    ensure_official_hot_wallet,
)
from scripts.testing.operational_soak_probe import (
    ApiClient,
    FORMAL_RAMP_LEVELS,
    MIN_SIGNOFF_SECONDS,
    SUPERVISED_LOAD_POLICIES,
    SentinelStats,
    aggregate_resource_evidence,
    aggregate_rounds,
    build_effective_load_sample,
    campaign_load_policy,
    configure_soak_storage_quota,
    finish_command,
    main as operational_soak_main,
    measured_active_workers,
    normalized_32_throughput,
    login_with_setup_backoff,
    provision_accounts,
    request_command_stop,
    round_rotation_offset,
    sanitized_command,
    start_command,
    setup_request_with_backoff,
    stop_control_reason,
    validate_run_policy,
)
from scripts.testing.system_stress_probe import (
    Client as StressClient,
    InflightWorkerTelemetry,
    OperationBudget,
    Stats,
    account_persona_assignments,
    persona_rotation_operation_account,
    record_operation_result,
    resolve_server_pids,
    resolve_session_pool_size,
    rotation_client_index,
    rotation_operation_account,
    run_in_client_slot,
    run_operation,
)


def test_setup_login_retries_controlled_rate_limit(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.results = [
                {"ok": False, "status": 429, "retry_after_seconds": 2.0},
                {"ok": True, "status": 200},
            ]

        def login(self):
            return dict(self.results.pop(0))

    waits = []
    monkeypatch.setattr("scripts.testing.operational_soak_probe.time.sleep", waits.append)
    result = login_with_setup_backoff(FakeClient(), attempts=3)

    assert result["ok"] is True
    assert result["attempts"] == 2
    assert result["setup_retry_wait_seconds"] == 2.0
    assert waits == [2.0]


def test_setup_request_does_not_retry_permission_denial(monkeypatch):
    class FakeClient:
        calls = 0

        def request(self, method, path, *, json_body=None):
            self.calls += 1
            return {"ok": False, "status": 403}

    waits = []
    client = FakeClient()
    monkeypatch.setattr("scripts.testing.operational_soak_probe.time.sleep", waits.append)
    result = setup_request_with_backoff(client, "POST", "/api/admin/users", attempts=8)

    assert result["status"] == 403
    assert result["attempts"] == 1
    assert client.calls == 1
    assert waits == []


def test_soak_api_client_refreshes_and_retries_exact_csrf_rejection(monkeypatch):
    class Response:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body
            self.headers = {}
            self.content = b"{}"
            self.text = json.dumps(body)

        def json(self):
            return self._body

    class Session:
        def __init__(self):
            self.verify = False
            self.cookies = {"csrf_token": "stale-csrf"}
            self.calls = 0

        def get(self, _url, **_kwargs):
            self.cookies["csrf_token"] = "fresh-csrf"
            return Response(200, {"ok": True, "csrf_token": "fresh-csrf"})

        def request(self, method, _url, *, headers=None, **_kwargs):
            assert method == "PUT"
            self.calls += 1
            if self.calls == 1:
                assert headers["X-CSRF-Token"] == "stale-csrf"
                return Response(403, {"ok": False, "error": "csrf_invalid"})
            assert headers["X-CSRF-Token"] == "fresh-csrf"
            self.cookies["csrf_token"] = "rotated-csrf"
            return Response(200, {"ok": True})

    monkeypatch.setattr("scripts.testing.operational_soak_probe.requests.Session", Session)
    client = ApiClient("https://soak.invalid", "root", "secret")
    client.csrf = "stale-csrf"

    result = client.request("PUT", "/api/root/storage/users/42/quota-override")

    assert result["ok"] is True
    assert result["status"] == 200
    assert result["csrf_retried"] is True
    assert result["initial_status"] == 403
    assert client.csrf == "rotated-csrf"
    assert client.session.calls == 2


def test_soak_storage_quota_is_scoped_and_verified():
    class FakeRoot:
        def __init__(self):
            self.calls = []

        def request(self, method, path, *, json_body=None):
            self.calls.append((method, path, json_body))
            return {
                "ok": True,
                "status": 200,
                "body": {
                    "ok": True,
                    "user": {
                        "total_bytes": 1024 * 1024 * 1024,
                        "max_file_size_bytes": 512 * 1024 * 1024,
                        "upload_rate_limit_per_day": 10_000,
                        "can_upload": True,
                    },
                },
            }

    root = FakeRoot()
    result = configure_soak_storage_quota(root, 42)

    assert result["ok"] is True
    assert root.calls == [
        (
            "PUT",
            "/api/root/storage/users/42/quota-override",
            {
                "quota_mb": 1024,
                "max_file_size_mb": 512,
                "upload_rate_limit_per_day": 10_000,
                "can_upload": True,
                "enabled": True,
                "reason": "isolated operational soak high-load account",
            },
        )
    ]


def test_soak_account_provisioning_applies_quota_before_member_login(monkeypatch):
    events = []

    class FakeRoot:
        base_url = "https://127.0.0.1:1"

        def request(self, method, path, *, json_body=None):
            events.append((method, path))
            if method == "GET":
                return {
                    "ok": True,
                    "status": 200,
                    "body": {"users": [{"id": 42, "username": "soak01"}]},
                }
            return {
                "ok": True,
                "status": 200,
                "body": {
                    "ok": True,
                    "user": {
                        "total_bytes": 1024 * 1024 * 1024,
                        "max_file_size_bytes": 512 * 1024 * 1024,
                        "upload_rate_limit_per_day": 10_000,
                        "can_upload": True,
                    },
                },
            }

    class FakeMember:
        def __init__(self, _base_url, username, _password):
            self.username = username

        def login(self):
            events.append(("LOGIN", self.username))
            return {"ok": True, "status": 200}

    monkeypatch.setattr("scripts.testing.operational_soak_probe.ApiClient", FakeMember)

    accounts = provision_accounts(FakeRoot(), prefix="soak", count=1, password="secret")

    assert accounts == [("soak01", "secret")]
    assert events == [
        ("GET", "/api/admin/users?q=soak01&page_size=100"),
        ("PUT", "/api/root/storage/users/42/quota-override"),
        ("LOGIN", "soak01"),
    ]


def test_native_worker_telemetry_does_not_treat_idle_executor_capacity_as_active():
    telemetry = InflightWorkerTelemetry(32, sample_interval_seconds=0.005)
    release = threading.Event()

    def operation() -> None:
        telemetry.begin_operation()
        try:
            release.wait(0.2)
        finally:
            telemetry.end_operation()

    telemetry.start()
    workers = [threading.Thread(target=operation) for _index in range(4)]
    for worker in workers:
        worker.start()
    assert telemetry.wait_until_sampled(1.0)
    release.set()
    for worker in workers:
        worker.join(timeout=1)
    result = telemetry.stop()

    assert result["configured_workers"] == 32
    assert result["active_workers_peak"] == 4
    assert 0 < result["sustained_active_workers"] <= 4
    assert result["active_worker_time_ratio_at_or_above_85_percent"] == 0.0
    assert result["complete"] is True


ROOT = Path(__file__).resolve().parents[3]


def test_standalone_soak_uses_bounded_non_ramping_policy():
    policy = campaign_load_policy("standalone")

    assert policy == SUPERVISED_LOAD_POLICIES["smoke"]
    assert policy["ramp_required"] is False


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


def test_truncated_server_busy_feature_gate_is_not_feature_disabled():
    stats = Stats()
    body = '{"error":"server_busy","gate":"feature","message":"請稍候 2 秒後再試。"'

    stats.record("profile", status=503, elapsed_ms=1.0, ok=False, error=body, body_sample=body)
    summary = stats.summary()

    assert summary["hard_failures_excluding_controlled_503"] == 1
    assert summary["ops"]["profile"]["unexpected_503"] == 1
    assert summary["ops"]["profile"]["feature_disabled_503"] == 0


def test_truncated_server_busy_with_rejection_proof_is_controlled():
    stats = Stats()
    body = '{"error":"server_busy","gate":"heavy","message":"請稍候 2 秒後再試。"'

    stats.record(
        "drive_upload",
        status=503,
        elapsed_ms=1.0,
        ok=False,
        error=body,
        body_sample=body,
        backpressure_rejected=True,
    )
    summary = stats.summary()

    assert summary["server_busy_503"] == 1
    assert summary["hard_failures_excluding_controlled_503"] == 0
    assert summary["ops"]["drive_upload"]["server_busy_503"] == 1


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
    assert summary["accounts"]["opsim-a"]["expected_success_operations"] == {"drive_list": 1}
    assert summary["ops"]["points_wallet"]["successful_2xx"] == 1
    assert summary["accounts"]["opsim-b"]["total_ops"] == 2
    assert summary["accounts"]["opsim-b"]["failed_ops"] == 1


def test_rotation_assigns_every_operation_to_every_account_before_repeating():
    operations = ["drive", "video", "trading"]
    accounts = ["alice", "bob"]

    assignments = [rotation_operation_account(index, operations, accounts) for index in range(6)]

    assert assignments == [
        ("drive", "alice"),
        ("drive", "bob"),
        ("video", "alice"),
        ("video", "bob"),
        ("trading", "alice"),
        ("trading", "bob"),
    ]


def test_repeated_soak_rounds_continue_rotation_instead_of_restarting_account_one():
    operations = ["drive", "video", "trading"]
    accounts = ["alice", "bob", "carol"]

    first_round = rotation_operation_account(round_rotation_offset(1, 3), operations, accounts)
    second_round = rotation_operation_account(round_rotation_offset(2, 3), operations, accounts)
    third_round = rotation_operation_account(round_rotation_offset(3, 3), operations, accounts)

    assert first_round == ("drive", "alice")
    assert second_round == ("video", "alice")
    assert third_round == ("trading", "alice")


def test_rotation_spreads_each_accounts_operations_across_clone_clients():
    # 24 accounts and 128 clones gives the first executor wave 128 distinct
    # client slots: eight accounts have six clones, sixteen have five.
    clone_counts = [6] * 8 + [5] * 16
    slots = set()
    for task_id in range(128):
        account_index = task_id % 24
        clone_index = rotation_client_index(task_id, 24, clone_counts[account_index])
        slots.add((account_index, clone_index))

    assert len(slots) == 128


def test_persona_rotation_preserves_full_baseline_then_specializes() -> None:
    operations = [
        "hf_status",
        "hf_quote",
        "hf_generate",
        "ai_agent_status",
        "ai_agent_tools",
        "jobs",
        "trading_markets",
        "trading_dashboard",
        "trading_asset_overview",
        "trading_bots",
        "trading_grid_bots",
        "trading_workflows",
        "trading_grid_preview",
        "bad_login",
        "remote_direct_reject",
        "bt_reject",
        "community_bad_thread",
        "chat_bad_message",
        "me",
    ]
    accounts = ["i2i-user", "trading-user", "security-user"]
    assignments = account_persona_assignments(accounts, operations)
    baseline_span = len(operations) * len(accounts)

    assert assignments["i2i-user"]["persona_id"] == "i2i_comfyui_research"
    assert assignments["trading-user"]["persona_id"] == "exchange_spot_lending_bots"
    assert assignments["security-user"]["persona_id"] == "web_security_adversary"
    assert persona_rotation_operation_account(0, operations, accounts, assignments) == (
        "hf_status",
        "i2i-user",
        "baseline",
    )
    assert persona_rotation_operation_account(
        baseline_span,
        operations,
        accounts,
        assignments,
    ) == ("hf_status", "i2i-user", "i2i_comfyui_research")


def test_expected_security_rejection_is_persona_success_but_not_2xx() -> None:
    stats = Stats()

    stats.record("bad_login", status=401, elapsed_ms=5, ok=True, account="security-user")
    summary = stats.summary()["accounts"]["security-user"]

    assert summary["expected_success_operations"] == {"bad_login": 1}
    assert summary["successful_operations"] == {}


def test_worker_telemetry_excludes_threads_waiting_for_same_client_slot():
    class FakeClient:
        lock = threading.RLock()

    client = FakeClient()
    telemetry = InflightWorkerTelemetry(4, sample_interval_seconds=0.005)
    entered = threading.Event()
    release = threading.Event()

    def operation():
        entered.set()
        release.wait(0.2)
        return {"ok": True}

    telemetry.start()
    workers = [
        threading.Thread(target=run_in_client_slot, args=(client, telemetry, operation))
        for _index in range(4)
    ]
    for worker in workers:
        worker.start()
    assert entered.wait(1)
    assert telemetry.wait_until_sampled(1)
    release.set()
    for worker in workers:
        worker.join(timeout=1)
    summary = telemetry.stop()

    assert summary["active_workers_peak"] == 1
    assert summary["operations_started"] == 4
    assert summary["operations_completed"] == 4


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


def test_operational_soak_defers_cross_campaign_heavy_success_without_relabeling_fallbacks():
    script = (ROOT / "scripts" / "testing" / "operational_soak_probe.py").read_text(encoding="utf-8")
    command_start = script.index('str(SYSTEM_STRESS),')
    command_end = script.index("if args.server_pids:", command_start)
    system_round_command = script[command_start:command_end]

    assert 'SOAK_DEFERRED_SUCCESS_OPERATIONS = frozenset({"hf_generate", "hls_master"})' in script
    assert '"--require-all-accounts"' in system_round_command
    assert '"--require-operation-coverage"' in system_round_command
    assert '"--require-operation-success"' not in system_round_command
    assert '"--require-account-success"' not in system_round_command


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


def test_hf_generate_zero_budget_records_status_fallback_not_positive_generate():
    calls = []

    class FakeClient:
        def request(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"op": args[0], "ok": True, "status": 200, "elapsed_ms": 1.0}

    result = run_operation(
        "hf_generate",
        FakeClient(),
        {},
        OperationBudget({"hf_generate": 0}),
        1,
    )
    stats = Stats()
    recorded = record_operation_result(
        stats,
        requested_operation="hf_generate",
        result=result,
        account="alice",
    )
    summary = stats.summary()

    assert calls[0][0][:3] == ("hf_generate_fallback_status", "GET", "/api/comfyui/status")
    assert recorded == "hf_generate_fallback_status"
    assert "hf_generate" not in summary["ops"]
    assert summary["ops"]["hf_generate_fallback_status"]["successful_2xx"] == 1
    assert summary["accounts"]["alice"]["successful_operations"] == {
        "hf_generate_fallback_status": 1
    }
    aggregate = aggregate_rounds(
        [
            {
                "ok": True,
                "registered_operations": ["hf_generate"],
                "account_operation_counts": {"alice": 1},
                "summary": summary,
            }
        ],
        ["alice"],
    )
    assert "hf_generate" in aggregate["missing_operations"]
    assert "hf_generate" not in aggregate["operations_without_success"]
    assert "hf_generate" in aggregate["deferred_success_operations"]


def test_actual_result_operation_name_preserves_other_budget_fallbacks():
    stats = Stats()

    recorded = record_operation_result(
        stats,
        requested_operation="drive_upload",
        result={
            "op": "drive_upload_fallback_list",
            "ok": True,
            "status": 200,
            "elapsed_ms": 2.0,
        },
        account="bob",
    )
    summary = stats.summary()

    assert recorded == "drive_upload_fallback_list"
    assert "drive_upload" not in summary["ops"]
    assert summary["ops"]["drive_upload_fallback_list"]["successful_2xx"] == 1


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


def test_operational_soak_aggregates_specialist_persona_evidence() -> None:
    contract = {
        "persona_id": "exchange_spot_lending_bots",
        "category": "exchange_spot_lending_margin_and_bots",
        "operations": ["trading_dashboard", "trading_bots"],
        "invariant_focus": ["decimal_recalculation"],
        "required_expected_operations": ["trading_dashboard", "trading_bots"],
        "deferred_terminal_operations": [],
    }
    rounds = [
        {
            "ok": True,
            "persona_coverage": {
                "trader": {
                    **contract,
                    "dispatch_counts": {"trading_dashboard": 2},
                    "expected_success_counts": {"trading_dashboard": 2, "trading_bots": 0},
                }
            },
        },
        {
            "ok": True,
            "persona_coverage": {
                "trader": {
                    **contract,
                    "dispatch_counts": {"trading_bots": 1},
                    "expected_success_counts": {"trading_dashboard": 0, "trading_bots": 1},
                }
            },
        },
    ]

    summary = aggregate_rounds(rounds, ["trader"])

    assert summary["persona_success_gaps"] == {}
    assert summary["persona_contract_conflicts"] == []
    assert summary["persona_coverage"]["trader"]["ok"] is True
    assert summary["persona_coverage"]["trader"]["expected_success_counts"] == {
        "trading_dashboard": 2,
        "trading_bots": 1,
    }


def test_formal_operational_soak_requires_real_4_8_16_32_ramp() -> None:
    assert FORMAL_RAMP_LEVELS == (4, 8, 16, 32)
    assert normalized_32_throughput(
        operations_completed=400,
        window_seconds=60,
        scheduled_load_level=8,
    ) == 1600.0


def test_idle_configured_workers_do_not_count_as_effective_target_load() -> None:
    native_worker_telemetry = {
        "schema_version": "hackme.system-stress-worker-telemetry.v1",
        "method": "native_inflight_operation_counter_time_samples",
        "configured_workers": 32,
        "sample_count": 20,
        "active_worker_histogram": {"4": 20},
        "sustained_active_workers": 4,
        "active_workers_at_stop": 0,
        "complete": True,
    }
    payload = {
        "ok": True,
        "total_ops_requested": 1_000,
        "worker_telemetry": native_worker_telemetry,
        "summary": {"total_ops": 1_000, "server_busy_503": 0},
    }
    # All 32 executor threads can exist (/proc sees 35 with orchestrators), but
    # native operation entry/exit sampling proves only four did work.
    run = {
        "returncode": 0,
        "partial": False,
        "terminal_status": "COMPLETED",
        "elapsed_seconds": 60.0,
        "process_thread_count_peak": 35,
        "process_thread_sample_count": 20,
    }

    sample = build_effective_load_sample(
        payload=payload,
        run=run,
        scheduled_load_level=32,
        expected_operations=1_000,
        baseline_32_operations_per_minute=1_000.0,
        window_started_at="2026-07-13T00:00:00Z",
    )

    assert measured_active_workers(run, 32, payload) == 4
    assert sample["worker_measurement"]["proc_task_active_worker_upper_bound"] == 32
    assert sample["worker_measurement"]["measured_active_workers"] == 4
    assert sample["target_conditions"]["active_workers_at_least_28"] is False
    assert sample["at_target_load"] is False
    assert "ACTIVE_WORKERS_BELOW_28" in sample["target_failure_reasons"]


def test_high_worker_count_without_required_throughput_is_not_target_load() -> None:
    sample = build_effective_load_sample(
        payload={
            "ok": True,
            "total_ops_requested": 1_000,
            "degraded_reasons": ["ordinary_p95_above_configured_limit"],
            "worker_telemetry": {
                "schema_version": "hackme.system-stress-worker-telemetry.v1",
                "method": "native_inflight_operation_counter_time_samples",
                "configured_workers": 32,
                "sample_count": 20,
                "active_worker_histogram": {"32": 20},
                "sustained_active_workers": 32,
                "active_workers_at_stop": 0,
                "complete": True,
            },
            "summary": {"total_ops": 100, "server_busy_503": 0},
        },
        run={
            "returncode": 0,
            "partial": False,
            "terminal_status": "COMPLETED",
            "elapsed_seconds": 60.0,
            "process_thread_count_peak": 35,
            "process_thread_sample_count": 20,
        },
        scheduled_load_level=32,
        expected_operations=1_000,
        baseline_32_operations_per_minute=1_000.0,
        window_started_at="2026-07-13T00:00:00Z",
    )

    assert sample["active_workers"] == 32
    assert sample["target_conditions"]["throughput_at_least_baseline_80_percent"] is False
    assert sample["target_conditions"]["effective_load_ratio_at_least_0_85"] is False
    assert sample["degradation_reason"] == "LATENCY_HIGH"
    assert sample["at_target_load"] is False


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
    assert '"--rotation-offset"' in script
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


def test_operational_soak_client_tracks_csrf_cookie_rotated_by_each_write():
    class Response:
        status_code = 200
        content = b"{}"
        text = ""
        headers: dict[str, str] = {}

        def json(self) -> dict[str, object]:
            return {"ok": True}

    class Session:
        def __init__(self):
            self.cookies = {"csrf_token": "csrf-1"}
            self.expected_csrf = "csrf-1"
            self.write_count = 0

        def request(self, method: str, _url: str, *, headers=None, **_kwargs: object) -> Response:
            assert method == "POST"
            assert (headers or {}).get("X-CSRF-Token") == self.expected_csrf
            self.write_count += 1
            self.expected_csrf = f"csrf-{self.write_count + 1}"
            self.cookies["csrf_token"] = self.expected_csrf
            return Response()

    client = ApiClient("https://soak.invalid", "root", "secret")
    client.session = Session()  # type: ignore[assignment]
    client.csrf = "csrf-1"

    for index in range(12):
        result = client.request("POST", "/api/admin/users", json_body={"index": index})
        assert result["ok"] is True

    assert client.csrf == "csrf-13"
    assert client.session.write_count == 12


def test_system_stress_client_tracks_csrf_cookie_rotated_by_each_write():
    class Response:
        status_code = 200
        content = b"{}"
        text = "{}"
        headers: dict[str, str] = {}

    class Session:
        def __init__(self):
            self.cookies = {"csrf_token": "csrf-1"}
            self.expected_csrf = "csrf-1"
            self.write_count = 0

        def request(self, method: str, _url: str, *, headers=None, **_kwargs: object) -> Response:
            assert method == "POST"
            assert (headers or {}).get("X-CSRF-Token") == self.expected_csrf
            self.write_count += 1
            self.expected_csrf = f"csrf-{self.write_count + 1}"
            self.cookies["csrf_token"] = self.expected_csrf
            return Response()

    client = StressClient("https://stress.invalid", "member", "secret")
    client.session = Session()  # type: ignore[assignment]
    client.csrf = "csrf-1"

    for index in range(12):
        result = client.request("write", "POST", "/api/community/threads", json={"index": index})
        assert result["ok"] is True

    assert client.csrf == "csrf-13"
    assert client.session.write_count == 12


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


def test_system_stress_resolves_explicit_or_runtime_pidfile(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    (runtime_root / "server.pid").write_text("1234\n", encoding="utf-8")

    assert resolve_server_pids("44, 55", str(runtime_root)) == ([44, 55], "explicit")
    discovered, source = resolve_server_pids("", str(runtime_root))
    assert discovered == [1234]
    assert source == f"pidfile:{runtime_root / 'server.pid'}"


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

    try:
        validate_run_policy("https://user:secret@127.0.0.1:54321", tmp_path, owns_target=False)
    except ValueError as exc:
        assert "must not contain credentials" in str(exc)
    else:
        raise AssertionError("credentials embedded in the base URL should be rejected")


def test_operational_soak_child_honors_external_stop_without_waiting_for_round_timeout(tmp_path):
    state = start_command(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout_path=tmp_path / "child.stdout",
    )
    stop_file = tmp_path / "campaign.stop"
    stop_file.write_text("stop", encoding="utf-8")
    started = time.monotonic()

    result = finish_command(state, timeout=120, stop_file=stop_file)

    assert time.monotonic() - started < 5
    assert result["stopped_by_control"] is True
    assert result["stop_reason"] == "external_stop_file"
    assert result["timed_out"] is False
    assert result["partial"] is True
    assert result["terminal_status"] == "NOT_EVALUATED"
    assert result["returncode"] != 0
    assert result["orphan_pids"] == []


def test_operational_soak_child_honors_campaign_deadline_and_is_not_fake_pass(tmp_path):
    state = start_command(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout_path=tmp_path / "deadline-child.stdout",
    )
    started = time.monotonic()

    result = finish_command(
        state,
        timeout=120,
        stop_at_monotonic=time.monotonic() + 0.2,
    )

    assert time.monotonic() - started < 3
    assert result["stopped_by_control"] is True
    assert result["stop_reason"] == "campaign_deadline"
    assert result["partial"] is True
    assert result["terminal_status"] == "NOT_EVALUATED"
    assert result["returncode"] != 0
    assert result["orphan_pids"] == []


def test_operational_soak_deadline_signals_round_points_and_browser_at_same_edge(tmp_path):
    states = {
        name: start_command(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout_path=tmp_path / f"{name}.stdout",
        )
        for name in ("round", "points", "browser")
    }

    round_result = finish_command(
        states["round"],
        timeout=120,
        stop_at_monotonic=time.monotonic() + 0.2,
        on_stop=lambda reason: (
            request_command_stop(states["points"], reason),
            request_command_stop(states["browser"], reason),
        ),
    )
    points_result = finish_command(states["points"], timeout=120)
    browser_result = finish_command(states["browser"], timeout=120)

    for result in (round_result, points_result, browser_result):
        assert result["partial"] is True
        assert result["terminal_status"] == "NOT_EVALUATED"
        assert result["stop_reason"] == "campaign_deadline"
        assert result["returncode"] != 0
        assert result["orphan_pids"] == []


def test_operational_soak_command_timeout_is_partial_harness_evidence(tmp_path):
    state = start_command(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout_path=tmp_path / "timeout-child.stdout",
    )

    result = finish_command(state, timeout=1)

    assert result["returncode"] == 124
    assert result["timed_out"] is True
    assert result["stopped_by_control"] is False
    assert result["stop_reason"] == "command_timeout"
    assert result["partial"] is True
    assert result["terminal_status"] == "TIMEOUT"
    assert result["orphan_pids"] == []


def test_operational_soak_stop_kills_escaped_descendant_without_orphan(tmp_path):
    child_pid_path = tmp_path / "escaped-child.pid"
    parent_code = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], start_new_session=True); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8'); "
        "time.sleep(60)"
    )
    state = start_command(
        [sys.executable, "-c", parent_code, str(child_pid_path)],
        stdout_path=tmp_path / "escaped-parent.stdout",
    )
    deadline = time.monotonic() + 5
    while not child_pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert child_pid_path.exists()
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    # Allow finish_command's process-tree sampler to observe the escaped child.
    time.sleep(1.05)
    stop_file = tmp_path / "campaign.stop"
    stop_file.write_text("stop", encoding="utf-8")

    result = finish_command(state, timeout=120, stop_file=stop_file)

    assert result["orphan_pids"] == []
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        pass
    else:
        stat = Path(f"/proc/{child_pid}/stat").read_text(encoding="utf-8")
        assert stat[stat.rfind(")") + 2 :].split()[0] == "Z"


def test_operational_soak_stop_control_reason_is_allowlisted_token_only(tmp_path):
    stop_file = tmp_path / "campaign.stop.json"
    stop_file.write_text(
        json.dumps({"reason": "required_continuous_active_duration_completed", "secret": "do-not-copy"}),
        encoding="utf-8",
    )

    assert stop_control_reason(stop_file) == "required_continuous_active_duration_completed"

    stop_file.write_text(json.dumps({"reason": "secret value with spaces"}), encoding="utf-8")
    assert stop_control_reason(stop_file) == "unrecognized"


def test_operational_soak_has_no_markdown_or_post_deadline_final_browser():
    script = (ROOT / "scripts" / "testing" / "operational_soak_probe.py").read_text(encoding="utf-8")

    assert "write_markdown" not in script
    assert 'f"browser_{len(browser_runs) + 1:03d}_final"' not in script


def test_operational_soak_preexisting_stop_writes_secret_free_terminal_json(tmp_path, monkeypatch):
    stop_file = tmp_path / "campaign.stop.json"
    stop_file.write_text(json.dumps({"reason": "campaign_runner_exception"}), encoding="utf-8")
    out_path = tmp_path / "terminal.json"
    secret = "must-not-appear-in-terminal-json"
    monkeypatch.setattr(sys, "argv", [
        "operational_soak_probe.py",
        "--base-url", "http://127.0.0.1:9",  # ci-safety: no request occurs after preexisting stop
        "--runtime-root", str(tmp_path),
        "--out", str(out_path),
        "--duration-seconds", "1",
        "--allow-short-duration",
        "--account-count", "2",
        "--root-password", secret,
        "--manager-password", secret,
        "--account-password", secret,
        "--test-password", secret,
        "--skip-points-stress",
        "--skip-browser",
        "--stop-file", str(stop_file),
    ])

    returncode = operational_soak_main()
    payload_text = out_path.read_text(encoding="utf-8")
    payload = json.loads(payload_text)
    checkpoint = json.loads(
        (tmp_path / "reports" / "operational_soak" / "operational_soak.checkpoint.json").read_text(encoding="utf-8")
    )

    assert returncode == 3
    assert payload["terminal_status"] == "INTERRUPTED"
    assert payload["termination_reason"] == "external_stop_file_preexisting"
    assert checkpoint["status"] == "terminal"
    assert secret not in payload_text
    assert not list(tmp_path.rglob("*.md"))


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


def test_points_chain_fixture_onboarding_retries_controlled_server_busy(monkeypatch):
    class Client:
        username = "stress-user"

        def __init__(self):
            self.calls = 0

        def request(self, method, path, **_kwargs):
            self.calls += 1
            if path == "/api/points/wallet" or method == "GET":
                return {"status": 200, "wallet": {}}
            if self.calls < 4:
                return {"status": 503, "error": "server_busy", "retry_after_seconds": 0.01}
            return {"status": 200, "wallet_identity": {"address": "pc0stress"}}

    monkeypatch.setattr("scripts.testing.points_chain_destructive_stress.time.sleep", lambda _seconds: None)
    client = Client()

    assert ensure_official_hot_wallet(client) == "pc0stress"
    assert client.calls == 4


def test_points_chain_user_provisioning_retries_management_rate_limit(monkeypatch):
    class Client:
        def __init__(self):
            self.responses = [
                {"status": 429, "error": "edge_rate_limited", "retry_after_seconds": 3},
                {"status": 200, "users": []},
                {"status": 429, "error": "edge_rate_limited", "retry_after_seconds": 2},
                {"status": 200, "ok": True},
                {"status": 200, "users": [{"id": 42, "username": "stress-user"}]},
            ]

        def request(self, _method, _path, **_kwargs):
            return self.responses.pop(0)

    waits = []
    monkeypatch.setattr("scripts.testing.points_chain_destructive_stress.time.sleep", waits.append)

    result = create_or_get_user(Client(), "stress-user", "unused-test-password")

    assert result == {"id": 42, "username": "stress-user", "created": True}
    assert waits == [3.0, 2.0]


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
