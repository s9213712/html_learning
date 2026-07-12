from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from types import SimpleNamespace

from scripts.testing import predeploy_capacity_probe as probe


def _args(**overrides):
    values = {
        "continue_after_app_limit": False,
        "hard_p95_ms": 8000,
        "hard_max_ms": 20000,
        "target_p95_ms": 1500,
        "ux_p95_ms": 2000,
        "ux_p99_ms": 4000,
        "ux_confirm_rounds": 3,
        "cpu_sample_interval": 0.01,
        "progress_interval": 0.0,
        "progress_active_limit": 0,
        "no_progress": True,
        "request_timeout": 1.0,
        "gunicorn_max_requests": 10000,
        "gunicorn_max_requests_jitter": 1000,
        "root_password": "root",
        "close_connections": False,
        "load_profile": "normal",
        "load_kinds": "",
        "heavy_repeat": 1,
        "heavy_upload_bytes": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _probe_summary(**overrides):
    payload = {
        "hard_failure_count": 0,
        "server_failure_count": 0,
        "unexpected_failure_count": 0,
        "app_limit_count": 0,
        "status_counts": {"200": 1},
        "latency_ms": {"p95": 100, "p99": 120, "max": 150},
        "cpu": {"active_worker_peak": 2, "total_worker_cpu_peak_percent": 120},
    }
    payload.update(overrides)
    return payload


def test_expected_503_with_ok_true_is_not_server_failure():
    summary = probe.summarize_samples(
        [
            {
                "label": "root snapshots list",
                "status": 503,
                "ok": True,
                "body": {"code": "feature_disabled"},
                "elapsed_ms": 12,
            }
        ],
        elapsed_seconds=0.25,
    )

    assert summary["ok"] is True
    assert summary["server_failure_count"] == 0
    assert summary["hard_failure_count"] == 0
    assert summary["status_counts"]["503"] == 1

    failed, reasons = probe.profile_failed(summary, _args())
    assert failed is False
    assert "server_busy" not in reasons


def test_server_busy_503_with_ok_false_is_server_failure():
    summary = probe.summarize_samples(
        [
                {
                    "label": "me",
                    "status": 503,
                    "ok": False,
                    "body": {"error": "server_busy"},
                    "backpressure_rejected": True,
                    "elapsed_ms": 4,
                }
        ],
        elapsed_seconds=0.1,
    )

    assert summary["ok"] is False
    assert summary["server_failure_count"] == 1
    assert summary["hard_failure_count"] == 1

    failed, reasons = probe.profile_failed(summary, _args())
    assert failed is True
    assert "server_busy" in reasons


def test_contaminated_after_app_limit_round_is_not_recommended():
    args = _args(target_p95_ms=500)
    results = [
        {
            "profile": {"workers": 2, "threads": 6, "label": "2x6"},
            "rounds": [
                {
                    "accounts": 12,
                    "contaminated_after_app_limit": False,
                    "probe": _probe_summary(),
                },
                {
                    "accounts": 96,
                    "contaminated_after_app_limit": True,
                    "probe": _probe_summary(),
                },
            ],
        }
    ]

    recommendation = probe.choose_recommendation(results, args)

    assert recommendation["ok"] is True
    assert recommendation["max_passing_accounts"] == 12
    assert recommendation["workers"] == 2
    assert recommendation["threads"] == 6


def test_rc1_capacity_gate_is_machine_readable_for_selected_round():
    args = _args(target_p95_ms=500)
    results = [
        {
            "profile": {"workers": 3, "threads": 6, "label": "3x6"},
            "rounds": [
                {
                    "accounts": 24,
                    "contaminated_after_app_limit": False,
                    "probe": _probe_summary(
                        latency_ms={"p95": 180, "p99": 250, "max": 400},
                        cpu={"active_worker_peak": 3, "total_worker_cpu_peak_percent": 180},
                    ),
                }
            ],
        }
    ]
    recommendation = probe.choose_recommendation(results, args)
    limits = probe.build_limit_report(results, args)

    gate = probe.build_rc1_capacity_gate(results, recommendation, limits)

    assert gate["pass"] is True
    assert gate["reasons"] == []
    assert gate["recommended_profile"] == "3x6"
    assert gate["max_safe_accounts"] == 24
    assert gate["selected_round_latency"]["p95"] == 180
    assert gate["selected_round_cpu"]["active_worker_peak"] == 3
    for key in (
        "ux_degradation_at",
        "server_instability_at",
        "app_limit_at",
        "selected_round_latency",
        "selected_round_cpu",
    ):
        assert key in gate


def test_ux_degradation_requires_all_confirmation_rounds():
    args = _args(ux_p95_ms=1000, ux_p99_ms=2000, ux_confirm_rounds=3)
    results = [
        {
            "profile": {"workers": 2, "threads": 6, "label": "2x6"},
            "rounds": [
                {
                    "accounts": 24,
                    "contaminated_after_app_limit": False,
                    "probe": _probe_summary(latency_ms={"p95": 1200, "p99": 1600, "max": 1800}),
                    "ux_confirmation_rounds": [
                        {"attempt": 1, "probe": _probe_summary(latency_ms={"p95": 1300, "p99": 1700, "max": 1900})},
                        {"attempt": 2, "probe": _probe_summary(latency_ms={"p95": 900, "p99": 1200, "max": 1500})},
                        {"attempt": 3, "probe": _probe_summary(latency_ms={"p95": 1250, "p99": 1650, "max": 1850})},
                    ],
                }
            ],
        }
    ]

    limits = probe.build_limit_report(results, args)

    assert (limits["experience"] or {})["degradation_starts_at"] is None
    assert (limits["experience"] or {})["max_accounts_before_degradation"]["accounts"] == 24


def test_ux_degradation_is_reported_after_three_confirmations():
    args = _args(ux_p95_ms=1000, ux_p99_ms=2000, ux_confirm_rounds=3)
    round_result = {
        "accounts": 24,
        "contaminated_after_app_limit": False,
        "probe": _probe_summary(latency_ms={"p95": 1200, "p99": 1600, "max": 1800}),
        "ux_confirmation_rounds": [
            {"attempt": 1, "probe": _probe_summary(latency_ms={"p95": 1300, "p99": 1700, "max": 1900})},
            {"attempt": 2, "probe": _probe_summary(latency_ms={"p95": 1100, "p99": 1500, "max": 1800})},
            {"attempt": 3, "probe": _probe_summary(latency_ms={"p95": 1250, "p99": 1650, "max": 1850})},
        ],
    }
    round_result["ux_confirmation"] = probe.build_ux_confirmation_summary(round_result, args)
    results = [{"profile": {"workers": 2, "threads": 6, "label": "2x6"}, "rounds": [round_result]}]

    limits = probe.build_limit_report(results, args)

    assert round_result["ux_confirmation"]["confirmed"] is True
    assert (limits["experience"] or {})["degradation_starts_at"]["accounts"] == 24
    assert (limits["experience"] or {})["degradation_starts_at"]["ux_confirmation"]["degraded_count"] == 3


def test_run_load_round_allocates_worker_for_root_flow(monkeypatch, tmp_path):
    captured = {}

    class CapturingExecutor(RealThreadPoolExecutor):
        def __init__(self, max_workers=None, *args, **kwargs):
            captured["max_workers"] = max_workers
            super().__init__(max_workers=max_workers, *args, **kwargs)

    class DummyCpuSampler:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def summary(self):
            return {"sample_count": 0}

    class DummyProgress:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(probe, "ThreadPoolExecutor", CapturingExecutor)
    monkeypatch.setattr(probe, "CpuSampler", DummyCpuSampler)
    monkeypatch.setattr(probe, "RoundProgress", DummyProgress)
    monkeypatch.setattr(
        probe,
        "collect_official_hot_wallet_addresses",
        lambda *_args, **_kwargs: {"u1": "pc0u1", "u2": "pc0u2", "u3": "pc0u3"},
    )
    monkeypatch.setattr(
        probe,
        "exercise_user",
        lambda *_args, **_kwargs: [{"label": "user", "status": 200, "ok": True, "elapsed_ms": 1}],
    )
    monkeypatch.setattr(
        probe,
        "exercise_root_points_chain",
        lambda *_args, **_kwargs: [{"label": "root", "status": 200, "ok": True, "elapsed_ms": 1}],
    )

    instance = SimpleNamespace(
        profile=SimpleNamespace(label="1x2"),
        base_url="https://127.0.0.1:1",
        run_root=tmp_path,
        master_pid=0,
    )
    summary = probe.run_load_round(instance, ["u1", "u2", "u3"], 3, _args())

    assert captured["max_workers"] == 4
    assert summary["sample_count"] == 4


def test_capacity_transfer_sink_username_fits_account_limit():
    username = probe.capacity_transfer_sink_username("20260526T034407Z_4x6_12_abcdefghijklmnopqrstuvwxyz")

    assert username.startswith("cap_sink_")
    assert len(username) <= 32


def test_capacity_server_launcher_keeps_credentials_out_of_argv(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return SimpleNamespace(
            returncode=0,
            stdout=f"[dev-tmp] pid: 43210\n[dev-tmp] log: {tmp_path / 'server.out'}\n",
        )

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    monkeypatch.setattr(probe, "wait_for_server", lambda *_args, **_kwargs: None)
    args = SimpleNamespace(
        gunicorn_max_requests=100,
        gunicorn_max_requests_jitter=10,
        root_password="capacity-root-secret",
        manager_password="capacity-manager-secret",
        test_password="capacity-test-secret",
        install=False,
        keep_app_limits=False,
        disable_backpressure=False,
        startup_timeout=1,
    )
    profile = probe.Profile(workers=2, threads=4)

    probe.start_isolated_server(args, profile, tmp_path, 55123)

    command_text = " ".join(captured["command"])
    assert "--root-password" not in captured["command"]
    assert "--manager-password" not in captured["command"]
    assert "--test-password" not in captured["command"]
    assert "capacity-root-secret" not in command_text
    assert captured["env"]["ROOT_PASSWORD"] == "capacity-root-secret"
    assert captured["env"]["MANAGER_PASSWORD"] == "capacity-manager-secret"
    assert captured["env"]["TEST_PASSWORD"] == "capacity-test-secret"
