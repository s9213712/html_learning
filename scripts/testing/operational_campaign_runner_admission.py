#!/usr/bin/env python3
"""Stage campaign imports behind strict host-I/O admission checkpoints.

This entrypoint is intentionally launched with ``python -S``.  It samples
``/proc/pressure/io`` before enabling ``site``, imports one reviewed module at
a time, and only calls the selected target's ``main(argv)`` after a private,
write-once admission receipt has been committed.  Target arguments are never
included in that receipt.
"""

from __future__ import annotations

import sys


_ORIGINAL_IMPORT = __import__
SCHEMA_VERSION = "hackme.operational-staged-import-admission.v1"
SOFT_IO_PRESSURE_MAXIMUM = 3.0
HARD_IO_PRESSURE_MAXIMUM = 10.0
DEFAULT_STAGE_TIMEOUT_SECONDS = 120.0
DEFAULT_POLL_SECONDS = 0.25
IMPORT_PACING_SECONDS = 0.1
HARD_IO_EXIT_CODE = 86
POST_RECEIPT_BARRIER_MODE = "required_before_target_main"
PRE_RECEIPT_BARRIER_MODE = "required_before_receipt_write"
NESTED_IMPORT_GUARD_MODE = "builtins_and_importlib_import_pre_post_v1"
TARGET_MODULES = {
    "runner": "scripts.testing.operational_campaign_24h",
    "watchdog": "scripts.testing.campaign_watchdog",
}

# Keep the receipt-support modules first.  If a later import fails, a single
# terminal failure receipt can still be written without loading anything else.
_RECEIPT_SUPPORT_MODULES = (
    "os",
    "json",
    "hashlib",
)

_RUNNER_MODULES = _RECEIPT_SUPPORT_MODULES + (
    "importlib",
    "argparse",
    "base64",
    "contextlib",
    "copy",
    "ctypes",
    "dataclasses",
    "datetime",
    "enum",
    "errno",
    "fcntl",
    "fnmatch",
    "hmac",
    "http.server",
    "io",
    "ipaddress",
    "math",
    "pathlib",
    "re",
    "secrets",
    "select",
    "shutil",
    "signal",
    "socket",
    "sqlite3",
    "stat",
    "statistics",
    "struct",
    "subprocess",
    "tarfile",
    "tempfile",
    "threading",
    "time",
    "traceback",
    "types",
    "typing",
    "unicodedata",
    "urllib.error",
    "urllib.parse",
    "urllib.request",
    "uuid",
    "weakref",
    "zipfile",
    "urllib3",
    "charset_normalizer",
    "requests",
    # Switch to the reviewed collector as soon as its dependencies are ready.
    "scripts.testing.campaign_observability",
    "scripts.testing.campaign_activation",
    "scripts.testing.campaign_readiness",
    "scripts.testing.campaign_load",
    "scripts.testing.campaign_cgroup",
    "scripts.testing.campaign_comfyui_sandbox",
    "scripts.testing.campaign_comfyui_backend",
    "scripts.testing.campaign_contract",
    "scripts.testing.campaign_artifacts",
    "scripts.testing.operation_coverage",
    "scripts.testing.campaign_scenario_binding",
    "scripts.testing.campaign_gate_bundle",
    "scripts.testing.campaign_qualification_capture",
    "scripts.testing.audit_evidence_triad",
    "scripts.testing.campaign_native_evidence",
    "scripts.testing.bt_formal_local_probe",
    "cryptography",
    "cryptography.x509",
    "cryptography.fernet",
    "cryptography.hazmat.primitives.hashes",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.hazmat.primitives.asymmetric.rsa",
    "cryptography.x509.oid",
    "services.server.runtime",
    "services.comfyui.template.seeding",
    "scripts.testing.campaign_native_selectors",
    "scripts.testing.campaign_security_sentinel",
    "scripts.testing.campaign_secret_scan",
    "scripts.testing.campaign_source_freeze",
    "scripts.testing.campaign_runtime_contract",
    "scripts.testing.campaign_state",
    "scripts.testing.campaign_control_channel",
    "scripts.testing.campaign_watchdog",
    TARGET_MODULES["runner"],
)

_WATCHDOG_MODULES = _RECEIPT_SUPPORT_MODULES + (
    "importlib",
    "argparse",
    "contextlib",
    "copy",
    "dataclasses",
    "datetime",
    "fcntl",
    "hmac",
    "pathlib",
    "re",
    "secrets",
    "signal",
    "socket",
    "stat",
    "struct",
    "time",
    "typing",
    "uuid",
    "scripts.testing.campaign_control_channel",
    TARGET_MODULES["watchdog"],
)

PROFILE_MODULES = {
    "runner": _RUNNER_MODULES,
    "watchdog": _WATCHDOG_MODULES,
}


class StagedImportError(RuntimeError):
    """Continuing to the campaign target cannot be proven safe."""


class HardIOPressureError(StagedImportError):
    """The non-waitable host-I/O ceiling was exceeded."""


class IOAdmissionTimeout(StagedImportError):
    """A staged import could not obtain I/O headroom in time."""


def _finite_nonnegative(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and float(value) == float(value)
        and float(value) not in {float("inf"), float("-inf")}
        and float(value) >= 0.0
    )


def parse_io_pressure(text: str) -> dict[str, float]:
    """Parse the kernel PSI ``full`` row and reject incomplete telemetry."""

    full_rows = []
    for raw_line in str(text).splitlines():
        parts = raw_line.split()
        if parts and parts[0] == "full":
            full_rows.append(parts[1:])
    if len(full_rows) != 1:
        raise StagedImportError("IO_PRESSURE_FULL_ROW_INVALID")
    values: dict[str, str] = {}
    for item in full_rows[0]:
        if "=" not in item:
            raise StagedImportError("IO_PRESSURE_FIELD_INVALID")
        name, raw_value = item.split("=", 1)
        if not name or name in values:
            raise StagedImportError("IO_PRESSURE_FIELD_INVALID")
        values[name] = raw_value
    try:
        avg10 = float(values["avg10"])
        avg60 = float(values["avg60"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StagedImportError("IO_PRESSURE_AVERAGE_INVALID") from exc
    if not _finite_nonnegative(avg10) or not _finite_nonnegative(avg60):
        raise StagedImportError("IO_PRESSURE_AVERAGE_INVALID")
    return {"avg10": avg10, "avg60": avg60}


def read_direct_io_pressure(path: str = "/proc/pressure/io") -> dict[str, float]:
    """Read only the tiny procfs PSI file; this works before ``site``."""

    try:
        with open(path, "r", encoding="ascii", errors="strict") as handle:
            content = handle.read(4097)
    except Exception as exc:
        raise StagedImportError("IO_PRESSURE_READ_FAILED") from exc
    if len(content) > 4096:
        raise StagedImportError("IO_PRESSURE_FILE_TOO_LARGE")
    return parse_io_pressure(content)


def read_process_start_ticks(
    path: str = "/proc/self/stat",
    *,
    expected_pid: int | None = None,
) -> int:
    """Read Linux starttime (field 22) without being confused by ``comm``."""

    try:
        with open(path, "r", encoding="ascii", errors="strict") as handle:
            content = handle.read(16385)
    except Exception as exc:
        raise StagedImportError("PROCESS_STAT_READ_FAILED") from exc
    if len(content) > 16384 or "\x00" in content:
        raise StagedImportError("PROCESS_STAT_INVALID")
    prefix, separator, suffix = content.strip().rpartition(") ")
    if not separator or " (" not in prefix:
        raise StagedImportError("PROCESS_STAT_INVALID")
    pid_text, command = prefix.split(" (", 1)
    fields = suffix.split()
    try:
        pid = int(pid_text)
        state = fields[0]
        start_ticks = int(fields[19])
    except (IndexError, TypeError, ValueError) as exc:
        raise StagedImportError("PROCESS_STAT_INVALID") from exc
    if (
        pid <= 1
        or not command
        or len(state) != 1
        or start_ticks <= 0
        or (expected_pid is not None and pid != int(expected_pid))
    ):
        raise StagedImportError("PROCESS_STAT_INVALID")
    return start_ticks


def _normalized_sample(collector) -> dict[str, float]:
    try:
        sample = collector()
    except StagedImportError:
        raise
    except Exception as exc:
        raise StagedImportError("IO_PRESSURE_COLLECTOR_FAILED") from exc
    if not isinstance(sample, dict):
        raise StagedImportError("IO_PRESSURE_SAMPLE_INVALID")
    avg10 = sample.get("avg10")
    avg60 = sample.get("avg60")
    if not _finite_nonnegative(avg10) or not _finite_nonnegative(avg60):
        raise StagedImportError("IO_PRESSURE_SAMPLE_INVALID")
    return {"avg10": float(avg10), "avg60": float(avg60)}


def _assert_not_hard_pressure(sample: dict[str, float], *, stage: str) -> None:
    if (
        float(sample["avg10"]) > HARD_IO_PRESSURE_MAXIMUM
        or float(sample["avg60"]) > HARD_IO_PRESSURE_MAXIMUM
    ):
        error = HardIOPressureError("HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED")
        error.stage = stage
        error.sample = sample
        raise error


def _prepare_time_module(*, initial_sample: dict[str, float], direct_collector):
    """Use interpreter-preloaded time or bracket its one explicit import.

    CPython normally preloads ``time`` even under ``-S``.  If an interpreter
    does not, the explicit import is allowed only from <=3/3 headroom and is
    followed immediately by another direct PSI sample before any other work.
    """

    sample = _normalized_sample(lambda: initial_sample)
    _assert_not_hard_pressure(sample, stage="pre_time_module")
    time_module = sys.modules.get("time")
    if time_module is not None:
        return time_module, sample, "preloaded_by_interpreter"
    if (
        sample["avg10"] > SOFT_IO_PRESSURE_MAXIMUM
        or sample["avg60"] > SOFT_IO_PRESSURE_MAXIMUM
    ):
        raise StagedImportError("TIME_IMPORT_REQUIRES_IO_HEADROOM")
    try:
        time_module = __import__("time")
    except Exception as exc:
        raise StagedImportError("TIME_IMPORT_FAILED") from exc
    post_import = _normalized_sample(direct_collector)
    _assert_not_hard_pressure(post_import, stage="post_time_module")
    return time_module, post_import, "explicit_import_directly_bracketed"


def wait_for_io_headroom(
    *,
    stage: str,
    deadline: float,
    collector,
    clock,
    sleeper,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    first_sample: dict[str, float] | None = None,
) -> dict[str, object]:
    """Wait for <=3/3, but abort the first time either average exceeds 10."""

    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    started = float(clock())
    sample_count = 0
    maximum_avg10 = 0.0
    maximum_avg60 = 0.0
    pending = first_sample
    while True:
        sample = _normalized_sample(lambda: pending) if pending is not None else _normalized_sample(collector)
        pending = None
        sample_count += 1
        avg10 = float(sample["avg10"])
        avg60 = float(sample["avg60"])
        maximum_avg10 = max(maximum_avg10, avg10)
        maximum_avg60 = max(maximum_avg60, avg60)
        _assert_not_hard_pressure(sample, stage=stage)
        now = float(clock())
        if now > float(deadline):
            error = IOAdmissionTimeout("HOST_IO_PRESSURE_STAGE_TIMEOUT")
            error.stage = stage
            error.sample = sample
            raise error
        if avg10 <= SOFT_IO_PRESSURE_MAXIMUM and avg60 <= SOFT_IO_PRESSURE_MAXIMUM:
            return {
                "sample_count": sample_count,
                "waited_seconds": round(max(0.0, float(clock()) - started), 6),
                "maximum": {
                    "avg10": round(maximum_avg10, 6),
                    "avg60": round(maximum_avg60, 6),
                },
                "admitted": sample,
            }
        if now >= float(deadline):
            error = IOAdmissionTimeout("HOST_IO_PRESSURE_STAGE_TIMEOUT")
            error.stage = stage
            error.sample = sample
            raise error
        sleeper(min(float(poll_seconds), max(0.0, float(deadline) - now)))


def _observability_collector(module):
    """Adapt the reviewed host collector without weakening its fail-closed checks."""

    def collect() -> dict[str, float]:
        evidence = module.collect_host_startup_safety_preflight()
        if not isinstance(evidence, dict):
            raise StagedImportError("OBSERVABILITY_EVIDENCE_INVALID")
        if evidence.get("errors"):
            raise StagedImportError("OBSERVABILITY_TELEMETRY_INCOMPLETE")
        tripped = [str(value) for value in evidence.get("tripped") or ()]
        allowed = {
            "HOST_IO_PRESSURE_HIGH",
            "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
        }
        if any(reason not in allowed for reason in tripped):
            raise StagedImportError("OBSERVABILITY_NON_IO_SAFETY_FAILURE")
        try:
            value = evidence["checks"]["host_io_pressure"]["value"]
            return {
                "avg10": float(value["avg10"]),
                "avg60": float(value["avg60"]),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise StagedImportError("OBSERVABILITY_IO_EVIDENCE_INVALID") from exc

    return collect


def _module_order_digest(module_order: tuple[str, ...], *, json_module, hashlib_module) -> str:
    canonical = json_module.dumps(
        list(module_order),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib_module.sha256(canonical).hexdigest()


def _atomic_write_evidence(path: str, payload: dict[str, object], *, os_module, json_module) -> None:
    """Create one mode-0600 JSON artifact atomically and never overwrite it."""

    normalized = os_module.path.abspath(os_module.path.normpath(str(path)))
    parent = os_module.path.dirname(normalized)
    if not normalized.startswith("/tmp/") or parent in {"", "/", "/tmp"}:
        raise StagedImportError("EVIDENCE_PATH_OUTSIDE_PRIVATE_TMP_RUN")
    if os_module.path.realpath(parent) != parent or not os_module.path.isdir(parent):
        raise StagedImportError("EVIDENCE_PARENT_INVALID")
    content = (
        json_module.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    temporary = ""
    descriptor = -1
    for attempt in range(8):
        candidate = os_module.path.join(
            parent,
            f".{os_module.path.basename(normalized)}.{os_module.getpid()}.{attempt}.tmp",
        )
        try:
            descriptor = os_module.open(
                candidate,
                os_module.O_WRONLY | os_module.O_CREAT | os_module.O_EXCL,
                0o600,
            )
            temporary = candidate
            break
        except FileExistsError:
            continue
    if descriptor < 0 or not temporary:
        raise StagedImportError("EVIDENCE_TEMPORARY_CREATE_FAILED")
    linked = False
    try:
        view = memoryview(content)
        while view:
            written = os_module.write(descriptor, view)
            if written <= 0:
                raise OSError("short evidence write")
            view = view[written:]
        os_module.fsync(descriptor)
        os_module.close(descriptor)
        descriptor = -1
        os_module.link(temporary, normalized)
        linked = True
        os_module.unlink(temporary)
        temporary = ""
        directory = os_module.open(parent, os_module.O_RDONLY | getattr(os_module, "O_DIRECTORY", 0))
        try:
            os_module.fsync(directory)
        finally:
            os_module.close(directory)
    finally:
        if descriptor >= 0:
            os_module.close(descriptor)
        if temporary:
            try:
                os_module.unlink(temporary)
            except FileNotFoundError:
                pass
        if linked:
            mode = os_module.stat(normalized, follow_symlinks=False).st_mode & 0o777
            if mode != 0o600:
                raise StagedImportError("EVIDENCE_MODE_INVALID")


def _build_receipt(
    *,
    campaign_uuid: str,
    profile: str,
    module_order: tuple[str, ...],
    stages: list[dict[str, object]],
    loaded: dict[str, object],
    status: str,
    stage_timeout_seconds: float,
    poll_seconds: float,
    time_module_bootstrap: str,
    nested_import_guard: dict[str, object],
    failure_reason: str = "",
    failed_module: str = "",
) -> dict[str, object]:
    os_module = loaded["os"]
    json_module = loaded["json"]
    hashlib_module = loaded["hashlib"]
    pid = int(os_module.getpid())
    process_start_ticks = read_process_start_ticks(expected_pid=pid)
    order_digest = _module_order_digest(
        module_order,
        json_module=json_module,
        hashlib_module=hashlib_module,
    )
    binding = {
        "campaign_uuid": campaign_uuid,
        "profile": profile,
        "pid": pid,
        "process_start_ticks": process_start_ticks,
        "target_module": TARGET_MODULES[profile],
        "module_order_sha256": order_digest,
    }
    binding_digest = hashlib_module.sha256(
        json_module.dumps(
            binding,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "verified": status == "PASS",
        "status": status,
        "campaign_uuid": campaign_uuid,
        "profile": profile,
        "pid": pid,
        "process_start_ticks": process_start_ticks,
        "python_no_site": bool(sys.flags.no_site),
        "bootstrap_collector": "direct_proc_pressure_io",
        "site_initialization_mode": (
            "site_paths_only_no_pth_or_customization"
        ),
        "time_module_bootstrap": time_module_bootstrap,
        "target_module": TARGET_MODULES[profile],
        "module_order": list(module_order),
        "module_order_sha256": order_digest,
        "binding_sha256": binding_digest,
        "completed_module_count": len(stages),
        "soft_io_pressure_maximum": SOFT_IO_PRESSURE_MAXIMUM,
        "hard_io_pressure_maximum": HARD_IO_PRESSURE_MAXIMUM,
        "import_pacing_seconds": IMPORT_PACING_SECONDS,
        "stage_timeout_seconds": float(stage_timeout_seconds),
        "poll_seconds": float(poll_seconds),
        "collector_mode": (
            "campaign_observability"
            if "scripts.testing.campaign_observability" in loaded
            else "direct_proc_pressure_io"
        ),
        "nested_import_guard": nested_import_guard,
        "stages": stages,
        "runner_main_invoked": False,
        "pre_receipt_io_barrier": PRE_RECEIPT_BARRIER_MODE,
        "post_receipt_io_barrier": POST_RECEIPT_BARRIER_MODE,
        "failure_reason": failure_reason,
        "failed_module": failed_module,
    }


def execute_profile(
    *,
    profile: str,
    campaign_uuid: str,
    evidence_path: str,
    target_argv: list[str],
    stage_timeout_seconds: float = DEFAULT_STAGE_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    initial_sample: dict[str, float] | None = None,
    importer=_ORIGINAL_IMPORT,
    direct_collector=read_direct_io_pressure,
    clock=None,
    sleeper=None,
    evidence_writer=_atomic_write_evidence,
) -> int:
    """Import the fixed profile and invoke its target in this exact process."""

    if profile not in PROFILE_MODULES or profile not in TARGET_MODULES:
        raise StagedImportError("PROFILE_INVALID")
    if stage_timeout_seconds <= 0 or poll_seconds <= 0:
        raise StagedImportError("STAGE_TIMING_INVALID")
    try:
        if initial_sample is None:
            initial_sample = _normalized_sample(direct_collector)
        time_module, initial_sample, time_module_bootstrap = _prepare_time_module(
            initial_sample=initial_sample,
            direct_collector=direct_collector,
        )
    except HardIOPressureError:
        return HARD_IO_EXIT_CODE
    except Exception:
        return 2
    clock = clock or time_module.monotonic
    sleeper = sleeper or time_module.sleep
    root_marker = "/scripts/testing/"
    script_name = str(__file__).replace("\\", "/")
    if root_marker not in script_name:
        raise StagedImportError("REPOSITORY_ROOT_UNRESOLVED")
    repository_root = script_name.rsplit(root_marker, 1)[0]
    module_order = ("site",) + tuple(PROFILE_MODULES[profile])
    loaded: dict[str, object] = {}
    stages: list[dict[str, object]] = []
    collector = direct_collector
    failed_module = ""
    failure_reason = ""
    hard_failure = False
    target_module = None
    builtins_module = None
    importlib_module = None
    original_import_module = None
    nested_import_guard: dict[str, object] = {
        "mode": "custom_importer_not_guarded",
        "call_count": 0,
        "calls_loading_modules": 0,
        "maximum_io_pressure": {"avg10": 0.0, "avg60": 0.0},
        "pacing_seconds": IMPORT_PACING_SECONDS,
        "restored_before_receipt": True,
    }

    def record_nested_admission(admission: dict[str, object]) -> None:
        maximum = admission.get("maximum")
        if not isinstance(maximum, dict):
            return
        recorded = nested_import_guard["maximum_io_pressure"]
        if not isinstance(recorded, dict):
            return
        recorded["avg10"] = max(
            float(recorded.get("avg10") or 0.0),
            float(maximum.get("avg10") or 0.0),
        )
        recorded["avg60"] = max(
            float(recorded.get("avg60") or 0.0),
            float(maximum.get("avg60") or 0.0),
        )

    try:
        if importer is _ORIGINAL_IMPORT:
            builtins_module = _ORIGINAL_IMPORT("builtins")
            nested_import_guard["mode"] = NESTED_IMPORT_GUARD_MODE
            nested_import_guard["restored_before_receipt"] = False

            def guarded_nested_load(loader):
                nested_import_guard["call_count"] = int(
                    nested_import_guard["call_count"]
                ) + 1
                nested_deadline = float(clock()) + float(stage_timeout_seconds)
                pre_nested = wait_for_io_headroom(
                    stage="pre_nested_import",
                    deadline=nested_deadline,
                    collector=collector,
                    clock=clock,
                    sleeper=sleeper,
                    poll_seconds=poll_seconds,
                )
                record_nested_admission(pre_nested)
                module_count_before = len(sys.modules)
                imported = None
                import_error: BaseException | None = None
                try:
                    imported = loader()
                except HardIOPressureError:
                    raise
                except BaseException as exc:
                    import_error = exc
                post_nested = wait_for_io_headroom(
                    stage="post_nested_import",
                    deadline=nested_deadline,
                    collector=collector,
                    clock=clock,
                    sleeper=sleeper,
                    poll_seconds=poll_seconds,
                )
                record_nested_admission(post_nested)
                if len(sys.modules) > module_count_before:
                    nested_import_guard["calls_loading_modules"] = int(
                        nested_import_guard["calls_loading_modules"]
                    ) + 1
                    sleeper(IMPORT_PACING_SECONDS)
                if import_error is not None:
                    raise import_error
                return imported

            def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
                return guarded_nested_load(
                    lambda: _ORIGINAL_IMPORT(
                        name,
                        globals,
                        locals,
                        fromlist,
                        level,
                    )
                )

            builtins_module.__import__ = guarded_import
        for index, module_name in enumerate(module_order):
            failed_module = module_name
            stage_started = float(clock())
            deadline = stage_started + float(stage_timeout_seconds)
            pre_admission = wait_for_io_headroom(
                stage=f"pre_import:{module_name}",
                deadline=deadline,
                collector=collector,
                clock=clock,
                sleeper=sleeper,
                poll_seconds=poll_seconds,
                first_sample=initial_sample if index == 0 else None,
            )
            initial_sample = None
            module = None
            import_error: BaseException | None = None
            try:
                module = importer(module_name, globals(), locals(), ("*",), 0)
            except HardIOPressureError:
                raise
            except BaseException as exc:
                import_error = exc
            if import_error is None and module_name == "site":
                getsitepackages = getattr(module, "getsitepackages", None)
                if not callable(getsitepackages):
                    raise StagedImportError("SITE_PACKAGES_DISCOVERY_MISSING")
                site_paths = getsitepackages()
                if (
                    not isinstance(site_paths, list)
                    or not site_paths
                    or any(
                        not isinstance(path, str) or not path.startswith("/")
                        for path in site_paths
                    )
                ):
                    raise StagedImportError("SITE_PACKAGES_DISCOVERY_INVALID")
                # Do not call site.main(): .pth code, sitecustomize, and
                # usercustomize would collapse back into one unpaced burst.
                for path in site_paths:
                    if path not in sys.path:
                        sys.path.append(path)
                if repository_root not in sys.path:
                    sys.path.insert(0, repository_root)
            if import_error is None:
                loaded[module_name] = module
            if (
                import_error is None
                and module_name == "importlib"
                and builtins_module is not None
            ):
                importlib_module = module
                original_import_module = getattr(
                    importlib_module,
                    "import_module",
                    None,
                )
                if not callable(original_import_module):
                    raise StagedImportError(
                        "IMPORTLIB_IMPORT_MODULE_MISSING"
                    )

                def guarded_import_module(name, package=None):
                    return guarded_nested_load(
                        lambda: original_import_module(name, package)
                    )

                importlib_module.import_module = guarded_import_module
            if (
                import_error is None
                and module_name == "scripts.testing.campaign_observability"
            ):
                collector = _observability_collector(module)
            post_admission = wait_for_io_headroom(
                stage=f"post_import:{module_name}",
                deadline=deadline,
                collector=collector,
                clock=clock,
                sleeper=sleeper,
                poll_seconds=poll_seconds,
            )
            stages.append({
                "sequence": index,
                "module": module_name,
                "pre_admission": pre_admission,
                "post_admission": post_admission,
            })
            if import_error is not None:
                raise import_error
            # The post-import sample is immediate; pacing happens only after
            # the import has been proven below the admission ceiling.
            sleeper(IMPORT_PACING_SECONDS)
        target_module = loaded.get(TARGET_MODULES[profile])
        if target_module is None or not callable(getattr(target_module, "main", None)):
            failed_module = TARGET_MODULES[profile]
            raise StagedImportError("TARGET_MAIN_MISSING")
    except Exception as exc:
        hard_failure = isinstance(exc, HardIOPressureError)
        failure_reason = (
            str(exc)
            if isinstance(exc, StagedImportError) and str(exc).isupper()
            else f"{exc.__class__.__name__}_DURING_STAGED_IMPORT".upper()
        )
    finally:
        if importlib_module is not None and original_import_module is not None:
            importlib_module.import_module = original_import_module
        if builtins_module is not None:
            builtins_module.__import__ = _ORIGINAL_IMPORT
            nested_import_guard["restored_before_receipt"] = True

    support_ready = all(name in loaded for name in _RECEIPT_SUPPORT_MODULES)
    if not support_ready:
        return HARD_IO_EXIT_CODE if hard_failure else 2
    # A hard PSI trip is non-waitable.  Do not add even one receipt fsync after
    # it; exit 86 is the supervisor's durable-free H24-skip authority.
    if hard_failure:
        return HARD_IO_EXIT_CODE
    status = "PASS" if not failure_reason else "FAIL"
    try:
        receipt = _build_receipt(
            campaign_uuid=campaign_uuid,
            profile=profile,
            module_order=module_order,
            stages=stages,
            loaded=loaded,
            status=status,
            stage_timeout_seconds=stage_timeout_seconds,
            poll_seconds=poll_seconds,
            time_module_bootstrap=time_module_bootstrap,
            nested_import_guard=nested_import_guard,
            failure_reason=failure_reason,
            failed_module=failed_module if failure_reason else "",
        )
    except HardIOPressureError:
        return HARD_IO_EXIT_CODE
    except Exception:
        return HARD_IO_EXIT_CODE if hard_failure else 2
    try:
        wait_for_io_headroom(
            stage="pre_receipt_write",
            deadline=float(clock()) + float(stage_timeout_seconds),
            collector=collector,
            clock=clock,
            sleeper=sleeper,
            poll_seconds=poll_seconds,
        )
    except HardIOPressureError:
        return HARD_IO_EXIT_CODE
    except Exception:
        return 2
    evidence_write_failed = False
    try:
        evidence_writer(
            evidence_path,
            receipt,
            os_module=loaded["os"],
            json_module=loaded["json"],
        )
    except HardIOPressureError:
        return HARD_IO_EXIT_CODE
    except Exception:
        evidence_write_failed = True
    # The receipt commit itself performs the only staged-import fsync.  Prove
    # that this write did not consume host headroom before target main is
    # allowed to authenticate or create any campaign state.  Authentication
    # subsequently proves to the supervisor that this in-process barrier was
    # crossed after the write-once receipt.
    try:
        wait_for_io_headroom(
            stage="post_receipt_write_pre_main",
            deadline=float(clock()) + float(stage_timeout_seconds),
            collector=collector,
            clock=clock,
            sleeper=sleeper,
            poll_seconds=poll_seconds,
        )
    except HardIOPressureError:
        return HARD_IO_EXIT_CODE
    except Exception:
        return 2
    if evidence_write_failed:
        return 2
    if failure_reason:
        return HARD_IO_EXIT_CODE if hard_failure else 2
    target_returncode = int(target_module.main(list(target_argv)))
    # Reserve 86 exclusively for a pre-main hard-I/O admission failure so the
    # supervisor can safely decide whether H24 source reads must be skipped.
    return 2 if target_returncode == HARD_IO_EXIT_CODE else target_returncode


def _valid_campaign_uuid(value: str) -> bool:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
    return 1 <= len(value) <= 128 and all(character in allowed for character in value)


def parse_wrapper_argv(argv: list[str]) -> tuple[dict[str, object], list[str]]:
    """Parse only wrapper flags; everything after ``--`` is target argv."""

    if "--" not in argv:
        raise StagedImportError("TARGET_ARGUMENT_SEPARATOR_REQUIRED")
    split_at = argv.index("--")
    wrapper = argv[:split_at]
    target = list(argv[split_at + 1:])
    values: dict[str, str] = {}
    index = 0
    allowed = {
        "--profile": "profile",
        "--campaign-uuid": "campaign_uuid",
        "--evidence-path": "evidence_path",
        "--stage-timeout-seconds": "stage_timeout_seconds",
        "--poll-seconds": "poll_seconds",
    }
    while index < len(wrapper):
        option = wrapper[index]
        if option not in allowed or index + 1 >= len(wrapper):
            raise StagedImportError("WRAPPER_ARGUMENT_INVALID")
        key = allowed[option]
        if key in values:
            raise StagedImportError("WRAPPER_ARGUMENT_DUPLICATE")
        values[key] = wrapper[index + 1]
        index += 2
    required = {"profile", "campaign_uuid", "evidence_path"}
    if not required.issubset(values):
        raise StagedImportError("WRAPPER_ARGUMENT_REQUIRED")
    if values["profile"] not in TARGET_MODULES:
        raise StagedImportError("PROFILE_INVALID")
    if not _valid_campaign_uuid(values["campaign_uuid"]):
        raise StagedImportError("CAMPAIGN_UUID_INVALID")
    try:
        timeout = float(values.get("stage_timeout_seconds", DEFAULT_STAGE_TIMEOUT_SECONDS))
        poll = float(values.get("poll_seconds", DEFAULT_POLL_SECONDS))
    except ValueError as exc:
        raise StagedImportError("STAGE_TIMING_INVALID") from exc
    if not _finite_nonnegative(timeout) or timeout <= 0:
        raise StagedImportError("STAGE_TIMING_INVALID")
    if not _finite_nonnegative(poll) or poll <= 0:
        raise StagedImportError("STAGE_TIMING_INVALID")
    return {
        "profile": values["profile"],
        "campaign_uuid": values["campaign_uuid"],
        "evidence_path": values["evidence_path"],
        "stage_timeout_seconds": timeout,
        "poll_seconds": poll,
    }, target


def _running_without_site() -> bool:
    return bool(sys.flags.no_site)


def main(argv: list[str] | None = None) -> int:
    try:
        # This is deliberately the first host telemetry read in the program,
        # before diagnostics, argument parsing, site, or profile imports.
        initial_sample = read_direct_io_pressure()
        _assert_not_hard_pressure(initial_sample, stage="initial_direct_pre_execute")
    except HardIOPressureError:
        return HARD_IO_EXIT_CODE
    except Exception:
        # Telemetry failure is fail-closed and deliberately silent because
        # host I/O headroom could not be established.
        return 2
    diagnostics_allowed = bool(
        initial_sample["avg10"] <= SOFT_IO_PRESSURE_MAXIMUM
        and initial_sample["avg60"] <= SOFT_IO_PRESSURE_MAXIMUM
    )
    if not _running_without_site():
        if diagnostics_allowed:
            sys.stderr.write("staged campaign admission requires python -S\n")
        return 2
    try:
        options, target_argv = parse_wrapper_argv(
            list(sys.argv[1:] if argv is None else argv)
        )
        return execute_profile(
            **options,
            target_argv=target_argv,
            initial_sample=initial_sample,
        )
    except HardIOPressureError:
        # Exit 86 is intentionally durable-free: even stderr may be a file or
        # pipe whose consumer adds more I/O while the host is above the hard
        # ceiling.
        return HARD_IO_EXIT_CODE
    except Exception as exc:
        if diagnostics_allowed:
            reason = (
                str(exc)
                if isinstance(exc, StagedImportError)
                else exc.__class__.__name__
            )
            sys.stderr.write(f"staged campaign admission failed: {reason}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
