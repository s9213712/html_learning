"""§10.3.2 cleanup + 24h sweeper regression."""

import fcntl
import os

import pytest

import services.comfyui.template.cleanup as cleanup_module

from services.comfyui.template.cleanup import (
    COMFYUI_RUN_TTL_SECONDS,
    cleanup_run_temp_files,
    discard_comfyui_ref_exact,
    get_run_cleanup_receipt,
    list_active_run_dirs,
    purge_comfyui_run_input,
    record_run_input_ref,
    register_run_dir,
    run_cleanup_maintenance_daemon,
    registry_size,
    reset_registry,
    sweep_orphaned_run_dirs,
    sweep_restart_orphaned_run_dirs,
    sweep_restart_orphaned_run_dirs_with_retry,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    reset_registry()
    yield
    reset_registry()


class _FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


def test_register_run_dir_persists_to_registry():
    register_run_dir(run_id="abc123", user_id=7)
    assert registry_size() == 1
    [entry] = list_active_run_dirs()
    assert entry.run_id == "abc123"
    assert entry.user_id == 7


def test_register_run_dir_is_idempotent_on_same_run_id():
    clock = _FakeClock()
    register_run_dir(run_id="abc", user_id=7, clock=clock)
    first_ts = list_active_run_dirs()[0].created_at
    clock.advance(60)
    register_run_dir(run_id="abc", user_id=7, clock=clock)
    # Only one entry; original timestamp preserved
    assert registry_size() == 1
    assert list_active_run_dirs()[0].created_at == first_ts


def test_register_run_dir_strips_unsafe_run_id_chars():
    register_run_dir(run_id="abc/../etc", user_id=1)
    [entry] = list_active_run_dirs()
    assert "/" not in entry.run_id
    assert entry.run_id == "abcetc"


def test_cleanup_run_temp_files_calls_callback_and_marks_purged():
    register_run_dir(run_id="abc", user_id=7)
    calls = []

    def _cb(*, run_id, user_id):
        calls.append((run_id, user_id))
        return True

    ok = cleanup_run_temp_files(
        run_id="abc",
        user_id=7,
        cleanup_callback=_cb,
        audit=None,
    )
    assert ok is True
    assert calls == [("abc", 7)]
    # Registry entry now marked purged → not in active list
    assert registry_size() == 1
    assert list_active_run_dirs() == []


def test_cleanup_run_temp_files_audit_emitted_on_success():
    register_run_dir(run_id="abc", user_id=7)
    audit_calls = []

    def _audit(action, ip, **kwargs):
        audit_calls.append((action, kwargs))

    cleanup_run_temp_files(
        run_id="abc",
        user_id=7,
        cleanup_callback=lambda **_: True,
        audit=_audit,
        audit_user="alice",
    )
    actions = [c[0] for c in audit_calls]
    assert "COMFYUI_TEMPLATE_RUN_INPUT_CLEANUP" in actions


def test_cleanup_run_temp_files_audit_emitted_on_failure():
    register_run_dir(run_id="abc", user_id=7)
    audit_calls = []

    def _audit(action, ip, **kwargs):
        audit_calls.append((action, kwargs))

    ok = cleanup_run_temp_files(
        run_id="abc",
        user_id=7,
        cleanup_callback=lambda **_: False,
        audit=_audit,
        audit_user="alice",
    )
    assert ok is False
    success_flags = [c[1].get("success") for c in audit_calls]
    assert False in success_flags


def test_cleanup_run_temp_files_callback_exception_does_not_propagate():
    register_run_dir(run_id="abc", user_id=7)

    def _bad_cb(**_):
        raise RuntimeError("comfyui down")

    audit_calls = []
    ok = cleanup_run_temp_files(
        run_id="abc",
        user_id=7,
        cleanup_callback=_bad_cb,
        audit=lambda *a, **k: audit_calls.append(k),
    )
    assert ok is False
    # Audit log records the failure
    assert audit_calls
    assert audit_calls[0]["success"] is False
    assert "callback_raised" in audit_calls[0]["detail"]


def test_sweeper_reaps_only_entries_past_ttl():
    clock = _FakeClock()
    register_run_dir(run_id="old", user_id=1, clock=clock)
    clock.advance(COMFYUI_RUN_TTL_SECONDS - 100)
    register_run_dir(run_id="young", user_id=2, clock=clock)
    clock.advance(150)  # old=now ~24h+50s; young=now 150s

    purged = []

    def _cb(*, run_id, user_id):
        purged.append(run_id)
        return True

    summary = sweep_orphaned_run_dirs(
        cleanup_callback=_cb,
        ttl_seconds=COMFYUI_RUN_TTL_SECONDS,
        clock=clock,
    )
    assert summary["candidates"] == 1
    assert summary["reaped"] == 1
    assert summary["failed"] == 0
    assert purged == ["old"]
    # young entry remains active
    remaining_ids = {e.run_id for e in list_active_run_dirs()}
    assert remaining_ids == {"young"}


def test_sweeper_skips_already_purged_entries():
    clock = _FakeClock()
    register_run_dir(run_id="abc", user_id=1, clock=clock)
    cleanup_run_temp_files(
        run_id="abc", user_id=1, cleanup_callback=lambda **_: True
    )
    clock.advance(COMFYUI_RUN_TTL_SECONDS + 1)

    calls = []
    sweep_orphaned_run_dirs(
        cleanup_callback=lambda **kw: (calls.append(kw), True)[1],
        ttl_seconds=COMFYUI_RUN_TTL_SECONDS,
        clock=clock,
    )
    # Already-purged entry never reaches the cleanup_callback again.
    assert calls == []


def test_sweeper_reports_failed_count_when_callback_returns_false():
    clock = _FakeClock()
    register_run_dir(run_id="dead", user_id=1, clock=clock)
    clock.advance(COMFYUI_RUN_TTL_SECONDS + 1)

    summary = sweep_orphaned_run_dirs(
        cleanup_callback=lambda **_: False,
        ttl_seconds=COMFYUI_RUN_TTL_SECONDS,
        clock=clock,
    )
    assert summary["candidates"] == 1
    assert summary["reaped"] == 0
    assert summary["failed"] == 1


def test_default_ttl_matches_spec():
    """§10.3.2: 24h reap window."""
    assert COMFYUI_RUN_TTL_SECONDS == 24 * 60 * 60


def _local_purge_callback(project_dir):
    def _purge(**kwargs):
        return purge_comfyui_run_input(
            **kwargs,
            client=object(),
            local_base_dir=project_dir,
        )

    return _purge


def _prove_local_binding(monkeypatch, project_dir):
    monkeypatch.setattr(
        cleanup_module,
        "_local_backend_binding_proof",
        lambda backend_url, local_base_dir: {
            "binding_verified": True,
            "backend_url": backend_url,
            "backend_host": "127.0.0.1",
            "backend_port": 8188,
            "project_dir": str(project_dir.resolve()),
            "listener_pid": 123,
            "listener_inode": "456",
            "listener_cwd": str(project_dir.resolve()),
            "listener_inodes": ["456"],
            "listeners": [],
            "detail": "test_exact_binding",
        },
    )


def test_exact_local_cleanup_returns_receipt_and_proves_directory_absent(tmp_path, monkeypatch):
    _prove_local_binding(monkeypatch, tmp_path)
    run_id = "run-local-1"
    run_dir = tmp_path / "input" / run_id
    run_dir.mkdir(parents=True)
    target = run_dir / "7_run-local-1_10.png"
    target.write_bytes(b"png")
    register_run_dir(
        run_id=run_id,
        user_id=7,
        backend_url="http://127.0.0.1:8188",
    )
    record_run_input_ref(
        run_id=run_id,
        user_id=7,
        backend_url="http://127.0.0.1:8188",
        input_ref={"filename": target.name, "subfolder": run_id, "type": "input"},
    )

    receipt = cleanup_run_temp_files(
        run_id=run_id,
        user_id=7,
        cleanup_callback=_local_purge_callback(tmp_path),
        return_receipt=True,
        reason="test_terminal_success",
    )

    assert receipt["ok"] is True
    assert receipt["absence_verified"] is True
    assert receipt["cleanup"]["method"] == "local_filesystem"
    assert receipt["cleanup"]["binding_verified"] is True
    assert receipt["cleanup"]["listener_pid"] == 123
    assert receipt["cleanup"]["listener_inode"] == "456"
    assert receipt["cleanup"]["listener_cwd"] == str(tmp_path.resolve())
    assert not target.exists()
    assert not run_dir.exists()
    assert list_active_run_dirs() == []


def test_cleanup_refuses_success_when_unrecorded_file_remains_in_run_dir(tmp_path, monkeypatch):
    _prove_local_binding(monkeypatch, tmp_path)
    run_id = "run-local-2"
    run_dir = tmp_path / "input" / run_id
    run_dir.mkdir(parents=True)
    target = run_dir / "7_run-local-2_10.png"
    target.write_bytes(b"png")
    (run_dir / "unexpected.bin").write_bytes(b"residual")
    record_run_input_ref(
        run_id=run_id,
        user_id=7,
        backend_url="http://localhost:8188",
        input_ref={"filename": target.name, "subfolder": run_id, "type": "input"},
    )

    receipt = cleanup_run_temp_files(
        run_id=run_id,
        user_id=7,
        cleanup_callback=_local_purge_callback(tmp_path),
        return_receipt=True,
    )

    assert receipt["ok"] is False
    assert receipt["absence_verified"] is False
    assert receipt["cleanup"]["directory_absent"] is False
    assert [entry.run_id for entry in list_active_run_dirs()] == [run_id]


def test_restart_sweeper_loads_durable_dead_owner_entry(tmp_path, monkeypatch):
    _prove_local_binding(monkeypatch, tmp_path)
    run_id = "dead-owner-run"
    run_dir = tmp_path / "input" / run_id
    run_dir.mkdir(parents=True)
    target = run_dir / "9_dead-owner-run_3.png"
    target.write_bytes(b"png")
    register_run_dir(
        run_id=run_id,
        user_id=9,
        backend_url="http://127.0.0.1:8188",
        owner_pid=999_999_999,
    )
    record_run_input_ref(
        run_id=run_id,
        user_id=9,
        backend_url="http://127.0.0.1:8188",
        input_ref={"filename": target.name, "subfolder": run_id, "type": "input"},
    )
    # Simulate a brand-new Python process: durable SQLite remains but the
    # old process-local cache does not.
    with cleanup_module._registry_lock:
        cleanup_module._registry.clear()

    summary = sweep_restart_orphaned_run_dirs(
        cleanup_callback=_local_purge_callback(tmp_path),
        owner_alive=lambda _entry: False,
    )

    assert summary["candidates"] == 1
    assert summary["reaped"] == 1
    assert summary["failed"] == 0
    assert summary["receipts"][0]["absence_verified"] is True
    assert not run_dir.exists()


def test_mapping_callback_cannot_claim_success_without_absence_proof():
    register_run_dir(run_id="no-false-green", user_id=1)
    receipt = cleanup_run_temp_files(
        run_id="no-false-green",
        user_id=1,
        cleanup_callback=lambda **_: {"ok": True, "absence_verified": False},
        return_receipt=True,
    )
    assert receipt["ok"] is False
    assert list_active_run_dirs()[0].run_id == "no-false-green"


def test_local_binding_proof_maps_listener_inode_pid_and_exact_cwd(tmp_path):
    proc_root = tmp_path / "proc"
    project_dir = tmp_path / "ComfyUI"
    project_dir.mkdir()
    (proc_root / "net").mkdir(parents=True)
    (proc_root / "net" / "tcp").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
        "   0: 0100007F:1FFC 00000000:0000 0A 00000000:00000000 00:00000000 00000000 1000 0 98765\n",
        encoding="utf-8",
    )
    (proc_root / "net" / "tcp6").write_text("header\n", encoding="utf-8")
    pid_dir = proc_root / "321"
    (pid_dir / "fd").mkdir(parents=True)
    (pid_dir / "cwd").symlink_to(project_dir, target_is_directory=True)
    (pid_dir / "fd" / "7").symlink_to("socket:[98765]")

    proof = cleanup_module._local_backend_binding_proof(
        "http://127.0.0.1:8188",
        project_dir,
        proc_root=proc_root,
    )

    assert proof["binding_verified"] is True
    assert proof["listener_pid"] == 321
    assert proof["listener_inode"] == "98765"
    assert proof["listener_cwd"] == str(project_dir.resolve())


def test_wrong_project_binding_never_treats_missing_local_path_as_backend_absence(tmp_path, monkeypatch):
    run_id = "wrong-binding"
    actual_dir = tmp_path / "actual" / "input" / run_id
    wrong_project = tmp_path / "wrong"
    actual_dir.mkdir(parents=True)
    wrong_project.mkdir()
    residual = actual_dir / "1_wrong-binding_1.png"
    residual.write_bytes(b"backend-residual")
    record_run_input_ref(
        run_id=run_id,
        user_id=1,
        backend_url="http://127.0.0.1:8188",
        input_ref={"filename": residual.name, "subfolder": run_id, "type": "input"},
    )
    monkeypatch.setattr(
        cleanup_module,
        "_local_backend_binding_proof",
        lambda *_args, **_kwargs: {
            "binding_verified": False,
            "listener_pid": 999,
            "listener_inode": "111",
            "listener_cwd": str((tmp_path / "actual").resolve()),
            "detail": "listener_owner_cwd_mismatch",
        },
    )
    monkeypatch.setattr(
        cleanup_module,
        "_remote_ref_absent",
        lambda _client, _ref: (False, "still_present"),
    )

    class _RemoteClient:
        timeout = 1

        def discard_image(self, *_args, **_kwargs):
            return {"file_deleted": True, "file_delete_supported": True}

    receipt = cleanup_run_temp_files(
        run_id=run_id,
        user_id=1,
        cleanup_callback=lambda **kwargs: purge_comfyui_run_input(
            **kwargs,
            client=_RemoteClient(),
            local_base_dir=wrong_project,
        ),
        return_receipt=True,
    )

    assert receipt["ok"] is False
    assert receipt["cleanup"]["method"] == "remote_delete_and_get"
    assert receipt["cleanup"]["binding_verified"] is False
    assert residual.exists()


def test_restart_sweeper_retries_first_failure_and_persists_final_receipt():
    run_id = "retry-after-backend-start"
    register_run_dir(
        run_id=run_id,
        user_id=5,
        backend_url="http://127.0.0.1:8188",
        owner_pid=999_999_998,
    )
    calls = []
    sleeps = []

    def _eventually_ready(**_kwargs):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            return {
                "ok": False,
                "absence_verified": False,
                "detail": "backend_starting",
            }
        return {
            "ok": True,
            "absence_verified": True,
            "detail": "no_upload_attempt",
        }

    summary = sweep_restart_orphaned_run_dirs_with_retry(
        cleanup_callback=_eventually_ready,
        owner_alive=lambda _entry: False,
        max_attempts=3,
        initial_backoff_seconds=0.25,
        sleep=lambda seconds: sleeps.append(seconds),
    )

    assert summary["ok"] is True
    assert summary["attempts"] == 2
    assert calls == [1, 2]
    assert sleeps == [0.25]
    assert summary["attempt_summaries"][0]["failed"] == 1
    assert summary["final"]["reaped"] == 1
    durable_receipt = get_run_cleanup_receipt(run_id)
    assert durable_receipt["ok"] is True
    assert durable_receipt["absence_verified"] is True
    assert durable_receipt["reason"] == "restart_orphan_sweeper"


def test_owner_identity_missing_boot_or_start_ticks_is_orphan(monkeypatch):
    monkeypatch.setattr(cleanup_module, "_boot_id", lambda: "boot-a")
    monkeypatch.setattr(cleanup_module, "_process_start_ticks", lambda _pid: "100")
    base = dict(run_id="owner", created_at=0, user_id=1, owner_pid=123)
    assert cleanup_module._owner_is_alive(
        cleanup_module._RunDirEntry(**base, owner_boot_id="", owner_start_ticks="100")
    ) is False
    assert cleanup_module._owner_is_alive(
        cleanup_module._RunDirEntry(**base, owner_boot_id="boot-a", owner_start_ticks="")
    ) is False
    assert cleanup_module._owner_is_alive(
        cleanup_module._RunDirEntry(**base, owner_boot_id="boot-a", owner_start_ticks="100")
    ) is True


def test_durable_sweeper_reaps_live_owner_entry_after_ttl():
    run_id = "live-but-expired"
    register_run_dir(run_id=run_id, user_id=1, clock=lambda: 10.0)
    summary = sweep_restart_orphaned_run_dirs(
        cleanup_callback=lambda **_: {"ok": True, "absence_verified": True},
        owner_alive=lambda _entry: True,
        ttl_seconds=5,
        clock=lambda: 20.0,
    )
    assert summary["ttl_candidates"] == 1
    assert summary["dead_owner_candidates"] == 0
    assert summary["reaped"] == 1
    assert summary["receipts"][0]["reason"] == "durable_ttl_sweeper"


def test_maintenance_daemon_yields_to_existing_process_leader(tmp_path):
    lock_path = tmp_path / "cleanup.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        summary = run_cleanup_maintenance_daemon(
            cleanup_callback=lambda **_: True,
            lock_path=lock_path,
            max_cycles=1,
            interval_seconds=0,
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert summary == {"leader": False, "cycles": 0, "reason": "maintenance_leader_exists"}


def test_input_root_symlink_is_rejected_before_run_cleanup(tmp_path, monkeypatch):
    project = tmp_path / "project"
    external_input = tmp_path / "external-input"
    project.mkdir()
    external_input.mkdir()
    (project / "input").symlink_to(external_input, target_is_directory=True)
    run_id = "root-symlink"
    run_dir = external_input / run_id
    run_dir.mkdir()
    residual = run_dir / "1_root-symlink_1.png"
    residual.write_bytes(b"do-not-delete")
    _prove_local_binding(monkeypatch, project)
    record_run_input_ref(
        run_id=run_id,
        user_id=1,
        backend_url="http://127.0.0.1:8188",
        input_ref={"filename": residual.name, "subfolder": run_id, "type": "input"},
    )
    receipt = cleanup_run_temp_files(
        run_id=run_id,
        user_id=1,
        cleanup_callback=_local_purge_callback(project),
        return_receipt=True,
    )
    assert receipt["ok"] is False
    assert receipt["cleanup"]["detail"] == "unsafe_symlink_input_root"
    assert residual.exists()


def test_generic_discard_rejects_output_root_symlink(tmp_path, monkeypatch):
    project = tmp_path / "project"
    external_output = tmp_path / "external-output"
    project.mkdir()
    external_output.mkdir()
    (project / "output").symlink_to(external_output, target_is_directory=True)
    residual = external_output / "result.png"
    residual.write_bytes(b"do-not-delete")
    _prove_local_binding(monkeypatch, project)

    class _MustNotDelete:
        def discard_image(self, *_args, **_kwargs):
            raise AssertionError("symlink root must fail before delete")

    result = discard_comfyui_ref_exact(
        client=_MustNotDelete(),
        file_ref={"filename": residual.name, "subfolder": "", "type": "output"},
        backend_url="http://127.0.0.1:8188",
        local_base_dir=project,
    )
    assert result["absence_verified"] is False
    assert result["verification"] == "unsafe_symlink_type_root"
    assert residual.exists()
