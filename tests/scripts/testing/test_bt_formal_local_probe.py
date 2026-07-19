from __future__ import annotations

import hashlib
import urllib.parse

from scripts.testing.bt_formal_local_probe import (
    MANDATORY_CHECK_IDS,
    PROBE_NAME,
    SCHEMA_VERSION,
    _bencode,
    _raw_query_parameters,
    derive_checks,
    validate_machine_report,
)


def raw_success_fixture() -> dict:
    info_hash = "a" * 40
    source_sha = "b" * 64
    isolation = {
        "dht-enabled": False,
        "lpd-enabled": False,
        "pex-enabled": True,
        "port-forwarding-enabled": False,
    }
    terminal = {
        "hash_string": info_hash,
        "metadata_percent_complete": 1.0,
        "percent_done": 1.0,
        "downloaded_bytes": 8 * 1024 * 1024,
        "left_bytes": 0,
        "error_code": 0,
        "status": "seeding",
    }
    video_rows = [
        {"role": role, "ok": True, "video_stream_count": 1}
        for role in ("source", "magnet_download", "torrent_file_download")
    ]
    primary_source_proof = {
        "ok": True,
        "expected_peer_ip": "172.20.0.2",
        "observed_source_ip": "172.20.0.2",
        "token_sha256": "c" * 64,
        "proof_age_seconds": 0.01,
        "one_time_challenge_consumed": True,
    }
    alternate_source_proof = {
        "ok": True,
        "expected_peer_ip": "172.21.0.2",
        "observed_source_ip": "10.255.255.254",
        "token_sha256": "d" * 64,
        "proof_age_seconds": 0.02,
        "one_time_challenge_consumed": True,
    }
    return {
        "payload": {
            "source_sha256": source_sha,
            "size_bytes": 8 * 1024 * 1024,
            "info_hash": info_hash,
            "video_probes": video_rows,
        },
        "local_seed": {
            "private_torrent": False,
            "discovery_isolated": True,
            "torrent_tracker_count": 1,
            "seed_terminal": True,
            "seed_hash": info_hash,
            "peer_bind_ip": "172.20.0.2",
            "initial_source_route_proof": primary_source_proof,
            "session_isolation": [dict(isolation), dict(isolation), dict(isolation), dict(isolation)],
        },
        "tracker": {
            "bind_ip": "172.20.0.2",
            "advertised_peer_ip": "172.20.0.2",
            "advertised_peer_ip_private": True,
            "advertised_peer_ips": ["172.20.0.2", "172.21.0.2"],
            "all_advertised_peer_ips_private": True,
            "registered_peer_endpoints": {"51001": "172.20.0.2", "51002": "172.21.0.2"},
            "registered_announce_sources": {
                "51001": {"expected_peer_ip": "172.20.0.2", "observed_source_ip": "172.20.0.2", "source_proof_sha256": "c" * 64},
                "51002": {"expected_peer_ip": "172.21.0.2", "observed_source_ip": "10.255.255.254", "source_proof_sha256": "d" * 64},
            },
            "source_route_proofs": [primary_source_proof, alternate_source_proof],
            "announces": [
                {"remote_ip": "172.20.0.2", "advertised_peer_ip": "172.20.0.2", "source_route_proof_sha256": "c" * 64},
                {"remote_ip": "10.255.255.254", "advertised_peer_ip": "172.21.0.2", "source_route_proof_sha256": "d" * 64},
            ],
            "all_announces_host_local": True,
            "seed_announce_seen": True,
            "peer_response_seen": True,
            "info_hash": info_hash,
        },
        "magnet": {
            "source_type": "magnet",
            "terminal_state": "success",
            "terminal": terminal,
            "download_path_exists": True,
            "download_size_bytes": 8 * 1024 * 1024,
            "download_sha256": source_sha,
            "pause_resume": {
                "stop_rpc_success": True,
                "start_rpc_success": True,
                "before_pause": {"downloaded_bytes": 300_000, "percent_done": 0.04, "files": [{"bytes_completed": 300_000}]},
                "stable_during_pause": {"downloaded_bytes": 300_000, "status": "stopped", "files": [{"bytes_completed": 300_000}]},
                "after_resume": {"downloaded_bytes": 100_000, "percent_done": 0.05, "files": [{"bytes_completed": 400_000}]},
                "resume_recovery": {
                    "strategy": "torrent_remove_readd_verify_preserve_partial",
                    "remove_rpc_success": True,
                    "old_torrent_absent": True,
                    "readd_rpc_success": True,
                    "same_info_hash": True,
                    "before_recreate": {"files": [{"bytes_completed": 300_000}], "hash_string": info_hash},
                    "after_verify": {"files": [{"bytes_completed": 262_144}], "hash_string": info_hash},
                    "preserved_completed_bytes": 300_000,
                    "verified_completed_bytes": 262_144,
                    "piece_size_bytes": 65_536,
                    "discarded_incomplete_piece_bytes": 37_856,
                    "partial_path_exists": True,
                    "seed_ip_rotation": {
                        "strategy": "seed_restart_on_distinct_host_private_ip",
                        "old_ip": "172.20.0.2",
                        "new_ip": "172.21.0.2",
                        "old_port": 51001,
                        "new_port": 51002,
                        "old_pid": 25,
                        "new_pid": 26,
                        "old_pid_exited": True,
                        "old_listener_closed": True,
                        "new_listener_open": True,
                        "torrent_persisted": True,
                        "tracker_updated": True,
                        "source_route_proof": alternate_source_proof,
                        "seed_generation": 2,
                        "stop_evidence": {"pid_remaining": False},
                    },
                },
            },
            "service_restart": {
                "old_pid": 100,
                "new_pid": 200,
                "old_pid_exited": True,
                "torrent_persisted": True,
                "same_info_hash": True,
                "before_restart": {"downloaded_bytes": 500_000, "hash_string": info_hash},
                "after_restart": {"downloaded_bytes": 500_000, "hash_string": info_hash},
                "after_restart_resume": {"downloaded_bytes": 700_000, "hash_string": info_hash},
                "client_generation": 2,
            },
        },
        "torrent_file": {
            "source_type": "torrent_file",
            "implementation": "services.storage.remote_downloads.download_torrent_file_with_aria2",
            "terminal_state": "success",
            "terminal": {
                "phase": "downloaded",
                "loaded_bytes": 8 * 1024 * 1024,
                "total_bytes": 8 * 1024 * 1024,
            },
            "download_path_exists": True,
            "download_size_bytes": 8 * 1024 * 1024,
            "download_sha256": source_sha,
        },
        "cleanup": {
            "tracker_stopped": True,
            "runtime_removed": True,
            "product_download_cleanup_dir_removed": True,
            "all_ports_released": True,
            "processes": [
                {"role": "client", "pid": 100, "pid_remaining": False},
                {"role": "client", "pid": 200, "pid_remaining": False},
                {"role": "seed", "pid": 300, "pid_remaining": False},
                {"role": "seed", "pid": 350, "pid_remaining": False},
            ],
            "orphan_pids": [],
        },
    }


def test_derive_checks_accepts_only_complete_raw_terminal_evidence() -> None:
    checks = derive_checks(raw_success_fixture())

    assert tuple(checks) == MANDATORY_CHECK_IDS
    assert all(row["mandatory"] is True for row in checks.values())
    assert all(row["ok"] is True for row in checks.values())


def test_derive_checks_rejects_fake_http_style_success_without_terminal_side_effects() -> None:
    raw = raw_success_fixture()
    raw["torrent_file"]["terminal"] = {"status": 200, "ok": True}
    raw["magnet"]["terminal"]["left_bytes"] = 4096

    checks = derive_checks(raw)

    assert checks["magnet_terminal_success"]["ok"] is False
    assert checks["torrent_file_terminal_success"]["ok"] is False


def test_derive_checks_rejects_progress_during_pause_or_loss_across_restart() -> None:
    raw = raw_success_fixture()
    raw["magnet"]["pause_resume"]["stable_during_pause"]["downloaded_bytes"] += 1
    raw["magnet"]["service_restart"]["after_restart"]["downloaded_bytes"] -= 1

    checks = derive_checks(raw)

    assert checks["pause_resume_progress"]["ok"] is False
    assert checks["bt_client_service_restart_resume"]["ok"] is False


def test_derive_checks_rejects_unverified_resume_recovery() -> None:
    raw = raw_success_fixture()
    raw["magnet"]["pause_resume"]["resume_recovery"]["verified_completed_bytes"] -= 1

    checks = derive_checks(raw)

    assert checks["pause_resume_progress"]["ok"] is False


def test_derive_checks_rejects_public_discovery_and_orphan_process() -> None:
    raw = raw_success_fixture()
    raw["tracker"]["all_announces_host_local"] = False
    raw["local_seed"]["session_isolation"][0]["dht-enabled"] = True
    raw["cleanup"]["orphan_pids"] = [300]

    checks = derive_checks(raw)

    assert checks["controlled_local_seed"]["ok"] is False
    assert checks["precise_process_and_fixture_cleanup"]["ok"] is False


def test_derive_checks_rejects_missing_or_forged_source_route_proof() -> None:
    raw = raw_success_fixture()
    raw["tracker"]["source_route_proofs"][1]["token_sha256"] = "forged"

    checks = derive_checks(raw)

    assert checks["controlled_local_seed"]["ok"] is False
    assert checks["pause_resume_progress"]["ok"] is False


def test_binary_query_parser_preserves_twenty_byte_info_hash_and_peer_id() -> None:
    info_hash = bytes(range(20))
    peer_id = b"-TR4000-abcdefghijkl"
    query = urllib.parse.urlencode(
        {
            "info_hash": info_hash,
            "peer_id": peer_id,
            "port": "51413",
            "left": "42",
        }
    )

    parsed = _raw_query_parameters(query)

    assert parsed["info_hash"] == [info_hash]
    assert parsed["peer_id"] == [peer_id]
    assert parsed["port"] == [b"51413"]


def test_bencode_compact_tracker_payload_is_binary_safe_and_deterministic() -> None:
    compact_peer = b"\x7f\x00\x00\x01\xc8\xd5"
    payload = _bencode({b"peers": compact_peer, b"interval": 1})

    assert payload == b"d8:intervali1e5:peers6:\x7f\x00\x00\x01\xc8\xd5e"
    assert hashlib.sha256(payload).hexdigest() == hashlib.sha256(payload).hexdigest()


def test_machine_report_validator_requires_exact_checks_and_validated_artifacts() -> None:
    checks = derive_checks(raw_success_fixture())
    artifacts = [
        {
            "artifact_id": artifact_id,
            "exists": True,
            "validated": True,
        }
        for artifact_id in (
            "source_video",
            "torrent_metainfo",
            "magnet_download",
            "torrent_file_download",
            "event_trace",
        )
    ]
    report = {
        "schema_version": SCHEMA_VERSION,
        "probe": PROBE_NAME,
        "terminal_state": "success",
        "ok": True,
        "checks": checks,
        "artifacts": artifacts,
    }

    assert validate_machine_report(report) == []

    artifacts[-1]["validated"] = False
    checks["payload_sha256_exact"]["ok"] = False
    errors = validate_machine_report(report)
    assert any("artifact invalid" in error for error in errors)
    assert "report ok conflicts with failed mandatory check" in errors
