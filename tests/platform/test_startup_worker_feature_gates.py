import json
import pytest
from datetime import datetime

from services.server import startup


class _LoopExit(BaseException):
    pass


class _ThreadStub:
    def __init__(self, *, target, name=None, daemon=None):
        self.target = target
        self.name = name
        self.daemon = daemon

    def start(self):
        return None


def _capture_thread(monkeypatch):
    holder = {}

    def factory(*, target, name=None, daemon=None):
        thread = _ThreadStub(target=target, name=name, daemon=daemon)
        holder["thread"] = thread
        return thread

    monkeypatch.setattr(startup.threading, "Thread", factory)
    return holder


def test_points_worker_skips_when_economy_feature_disabled(monkeypatch):
    holder = _capture_thread(monkeypatch)
    calls = {"backup": 0, "seal": 0}

    class PointsStub:
        def create_scheduled_backup_if_due(self):
            calls["backup"] += 1
            return {"created": False}

        def seal_due_block(self, **kwargs):
            calls["seal"] += 1
            return {"sealed": False}

    monkeypatch.setattr(startup.time, "sleep", lambda seconds: (_ for _ in ()).throw(_LoopExit()))

    startup.start_points_chain_block_worker(
        points_service=PointsStub(),
        audit=lambda *args, **kwargs: None,
        default_block_ledger_threshold=10,
        default_block_max_interval_seconds=60,
        get_system_settings=lambda: {"feature_economy_enabled": False},
    )

    with pytest.raises(_LoopExit):
        holder["thread"].target()

    assert calls == {"backup": 0, "seal": 0}


def test_trading_liquidation_worker_skips_when_trading_feature_disabled(monkeypatch):
    holder = _capture_thread(monkeypatch)
    calls = {"match": 0, "liquidate": 0}

    class TradingStub:
        def match_open_limit_orders(self, **kwargs):
            calls["match"] += 1
            return {"matched": [], "errors": []}

        def scan_margin_liquidations(self, **kwargs):
            calls["liquidate"] += 1
            return {"liquidated": [], "errors": []}

    monkeypatch.setattr(startup.time, "sleep", lambda seconds: (_ for _ in ()).throw(_LoopExit()))

    startup.start_trading_liquidation_worker(
        trading_service=TradingStub(),
        audit=lambda *args, **kwargs: None,
        get_system_settings=lambda: {
            "feature_economy_enabled": True,
            "feature_trading_enabled": False,
        },
    )

    with pytest.raises(_LoopExit):
        holder["thread"].target()

    assert calls == {"match": 0, "liquidate": 0}


def test_points_worker_honors_pre_set_shutdown_event(monkeypatch):
    holder = _capture_thread(monkeypatch)
    calls = {"backup": 0, "seal": 0}

    class PointsStub:
        def create_scheduled_backup_if_due(self):
            calls["backup"] += 1
            return {"created": False}

        def seal_due_block(self, **kwargs):
            calls["seal"] += 1
            return {"sealed": False}

    stop_event = startup.threading.Event()
    stop_event.set()

    startup.start_points_chain_block_worker(
        points_service=PointsStub(),
        audit=lambda *args, **kwargs: None,
        default_block_ledger_threshold=10,
        default_block_max_interval_seconds=60,
        get_system_settings=lambda: {"feature_economy_enabled": True},
        shutdown_event=stop_event,
    )

    holder["thread"].target()
    assert calls == {"backup": 0, "seal": 0}


def test_trading_bot_worker_honors_pre_set_shutdown_event(monkeypatch):
    holder = _capture_thread(monkeypatch)
    calls = {"scan": 0}

    class TradingStub:
        def get_root_settings(self):
            return {"settings": {"enabled": True, "bot_auto_scan_enabled": True, "bot_auto_scan_interval_seconds": 30}}

        def run_due_trading_bots(self, **kwargs):
            calls["scan"] += 1
            return {"triggered": [], "failed": [], "scanned": 0}

    stop_event = startup.threading.Event()
    stop_event.set()

    startup.start_trading_bot_worker(
        trading_service=TradingStub(),
        audit=lambda *args, **kwargs: None,
        get_system_settings=lambda: {"feature_economy_enabled": True, "feature_trading_enabled": True},
        shutdown_event=stop_event,
    )

    holder["thread"].target()
    assert calls == {"scan": 0}


def test_admin_weekly_salary_schedule_uses_root_settings():
    payout_time = datetime(2026, 5, 4, 9, 0)
    year, week, _weekday = payout_time.isocalendar()

    assert startup._scheduled_admin_salary_week(
        {
            "points_admin_weekly_salary_enabled": True,
            "points_admin_weekly_salary_weekday": 1,
            "points_admin_weekly_salary_time": "09:00",
        },
        now=payout_time,
    ) == f"{int(year)}-W{int(week):02d}"
    assert startup._scheduled_admin_salary_week(
        {
            "points_admin_weekly_salary_enabled": True,
            "points_admin_weekly_salary_weekday": 1,
            "points_admin_weekly_salary_time": "09:00",
        },
        now=datetime(2026, 5, 4, 8, 59),
    ) is None
    assert startup._scheduled_admin_salary_week(
        {
            "points_admin_weekly_salary_enabled": False,
            "points_admin_weekly_salary_weekday": 1,
            "points_admin_weekly_salary_time": "09:00",
        },
        now=payout_time,
    ) is None


def test_points_bootstrap_initial_grants_run_when_economy_enabled_in_dev_ready():
    calls = []
    audits = []

    class PointsStub:
        def bootstrap_admin_initial_grants(self, **kwargs):
            calls.append(("genesis", kwargs))
            return {"created_count": 2}

        def award_admin_weekly_salaries(self, **kwargs):
            calls.append(("salary", kwargs))
            return {"created_count": 0, "salary_week": kwargs.get("salary_week")}

    result = startup.bootstrap_points_initial_grants_if_due(
        points_service=PointsStub(),
        get_system_settings=lambda: {
            "feature_economy_enabled": True,
            "points_admin_weekly_salary_enabled": False,
        },
        get_runtime_server_mode=lambda: "dev_ready",
        audit=lambda *args, **kwargs: audits.append((args, kwargs)),
    )

    assert result["ok"] is True
    assert result["skipped"] is False
    assert calls[0][0] == "genesis"
    assert calls[0][1]["seal_genesis"] is False
    assert audits and audits[0][0][0] == "POINTS_BOOTSTRAP_GRANTS"


def test_points_bootstrap_initial_grants_skip_internal_test_without_force():
    class PointsStub:
        def bootstrap_admin_initial_grants(self, **kwargs):
            raise AssertionError("should not write production ledger in internal_test")

    result = startup.bootstrap_points_initial_grants_if_due(
        points_service=PointsStub(),
        get_system_settings=lambda: {"feature_economy_enabled": True},
        get_runtime_server_mode=lambda: "internal_test",
        audit=lambda *args, **kwargs: None,
    )

    assert result["skipped"] is True
    assert result["mode"] == "internal_test"


def test_points_bootstrap_failure_audit_includes_safe_mode_forensics():
    audits = []

    class PointsStub:
        def bootstrap_admin_initial_grants(self, **kwargs):
            raise ValueError(
                "PointsChain safe mode active; ledger transaction is paused until "
                "branch/governance recovery resolves the incident"
            )

        def safe_mode_status(self):
            return {
                "safe_mode": True,
                "reason": "governance_clock_jump_detected",
                "forensic_bundle_id": "fb-clock-1",
                "verification": {
                    "violation": "wall_clock_fast_forward",
                    "observed_ip": "203.0.113.42",
                },
                "restore_plan": {"governance_recovery_required": True},
            }

    result = startup.bootstrap_points_initial_grants_if_due(
        points_service=PointsStub(),
        get_system_settings=lambda: {
            "feature_economy_enabled": True,
            "feature_points_chain_enabled": True,
        },
        get_runtime_server_mode=lambda: "production",
        audit=lambda *args, **kwargs: audits.append((args, kwargs)),
    )

    assert result["ok"] is False
    assert result["safe_mode"]["safe_mode"] is True
    assert audits and audits[0][0][0] == "POINTS_BOOTSTRAP_GRANTS_FAILED"
    assert audits[0][0][1] == "0.0.0.0"
    detail = json.loads(audits[0][1]["detail"])
    assert detail["severity"] == "critical"
    assert detail["attack_surface"] == "points_chain"
    assert detail["attack_method"] == "suspected_governance_clock_manipulation"
    assert detail["attack_classification"]["score"] >= detail["attack_classification"]["threshold"]
    assert detail["attack_classification"]["confidence"] in {"medium", "high"}
    assert detail["source"] == "server_startup.bootstrap_points_initial_grants"
    assert detail["source_ip"] == "0.0.0.0"
    assert "no direct client request IP" in detail["source_ip_note"]
    assert detail["safe_mode_reason"] == "governance_clock_jump_detected"
    assert detail["forensic_bundle_id"] == "fb-clock-1"
    assert detail["verification"]["violation"] == "wall_clock_fast_forward"
    assert any(item["value"] == "203.0.113.42" for item in detail["ip_evidence"])


def test_import_mode_workers_start_for_gunicorn_import_but_not_plain_import():
    env = {}

    assert startup.should_start_import_mode_workers(
        module_name="server",
        argv=["python", "-m", "gunicorn", "server:app"],
        environ=env,
    ) is True
    assert startup.should_start_import_mode_workers(
        module_name="server",
        argv=["python", "-"],
        environ=env,
    ) is False
    assert startup.should_start_import_mode_workers(
        module_name="__main__",
        argv=["python", "server.py"],
        environ=env,
    ) is False


def test_import_mode_workers_env_override_and_disable():
    assert startup.should_start_import_mode_workers(
        module_name="server",
        argv=["pytest"],
        environ={"HTML_LEARNING_START_IMPORT_WORKERS": "true"},
    ) is True
    assert startup.should_start_import_mode_workers(
        module_name="server",
        argv=["python", "-m", "gunicorn", "server:app"],
        environ={"HTML_LEARNING_DISABLE_IMPORT_WORKERS": "1", "HTML_LEARNING_START_IMPORT_WORKERS": "1"},
    ) is False
