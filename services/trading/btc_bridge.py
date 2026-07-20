import csv
import json
import os
import subprocess
import sys
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.core.sqlite_hardening import connect_sqlite
from services.server.runtime import default_runtime_root_path


ASSET_SCALE = 100_000_000
DEFAULT_TIMEFRAME = "4h"
DEFAULT_BTC_TRADE_REPO_URL = "https://github.com/s9213712/BTC_trade.git"
DEFAULT_BTC_TRADE_BRANCH = "strategy/v15b-plus"
BTC_TRADE_BUILD_STEPS = [
    ("下載行情資料", ["update_data.py"]),
    ("訓練模型", ["retrain_models.py", "--timeframe", "4h"]),
    ("產生最新預測", ["hourly_check.py", "--timeframe", "4h"]),
    ("產生回測報告", ["backtest_report.py", "--timeframe", "4h"]),
]
BTC_TRADE_FALLBACK_DEPENDENCIES = ["pandas", "numpy", "ccxt", "requests", "scikit-learn", "ta", "pytest"]
BTC_TRADE_TIMEFRAME_SECONDS = {"1h": 60 * 60, "4h": 4 * 60 * 60, "1d": 24 * 60 * 60}
BTC_TRADE_FRESHNESS_GRACE_SECONDS = 30 * 60
BTC_TRADE_START_WAIT_SECONDS = 180
BTC_TRADE_MODEL_SEARCH_DIRS = ("models", "artifacts", "checkpoints", "runtime/models", "outputs")
BTC_TRADE_MODEL_FILE_PATTERNS = ("*.pkl", "*.joblib", "*.pt", "*.pth", "*.onnx", "*.keras", "*.h5", "*.cbm")
BTC_TRADE_SIGNAL_KEYS = {
    "bar_ts",
    "generated_at",
    "strategy_version",
    "report_title",
    "signal_ok",
    "ml_ok",
    "position",
    "entry_price",
    "entry_atr",
    "current_price",
    "fear_greed",
    "capital",
    "btc",
    "total_equity",
    "total_pnl_pct",
    "hold_bars",
    "entry_checks",
    "ml_status",
    "report_text",
    "telegram_text",
    "timeframe",
}
BTC_TRADE_START_JOBS = {}
BTC_TRADE_START_JOB_LOCK = threading.Lock()


def expand_server_path(raw_path):
    value = str(raw_path or "").strip()
    if not value:
        return None
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def default_runtime_root(base_dir=None):
    explicit = expand_server_path(os.environ.get("HACKME_RUNTIME_DIR"))
    if explicit:
        return explicit
    return default_runtime_root_path()


def default_btc_trade_project_dir(base_dir=None):
    root = Path(base_dir).resolve() if base_dir else Path(__file__).resolve().parents[2]
    return root / "external" / "BTC_trade"


def _safe_text_tail(value, limit=2000):
    text = str(value or "")
    return text[-limit:] if len(text) > limit else text


def _run_step(command, *, cwd, timeout):
    started = time.time()
    try:
        run_kwargs = {
            "cwd": str(cwd),
            "text": True,
            "capture_output": True,
            "check": False,
        }
        if timeout is not None:
            run_kwargs["timeout"] = timeout
        proc = subprocess.run(command, **run_kwargs)
        return {
            "command": " ".join(command),
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "seconds": round(time.time() - started, 2),
            "stdout_tail": _safe_text_tail(proc.stdout),
            "stderr_tail": _safe_text_tail(proc.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": " ".join(command),
            "ok": False,
            "returncode": None,
            "seconds": round(time.time() - started, 2),
            "stdout_tail": _safe_text_tail(exc.stdout),
            "stderr_tail": _safe_text_tail(exc.stderr),
            "error": "timeout",
        }


def _run_git(args, *, cwd=None, timeout=180):
    return _run_step(["git", *args], cwd=cwd or Path.cwd(), timeout=timeout)


def _seed_btc_trade_report_log(root):
    runtime = root / "runtime"
    report_path = runtime / "report_log_4h.jsonl"
    if report_path.is_file() and report_path.stat().st_size > 0:
        return {"ok": True, "skipped": True, "message": "report log already exists"}
    data_path = root / "data" / "btc_4h.csv"
    if not data_path.is_file():
        return {"ok": False, "message": "btc_4h.csv not found"}
    last_row = None
    with open(data_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row:
                last_row = row
    if not last_row:
        return {"ok": False, "message": "btc_4h.csv is empty"}
    try:
        price = float(last_row.get("close") or 0)
    except Exception:
        price = 0
    now = datetime.now(timezone.utc).isoformat()
    runtime.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": now,
        "bar_ts": last_row.get("timestamp") or now,
        "strategy_version": "bootstrap",
        "report_title": "BTC_trade bootstrap report",
        "signal_ok": False,
        "ml_ok": False,
        "position": "BOOTSTRAP",
        "current_price": price,
        "capital": 10000,
        "btc": 0,
        "total_equity": 10000,
        "total_pnl_pct": 0,
        "timeframe": "4h",
        "report_text": "bootstrap report for first prediction run",
        "telegram_text": "bootstrap report for first prediction run",
    }
    with open(report_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return {"ok": True, "skipped": False, "message": "bootstrap report log created", "path": str(report_path)}


def _utc_iso_from_timestamp(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def _tail_csv_timestamp(path):
    if not path.is_file():
        return None
    last_row = None
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row:
                last_row = row
    if not last_row:
        return None
    for key in ("timestamp", "time", "datetime", "date"):
        parsed = _parse_btc_trade_time(last_row.get(key))
        if parsed:
            return parsed
    return None


def _collect_model_artifacts(root):
    found = []
    for relative in BTC_TRADE_MODEL_SEARCH_DIRS:
        directory = root / relative
        if not directory.is_dir():
            continue
        for pattern in BTC_TRADE_MODEL_FILE_PATTERNS:
            found.extend(path for path in directory.rglob(pattern) if path.is_file())
    deduped = []
    seen = set()
    for path in sorted(found):
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _artifact_freshness_info(path, *, now_ts, freshness_window_seconds, reference_ts=None):
    info = {
        "path": str(path) if path else "",
        "exists": bool(path and path.is_file()),
        "updated_at": None,
        "age_seconds": None,
        "fresh": False,
        "needs_refresh": True,
        "reason": "missing",
    }
    if not path or not path.is_file():
        return info
    stat = path.stat()
    age_seconds = max(0, int(now_ts - stat.st_mtime))
    info["updated_at"] = _utc_iso_from_timestamp(stat.st_mtime)
    info["age_seconds"] = age_seconds
    is_fresh_by_age = age_seconds <= freshness_window_seconds
    is_fresh_by_reference = reference_ts is None or stat.st_mtime + 1 >= reference_ts
    info["fresh"] = bool(is_fresh_by_age and is_fresh_by_reference)
    info["needs_refresh"] = not info["fresh"]
    if info["fresh"]:
        info["reason"] = "fresh"
    elif not is_fresh_by_age:
        info["reason"] = "stale"
    else:
        info["reason"] = "older_than_reference"
    return info


def btc_trade_artifact_status(project_dir, *, timeframe=DEFAULT_TIMEFRAME, now_ts=None):
    root = expand_server_path(project_dir)
    now_ts = float(now_ts if now_ts is not None else time.time())
    interval_seconds = BTC_TRADE_TIMEFRAME_SECONDS.get(str(timeframe or DEFAULT_TIMEFRAME).lower(), 4 * 60 * 60)
    freshness_window_seconds = interval_seconds + BTC_TRADE_FRESHNESS_GRACE_SECONDS
    if not root or not root.is_dir():
        return {
            "project_dir": str(root) if root else "",
            "timeframe": str(timeframe or DEFAULT_TIMEFRAME).lower(),
            "data": {"exists": False, "fresh": False, "needs_update": True, "reason": "missing"},
            "models": {"exists": False, "fresh": False, "needs_retrain": True, "reason": "missing", "count": 0},
            "prediction": {"exists": False, "fresh": False, "needs_refresh": True, "reason": "missing"},
        }
    data_path = root / "data" / f"btc_{str(timeframe or DEFAULT_TIMEFRAME).lower()}.csv"
    report_path = root / "runtime" / f"report_log_{str(timeframe or DEFAULT_TIMEFRAME).lower()}.jsonl"
    model_files = _collect_model_artifacts(root)
    data_last_bar = _tail_csv_timestamp(data_path)
    data_reference_ts = data_last_bar.timestamp() if data_last_bar else None
    data_info = _artifact_freshness_info(
        data_path,
        now_ts=now_ts,
        freshness_window_seconds=freshness_window_seconds,
        reference_ts=data_reference_ts,
    )
    data_info["last_bar_at"] = data_last_bar.isoformat() if data_last_bar else None
    data_info["needs_update"] = bool(not data_info["exists"] or not data_info["fresh"])
    latest_model = max(model_files, key=lambda path: path.stat().st_mtime) if model_files else None
    model_reference_ts = None
    if data_path.is_file():
        model_reference_ts = data_path.stat().st_mtime
    model_info = _artifact_freshness_info(
        latest_model,
        now_ts=now_ts,
        freshness_window_seconds=freshness_window_seconds * 7,
        reference_ts=model_reference_ts,
    )
    model_info["count"] = len(model_files)
    model_info["latest_path"] = str(latest_model) if latest_model else ""
    model_info["needs_retrain"] = bool(not model_info["exists"] or model_info["reason"] == "older_than_reference")
    prediction_reference_ts = max(
        ts for ts in (
            data_path.stat().st_mtime if data_path.is_file() else None,
            latest_model.stat().st_mtime if latest_model and latest_model.is_file() else None,
        )
        if ts is not None
    ) if (data_path.is_file() or latest_model) else None
    prediction_info = _artifact_freshness_info(
        report_path,
        now_ts=now_ts,
        freshness_window_seconds=freshness_window_seconds,
        reference_ts=prediction_reference_ts,
    )
    prediction_info["needs_refresh"] = bool(not prediction_info["exists"] or not prediction_info["fresh"])
    try:
        latest_report = _latest_jsonl_record(report_path)
        prediction_info["generated_at"] = latest_report.get("generated_at")
        prediction_info["bar_ts"] = latest_report.get("bar_ts")
        prediction_info["strategy_version"] = latest_report.get("strategy_version")
    except Exception:
        pass
    return {
        "project_dir": str(root),
        "timeframe": str(timeframe or DEFAULT_TIMEFRAME).lower(),
        "freshness_window_seconds": freshness_window_seconds,
        "data": data_info,
        "models": model_info,
        "prediction": prediction_info,
    }


def _wait_for_prediction_report(root, *, timeframe=DEFAULT_TIMEFRAME, previous_mtime=None, previous_generated_at=None, timeout_seconds=BTC_TRADE_START_WAIT_SECONDS):
    report_path = root / "runtime" / f"report_log_{str(timeframe or DEFAULT_TIMEFRAME).lower()}.jsonl"
    deadline = time.time() + max(1, int(timeout_seconds or BTC_TRADE_START_WAIT_SECONDS))
    while time.time() <= deadline:
        if report_path.is_file():
            current_mtime = report_path.stat().st_mtime
            generated_at = None
            try:
                latest = _latest_jsonl_record(report_path)
                generated_at = latest.get("generated_at")
            except Exception:
                latest = None
            if previous_mtime is None or current_mtime > previous_mtime + 0.5:
                return {
                    "ok": True,
                    "refreshed": True,
                    "report_mtime": current_mtime,
                    "generated_at": generated_at,
                    "message": "已等到新的 BTC_trade 預測資料",
                }
            if generated_at and generated_at != previous_generated_at:
                return {
                    "ok": True,
                    "refreshed": True,
                    "report_mtime": current_mtime,
                    "generated_at": generated_at,
                    "message": "BTC_trade 預測資料已更新",
                }
            readiness = btc_trade_artifact_status(root, timeframe=timeframe, now_ts=time.time())
            if readiness.get("prediction", {}).get("exists") and not readiness.get("prediction", {}).get("needs_refresh"):
                return {
                    "ok": True,
                    "refreshed": False,
                    "report_mtime": current_mtime,
                    "generated_at": generated_at,
                    "message": "預測腳本已執行，沿用仍在有效期內的最新預測資料",
                }
        time.sleep(1)
    return {
        "ok": False,
        "refreshed": False,
        "message": "預測腳本已執行，但在等待時間內沒有看到新的預測資料",
    }


def _validate_repo_url(repo_url):
    value = str(repo_url or DEFAULT_BTC_TRADE_REPO_URL).strip()
    if not value.startswith("https://github.com/") or not value.endswith(".git"):
        raise ValueError("BTC_trade repo URL must be an https GitHub .git URL")
    return value


def _validate_branch(branch):
    value = str(branch or DEFAULT_BTC_TRADE_BRANCH).strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-+")
    if not value or len(value) > 160 or any(ch not in allowed for ch in value):
        raise ValueError("BTC_trade branch name is invalid")
    return value


def btc_trade_setup(project_dir=None, *, repo_url=None, branch=None, base_dir=None, timeout_per_step=900):
    """Clone/update BTC_trade and run its build steps.

    This function is intentionally best-effort for the web app integration:
    callers should surface failures to root, but a failure here must not break
    the trading page.
    """
    root = expand_server_path(project_dir) or default_btc_trade_project_dir(base_dir)
    repo_url = str(repo_url or DEFAULT_BTC_TRADE_REPO_URL).strip()
    branch = str(branch or DEFAULT_BTC_TRADE_BRANCH).strip()
    steps = []
    try:
        repo_url = _validate_repo_url(repo_url)
        branch = _validate_branch(branch)
        if root.exists() and not (root / ".git").is_dir():
            return {
                "ok": False,
                "project_dir": str(root),
                "repo_url": repo_url,
                "branch": branch,
                "steps": steps,
                "status": btc_trade_status(root),
                "message": "BTC_trade 目錄已存在但不是 Git repo，請改用空目錄或自行建置後再填路徑",
            }
        if (root / ".git").is_dir():
            for label, args in (
                ("抓取最新分支", ["fetch", "origin", branch]),
                ("切換分支", ["checkout", branch]),
                ("快轉更新", ["pull", "--ff-only", "origin", branch]),
            ):
                step = _run_git(args, cwd=root, timeout=240)
                step["label"] = label
                steps.append(step)
                if not step["ok"]:
                    status = btc_trade_status(root)
                    return {
                        "ok": False,
                        "project_dir": str(root),
                        "repo_url": repo_url,
                        "branch": branch,
                        "steps": steps,
                        "status": status,
                        "message": f"{label}失敗，請自行檢查 BTC_trade repo 狀態",
                    }
        else:
            root.parent.mkdir(parents=True, exist_ok=True)
            step = _run_git(["clone", "--branch", branch, "--depth", "1", repo_url, str(root)], cwd=root.parent, timeout=900)
            step["label"] = "下載 BTC_trade"
            steps.append(step)
            if not step["ok"]:
                return {
                    "ok": False,
                    "project_dir": str(root),
                    "repo_url": repo_url,
                    "branch": branch,
                    "steps": steps,
                    "status": btc_trade_status(root),
                    "message": "下載 BTC_trade 失敗，請自行建置後回來檢查",
                }
        requirements = sorted(root.glob("requirements*.txt"))
        if requirements:
            for req in requirements:
                step = _run_step([sys.executable, "-m", "pip", "install", "-r", str(req)], cwd=root, timeout=timeout_per_step)
                step["label"] = f"安裝依賴 {req.name}"
                steps.append(step)
                if not step["ok"]:
                    return {
                        "ok": False,
                        "project_dir": str(root),
                        "repo_url": repo_url,
                        "branch": branch,
                        "steps": steps,
                        "status": btc_trade_status(root),
                        "message": f"安裝 BTC_trade 依賴失敗：{req.name}，請自行建置",
                    }
        else:
            step = _run_step([sys.executable, "-m", "pip", "install", *BTC_TRADE_FALLBACK_DEPENDENCIES], cwd=root, timeout=timeout_per_step)
            step["label"] = "安裝 BTC_trade 預設依賴"
            steps.append(step)
            if not step["ok"]:
                return {
                    "ok": False,
                    "project_dir": str(root),
                    "repo_url": repo_url,
                    "branch": branch,
                    "steps": steps,
                    "status": btc_trade_status(root),
                    "message": "安裝 BTC_trade 預設依賴失敗，請自行建置",
                }
        for label, script_args in BTC_TRADE_BUILD_STEPS:
            script_path = root / script_args[0]
            if not script_path.is_file():
                steps.append({
                    "label": label,
                    "command": f"{sys.executable} {' '.join(script_args)}",
                    "ok": False,
                    "error": "missing_script",
                    "message": f"缺少 {script_args[0]}",
                })
                return {
                    "ok": False,
                    "project_dir": str(root),
                    "repo_url": repo_url,
                    "branch": branch,
                    "steps": steps,
                    "status": btc_trade_status(root),
                    "message": f"BTC_trade 缺少 {script_args[0]}，請自行建置",
                }
            if script_args[0] == "hourly_check.py":
                seed_result = _seed_btc_trade_report_log(root)
                seed_step = {
                    "label": "初始化 BTC_trade runtime 報告",
                    "command": "internal seed report_log_4h.jsonl",
                    "ok": bool(seed_result.get("ok")),
                    "message": seed_result.get("message"),
                    "skipped": bool(seed_result.get("skipped")),
                }
                steps.append(seed_step)
                if not seed_step["ok"]:
                    return {
                        "ok": False,
                        "project_dir": str(root),
                        "repo_url": repo_url,
                        "branch": branch,
                        "steps": steps,
                        "status": btc_trade_status(root),
                        "message": "初始化 BTC_trade runtime 報告失敗，請自行建置",
                    }
            step = _run_step([sys.executable, *script_args], cwd=root, timeout=timeout_per_step)
            step["label"] = label
            steps.append(step)
            if not step["ok"]:
                return {
                    "ok": False,
                    "project_dir": str(root),
                    "repo_url": repo_url,
                    "branch": branch,
                    "steps": steps,
                    "status": btc_trade_status(root),
                    "message": f"{label}失敗，請自行建置 BTC_trade",
                }
        status = btc_trade_status(root)
        return {
            "ok": bool(status.get("available")),
            "project_dir": str(root),
            "repo_url": repo_url,
            "branch": branch,
            "steps": steps,
            "status": status,
            "message": "BTC_trade 建置完成" if status.get("available") else "BTC_trade 建置完成但信號仍不可用，請自行檢查 runtime 報告",
        }
    except Exception as exc:
        return {
            "ok": False,
            "project_dir": str(root),
            "repo_url": repo_url,
            "branch": branch,
            "steps": steps,
            "status": btc_trade_status(root),
            "message": f"BTC_trade 建置失敗：{exc.__class__.__name__}",
        }


def _load_json_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _parse_btc_trade_time(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _btc_trade_next_prediction(signal, *, timeframe=DEFAULT_TIMEFRAME, fallback_updated_at=None):
    interval = BTC_TRADE_TIMEFRAME_SECONDS.get(str(timeframe or DEFAULT_TIMEFRAME).lower(), 4 * 60 * 60)
    base = (
        _parse_btc_trade_time(signal.get("bar_ts"))
        or _parse_btc_trade_time(signal.get("generated_at"))
        or _parse_btc_trade_time(fallback_updated_at)
    )
    if not base:
        return None
    now = datetime.now(timezone.utc)
    next_at = base + timedelta(seconds=interval)
    while next_at <= now:
        next_at += timedelta(seconds=interval)
    return {
        "next_prediction_at": next_at.isoformat(),
        "next_prediction_seconds": max(0, int((next_at - now).total_seconds())),
        "prediction_interval_seconds": interval,
    }


def _latest_jsonl_record(path):
    latest_line = ""
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                latest_line = line.strip()
    if not latest_line:
        raise ValueError("empty report log")
    payload = json.loads(latest_line)
    if not isinstance(payload, dict):
        raise ValueError("latest report is not an object")
    return payload


def btc_trade_status(project_dir):
    root = expand_server_path(project_dir)
    if not root:
        return {
            "configured": False,
            "available": False,
            "needs_initialization": True,
            "message": "root 尚未設定 BTC_trade 專案資料夾",
        }
    runtime = root / "runtime"
    report_path = runtime / "report_log_4h.jsonl"
    portfolio_path = runtime / "portfolio_state_4h.json"
    trade_log_path = runtime / "trade_log_4h.json"
    checks = {
        "project_dir": root.is_dir(),
        "hourly_check": (root / "hourly_check.py").is_file(),
        "update_data": (root / "update_data.py").is_file(),
        "retrain_models": (root / "retrain_models.py").is_file(),
        "backtest_report": (root / "backtest_report.py").is_file(),
        "runtime_dir": runtime.is_dir(),
        "report_log": report_path.is_file(),
    }
    missing = [name for name, ok in checks.items() if not ok]
    payload = {
        "configured": True,
        "available": False,
        "needs_initialization": bool(missing),
        "checks": checks,
        "missing": missing,
        "message": "",
        "project_dir": str(root),
        "commands": [
            "python3 -m pip install pandas numpy ccxt requests scikit-learn ta pytest",
            "python3 update_data.py",
            "python3 retrain_models.py --timeframe 4h",
            "python3 hourly_check.py --timeframe 4h",
            "python3 backtest_report.py --timeframe 4h",
        ],
        "artifacts": btc_trade_artifact_status(root),
    }
    if missing:
        payload["message"] = "BTC_trade 專案尚未可用，請先在該資料夾執行初始化或產生信號報告"
        return payload
    try:
        latest = _latest_jsonl_record(report_path)
    except Exception as exc:
        payload["needs_initialization"] = True
        payload["message"] = f"BTC_trade 信號報告無法讀取：{exc.__class__.__name__}"
        return payload
    signal = {key: latest.get(key) for key in BTC_TRADE_SIGNAL_KEYS if key in latest}
    timeframe = str(signal.get("timeframe") or DEFAULT_TIMEFRAME).lower()
    signal["timeframe"] = timeframe
    signal["source"] = "BTC_trade/runtime/report_log_4h.jsonl"
    try:
        stat = report_path.stat()
        signal["updated_at"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        signal["age_seconds"] = max(0, int(time.time() - stat.st_mtime))
    except Exception:
        pass
    if portfolio_path.is_file():
        try:
            portfolio = _load_json_file(portfolio_path)
            if isinstance(portfolio, dict):
                signal["portfolio"] = {
                    "position": portfolio.get("position"),
                    "capital": portfolio.get("capital"),
                    "cash": portfolio.get("cash", portfolio.get("capital")),
                    "btc": portfolio.get("btc"),
                    "entry_price": portfolio.get("entry_price"),
                    "total_equity": portfolio.get("total_equity"),
                    "updated_at": portfolio.get("updated_at") or portfolio.get("timestamp") or portfolio.get("last_bar_ts"),
                }
        except Exception:
            pass
    if trade_log_path.is_file():
        try:
            trades = _load_json_file(trade_log_path)
            if isinstance(trades, list) and trades:
                last_trade = trades[-1] if isinstance(trades[-1], dict) else {}
                signal["last_trade"] = {
                    "action": last_trade.get("action"),
                    "timestamp": last_trade.get("timestamp"),
                    "pnl_pct": last_trade.get("pnl_pct"),
                    "exit_reason": last_trade.get("exit_reason") or last_trade.get("reason"),
                    "strategy_version": last_trade.get("strategy_version"),
                    "batch": last_trade.get("batch"),
                }
        except Exception:
            pass
    next_prediction = _btc_trade_next_prediction(signal, timeframe=timeframe, fallback_updated_at=signal.get("updated_at"))
    if next_prediction:
        signal.update(next_prediction)
    payload["available"] = True
    payload["needs_initialization"] = False
    payload["message"] = "BTC_trade 信號可用"
    payload["signal"] = signal
    payload["artifacts"] = btc_trade_artifact_status(root, timeframe=timeframe)
    return payload


def _btc_trade_job_payload(job):
    return {
        "job_id": job.get("job_id"),
        "project_dir": job.get("project_dir"),
        "timeframe": job.get("timeframe"),
        "status": job.get("status"),
        "message": job.get("message"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "steps": list(job.get("steps") or []),
        "result": job.get("result"),
    }


def btc_trade_start_prediction_pipeline(
    project_dir,
    *,
    timeframe=DEFAULT_TIMEFRAME,
    timeout_per_step=None,
    wait_seconds=BTC_TRADE_START_WAIT_SECONDS,
    progress_cb=None,
    setup_if_needed=False,
    repo_url=None,
    branch=None,
    base_dir=None,
):
    root = expand_server_path(project_dir) or default_btc_trade_project_dir(base_dir)
    status = btc_trade_status(root)
    steps = []

    def record_step(step):
        steps.append(step)
        if callable(progress_cb):
            progress_cb({"steps": list(steps), "latest_step": step})

    if not root:
        return {
            "ok": False,
            "project_dir": "",
            "timeframe": str(timeframe or DEFAULT_TIMEFRAME).lower(),
            "steps": steps,
            "status": status,
            "message": "尚未設定 BTC_trade 專案資料夾",
        }
    required_scripts = {
        "update_data.py": root / "update_data.py",
        "retrain_models.py": root / "retrain_models.py",
        "hourly_check.py": root / "hourly_check.py",
    }
    missing_scripts = [name for name, path in required_scripts.items() if not path.is_file()]
    if missing_scripts and setup_if_needed:
        record_step({
            "label": "自動下載/安裝 BTC_trade",
            "ok": True,
            "skipped": False,
            "message": "偵測到 BTC_trade 尚未可用，開始自動下載/更新、安裝依賴並建置",
        })
        setup_result = btc_trade_setup(
            root,
            repo_url=repo_url,
            branch=branch,
            base_dir=base_dir,
            timeout_per_step=timeout_per_step or 900,
        )
        for setup_step in setup_result.get("steps") or []:
            copied_step = dict(setup_step)
            copied_step["label"] = f"準備 BTC_trade：{copied_step.get('label') or copied_step.get('command') or 'step'}"
            record_step(copied_step)
        status = setup_result.get("status") or btc_trade_status(root)
        missing_scripts = [name for name, path in required_scripts.items() if not path.is_file()]
        if not setup_result.get("ok") or missing_scripts:
            return {
                "ok": False,
                "project_dir": str(root),
                "timeframe": str(timeframe or DEFAULT_TIMEFRAME).lower(),
                "steps": steps,
                "status": status,
                "message": setup_result.get("message") or f"BTC_trade 自動準備失敗：缺少 {', '.join(missing_scripts)}",
            }
    if missing_scripts:
        return {
            "ok": False,
            "project_dir": str(root),
            "timeframe": str(timeframe or DEFAULT_TIMEFRAME).lower(),
            "steps": steps,
            "status": status,
            "message": f"BTC_trade 缺少必要腳本：{', '.join(missing_scripts)}",
        }

    readiness_before = btc_trade_artifact_status(root, timeframe=timeframe)
    record_step({
        "label": "檢查資料與模型狀態",
        "ok": True,
        "skipped": False,
        "message": (
            f"data={'需更新' if readiness_before['data']['needs_update'] else '已是最新'}；"
            f"model={'需重訓' if readiness_before['models']['needs_retrain'] else '已是最新'}；"
            f"prediction={'需刷新' if readiness_before['prediction']['needs_refresh'] else '可沿用'}"
        ),
        "readiness": readiness_before,
    })

    data_updated = False
    model_retrained = False

    if readiness_before["data"]["needs_update"]:
        step = _run_step([sys.executable, "update_data.py"], cwd=root, timeout=timeout_per_step)
        step["label"] = "更新 BTC_trade 資料"
        record_step(step)
        if not step["ok"]:
            return {
                "ok": False,
                "project_dir": str(root),
                "timeframe": str(timeframe or DEFAULT_TIMEFRAME).lower(),
                "steps": steps,
                "status": btc_trade_status(root),
                "message": "BTC_trade 資料更新失敗",
            }
        data_updated = True
    else:
        record_step({"label": "更新 BTC_trade 資料", "ok": True, "skipped": True, "message": "資料仍在有效期內，略過更新"})

    readiness_after_data = btc_trade_artifact_status(root, timeframe=timeframe)
    retrain_needed = bool(data_updated or readiness_after_data["models"]["needs_retrain"])
    if retrain_needed:
        step = _run_step([sys.executable, "retrain_models.py", "--timeframe", str(timeframe or DEFAULT_TIMEFRAME)], cwd=root, timeout=timeout_per_step)
        step["label"] = "重訓 BTC_trade 模型"
        record_step(step)
        if not step["ok"]:
            return {
                "ok": False,
                "project_dir": str(root),
                "timeframe": str(timeframe or DEFAULT_TIMEFRAME).lower(),
                "steps": steps,
                "status": btc_trade_status(root),
                "message": "BTC_trade 模型重訓失敗",
            }
        model_retrained = True
    else:
        record_step({"label": "重訓 BTC_trade 模型", "ok": True, "skipped": True, "message": "模型已晚於資料，不需重訓"})

    report_path = root / "runtime" / f"report_log_{str(timeframe or DEFAULT_TIMEFRAME).lower()}.jsonl"
    before_mtime = report_path.stat().st_mtime if report_path.is_file() else None
    before_generated_at = None
    if report_path.is_file():
        try:
            before_generated_at = _latest_jsonl_record(report_path).get("generated_at")
        except Exception:
            before_generated_at = None
    prediction_step = _run_step([sys.executable, "hourly_check.py", "--timeframe", str(timeframe or DEFAULT_TIMEFRAME)], cwd=root, timeout=timeout_per_step)
    prediction_step["label"] = "執行 BTC_trade 預測"
    record_step(prediction_step)
    if not prediction_step["ok"]:
        return {
            "ok": False,
            "project_dir": str(root),
            "timeframe": str(timeframe or DEFAULT_TIMEFRAME).lower(),
            "steps": steps,
            "status": btc_trade_status(root),
            "message": "BTC_trade 預測腳本執行失敗",
        }

    wait_result = _wait_for_prediction_report(
        root,
        timeframe=timeframe,
        previous_mtime=before_mtime,
        previous_generated_at=before_generated_at,
        timeout_seconds=wait_seconds,
    )
    wait_step = {
        "label": "等待 BTC_trade 預測資料",
        "ok": bool(wait_result.get("ok")),
        "skipped": False,
        "message": wait_result.get("message"),
        "refreshed": bool(wait_result.get("refreshed")),
    }
    record_step(wait_step)
    final_status = btc_trade_status(root)
    return {
        "ok": bool(wait_result.get("ok")) and bool(final_status.get("available")),
        "project_dir": str(root),
        "timeframe": str(timeframe or DEFAULT_TIMEFRAME).lower(),
        "steps": steps,
        "status": final_status,
        "actions": {
            "data_updated": data_updated,
            "model_retrained": model_retrained,
            "prediction_refreshed": bool(wait_result.get("refreshed")),
        },
        "message": wait_result.get("message") if wait_result.get("ok") else (wait_result.get("message") or "BTC_trade 預測資料未完成"),
    }


def btc_trade_start_prediction_job(
    project_dir,
    *,
    timeframe=DEFAULT_TIMEFRAME,
    wait_seconds=BTC_TRADE_START_WAIT_SECONDS,
    repo_url=None,
    branch=None,
    base_dir=None,
    setup_if_needed=True,
):
    root = expand_server_path(project_dir) or default_btc_trade_project_dir(base_dir)
    project_key = f"{str(root) if root else ''}|{str(timeframe or DEFAULT_TIMEFRAME).lower()}"
    with BTC_TRADE_START_JOB_LOCK:
        for job in BTC_TRADE_START_JOBS.values():
            if job.get("project_key") == project_key and job.get("status") in {"queued", "running"}:
                return {"ok": True, "job_ok": True, "started": False, "job": _btc_trade_job_payload(job)}
        job_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        done_event = threading.Event()
        job = {
            "job_id": job_id,
            "project_key": project_key,
            "project_dir": str(root) if root else "",
            "timeframe": str(timeframe or DEFAULT_TIMEFRAME).lower(),
            "status": "queued",
            "message": "已建立 BTC_trade 一鍵啟動工作，等待背景執行",
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "steps": [],
            "result": None,
            "_done_event": done_event,
        }
        BTC_TRADE_START_JOBS[job_id] = job

    def update_job(payload):
        with BTC_TRADE_START_JOB_LOCK:
            current = BTC_TRADE_START_JOBS.get(job_id)
            if not current:
                return
            current.update(payload)

    def worker():
        update_job({
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "message": "BTC_trade 背景工作執行中：必要時自動下載/安裝，再更新資料、重訓並預測",
        })
        try:
            result = btc_trade_start_prediction_pipeline(
                root,
                timeframe=timeframe,
                timeout_per_step=None,
                wait_seconds=wait_seconds,
                setup_if_needed=setup_if_needed,
                repo_url=repo_url,
                branch=branch,
                base_dir=base_dir,
                progress_cb=lambda payload: update_job({
                    "steps": payload.get("steps"),
                    "message": payload.get("latest_step", {}).get("message") or payload.get("latest_step", {}).get("label") or "BTC_trade 背景工作執行中",
                }),
            )
            update_job({
                "status": "completed" if result.get("ok") else "error",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "message": result.get("message") or ("BTC_trade 一鍵啟動完成" if result.get("ok") else "BTC_trade 一鍵啟動失敗"),
                "steps": list(result.get("steps") or []),
                "result": result,
            })
            done_event.set()
        except Exception as exc:
            update_job({
                "status": "error",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "message": f"BTC_trade 背景工作失敗：{exc.__class__.__name__}",
                "result": {
                    "ok": False,
                    "project_dir": str(root) if root else "",
                    "timeframe": str(timeframe or DEFAULT_TIMEFRAME).lower(),
                    "steps": list(BTC_TRADE_START_JOBS.get(job_id, {}).get("steps") or []),
                    "message": f"BTC_trade 背景工作失敗：{exc.__class__.__name__}",
                },
            })
            done_event.set()

    threading.Thread(target=worker, name=f"btc-trade-start-{job_id[:8]}", daemon=True).start()
    response_wait_seconds = min(max(int(wait_seconds or 0), 0), 5)
    if response_wait_seconds:
        done_event.wait(response_wait_seconds)
    with BTC_TRADE_START_JOB_LOCK:
        payload = _btc_trade_job_payload(BTC_TRADE_START_JOBS[job_id])
    job_ok = payload.get("status") != "error"
    return {"ok": job_ok, "job_ok": job_ok, "started": True, "job": payload}


def btc_trade_start_prediction_job_status(job_id):
    with BTC_TRADE_START_JOB_LOCK:
        job = BTC_TRADE_START_JOBS.get(str(job_id or "").strip())
        return _btc_trade_job_payload(job) if job else None


def btc_to_units(btc_amount):
    return int(float(btc_amount) * ASSET_SCALE)


def units_to_btc(units):
    return int(units or 0) / ASSET_SCALE


def units_to_btc_str(units):
    return f"{units_to_btc(units):.8f}"


class BtcTradeBridge:
    def __init__(
        self,
        *,
        hackme_dir,
        btc_trade_dir,
        bridge_username="btc_bridge",
        market_symbol="BTC/USDT",
        quantity_scale=1.0,
        min_btc_quantity=0.000001,
        db_path=None,
        chain_seed_path=None,
        state_path=None,
    ):
        self.hackme_dir = expand_server_path(hackme_dir) or Path.cwd()
        self.runtime_root = default_runtime_root(self.hackme_dir)
        self.btc_trade_dir = expand_server_path(btc_trade_dir)
        self.bridge_username = str(bridge_username or "btc_bridge")
        self.market_symbol = str(market_symbol or "BTC/USDT").strip().upper()
        self.quantity_scale = float(quantity_scale or 0)
        self.min_btc_quantity = float(min_btc_quantity or 0)
        self.db_path = Path(db_path) if db_path else self.runtime_root / "database" / "database.db"
        self.chain_seed_path = Path(chain_seed_path) if chain_seed_path else self.runtime_root / ".chain_seed"
        runtime = self.btc_trade_dir / "runtime" if self.btc_trade_dir else Path("runtime")
        self.trade_log_path = runtime / "trade_log_4h.json"
        self.state_path = Path(state_path) if state_path else runtime / "bridge_state.json"

    def status(self):
        return btc_trade_status(self.btc_trade_dir)

    def load_state(self):
        if not self.state_path.exists():
            return {
                "last_trade_count": 0,
                "open_quantity_units": 0,
                "open_entry_trade_idx": None,
                "total_buy_orders": 0,
                "total_sell_orders": 0,
                "last_run_at": None,
            }
        return _load_json_file(self.state_path)

    def save_state(self, state):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        state["last_run_at"] = datetime.now().isoformat(timespec="seconds")
        tmp = self.state_path.with_name(f"{self.state_path.name}.{uuid.uuid4().hex}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    def get_db(self):
        return connect_sqlite(self.db_path, timeout=15, row_factory=True, foreign_keys=True, wal=True)

    def _chain_seed(self):
        with open(self.chain_seed_path, encoding="utf-8") as fh:
            return fh.read().strip()

    def init_services(self):
        hackme_dir = str(self.hackme_dir)
        if hackme_dir not in sys.path:
            sys.path.insert(0, hackme_dir)
        from services.points_chain import PointsLedgerService
        from services.trading.trading_engine import TradingEngineService

        points_service = PointsLedgerService(
            get_db=self.get_db,
            chain_secret=self._chain_seed(),
            backup_dir=self.runtime_root / "database" / "points_chain_backups",
        )
        return TradingEngineService(get_db=self.get_db, points_service=points_service)

    def bridge_actor(self):
        conn = self.get_db()
        try:
            row = conn.execute(
                "SELECT id, username, role FROM users WHERE username=?",
                (self.bridge_username,),
            ).fetchone()
            if not row:
                return None
            return {"id": int(row["id"]), "username": row["username"], "role": row["role"]}
        finally:
            conn.close()

    def spot_position_units(self, user_id):
        conn = self.get_db()
        try:
            row = conn.execute(
                "SELECT quantity_units FROM trading_spot_positions WHERE user_id=? AND market_symbol=?",
                (int(user_id), self.market_symbol),
            ).fetchone()
            return int(row["quantity_units"]) if row else 0
        finally:
            conn.close()

    def _load_trades(self):
        if not self.trade_log_path.exists():
            raise FileNotFoundError(f"BTC_trade trade log not found: {self.trade_log_path}")
        trades = _load_json_file(self.trade_log_path)
        if not isinstance(trades, list):
            raise ValueError("BTC_trade trade log must be a list")
        return trades

    def run(self, *, dry_run=False):
        trades = self._load_trades()
        state = self.load_state()
        last_count = int(state.get("last_trade_count", 0) or 0)
        new_trades = trades[last_count:]
        result = {
            "ok": True,
            "dry_run": bool(dry_run),
            "processed_from": last_count,
            "trade_count": len(trades),
            "new_count": len(new_trades),
            "orders": [],
            "skipped": [],
            "errors": [],
        }
        if not new_trades:
            if not dry_run:
                self.save_state(state)
            return result

        actor = {"id": 0, "username": self.bridge_username, "role": "user"} if dry_run else self.bridge_actor()
        if not actor:
            result["ok"] = False
            result["errors"].append(f"bridge account not found: {self.bridge_username}")
            return result
        trading_service = None if dry_run else self.init_services()
        open_units = int(state.get("open_quantity_units", 0) or 0)

        for offset, trade in enumerate(new_trades):
            trade_idx = last_count + offset
            if not isinstance(trade, dict):
                result["skipped"].append({"idx": trade_idx, "reason": "trade row is not an object"})
                continue
            action = str(trade.get("action") or "").upper()
            if action == "ENTRY":
                raw_btc = float(trade.get("btc") or 0)
                scaled_btc = raw_btc * self.quantity_scale
                if scaled_btc < self.min_btc_quantity:
                    result["skipped"].append({"idx": trade_idx, "action": action, "reason": "quantity below minimum", "btc": scaled_btc})
                    continue
                quantity = f"{scaled_btc:.8f}"
                quantity_units = btc_to_units(scaled_btc)
                if dry_run:
                    result["orders"].append({"idx": trade_idx, "action": action, "side": "buy", "quantity": quantity, "dry_run": True})
                    continue
                try:
                    order_result = trading_service.place_order(
                        actor=actor,
                        market_symbol=self.market_symbol,
                        side="buy",
                        order_type="market",
                        quantity=quantity,
                    )
                    open_units += quantity_units
                    state["open_quantity_units"] = open_units
                    state["open_entry_trade_idx"] = trade_idx
                    state["total_buy_orders"] = int(state.get("total_buy_orders", 0) or 0) + 1
                    result["orders"].append({"idx": trade_idx, "action": action, "side": "buy", "quantity": quantity, "order": order_result.get("order")})
                except Exception as exc:
                    result["ok"] = False
                    result["errors"].append({"idx": trade_idx, "action": action, "error": str(exc)})
            elif action == "PARTIAL_EXIT":
                actual_units = self.spot_position_units(actor["id"]) if not dry_run else max(0, open_units)
                if actual_units <= 0:
                    result["skipped"].append({"idx": trade_idx, "action": action, "reason": "no hackme spot position"})
                    continue
                sell_units = max(1, actual_units // 3)
                quantity = units_to_btc_str(sell_units)
                if dry_run:
                    result["orders"].append({"idx": trade_idx, "action": action, "side": "sell", "quantity": quantity, "dry_run": True})
                    continue
                try:
                    order_result = trading_service.place_order(
                        actor=actor,
                        market_symbol=self.market_symbol,
                        side="sell",
                        order_type="market",
                        quantity=quantity,
                    )
                    open_units = max(0, open_units - sell_units)
                    state["open_quantity_units"] = open_units
                    state["total_sell_orders"] = int(state.get("total_sell_orders", 0) or 0) + 1
                    result["orders"].append({"idx": trade_idx, "action": action, "side": "sell", "quantity": quantity, "order": order_result.get("order")})
                except Exception as exc:
                    result["ok"] = False
                    result["errors"].append({"idx": trade_idx, "action": action, "error": str(exc)})
            elif action == "FULL_EXIT":
                actual_units = self.spot_position_units(actor["id"]) if not dry_run else max(0, open_units)
                if actual_units <= 0:
                    open_units = 0
                    state["open_quantity_units"] = 0
                    state["open_entry_trade_idx"] = None
                    result["skipped"].append({"idx": trade_idx, "action": action, "reason": "no hackme spot position"})
                    continue
                quantity = units_to_btc_str(actual_units)
                if dry_run:
                    result["orders"].append({"idx": trade_idx, "action": action, "side": "sell", "quantity": quantity, "dry_run": True})
                    continue
                try:
                    order_result = trading_service.place_order(
                        actor=actor,
                        market_symbol=self.market_symbol,
                        side="sell",
                        order_type="market",
                        quantity=quantity,
                    )
                    open_units = 0
                    state["open_quantity_units"] = 0
                    state["open_entry_trade_idx"] = None
                    state["total_sell_orders"] = int(state.get("total_sell_orders", 0) or 0) + 1
                    result["orders"].append({"idx": trade_idx, "action": action, "side": "sell", "quantity": quantity, "order": order_result.get("order")})
                except Exception as exc:
                    result["ok"] = False
                    result["errors"].append({"idx": trade_idx, "action": action, "error": str(exc)})
            else:
                result["skipped"].append({"idx": trade_idx, "action": action, "reason": "unknown trade action"})

        if result["ok"] and not dry_run:
            state["last_trade_count"] = len(trades)
        if not dry_run:
            self.save_state(state)
        result["state"] = state
        return result
