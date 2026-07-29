from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path

from scripts.testing.campaign_scenario_binding import FORMAL_SCENARIO_BINDINGS
from scripts.testing.campaign_native_selectors import (
    EXPECTED_COMFY_CGROUP_LIMITS,
    EXPECTED_COMFY_SAFETY_FIELDS,
    ai_agent_positive_assertions,
    backup_restore_assertions,
    bt_download_assertions,
    cloud_drive_stream_assertions,
    comfyui_workflow_assertions,
    community_governance_assertions,
    final_ui_assertions,
    media_long_assertions,
    media_proxy_assertions,
    pointschain_hft_assertions,
    server_emergency_assertions,
    trading_workflow_assertions,
    wallet_incident_assertions,
)
from scripts.testing.bt_formal_local_probe import (
    PROBE_NAME as BT_PROBE_NAME,
    SCHEMA_VERSION as BT_PROBE_SCHEMA_VERSION,
    derive_checks as derive_bt_checks,
)
from services.comfyui.template.seeding import SYSTEM_WORKFLOW_IDS


def ai_agent_fixture() -> tuple[dict, dict]:
    probe = {
        "ok": True,
        "errors": [],
        "catalogs": {
            "root": {
                "actor_role": "super_admin", "role_scoped": True,
                "write_enabled": True, "catalog_sha256": "a" * 64,
                "tool_count": 20,
                "names": [
                    "write_server_restart", "write_incident_enter",
                    "write_incident_resolve", "write_appeal_review",
                    "write_trading_verify_jobs",
                ],
            },
            "manager": {
                "actor_role": "manager", "role_scoped": True,
                "write_enabled": True, "catalog_sha256": "b" * 64,
                "tool_count": 12, "names": ["write_governance_vote"],
            },
            "user": {
                "actor_role": "user", "role_scoped": True,
                "write_enabled": True, "catalog_sha256": "c" * 64,
                "tool_count": 8,
                "names": [
                    "write_cloud_drive_create_text", "write_video_publish",
                    "write_trading_place_order", "write_appeal_create",
                ],
            },
        },
        "settings": {
            "before": {f"setting_{index}": index for index in range(12)},
            "enabled": {
                "feature_ai_agent_enabled": True,
                "audit_chain_enabled": True,
                "audit_chain_reseal_required": False,
                "ai_agent_operation_mode": "write",
                "module_ai_agent_min_role": "user",
            },
            "enabled_readback": True,
        },
        "orchestration": {
            "real_provider": True,
            "provider_models": ["formal-provider-model"],
            "chat_call_count": 2,
            "write_plan": {
                "action": "write_tool", "tool": "write_album_create",
                "execute_write": True, "planner_strategy": "hybrid_verified",
                "fallback_error": "",
            },
            "write_handled": True,
            "write_request": {"tool": "write_album_create", "confirm": "EXECUTE"},
            "write_terminal": {
                "status": 200, "ok": True, "album_id": "album-1",
                "title": "Formal Agent Orchestration", "visibility": "private",
            },
            "readonly_plan": {
                "action": "readonly", "readonly_scope": "server_mode",
                "planner_strategy": "llm_only", "fallback_error": "",
            },
            "readonly_handled": True,
            "readonly_terminal": {"status": 200, "ok": True},
            "cleanup": {"album_absent_status": 404, "album_absent": True},
            "browser": {"page_errors": [], "console_errors": []},
        },
        "drive": {
            "create": {"ok": True}, "share_create": {"ok": True},
            "owner_content": {
                "status": 200, "size_bytes": 32, "exact": True,
                "sha256": "f" * 64, "expected_sha256": "f" * 64,
            },
            "share_update": {"ok": True}, "terminal_max_views": 7,
            "public_access_status": 200, "share_revoke": {"ok": True},
            "shared_content": {
                "status": 200, "size_bytes": 32, "exact": True,
                "sha256": "f" * 64, "expected_sha256": "f" * 64,
            },
            "revoked_access_status": 410, "delete": {"ok": True},
            "file_absent": True, "share_token_sha256": "d" * 64,
        },
        "video": {
            "fixture": {"size_bytes": 4096, "duration_seconds": 6.0, "sha256": "e" * 64},
            "publish": {"ok": True}, "published_video_id": 9,
            "terminal": {"status": "ready", "streaming_ready": True, "mode": "hls", "master_url_present": True},
            "hls": {"master_status": 200, "variant_status": 200, "segment_status": 200, "segment_bytes": 2048},
            "delete_video": {"ok": True}, "delete_cloud_file": {"ok": True},
            "playback_after_delete_status": 404,
        },
        "trading": {
            "funding_before": {
                "available_points": 1000, "locked_points": 0,
                "wallet_available_points": 0, "wallet_locked_points": 0,
                "trial_available_points": 1000, "trial_locked_points": 0,
            },
            "spot": {
                "create": {"ok": True}, "order_uuid": "spot-1",
                "cancel": {"ok": True}, "terminal_status": "cancelled",
                "funding_after": {
                    "available_points": 1000, "locked_points": 0,
                    "wallet_available_points": 0, "wallet_locked_points": 0,
                    "trial_available_points": 1000, "trial_locked_points": 0,
                },
            },
            "margin_lending": {
                "open": {"ok": True}, "initial_status": "open",
                "borrowed_asset_symbol": "POINTS", "principal_points": 50,
                "close": {"ok": True}, "terminal_status": "closed",
                "funding_after": {
                    "available_points": 999, "locked_points": 0,
                    "wallet_available_points": 0, "wallet_locked_points": 0,
                    "trial_available_points": 999, "trial_locked_points": 0,
                },
                "funding_pool_before": {
                    "balance_points": 10000, "outstanding_principal_points": 0,
                    "capacity_points": 10000,
                },
                "funding_pool_after": {
                    "balance_points": 10001, "outstanding_principal_points": 0,
                    "capacity_points": 10000,
                },
            },
            "custom_workflow_bot": {
                "create": {"ok": True}, "bot_uuid": "bot-1",
                "workflow_source": "formal_ai_agent_abc", "workflow_node_count": 3,
                "scan": {"ok": True},
                "scan_triggered": {"bot_uuid": "bot-1", "order_uuid": "order-1"},
                "run_count": 1, "cancel_order": {"ok": True},
                "cancelled_order_terminal_status": "cancelled",
                "funding_after": {
                    "available_points": 999, "locked_points": 0,
                    "wallet_available_points": 0, "wallet_locked_points": 0,
                    "trial_available_points": 999, "trial_locked_points": 0,
                },
                "delete_status": 200, "absent": True,
            },
            "invariants": {
                "verify": {"ok": True},
                "job": {"terminal_status": "succeeded"},
                "verification_ok": True, "errors": [],
            },
        },
        "community": {
            "create": {"ok": True}, "reply": {"ok": True},
            "thread_id": 10, "terminal_title": "AI thread", "terminal_reply_id": 11,
            "persistent_rewards": {
                "accounted": True,
                "thread_author": {
                    "balance_before": 100, "balance_after": 103,
                    "ledger_id": 91, "ledger_uuid": "post-reward-1",
                    "action_type": "forum_post_reward", "reference_type": "forum_thread",
                    "reference_id": "10", "amount": 3,
                },
                "reply_author": {
                    "balance_before": 200, "balance_after": 201,
                    "ledger_id": 92, "ledger_uuid": "reply-reward-1",
                    "action_type": "forum_comment_reward", "reference_type": "forum_post",
                    "reference_id": "11", "amount": 1,
                },
            },
            "delete_status": 200, "absent_status": 404,
        },
        "governance": {
            "create": {"ok": True}, "vote": {"ok": True},
            "approved_status": "approved", "execute": {"ok": True},
            "terminal_status": "executed", "action_type": "warn",
            "violation_id": 19, "violation_count_before": 2,
            "violation_count_after_warning": 3,
            "appeal_create": {"ok": True}, "appeal_id": 20,
            "appeal_review": {"ok": True}, "appeal_terminal_status": "approved",
            "violation_count_restored": 2, "account_state_restored": True,
        },
        "launch": {
            "preflight": {"ok": True}, "dry_run": True, "auto_switch": False,
            "preflight_passed": True, "blocker_count": 0, "blockers": [],
            "outcome_consistent": True,
            "mode_before": "dev_ready", "mode_after": "dev_ready",
            "step_names": ["requirements_gate", "log_chain_verify", "ai_agent_audit_scan", "switch_production", "final_mode_status"],
            "logs_verify": {"ok": True, "broken_links": 0},
        },
        "incident": {
            "enter": {"ok": True}, "enter_root_relogin": {"status": 200, "ok": True},
            "active_terminal": {"active": True, "status": "active"},
            "resolve": {"ok": True}, "resolve_root_relogin": {"status": 200, "ok": True},
            "resolved_terminal": {"active": False, "status": "resolved"},
            "mode_before": "dev_ready", "mode_after": "dev_ready",
        },
        "restart_request": {
            "tool": {"ok": True}, "mode": "supervised-request",
            "requires_supervisor_restart": True,
            "request_schema_version": "hackme.supervised-restart-request/v1",
            "receipt_schema_version": "hackme.supervised-restart-request/v1",
            "receipt_nonce_matches": True, "reason_matches_request": True,
        },
        "audit": {
            "required_tools_present": True, "ai_tool_success_count": 12,
            "expected_tool_calls": {"root:write_server_restart": 1},
            "audited_tool_calls": {"root:write_server_restart": 1},
            "expected_tool_call_count": 1,
            "audited_expected_tool_call_count": 1,
            "missing_expected_tool_calls": {},
            "audit_start_id": 1, "audit_last_id": 50,
            "secure_audit_chain": {
                "enabled": True, "ok": True, "broken_at": None,
            },
            "log_chain_verified": True, "audit_scan_terminal": True,
        },
        "cleanup": {
            "settings_restored": True, "orchestration_album_absent": True,
            "drive_fixture_absent": True,
            "video_fixture_absent": True, "trading_orders_terminal": True,
            "custom_workflow_bot_absent": True, "community_thread_absent": True,
            "community_persistent_rewards_accounted": True,
            "governance_account_restored": True, "incident_resolved": True,
            "errors": [],
        },
    }
    restart = {
        "schema_version": "hackme.formal-ai-agent-supervised-restart/v1",
        "receipt_valid": True, "receipt_nonce_matches_probe": True,
        "requesting_pid_in_old_tree": True, "before_pid": 100, "after_pid": 200,
        "old_tree_gone": True, "outage_observed": True, "outage_sample_count": 4,
        "post_restart_ready": True, "restart_request_removed": True,
        "restart": {"ok": True},
    }
    return probe, restart


def test_ai_agent_selector_requires_exact_positive_operations_and_supervised_restart() -> None:
    selected = ai_agent_positive_assertions(*ai_agent_fixture())

    assert all(selected["scenario_assertions"].values())
    assert all(selected["terminal_assertions"].values())
    assert all(selected["cleanup_assertions"].values())


def test_ai_agent_selector_rejects_catalog_leak_fake_hls_and_unobserved_restart() -> None:
    probe, restart = ai_agent_fixture()
    probe["catalogs"]["user"]["names"].append("write_server_restart")
    probe["video"]["hls"]["segment_bytes"] = 0
    probe["governance"]["account_state_restored"] = False
    restart["outage_observed"] = False

    selected = ai_agent_positive_assertions(probe, restart)

    assert selected["scenario_assertions"]["role_scoped_tool_catalog"] is False
    assert selected["scenario_assertions"]["video_hls_publish_and_terminal_job"] is False
    assert selected["scenario_assertions"]["community_and_governance_operations"] is False
    assert selected["scenario_assertions"]["scheduled_restart_outage_and_readiness"] is False


def test_ai_agent_selector_rejects_fallback_planner_and_unaccounted_persistent_reward() -> None:
    probe, restart = ai_agent_fixture()
    probe["orchestration"]["write_plan"]["planner_strategy"] = "deterministic_fallback"
    probe["community"]["persistent_rewards"]["reply_author"]["balance_after"] = 200

    selected = ai_agent_positive_assertions(probe, restart)

    assert selected["scenario_assertions"]["role_scoped_tool_catalog"] is False
    assert selected["scenario_assertions"]["community_and_governance_operations"] is False


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def community_fixture(tmp_path: Path) -> dict:
    desktop = tmp_path / "community-desktop.png"
    mobile = tmp_path / "community-mobile.png"
    desktop.write_bytes(b"desktop")
    mobile.write_bytes(b"mobile")
    rows = []
    for viewport, screenshot in (("desktop", desktop), ("mobile", mobile)):
        rows.append({
            "viewport": viewport,
            "community": {
                "active": True,
                "thread_text": "Formal community governance abc",
                "overflow_px": 0,
            },
            "chat": {
                "active": True,
                "messages": "Formal private message abc",
                "overflow_px": 0,
            },
            "console_errors": [],
            "page_errors": [],
            "failed_responses": [],
            "screenshot": str(screenshot),
            "screenshot_size_bytes": screenshot.stat().st_size,
            "context_closed": True,
        })
    return {
        "ok": True,
        "errors": [],
        "actors": {
            "user_one": {"id": 11, "username": "qa_one"},
            "user_two": {"id": 12, "username": "qa_two"},
        },
        "forum": {
            "thread_id": 101,
            "thread": {"id": 101, "title": "Formal community governance abc", "status": "approved"},
            "post": {"id": 102, "content": "Formal reply abc"},
            "report_id": 103,
            "terminal_report": {
                "id": 103,
                "status": "rejected",
                "claimed_by_username": "admin",
                "reviewed_by": "admin",
            },
        },
        "chat": {
            "private_room_id": 201,
            "private_room": {"body": {"room": {"id": 201, "is_private": 1}}},
            "terminal_message": {"id": 202, "sender_id": 1, "content": "Formal private message abc"},
            "notification": {"id": 203, "type": "chat_private_message"},
        },
        "friends": {
            "accept_terminal": {"friends": [{"other_user_id": 12, "other_username": "qa_two"}]},
            "profile": {"profile": {"id": 12, "username": "qa_two"}},
            "block": {"body": {"block": {"status": "blocked", "target": {"id": 12}}}},
            "blocked_state": {"blocked": [{"other_user_id": 12}]},
            "blocked_dm": {"status": 403, "body": {"ok": False}},
            "unblock": {"status": 200, "body": {"ok": True}},
        },
        "governance": {
            "proposal_id": 301,
            "proposer_vote_denied": {"status": 403},
            "vote": {"body": {"proposal": {"id": 301, "status": "approved"}}},
            "terminal_proposal": {"id": 301, "action_type": "warn", "status": "executed"},
        },
        "boundaries": {
            "member_governance_denied": {"status": 403},
            "csrf_missing_denied": {"status": 403},
            "chat_rate_limit": {
                "attempt_count": 21,
                "success_count": 20,
                "terminal": {"status": 429, "body": {"ok": False}},
            },
        },
        "browser": {"browser_closed": True, "rows": rows},
        "cleanup": {
            "thread_deleted": True,
            "thread_denied_to_member": True,
            "private_room_deleted": True,
            "private_room_absent": True,
            "rate_room_deleted": True,
            "rate_room_absent": True,
            "friendship_absent": True,
            "block_absent": True,
            "settings_restored": True,
            "notification_ids_dismissed": [401, 402],
            "notifications_dismissed": True,
            "cleanup_errors": [],
        },
    }


def test_community_selector_requires_full_domain_terminal_ui_and_cleanup(tmp_path: Path) -> None:
    probe = community_fixture(tmp_path)
    selected = community_governance_assertions(probe)

    assert all(selected["scenario_assertions"].values())
    assert all(selected["terminal_assertions"].values())
    assert all(selected["cleanup_assertions"].values())

    probe["governance"]["terminal_proposal"]["status"] = "approved"
    failed = community_governance_assertions(probe)
    assert failed["scenario_assertions"]["social_proposal_vote_execute"] is False


def test_community_selector_rejects_fake_rate_limit_open_browser_and_residuals(tmp_path: Path) -> None:
    probe = community_fixture(tmp_path)
    probe["boundaries"]["chat_rate_limit"]["terminal"]["status"] = 200
    probe["browser"]["rows"][0]["context_closed"] = False
    probe["cleanup"]["friendship_absent"] = False

    selected = community_governance_assertions(probe)

    assert selected["scenario_assertions"]["role_permission_and_rate_limit_boundaries"] is False
    assert selected["scenario_assertions"]["desktop_mobile_community_ui"] is False
    assert selected["cleanup_assertions"]["reversible_community_chat_friend_fixtures_removed"] is False


def comfyui_fixture(tmp_path: Path) -> tuple[dict, dict]:
    def artifact(name: str, data: bytes) -> dict:
        from PIL import Image

        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        colour = tuple(hashlib.sha256(data).digest()[:3])
        image = Image.new("RGB", (32, 32), colour)
        encoded = BytesIO()
        image.save(encoded, format="PNG")
        path.write_bytes(encoded.getvalue())
        return {
            "path": str(path.resolve()),
            "mime_type": "image/png",
            "kind": "image",
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }

    def no_temp_cleanup_validation() -> dict:
        return {
            "ok": True,
            "accepted_run_id": "",
            "terminal_run_id": "",
            "accepted_assignment_count": 0,
            "terminal_assignment_count": 0,
            "input_ref_count": 0,
            "reasons": [],
            "receipt": {
                "schema_version": 1,
                "run_id": "",
                "ok": True,
                "absence_verified": True,
                "detail": "no_temp_inputs",
                "input_ref_count": 0,
            },
        }

    def manifest_dependency_contract() -> dict:
        return {
            "schema_version": "hackme.comfyui-manifest-dependency-contract/v1",
            "ok": True,
            "scope": {
                "model_dependencies": "exact_loader_class_and_input_mapping_plus_prompt_embeddings",
                "custom_nodes": "explicit_class_to_package_mapping_only",
            },
            "graph": {
                "models": [],
                "loras": [],
                "controlnets": [],
                "custom_nodes": [],
            },
            "manifest": {
                "models": [],
                "loras": [],
                "controlnets": [],
                "custom_nodes": [],
            },
            "differences": {
                category: {"missing_from_manifest": [], "extra_in_manifest": []}
                for category in ("models", "loras", "controlnets", "custom_nodes")
            },
            "custom_node_evidence": {
                "scope": "explicit_class_to_package_mapping_only",
                "limitation": "API graphs do not expose authoritative package provenance",
            },
            "errors": [],
        }

    official_artifacts = [
        {
            "bundle_id": workflow_id,
            **artifact(f"official/{index:02d}_{workflow_id}.png", f"official-{workflow_id}".encode()),
        }
        for index, workflow_id in enumerate(SYSTEM_WORKFLOW_IDS)
    ]
    custom_artifact = artifact("outputs/custom.png", b"custom-output")
    agent_artifact = artifact("outputs/agent.png", b"agent-output")
    canary_artifact = artifact("outputs/safe_gguf_canary.png", b"safe-gguf-canary-output")
    campaign_scope = "/hackme-campaign-unit.scope"
    exact_remote_discard = {
        "file_deleted": True,
        "file_missing": False,
        "absence_verified": True,
        "verification": "http_404",
        "remote_preview_only": False,
        "local_binding": {"binding_verified": False},
    }
    safety_samples = tmp_path / "resource_samples/comfyui_safety_samples.jsonl"
    safety_samples.parent.mkdir(parents=True, exist_ok=True)
    safety_sample = {
        "sample_schema_version": "hackme.formal-comfyui-safety-sample/v1",
        "expected_fields": sorted(EXPECTED_COMFY_SAFETY_FIELDS),
        "valid_fields": sorted(EXPECTED_COMFY_SAFETY_FIELDS),
        "missing_fields": [],
        "collector_errors": [],
        "backend": {
            "process_pid": 4321,
            "process_cgroup_path": campaign_scope,
            "process_inside_campaign_scope": True,
            "process_listening_socket_verified": True,
            "process_tree_pids": [4321, 4323],
            "process_tree_rss_bytes": 512 * 1024**2,
            "process_tree_threads": 12,
            "process_tree_fd_count": 40,
            "gpu_utilization_percent": [25],
            "gpu_temperature_c": [55],
            "device_vram_free_bytes": [1024 * 1024**2],
        },
        "cgroup": {
            "path": campaign_scope,
            "memory_high": EXPECTED_COMFY_CGROUP_LIMITS["memory_high"],
            "memory_max": EXPECTED_COMFY_CGROUP_LIMITS["memory_max"],
            "memory_swap_max": EXPECTED_COMFY_CGROUP_LIMITS["memory_swap_max"],
            "cpu_max": {
                "quota": EXPECTED_COMFY_CGROUP_LIMITS["cpu_quota"],
                "period": EXPECTED_COMFY_CGROUP_LIMITS["cpu_period"],
            },
            "pids_max": EXPECTED_COMFY_CGROUP_LIMITS["pids_max"],
        },
        "hard_limit_state": {"ok": True},
    }
    safety_samples.write_text(
        "".join(json.dumps(safety_sample) + "\n" for _index in range(3)),
        encoding="utf-8",
    )
    feature_report = tmp_path / "feature_probe.json"
    feature_report.write_text('{"ok": true}\n', encoding="utf-8")
    official_report = tmp_path / "official_results.json"
    official_report.write_text('{"ok": true}\n', encoding="utf-8")
    dependency_all_report = tmp_path / "dependency_preflight/all.json"
    dependency_all_report.parent.mkdir(parents=True, exist_ok=True)
    dependency_all_report.write_text('{"ok": true}\n', encoding="utf-8")
    dependency_safe_report = tmp_path / "dependency_preflight/safe.json"
    dependency_safe_report.write_text('{"ok": true}\n', encoding="utf-8")

    models_root = tmp_path / "models"
    safe_model_specs = {
        "gguf_file": (
            models_root / "diffusion_models" / "diving-illustrious-flat-anime-paradigm-shift.Q4_K_M.gguf",
            1_446_633_120,
        ),
        "clip_name1": (models_root / "text_encoders" / "clip_l.safetensors", 246_144_378),
        "clip_name2": (models_root / "text_encoders" / "clip_g.safetensors", 1_389_363_370),
        "vae_name": (models_root / "vae" / "illustrious_vae.safetensors", 167_340_358),
    }
    for path, size in safe_model_specs.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            handle.truncate(size)
    safe_actual_files = {
        slot: {
            "path": str(path.resolve()),
            "relative_path": path.resolve().relative_to(models_root.resolve()).as_posix(),
            "size_bytes": size,
            "sha256": chr(97 + index) * 64,
        }
        for index, (slot, (path, size)) in enumerate(safe_model_specs.items())
    }

    feature_model_specs = {
        "checkpoint": (models_root / "checkpoints" / "feature.safetensors", b"checkpoint-model"),
        "upscale": (models_root / "upscale_models" / "feature-upscale.pth", b"upscale-model"),
        "controlnet": (models_root / "controlnet" / "feature-controlnet.safetensors", b"controlnet-model"),
    }
    for path, data in feature_model_specs.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    feature_checkpoint = {
        "ok": True,
        "selection_rule": "all_explicit_exact_inventory_actual_stat_sha256_no_fallback",
        "checkpoint": {
            "checkpoint": "feature.safetensors",
            "path": str(feature_model_specs["checkpoint"][0].resolve()),
            "size_bytes": feature_model_specs["checkpoint"][0].stat().st_size,
            "sha256": _sha256(feature_model_specs["checkpoint"][0]),
        },
        "upscale_model": {
            "name": "feature-upscale.pth",
            "path": str(feature_model_specs["upscale"][0].resolve()),
            "size_bytes": feature_model_specs["upscale"][0].stat().st_size,
            "sha256": _sha256(feature_model_specs["upscale"][0]),
        },
        "controlnet": {
            "type": "canny",
            "model_name": "feature-controlnet.safetensors",
            "preprocessor": "CannyEdgePreprocessor",
            "path": str(feature_model_specs["controlnet"][0].resolve()),
            "size_bytes": feature_model_specs["controlnet"][0].stat().st_size,
            "sha256": _sha256(feature_model_specs["controlnet"][0]),
        },
        "actual_model_total_bytes": sum(path.stat().st_size for path, _data in feature_model_specs.values()),
        "max_file_bytes": 2 * 1024**3,
        "max_total_bytes": 4 * 1024**3,
    }
    audited_model = models_root / "checkpoints" / "audited-small.safetensors"
    audited_model.write_bytes(b"audited-model")

    def final_model_safety_validation(tag):
        graph_sha256 = hashlib.sha256(f"graph:{tag}".encode()).hexdigest()
        receipt_sha256 = hashlib.sha256(f"receipt:{tag}".encode()).hexdigest()
        file_stat = audited_model.stat()
        return {
            "ok": True,
            "schema_version": "hackme.comfyui-final-model-safety/v1",
            "graph_sha256": graph_sha256,
            "receipt_sha256": receipt_sha256,
            "recomputed_receipt_sha256": receipt_sha256,
            "backend_origin": "http://127.0.0.1:8188",
            "models_root_realpath": str(models_root.resolve()),
            "reference_count": 1,
            "distinct_model_file_count": 1,
            "distinct_model_total_bytes": file_stat.st_size,
            "terminal_model_file_revalidated_count": 1,
            "terminal_model_files_unchanged": True,
            "backend_history_binding_verified": True,
            "terminal_prompt_id": f"prompt-{tag}",
            "backend_history_binding": {
                "schema_version": "hackme.comfyui-final-model-safety-backend-binding/v1",
                "ok": True,
                "prompt_id": f"prompt-{tag}",
                "graph_sha256": graph_sha256,
                "receipt_sha256": receipt_sha256,
                "history_prompt_tuple_minimum_fields": 4,
                "history_graph_verified": True,
                "history_marker_verified": True,
            },
            "model_files": [{
                "relative_path": audited_model.relative_to(models_root).as_posix(),
                "size_bytes": file_stat.st_size,
                "sha256": _sha256(audited_model),
                "stat": {
                    "device": file_stat.st_dev,
                    "inode": file_stat.st_ino,
                    "mode": file_stat.st_mode,
                    "link_count": file_stat.st_nlink,
                    "size_bytes": file_stat.st_size,
                    "mtime_ns": file_stat.st_mtime_ns,
                    "ctime_ns": file_stat.st_ctime_ns,
                },
            }],
            "errors": [],
        }
    model_safety_rows = {
        workflow_id: {
            "ok": True,
            "reference_count": 1,
            "model_file_count": 1,
            "model_total_bytes": audited_model.stat().st_size,
            "models": [{
                "path": str(audited_model.resolve()),
                "relative_path": audited_model.resolve().relative_to(models_root.resolve()).as_posix(),
                "size_bytes": audited_model.stat().st_size,
                "sha256": _sha256(audited_model),
                "references": [{
                    "node_id": "1",
                    "class_type": "CheckpointLoaderSimple",
                    "input_name": "ckpt_name",
                    "value": audited_model.name,
                }],
            }],
            "errors": [],
            "oversized_model_files": [],
            "reasons": [],
        }
        for workflow_id in SYSTEM_WORKFLOW_IDS
    }
    model_safety = {
        "schema_version": "hackme.formal-comfyui-model-safety/v1",
        "ok": True,
        "expected_workflow_count": len(SYSTEM_WORKFLOW_IDS),
        "actual_workflow_count": len(SYSTEM_WORKFLOW_IDS),
        "safe_workflow_count": len(SYSTEM_WORKFLOW_IDS),
        "unsafe_workflow_count": 0,
        "unsafe_workflows": [],
        "hash_coverage_complete": True,
        "limits": {
            "max_model_file_bytes": 2 * 1024**3,
            "max_workflow_model_total_bytes": 4 * 1024**3,
            "limits_can_only_tighten": True,
        },
        "workflows": model_safety_rows,
    }

    ui_rows = []
    for label in ("desktop", "mobile"):
        main = artifact(f"ui/{label}_main.png", f"{label}-main".encode())
        editor = artifact(f"ui/{label}_editor.png", f"{label}-editor".encode())
        ui_rows.append({
            "label": label,
            "ok": True,
            "context_closed": True,
            "main": {
                "options": len(SYSTEM_WORKFLOW_IDS) + 1,
                "official": len(SYSTEM_WORKFLOW_IDS),
                "visualLinkVisible": True,
            },
            "editor": {"nodes": 7, "edges": 8},
            "main_screenshot": main["path"],
            "editor_screenshot": editor["path"],
            "console_errors": [],
            "page_errors": [],
            "failed_requests": [],
            "overflow": False,
            "editor_overflow": False,
        })
    offline_screenshot = artifact("ui/offline.png", b"offline-visible")
    contract = {
        "real_backend_required": True,
        "feature_probe": True,
        "official_templates_execute": True,
        "custom_workflow_create_import_run_output_delete": True,
        "ai_agent_generation_terminal_output": True,
        "desktop_mobile_workflow_ui": True,
        "offline_and_dependency_failure_visible": True,
    }
    probe = {
        "schema_version": "hackme.formal-comfyui-workflows-probe/v1",
        "run_id": "formal-comfyui-fixture",
        "comfyui_url": "http://127.0.0.1:8188",
        "ok": True,
        "errors": [],
        "contract": contract,
        "sections": {
            "real_backend": {
                "ok": True,
                "health": {"ok": True},
                "object_info_node_count": 50,
                "required_nodes": [
                    "CheckpointLoaderSimple",
                    "KSampler",
                    "SaveImage",
                    "VAEDecode",
                ],
                "missing_nodes": [],
                "safe_required_nodes": [
                    "CLIPTextEncode",
                    "DualCLIPLoader",
                    "EmptyLatentImage",
                    "UnetLoaderGGUF",
                    "VAELoader",
                ],
                "missing_safe_nodes": [],
            },
            "safety": {
                "ok": True,
                "selection": {
                    "profile_id": "diving_illustrious_flat_anime_sdxl",
                    "variant_id": "q4_k_m",
                    "gguf_file": "diving-illustrious-flat-anime-paradigm-shift.Q4_K_M.gguf",
                    "size_bytes": 1_446_633_120,
                    "size_evidence": "versioned_allowlist_plus_actual_file_stat_and_sha256",
                    "remote_file_stat_available": True,
                    "max_size_bytes": 2 * 1024 * 1024 * 1024,
                    "max_workflow_model_total_bytes": 4 * 1024**3,
                    "actual_model_total_bytes": sum(size for _path, size in safe_model_specs.values()),
                    "actual_files": safe_actual_files,
                    "safe_vae_override": "illustrious_vae.safetensors",
                    "backend_url": "http://127.0.0.1:8188",
                    "selection_rule": "first_exact_match_in_versioned_allowlist",
                },
                "canary": {
                    "ok": True,
                    "workflow_id": "origin_sdxl_gguf_txt2img",
                    "workflow_run_id": 191,
                    "terminal_workflow_run_id": 191,
                    "job_id": "safe-canary-job",
                    "terminal_status": "completed",
                    "profile_id": "diving_illustrious_flat_anime_sdxl",
                    "variant_id": "q4_k_m",
                    "size_bytes": 1_446_633_120,
                    "safe_vae_override": "illustrious_vae.safetensors",
                    "artifact_count": 1,
                    "artifacts": [canary_artifact],
                    "input_cleanup_validation": no_temp_cleanup_validation(),
                    "final_model_safety_validation": final_model_safety_validation("canary"),
                },
                "monitor": {
                    "sample_schema_version": "hackme.formal-comfyui-safety-sample/v1",
                    "sample_path": str(safety_samples.resolve()),
                    "limits": {
                        "min_mem_available_bytes": 1024 * 1024 * 1024,
                        "min_disk_free_bytes": 20 * 1024 * 1024 * 1024,
                        "max_queue_depth": 1,
                        "cancel_grace_seconds": 45,
                        "min_backend_vram_free_bytes": 256 * 1024**2,
                        "max_gpu_temperature_c": 80,
                        "expected_cgroup_limits": EXPECTED_COMFY_CGROUP_LIMITS,
                    },
                    "backend_scope": {
                        "ok": True,
                        "campaign_cgroup_path": campaign_scope,
                        "backend_pid": 4321,
                        "backend_start_ticks": 100,
                        "backend_inside_campaign_scope": True,
                        "backend_cgroup_path": campaign_scope,
                        "backend_cmdline_sha256": "a" * 64,
                        "backend_cwd": str(tmp_path.resolve()),
                        "models_root": str(models_root.resolve()),
                        "models_root_bound_to_backend": True,
                        "backend_port": 8188,
                        "listening_socket_verified": True,
                        "matching_listening_socket_inodes": [12345],
                        "probe_pid": 4322,
                        "probe_inside_campaign_scope": True,
                        "probe_cgroup_path": campaign_scope,
                    },
                    "sample_count": 3,
                    "samples_complete": True,
                    "field_completeness_ratio": 1.0,
                    "sample_gap_within_30_seconds": True,
                    "collector_errors": [],
                    "hard_stop_samples": [],
                    "abort_events": [],
                },
            },
            "feature_probe": {
                "ok": True,
                "child": {"exit_code": 0},
                "validation": {
                    "ok": True,
                    "missing": [],
                    "duplicates": [],
                    "non_pass": {},
                },
                "decoded_output_count": 6,
                "history_inventory_exact": True,
                "created_history_ids": list(range(101, 108)),
                "feature_checkpoint": feature_checkpoint,
                "input_cleanup": {
                    "exact": True,
                    "attempted_count": 3,
                    "exact_deleted_or_missing_count": 3,
                    "immutable_residuals": [],
                    "uncertain_uploads": [],
                    "failures": [],
                    "rows": [
                        {
                            "correlated": True,
                            "exact": True,
                            "immutable_residual": False,
                            "response": {
                                "_http_status": 200,
                                "ok": True,
                                "discard": dict(exact_remote_discard),
                            },
                        }
                        for _index in range(3)
                    ],
                },
                "report_path": str(feature_report.resolve()),
            },
            "dependency_preflight": {
                "ok": True,
                "expected_count": len(SYSTEM_WORKFLOW_IDS),
                "actual_count": len(SYSTEM_WORKFLOW_IDS),
                "missing_workflows": [],
                "unexpected_workflows": [],
                "dependency_failures": {},
                "source_dependency_contract_count": len(SYSTEM_WORKFLOW_IDS),
                "source_dependency_contracts_ok": True,
                "source_dependency_contracts": {
                    workflow_id: manifest_dependency_contract()
                    for workflow_id in SYSTEM_WORKFLOW_IDS
                },
                "safe_override_ok": True,
                "safe_profile_id": "diving_illustrious_flat_anime_sdxl",
                "safe_variant_id": "q4_k_m",
                "model_safety": model_safety,
                "feature_checkpoint": feature_checkpoint,
                "all_report_path": str(dependency_all_report.resolve()),
                "safe_report_path": str(dependency_safe_report.resolve()),
            },
            "official_templates": {
                "ok": True,
                "child": {"exit_code": 0},
                "validation": {
                    "ok": True,
                    "expected_count": len(SYSTEM_WORKFLOW_IDS),
                    "actual_count": len(SYSTEM_WORKFLOW_IDS),
                    "missing": [],
                    "unexpected": [],
                    "duplicates": [],
                    "bad_status": {},
                    "exact_counts": True,
                    "connection_ok": True,
                    "error_console_count": 0,
                    "page_error_count": 0,
                    "network_error_count": 0,
                },
                "report_path": str(official_report.resolve()),
                "artifact_count": len(official_artifacts),
                "artifacts": official_artifacts,
                "input_cleanup_validated_count": len(SYSTEM_WORKFLOW_IDS),
                "input_cleanup_validations": [
                    {
                        "bundle_id": workflow_id,
                        **no_temp_cleanup_validation(),
                    }
                    for workflow_id in SYSTEM_WORKFLOW_IDS
                ],
                "final_model_safety_validated_count": len(SYSTEM_WORKFLOW_IDS),
                "final_model_safety_validations": [
                    {
                        "bundle_id": workflow_id,
                        **final_model_safety_validation(f"official:{workflow_id}"),
                    }
                    for workflow_id in SYSTEM_WORKFLOW_IDS
                ],
            },
            "custom_workflow": {
                "ok": True,
                "preset_id": 101,
                "workflow_run_id": 201,
                "job_id": "custom-job",
                "terminal_status": "completed",
                "safe_profile_id": "diving_illustrious_flat_anime_sdxl",
                "safe_variant_id": "q4_k_m",
                "safe_gguf_file": "diving-illustrious-flat-anime-paradigm-shift.Q4_K_M.gguf",
                "safe_vae_override": "illustrious_vae.safetensors",
                "workflow_sha256": "c" * 64,
                "artifact_count": 1,
                "artifacts": [custom_artifact],
                "input_cleanup_validation": no_temp_cleanup_validation(),
                "final_model_safety_validation": final_model_safety_validation("custom"),
                "delete": {"ok": True},
                "delete_verified_http_status": 404,
            },
            "ai_agent_generation": {
                "ok": True,
                "catalog_names": ["write_comfyui_generate"],
                "job_id": "agent-job",
                "history_id": 0,
                "workflow_run_id": 301,
                "official_workflow_id": "origin_sdxl_gguf_txt2img",
                "safe_profile_id": "diving_illustrious_flat_anime_sdxl",
                "safe_variant_id": "q4_k_m",
                "safe_gguf_file": "diving-illustrious-flat-anime-paradigm-shift.Q4_K_M.gguf",
                "safe_vae_override": "illustrious_vae.safetensors",
                "terminal_status": "completed",
                "artifact_count": 1,
                "artifacts": [agent_artifact],
                "input_cleanup_validation": no_temp_cleanup_validation(),
                "final_model_safety_validation": final_model_safety_validation("ai-agent"),
                "action_policy": {"mode": "write"},
            },
            "workflow_ui": {
                "ok": True,
                "browser_closed": True,
                "rows": ui_rows,
            },
            "offline_failure": {
                "ok": True,
                "status_http": 200,
                "status": {"available": False},
                "workflows_http": 200,
                "dependency_warning": "ComfyUI offline",
                "generation_http": 503,
                "generation": {"ok": False},
                "terminal_failure": None,
                "ui_status_text": "ComfyUI offline",
                "ui_screenshot": offline_screenshot["path"],
                "page_errors": [],
                "browser_closed": True,
                "restored_available": True,
            },
        },
        "cleanup": {
            "exact": True,
            "safety_final": {"ok": True, "queue_empty_verified": True},
            "history": {
                "ok": True,
                "exact": True,
                "baseline_ids": ["1"],
                "after_ids": ["1"],
            },
            "workflow_inventory": {
                "ok": True,
                "baseline_ids": [1],
                "after_ids": [1],
                "unexpected": [],
                "missing": [],
            },
            "settings_restore": {"ok": True, "exact": True, "errors": []},
            "discard": [{
                "ok": True,
                "http_status": 200,
                "warning": "",
                "image_ref": {"filename": "generated.png"},
                "discard": dict(exact_remote_discard),
            }],
            "retained_remote_output_allowlist": [],
        },
    }
    report_path = tmp_path / "formal_comfyui_workflows_probe.json"
    report_path.write_text(
        json.dumps(probe, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    all_paths = [
        report_path,
        feature_report,
        official_report,
        dependency_all_report,
        dependency_safe_report,
        safety_samples,
        Path(canary_artifact["path"]),
        *[Path(row["path"]) for row in official_artifacts],
        Path(custom_artifact["path"]),
        Path(agent_artifact["path"]),
        *[
            Path(path)
            for row in ui_rows
            for path in (row["main_screenshot"], row["editor_screenshot"])
        ],
        Path(offline_screenshot["path"]),
    ]
    rows = [{
        "path": str(path.resolve()),
        "relative_path": path.resolve().relative_to(tmp_path.resolve()).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    } for path in all_paths]
    artifact_index = {
        "schema_version": "hackme.formal-comfyui-workflows-artifact-index/v1",
        "run_id": probe["run_id"],
        "report": {
            "path": str(report_path.resolve()),
            "size_bytes": report_path.stat().st_size,
            "sha256": _sha256(report_path),
        },
        "artifact_count": len(rows),
        "artifacts": rows,
    }
    return probe, artifact_index


def test_comfyui_selector_requires_real_terminal_outputs_ui_failure_and_cleanup(
    tmp_path: Path,
) -> None:
    probe, artifact_index = comfyui_fixture(tmp_path)

    selected = comfyui_workflow_assertions(probe, artifact_index)

    assert all(selected["scenario_assertions"].values())
    assert all(selected["terminal_assertions"].values())
    assert all(selected["cleanup_assertions"].values())

    probe["sections"]["official_templates"]["validation"]["actual_count"] -= 1
    probe["sections"]["workflow_ui"]["rows"][0]["context_closed"] = False
    probe["cleanup"]["settings_restore"]["exact"] = False
    failed = comfyui_workflow_assertions(probe, artifact_index)

    assert failed["scenario_assertions"]["official_templates_execute"] is False
    assert failed["scenario_assertions"]["desktop_mobile_workflow_ui"] is False
    assert failed["cleanup_assertions"]["exact_history_workflow_and_settings_restore"] is False


def test_comfyui_selector_rejects_artifact_index_digest_drift(tmp_path: Path) -> None:
    probe, artifact_index = comfyui_fixture(tmp_path)
    output = Path(probe["sections"]["custom_workflow"]["artifacts"][0]["path"])
    output.write_bytes(b"mutated-after-index")

    selected = comfyui_workflow_assertions(probe, artifact_index)

    assert selected["terminal_assertions"][
        "terminal_outputs_backend_restored_and_index_verified"
    ] is False
    assert selected["cleanup_assertions"][
        "browser_contexts_closed_and_artifacts_readable"
    ] is False


def test_comfyui_selector_rejects_tampered_final_graph_model_receipts(tmp_path: Path) -> None:
    probe, artifact_index = comfyui_fixture(tmp_path)
    probe["sections"]["safety"]["canary"]["final_model_safety_validation"][
        "recomputed_receipt_sha256"
    ] = "0" * 64
    probe["sections"]["official_templates"]["final_model_safety_validations"][0][
        "model_files"
    ][0]["relative_path"] = "../escape.safetensors"
    probe["sections"]["custom_workflow"]["final_model_safety_validation"]["errors"] = [
        "receipt_sha256_mismatch"
    ]
    probe["sections"]["ai_agent_generation"]["final_model_safety_validation"][
        "distinct_model_total_bytes"
    ] = 4 * 1024**3 + 1

    selected = comfyui_workflow_assertions(probe, artifact_index)

    assert selected["scenario_assertions"]["real_backend_required"] is False
    assert selected["scenario_assertions"]["official_templates_execute"] is False
    assert selected["scenario_assertions"][
        "custom_workflow_create_import_run_output_delete"
    ] is False
    assert selected["scenario_assertions"]["ai_agent_generation_terminal_output"] is False


def test_comfyui_selector_requires_terminal_model_file_revalidation(tmp_path: Path) -> None:
    probe, artifact_index = comfyui_fixture(tmp_path)
    validation = probe["sections"]["official_templates"][
        "final_model_safety_validations"
    ][0]
    validation["terminal_model_files_unchanged"] = False
    validation["terminal_model_file_revalidated_count"] = 0

    selected = comfyui_workflow_assertions(probe, artifact_index)

    assert selected["scenario_assertions"]["official_templates_execute"] is False


def test_comfyui_selector_requires_exact_backend_history_binding(tmp_path: Path) -> None:
    probe, artifact_index = comfyui_fixture(tmp_path)
    validation = probe["sections"]["official_templates"][
        "final_model_safety_validations"
    ][0]
    validation["backend_history_binding"]["graph_sha256"] = "0" * 64

    selected = comfyui_workflow_assertions(probe, artifact_index)

    assert selected["scenario_assertions"]["official_templates_execute"] is False


def test_comfyui_selector_rejects_discard_success_without_exact_absence_proof(
    tmp_path: Path,
) -> None:
    probe, artifact_index = comfyui_fixture(tmp_path)
    probe["cleanup"]["discard"][0]["discard"]["verification"] = ""
    feature_row = probe["sections"]["feature_probe"]["input_cleanup"]["rows"][0]
    feature_row["response"]["discard"]["verification"] = ""

    selected = comfyui_workflow_assertions(probe, artifact_index)

    assert selected["scenario_assertions"]["feature_probe"] is False
    assert selected["cleanup_assertions"][
        "exact_history_workflow_and_settings_restore"
    ] is False


def test_comfyui_selector_rejects_manifest_contract_claim_with_raw_difference(
    tmp_path: Path,
) -> None:
    probe, artifact_index = comfyui_fixture(tmp_path)
    workflow_id = next(iter(SYSTEM_WORKFLOW_IDS))
    contract = probe["sections"]["dependency_preflight"][
        "source_dependency_contracts"
    ][workflow_id]
    contract["differences"]["models"]["missing_from_manifest"] = [
        ["clip", "missing.safetensors"]
    ]

    selected = comfyui_workflow_assertions(probe, artifact_index)

    assert selected["scenario_assertions"]["official_templates_execute"] is False


def test_comfyui_selector_rejects_digest_consistent_non_image_artifact(tmp_path: Path) -> None:
    probe, artifact_index = comfyui_fixture(tmp_path)
    artifact = probe["sections"]["custom_workflow"]["artifacts"][0]
    output = Path(artifact["path"])
    output.write_bytes(b"not-a-real-png")
    artifact["size_bytes"] = output.stat().st_size
    artifact["sha256"] = _sha256(output)
    for row in artifact_index["artifacts"]:
        if row["path"] == str(output.resolve()):
            row["size_bytes"] = output.stat().st_size
            row["sha256"] = _sha256(output)

    report_path = Path(artifact_index["report"]["path"])
    report_path.write_text(json.dumps(probe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_sha = _sha256(report_path)
    artifact_index["report"]["size_bytes"] = report_path.stat().st_size
    artifact_index["report"]["sha256"] = report_sha
    for row in artifact_index["artifacts"]:
        if row["path"] == str(report_path.resolve()):
            row["size_bytes"] = report_path.stat().st_size
            row["sha256"] = report_sha

    selected = comfyui_workflow_assertions(probe, artifact_index)

    assert selected["scenario_assertions"][
        "custom_workflow_create_import_run_output_delete"
    ] is False


def cloud_fixture(tmp_path: Path) -> dict:
    video = tmp_path / "fixture.mkv"
    desktop = tmp_path / "desktop.png"
    mobile = tmp_path / "mobile.png"
    video.write_bytes(b"fixture")
    desktop.write_bytes(b"png-desktop")
    mobile.write_bytes(b"png-mobile")
    browser_rows = []
    for viewport, screenshot in (("desktop", desktop), ("mobile", mobile)):
        browser_rows.append({
            "viewport": viewport,
            "state": {"player_present": True, "root_overflow_px": 0},
            "page_errors": [],
            "screenshot": str(screenshot),
            "screenshot_size_bytes": screenshot.stat().st_size,
            "context_closed": True,
        })
    return {
        "ok": True,
        "errors": [],
        "fixture": {
            "path": str(video),
            "size_bytes": video.stat().st_size,
            "sha256": "a" * 64,
            "duration_seconds": 12,
            "video_streams": 1,
            "audio_streams": 2,
            "subtitle_streams": 1,
        },
        "upload": {
            "status": 200,
            "body": {"storage_file": {"id": "storage-1", "file_id": "file-1"}},
        },
        "hls_worker": {"returncode": 0, "payload": {"ok": True}},
        "stream": {"status": "ready", "master_manifest_ready": True},
        "share": {
            "share_id": "share-1",
            "token": "token-1",
            "password_required_status": 401,
            "wrong_password_status": 403,
            "unlocked": {"status": 200},
            "audio_track_count": 2,
            "subtitle_count": 1,
            "master_status": 200,
            "master_extm3u": True,
            "variant_status": 200,
            "variant_extm3u": True,
            "segment_status": 200,
            "segment_bytes": 1024,
            "subtitle_status": 200,
            "subtitle_webvtt": True,
            "realtime_proxy": {
                "status": 200,
                "streaming_mode": "realtime_proxy",
                "transfer_mode": "python_realtime_proxy",
                "first_chunk_bytes": 4096,
            },
        },
        "browser": {"browser_closed": True, "rows": browser_rows},
        "cleanup": {
            "revoke": {"status": 200},
            "revoked_access_status": 404,
            "trash": {"status": 200},
            "purge": {"status": 200},
            "owner_preview_after_purge_status": 404,
        },
    }


def test_cloud_selector_requires_real_hls_proxy_mobile_and_revoke(tmp_path: Path) -> None:
    probe = cloud_fixture(tmp_path)
    selected = cloud_drive_stream_assertions(probe)

    assert all(selected["scenario_assertions"].values())
    assert all(selected["terminal_assertions"].values())
    assert all(selected["cleanup_assertions"].values())

    probe["share"]["segment_bytes"] = 0
    failed = cloud_drive_stream_assertions(probe)
    assert failed["scenario_assertions"]["storage_share_master_variant_segment_subtitle"] is False


def test_cloud_selector_rejects_unpurged_file_and_open_browser(tmp_path: Path) -> None:
    probe = cloud_fixture(tmp_path)
    probe["cleanup"]["purge"]["status"] = 500
    probe["browser"]["rows"][0]["context_closed"] = False

    selected = cloud_drive_stream_assertions(probe)

    assert selected["cleanup_assertions"]["share_revoked_and_product_file_purged"] is False
    assert selected["cleanup_assertions"]["browser_contexts_closed"] is False


def trading_fixture() -> tuple[dict, dict, dict, dict, dict, dict]:
    check_names = (
        "background matched limit order without active member browser",
        "margin liquidation seed open",
        "margin take-profit seed open",
        "margin interest seed open",
        "background liquidated margin account without active member browser",
        "background triggered margin take-profit without active member browser",
        "background accrued margin interest without active member browser",
        "background triggered spot stop-loss without active member browser",
        "background triggered spot take-profit without active member browser",
        "background triggered workflow/conditional bot without active browser",
        "background triggered DCA bot without active browser",
        "background scanned grid bot and filled crossed grid order without active browser",
        "background jobs have no recorded failures",
        "Playwright concurrent order stress has no 5xx and produces fills",
    )
    job_keys = [
        "order_matching",
        "take_profit_stop_loss_scan",
        "bot_trigger_scan",
        "margin_liquidation_scan",
        "interest_accrual",
    ]
    background = {
        "checks": [{"name": name, "ok": True} for name in check_names],
        "scenario": {
            "trigger_mode": "auto",
            "users": {
                "limit": {"username": "qa_limit"},
                "workflow_bot": {"username": "qa_workflow"},
            },
            "member_contexts_closed_before_background": True,
            "root_context_closed_before_background": True,
            "runtime_settings_restored": True,
            "feature_flags_restored": True,
            "background_terminal": {
                "recent_job_keys": job_keys,
                "failure_counts": {key: 0 for key in job_keys},
            },
            "concurrent_stress": {
                "requested_per_user": 150,
                "request_count": 300,
                "success_count": 300,
                "no_5xx": True,
            },
            "domain_terminal": {
                "limit_order_status": "filled",
                "margin_liquidation_status": "liquidated",
                "margin_take_profit_status": "closed",
                "margin_interest_hours": 3,
                "spot_stop_loss_quantity_units": 0,
                "spot_take_profit_quantity_units": 0,
                "workflow_bot_triggered_runs": 1,
                "dca_bot_triggered_runs": 1,
                "grid_filled_orders": 1,
                "negative_wallet_rows": [],
                "negative_spot_lock_rows": [],
                "reserve_before": 100,
                "reserve_after": 120,
            },
        },
    }
    cancel = {
        "order_uuid": "cancel-order",
        "cancel_results": [
            {"status": 200, "body": {"status": "cancelled"}},
            {"status": 400, "body": {"ok": False}},
        ],
        "final_order": {"order_uuid": "cancel-order", "status": "cancelled"},
        "locked_points_increased": True,
        "locked_points_restored_exactly": True,
    }
    custom = {
        "template_id": "formal_trade_fixture",
        "edited_label": "Formal workflow v2",
        "template_absent_before": True,
        "initial_save": {
            "status": 200,
            "body": {"template": {"id": "formal_trade_fixture"}},
        },
        "edited_save": {
            "status": 200,
            "body": {"template": {"label": "Formal workflow v2"}},
        },
        "edited_template_visible": True,
        "backtest": {"status": 200, "body": {"ok": True, "candle_count": 50, "trade_count": 1}},
        "bot_uuid": "bot-1",
        "bot_create": {"status": 200, "body": {"bot": {"enabled": False}}},
        "bot_enable": {"status": 200, "body": {"bot": {"enabled": True}}},
        "scan_trigger": {"bot_uuid": "bot-1", "order_uuid": "trade-1"},
        "trade_order": {"order_uuid": "trade-1", "status": "filled"},
    }
    restart = {
        "old_pid": 10,
        "new_pid": 20,
        "old_master_remaining": False,
        "old_process_group_remaining": False,
        "readiness": {"ok": True},
        "template_found": True,
        "bot_found": True,
        "trade_order_found": True,
        "template_file_hash_preserved": True,
    }
    final_state = {
        "readiness": {"ok": True},
        "trading_verify_job": {"terminal_status": "succeeded"},
        "trading_verify_latest": {"body": {"verification": {"ok": True, "errors": []}}},
        "points_verify_job": {"terminal_status": "succeeded"},
        "points_verify_latest": {
            "body": {"verification": {"ok": True, "errors": [], "financial_ok": True}},
        },
        "reserve_balance_points": 120,
    }
    cleanup = {
        "bot_deleted": True,
        "bot_absent": True,
        "template_file_removed": True,
        "template_absent_from_api": True,
        "custom_user_directory_absent": True,
        "account_records": [
            {"username": "qa_limit", "deleted": True, "residual_exact_count": 0},
            {"username": "qa_workflow", "deleted": True, "residual_exact_count": 0},
        ],
    }
    return background, cancel, custom, restart, final_state, cleanup


def test_trading_selector_requires_cancel_race_workflow_trade_restart_and_cleanup() -> None:
    selected = trading_workflow_assertions(*trading_fixture())

    assert all(selected["scenario_assertions"].values())
    assert all(selected["terminal_assertions"].values())
    assert all(selected["cleanup_assertions"].values())

    background, cancel, custom, restart, final_state, cleanup = trading_fixture()
    cancel["cancel_results"][1]["status"] = 500
    failed = trading_workflow_assertions(background, cancel, custom, restart, final_state, cleanup)
    assert failed["scenario_assertions"]["spot_order_match_cancel_race"] is False


def test_trading_selector_rejects_residual_account_and_missing_workflow_file_cleanup() -> None:
    background, cancel, custom, restart, final_state, cleanup = trading_fixture()
    cleanup["account_records"][0]["residual_exact_count"] = 1
    cleanup["template_file_removed"] = False

    selected = trading_workflow_assertions(background, cancel, custom, restart, final_state, cleanup)

    assert selected["cleanup_assertions"]["exact_fixture_accounts_removed"] is False
    assert selected["cleanup_assertions"]["custom_bot_and_workflow_removed"] is False


def points_fixture() -> tuple[dict, dict, dict, dict]:
    stress = {
        "accounts_requested": 24,
        "accounts_active": 24,
        "direct_transfer_ops_requested": 12000,
        "direct_transfer_completed": 12000,
        "direct_transfer_errors": 0,
        "transfer_ops_requested": 1200,
        "external_transfer_count": 40,
        "fixture_usernames": ["dstress_a", "dstress_b"],
        "findings": [],
        "sample_errors": [],
        "db_counts": {
            "prefix_confirmed": 1240,
            "prefix_pending": 0,
            "duplicate_request_uuid_groups": 0,
            "duplicate_active_wallet_address_groups": 0,
            "database_bytes": 4096,
        },
        "idempotency_probe": {
            "first_transaction_hash": "a" * 64,
            "second_transaction_hash": "a" * 64,
            "second_created": False,
        },
        "overspend_probe": {
            "attempt_count": 12,
            "insufficient_balance_rejection_count": 8,
            "balance_before": 100,
            "balance_after": 0,
        },
        "explorer_finalized_transfers": {
            "remaining_pending": 0,
            "remaining_request_uuids": [],
        },
        "verify": {
            "verification": {"ok": True, "errors": [], "financial_ok": True},
        },
    }
    dispute = {
        "fixture_usernames": ["victim_a", "suspect_a"],
        "tx_hash": "b" * 64,
        "dispute_uuid": "dispute-1",
        "proposal_uuid": "proposal-1",
        "address_risk_proposal_uuid": "risk-1",
        "address_freeze_proposal_uuid": "freeze-1",
        "review_status": "approved",
        "provisional_freeze_status": "active",
        "wrong_purpose_status": 400,
        "wrong_branch_status": 400,
        "replay_status": 400,
        "redaction": {
            "create_response_identity_leak": False,
            "review_response_identity_leak": False,
            "list_response_identity_leak": False,
        },
    }
    frontend = {
        "root": {"ok": True, "chain": {"visible": True, "ok_text": "完整"}},
        "member": {"ok": True},
        "browser_errors": [],
    }
    cleanup = {
        "records": [
            {"username": name, "deleted": True, "residual_exact_count": 0}
            for name in ("dstress_a", "dstress_b", "victim_a", "suspect_a")
        ],
    }
    return stress, dispute, frontend, cleanup


def proxy_fixture() -> tuple[dict, dict, dict, dict]:
    service = {
        "first_stream": {
            "active_after_first_open": 1,
            "busy_error": "realtime_proxy_busy:2",
            "first_chunk_bytes": 4096,
            "active_after_close": 0,
            "metrics": {"closed_by_client": True, "runtime_scope": "global"},
        },
        "reopen_stream": {
            "output_bytes": 8192,
            "selected_audio": "audio_02_eng",
            "active_after_reopen": 0,
            "metrics": {"runtime_scope": "global"},
        },
        "cleanup": {
            "active_slots_released": True,
            "held_slots_released": True,
            "environment_restored": True,
        },
    }
    http = {
        "http_phase": {
            "held_standard": {"status": 200},
            "busy_standard": {"status": 429, "body_sample": "realtime_proxy_busy"},
            "basic_direct": {"status": 206, "first_chunk_bytes": 2048},
            "premium_hls": {
                "master": {"status": 200},
                "playlist": {"status": 200},
                "segment": {"status": 206, "first_chunk_bytes": 2048},
            },
            "held_standard_first_chunk": {"first_chunk_bytes": 2048},
        },
        "server_metrics_summary": {
            "count": 2,
            "latest": {
                "runtime": {"scope": "global"},
                "metrics": {
                    "finished": True,
                    "bytes_sent": 4096,
                    "closed_by_client": True,
                    "runtime_scope": "global",
                },
            },
        },
        "cleanup": {"server_stopped": True},
    }
    checks = []
    for name in ("chromium", "firefox", "webkit"):
        for viewport in ("desktop", "mobile"):
            checks.append({
                "browser": name,
                "viewport": viewport,
                "ok": True,
                "standard": {"audio_switched": True},
                "subtitle_shift": {
                    "ok": True,
                    "reset": True,
                    "expected_subtitle_count": 1,
                },
            })
    browser = {
        "require_all_browsers": True,
        "checks": checks,
        "browser_coverage": {
            "mode": "require_all_browsers",
            "expected_check_count": 6,
            "observed_check_count": 6,
            "missing": [],
            "skipped": [],
            "failed": [],
        },
        "cleanup": {"server_stopped": True},
    }
    chat = {
        "href": "https://example.invalid/shared/videos/probeToken_ABC-123#vk=probe-fragment_456",
        "browser_errors": [],
        "cleanup": {
            "message_deleted": True,
            "room_deleted": True,
            "room_absent": True,
            "setting_restored": True,
        },
    }
    return service, http, browser, chat


def test_points_selector_requires_raw_terminal_invariants_and_exact_cleanup() -> None:
    selected = pointschain_hft_assertions(*points_fixture())

    assert all(selected["scenario_assertions"].values())
    assert all(selected["terminal_assertions"].values())
    assert all(selected["cleanup_assertions"].values())

    stress, dispute, frontend, cleanup = points_fixture()
    stress["idempotency_probe"]["second_transaction_hash"] = "c" * 64
    failed = pointschain_hft_assertions(stress, dispute, frontend, cleanup)
    assert failed["scenario_assertions"]["idempotency_overspend_replay_rejection"] is False


def test_points_selector_rejects_residual_fixture_account() -> None:
    stress, dispute, frontend, cleanup = points_fixture()
    cleanup["records"][0]["residual_exact_count"] = 1

    selected = pointschain_hft_assertions(stress, dispute, frontend, cleanup)

    assert selected["cleanup_assertions"]["exact_fixture_accounts_removed"] is False


def test_proxy_selector_requires_all_six_browser_observations_and_real_switches() -> None:
    selected = media_proxy_assertions(*proxy_fixture())

    assert all(selected["scenario_assertions"].values())
    assert all(selected["terminal_assertions"].values())
    assert all(selected["cleanup_assertions"].values())

    service, http, browser, chat = proxy_fixture()
    browser["checks"][-1]["subtitle_shift"]["reset"] = False
    failed = media_proxy_assertions(service, http, browser, chat)
    assert failed["scenario_assertions"]["audio_track_and_subtitle_switch"] is False


def test_proxy_selector_rejects_leaked_probe_server_and_chat_room() -> None:
    service, http, browser, chat = proxy_fixture()
    http["cleanup"]["server_stopped"] = False
    chat["cleanup"]["room_absent"] = False

    selected = media_proxy_assertions(service, http, browser, chat)

    assert selected["cleanup_assertions"]["probe_servers_stopped"] is False
    assert selected["scenario_assertions"]["chat_video_share_embed"] is False


def final_ui_fixture() -> tuple[dict, dict, dict, dict, dict]:
    clean_observation = {
        "rootOverflowPx": 0,
        "undersized": [],
        "outside": [],
        "clipped": [],
        "hiddenFocusable": [],
        "frontendFailures": [],
    }
    roles = []
    for role, identity, module_count in (
        ("root_desktop", "root", 10),
        ("member_mobile", "user", 5),
    ):
        modules = [
            {
                "module": f"module_{index}",
                "passed": True,
                "observation": dict(clean_observation),
            }
            for index in range(module_count)
        ]
        roles.append({
            "role": role,
            "identity_role": identity,
            "passed": True,
            "context_closed": True,
            "visible_modules": [row["module"] for row in modules],
            "modules": modules,
            "browser_errors": [],
            "failed_responses": [],
            "failed_requests": [],
        })
    screenshots = [
        {
            "role": role,
            "path": f"/tmp/{role}_{index}.png",
            "size_bytes": 1024,
            "viewport": {"width": width, "height": 844},
        }
        for role, width in (("root_desktop", 1366), ("member_mobile", 390))
        for index in range(2)
    ]
    ui = {
        "terminal_pass": True,
        "browser_closed": True,
        "roles": roles,
        "screenshots": screenshots,
    }
    launch_gate = {
        "WHOLE_SITE_PRODUCTION_GATE_SUMMARY": {
            "result": "PASS",
            "production_readiness": "YES",
            "modules_failed": 0,
            "critical_findings": 0,
            "high_findings": 0,
            "unresolved_risks": [],
            "required_followups": [],
        },
        "modules": [{"name": "A", "status": "PASS"}],
    }
    invariants = {
        "readiness": {"ok": True},
        "audit_integrity": {
            "body": {
                "audit_integrity": {
                    "enabled": True,
                    "ok": True,
                    "broken_at": None,
                },
            },
        },
        "database_integrity": {"body": {"database": {"ok": True}}},
        "mode_log_chain": {
            "body": {
                "broken_links": 0,
                "invalid_signatures": [],
                "result": "PASS",
            },
        },
        "points_verify_job": {"terminal_status": "succeeded"},
        "points_verify_latest": {
            "body": {
                "verification": {"ok": True, "errors": [], "financial_ok": True},
            },
        },
        "trading_verify_job": {"terminal_status": "succeeded"},
        "trading_verify_latest": {
            "body": {"verification": {"ok": True, "errors": []}},
        },
        "sqlite_quick_checks": {"database.db": {"ok": True}},
    }
    load_context = {
        "campaign_active": True,
        "core_load_process_alive": True,
        "resource_monitor_alive": True,
        "latest_target_sample_at_load": True,
    }
    process_cleanup = {"new_descendant_pids": []}
    return ui, launch_gate, invariants, load_context, process_cleanup


def bt_download_fixture(tmp_path: Path) -> tuple[dict, dict, dict]:
    payload_bytes = b"formal-bt-downloaded-video" * 512
    source = tmp_path / "source.ts"
    magnet = tmp_path / "magnet.ts"
    torrent_download = tmp_path / "torrent-download.ts"
    metainfo = tmp_path / "fixture.torrent"
    trace = tmp_path / "events.jsonl"
    for path in (source, magnet, torrent_download):
        path.write_bytes(payload_bytes)
    metainfo.write_bytes(b"d4:infod4:name7:video.tsee")
    trace.write_text('{"event":"complete"}\n', encoding="utf-8")
    digest = hashlib.sha256(payload_bytes).hexdigest()
    info_hash = "a" * 40
    size_bytes = len(payload_bytes)
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
        "downloaded_bytes": size_bytes,
        "left_bytes": 0,
        "error_code": 0,
        "status": "seeding",
    }
    primary_source_proof = {
        "ok": True,
        "expected_peer_ip": "192.168.18.18",
        "observed_source_ip": "192.168.18.18",
        "token_sha256": "c" * 64,
        "proof_age_seconds": 0.01,
        "one_time_challenge_consumed": True,
    }
    alternate_source_proof = {
        "ok": True,
        "expected_peer_ip": "192.168.122.1",
        "observed_source_ip": "10.255.255.254",
        "token_sha256": "d" * 64,
        "proof_age_seconds": 0.02,
        "one_time_challenge_consumed": True,
    }
    raw = {
        "payload": {
            "source_path": str(source.resolve()),
            "torrent_path": str(metainfo.resolve()),
            "source_sha256": digest,
            "size_bytes": size_bytes,
            "info_hash": info_hash,
            "video_probes": [
                {"role": role, "ok": True, "video_stream_count": 1}
                for role in ("source", "magnet_download", "torrent_file_download")
            ],
        },
        "local_seed": {
            "private_torrent": False,
            "discovery_isolated": True,
            "torrent_tracker_count": 1,
            "seed_terminal": True,
            "seed_hash": info_hash,
            "peer_bind_ip": "192.168.18.18",
            "initial_source_route_proof": primary_source_proof,
            "session_isolation": [dict(isolation), dict(isolation), dict(isolation), dict(isolation)],
        },
        "tracker": {
            "bind_ip": "192.168.18.18",
            "advertised_peer_ip": "192.168.18.18",
            "advertised_peer_ip_private": True,
            "advertised_peer_ips": ["192.168.18.18", "192.168.122.1"],
            "all_advertised_peer_ips_private": True,
            "registered_peer_endpoints": {"51001": "192.168.18.18", "51002": "192.168.122.1"},
            "registered_announce_sources": {
                "51001": {"expected_peer_ip": "192.168.18.18", "observed_source_ip": "192.168.18.18", "source_proof_sha256": "c" * 64},
                "51002": {"expected_peer_ip": "192.168.122.1", "observed_source_ip": "10.255.255.254", "source_proof_sha256": "d" * 64},
            },
            "source_route_proofs": [primary_source_proof, alternate_source_proof],
            "announces": [
                {"remote_ip": "192.168.18.18", "advertised_peer_ip": "192.168.18.18", "source_route_proof_sha256": "c" * 64},
                {"remote_ip": "10.255.255.254", "advertised_peer_ip": "192.168.122.1", "source_route_proof_sha256": "d" * 64},
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
            "download_path": str(magnet.resolve()),
            "download_path_exists": True,
            "download_size_bytes": size_bytes,
            "download_sha256": digest,
            "pause_resume": {
                "stop_rpc_success": True,
                "start_rpc_success": True,
                "before_pause": {"downloaded_bytes": 300_000, "percent_done": 0.25, "files": [{"bytes_completed": 300_000}]},
                "stable_during_pause": {"downloaded_bytes": 300_000, "status": "stopped", "files": [{"bytes_completed": 300_000}]},
                "after_resume": {"downloaded_bytes": 100_000, "percent_done": 0.5, "files": [{"bytes_completed": 400_000}]},
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
                        "old_ip": "192.168.18.18",
                        "new_ip": "192.168.122.1",
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
                "loaded_bytes": size_bytes,
                "total_bytes": size_bytes,
            },
            "download_path": str(torrent_download.resolve()),
            "download_path_exists": True,
            "download_size_bytes": size_bytes,
            "download_sha256": digest,
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

    def artifact(artifact_id: str, path: Path) -> dict:
        return {
            "artifact_id": artifact_id,
            "path": str(path.resolve()),
            "exists": True,
            "validated": True,
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    probe = {
        "schema_version": BT_PROBE_SCHEMA_VERSION,
        "probe": BT_PROBE_NAME,
        "terminal_state": "success",
        "ok": True,
        "local_only": True,
        "tracker_host_local_only": True,
        "errors": [],
        "raw": raw,
        "checks": derive_bt_checks(raw),
        "artifacts": [
            artifact("source_video", source),
            artifact("torrent_metainfo", metainfo),
            artifact("magnet_download", magnet),
            artifact("torrent_file_download", torrent_download),
            artifact("event_trace", trace),
        ],
    }
    upload = {
        "phase": "upload",
        "ok": True,
        "video": str(magnet.resolve()),
        "video_size_bytes": size_bytes,
        "uploads": [{"ok": True, "username": "campaign_1", "video_id": 71}],
    }
    stress = {
        "ok": True,
        "verdict": "PASS",
        "source_media": {
            "ok": True,
            "path": str(magnet.resolve()),
            "size_bytes": size_bytes,
            "duration_seconds": 5.0,
            "video_streams": 1,
        },
        "source_checks": {
            "probe": True,
            "duration": True,
            "audio_tracks": True,
            "subtitles": True,
        },
        "phases": [
            upload,
            {
                "phase": "wait",
                "ok": True,
                "failed_jobs": [],
                "final_state": {"jobs": [{"status": "succeeded"}]},
            },
            {
                "phase": "measure",
                "ok": True,
                "measurements": [{
                    "playback_json": {
                        "status": 200,
                        "payload": {"streaming_ready": True},
                    },
                    "variants": [{
                        "playlist": {"status": 200},
                        "media_segment_count": 2,
                        "burst": {"requested_segments": 2, "ok_segments": 2},
                    }],
                }],
            },
            {
                "phase": "share",
                "ok": True,
                "shares": [{
                    "ok": True,
                    "locked_without_password": {"status": 403},
                    "wrong_password": {"status": 403},
                    "unlock": {"share_session_present": True},
                    "playback": {"status": 200, "mode": "hls", "streaming_ready": True},
                    "master": {"ok": True, "extm3u": True},
                    "variant": {"ok": True, "status": 200},
                    "segments": [{"status": 200, "bytes": 2048}],
                    "revoke": {
                        "status": 200,
                        "post_revoke_playback_status": 410,
                        "post_revoke_master_status": 410,
                    },
                }],
            },
        ],
    }
    stream = {
        "playback_status": 200,
        "streaming_ready": True,
        "duration_seconds": 5.0,
        "variant_names": ["360p"],
        "master_status": 200,
        "master_extm3u": True,
        "variant_status": 200,
        "variant_segment_count": 2,
        "sample_segment_status": 200,
        "sample_segment_bytes": 2048,
    }
    restart = {
        "share_created": True,
        "restart": {
            "stopped": {
                "old_pid": 400,
                "master_process_remaining": False,
                "process_group_remaining": False,
            },
            "started": {"new_pid": 500, "ready": True},
        },
        "before_restart": dict(stream),
        "after_restart": dict(stream),
        "cleanup": {
            "continuity_share_revoked": True,
            "post_revoke_denied": True,
            "expected_video_count": 1,
            "deleted_video_count": 1,
            "all_videos_absent": True,
        },
    }
    return probe, stress, restart


def test_bt_selector_requires_recomputed_lifecycle_same_hash_and_hls(
    tmp_path: Path,
) -> None:
    selected = bt_download_assertions(*bt_download_fixture(tmp_path))

    assert all(selected["scenario_assertions"].values())
    assert all(selected["terminal_assertions"].values())
    assert all(selected["cleanup_assertions"].values())


def test_bt_selector_rejects_stored_fake_green_and_different_upload_source(
    tmp_path: Path,
) -> None:
    probe, stress, restart = bt_download_fixture(tmp_path)
    probe["raw"]["magnet"]["pause_resume"]["stable_during_pause"]["downloaded_bytes"] += 1
    stress["source_media"]["path"] = probe["raw"]["torrent_file"]["download_path"]
    stress["phases"][0]["video"] = probe["raw"]["torrent_file"]["download_path"]

    selected = bt_download_assertions(probe, stress, restart)

    assert selected["scenario_assertions"]["pause_resume_progress"] is False
    assert selected["scenario_assertions"]["downloaded_video_preview_share_stream_hls"] is False
    assert selected["terminal_assertions"]["bt_machine_report_terminal_success"] is False


def media_long_fixture() -> tuple[dict, dict]:
    uploads = [
        {
            "ok": True,
            "username": f"campaign_{index}",
            "video_id": index,
            "upload_started_at_ms": 1000 + index,
            "upload_finished_at_ms": 5000 + index,
        }
        for index in range(1, 4)
    ]
    measurement = {
        "playback_json": {
            "status": 200,
            "payload": {
                "streaming_ready": True,
                "audio_tracks": [{}, {}],
            },
        },
        "subtitles": [{"ok": True, "looks_like_webvtt": True}],
        "variants": [
            {
                "playlist": {"status": 200},
                "media_segment_count": 130,
                "burst": {"requested_segments": 16, "ok_segments": 16},
            },
        ],
    }
    shares = []
    for index in range(3):
        shares.append({
            "locked_without_password": {"status": 403},
            "wrong_password": {"status": 403},
            "unlock": {"share_session_present": True},
            "playback": {"audio_tracks": 2, "subtitles": 1},
            "master": {"extm3u": True},
            "variant": {"status": 200, "sampled_segments": 5},
            "segments": [{"status": 200, "bytes": 2048}],
            "revoke": {
                "status": 200,
                "post_revoke_playback_status": 410,
                "post_revoke_master_status": 410,
            },
            "browser": (
                [
                    {
                        "schema_version": "hackme.browser-video-latency/v1",
                        "viewport": viewport,
                        "ok": True,
                        "emulation": {
                            "is_mobile": viewport == "mobile",
                            "has_touch": viewport == "mobile",
                        },
                        "latency_thresholds_ms": {
                            "first_frame": 8000,
                            "random_seek_terminal": 5000,
                        },
                        "first_frame": {
                            "origin": "unlock_submit",
                            "terminal_event": "playing_and_video_frame",
                            "playing_observed": True,
                            "frame_observed": True,
                            "frame_observation_method": "requestVideoFrameCallback",
                            "elapsed_ms": 1200,
                            "play_to_frame_latency_ms": 500,
                            "ready_state": 4,
                            "paused": False,
                            "play_error": "",
                            "frame_metadata": {
                                "presentedFrames": 2,
                                "width": 1280,
                                "height": 720,
                            },
                        },
                        "seek": {
                            "duration": 3900,
                            "currentTime": 2400,
                            "target": 2400,
                            "target_ratio": 0.615,
                            "random_source": "crypto.getRandomValues",
                            "terminal_event": "seeked_and_video_frame",
                            "terminal_latency_ms": 1600,
                            "seeked_observed": True,
                            "frame_observed": True,
                            "frame_observation_method": "requestVideoFrameCallback",
                            "readyState": 4,
                            "paused": False,
                            "play_error": "",
                            "frame_metadata": {
                                "presentedFrames": 3,
                                "width": 1280,
                                "height": 720,
                            },
                        },
                        "layout": {
                            "viewportWidth": 390 if viewport == "mobile" else 1366,
                            "scrollWidth": 390 if viewport == "mobile" else 1366,
                            "playerWidth": 390 if viewport == "mobile" else 960,
                            "playerHeight": 219 if viewport == "mobile" else 540,
                        },
                        "fatal_errors": [],
                        "console_errors": [],
                    }
                    for viewport in ("desktop", "mobile")
                ]
                if index == 0
                else []
            ),
        })
    stress = {
        "fixture_generation": {
            "requested_duration_seconds": 3900,
            "media": {"ok": True, "duration_seconds": 3900},
        },
        "source_media": {
            "ok": True,
            "duration_seconds": 3900,
            "audio_streams": 2,
            "subtitle_streams": 1,
        },
        "phases": [
            {"phase": "upload", "accounts": ["a", "b", "c"], "uploads": uploads},
            {
                "phase": "wait",
                "failed_jobs": [],
                "final_state": {"jobs": [{"status": "succeeded"} for _ in uploads]},
            },
            {"phase": "measure", "measurements": [dict(measurement) for _ in uploads]},
            {"phase": "share", "shares": shares},
        ],
    }
    stream_state = {
        "streaming_ready": True,
        "duration_seconds": 3900,
        "variant_names": ["720p", "480p"],
        "master_extm3u": True,
        "variant_segment_count": 130,
        "sample_segment_bytes": 2048,
    }
    restart = {
        "restart": {
            "stopped": {
                "old_pid": 100,
                "master_process_remaining": False,
                "process_group_remaining": False,
            },
            "started": {"new_pid": 200, "ready": True},
        },
        "before_restart": dict(stream_state),
        "after_restart": dict(stream_state),
        "cleanup": {
            "continuity_share_revoked": True,
            "post_revoke_denied": True,
            "expected_video_count": 3,
            "deleted_video_count": 3,
            "all_videos_absent": True,
        },
    }
    return stress, restart


def test_media_long_selector_requires_real_parallel_hls_restart_and_cleanup() -> None:
    selected = media_long_assertions(*media_long_fixture())

    assert all(selected["scenario_assertions"].values())
    assert all(selected["terminal_assertions"].values())
    assert all(selected["cleanup_assertions"].values())


def test_media_long_selector_rejects_reused_pid_or_missing_video_cleanup() -> None:
    stress, restart = media_long_fixture()
    restart["restart"]["started"]["new_pid"] = 100
    restart["cleanup"]["deleted_video_count"] = 2

    selected = media_long_assertions(stress, restart)

    assert selected["scenario_assertions"]["primary_planned_restart"] is False
    assert selected["cleanup_assertions"]["uploaded_video_fixtures_removed"] is False


def test_media_long_selector_fail_closed_on_frame_seek_layout_or_browser_errors() -> None:
    corruptions = (
        lambda row: row["first_frame"].__setitem__("elapsed_ms", 8000.01),
        lambda row: row["first_frame"].__setitem__("frame_observed", False),
        lambda row: row["seek"].__setitem__("terminal_latency_ms", 5000.01),
        lambda row: row["seek"].__setitem__("seeked_observed", False),
        lambda row: row["layout"].__setitem__("scrollWidth", row["layout"]["viewportWidth"] + 3),
        lambda row: row["console_errors"].append("console.error: media failed"),
    )
    for corrupt in corruptions:
        stress, restart = media_long_fixture()
        desktop = stress["phases"][3]["shares"][0]["browser"][0]
        corrupt(desktop)

        selected = media_long_assertions(stress, restart)

        assert selected["scenario_assertions"]["desktop_mobile_random_seek"] is False
        assert selected["terminal_assertions"]["all_domain_assertions_true"] is False


def test_media_long_selector_allows_the_expected_password_gate_console_message() -> None:
    stress, restart = media_long_fixture()
    expected_message = "console.error:Failed to load resource: the server responded with a status of 401 (UNAUTHORIZED)"
    for row in stress["phases"][3]["shares"][0]["browser"]:
        row["console_errors"] = [expected_message]
        row["expected_console_errors"] = [expected_message]

    selected = media_long_assertions(stress, restart)

    assert selected["scenario_assertions"]["desktop_mobile_random_seek"] is True
    assert set(selected["scenario_assertions"]) == set(
        FORMAL_SCENARIO_BINDINGS["media_long_hls_share"].evidence_adapter_ids
    )


def test_media_long_selector_preserves_3900_parallel_hls_revoke_restart_contracts() -> None:
    stress, restart = media_long_fixture()
    stress["fixture_generation"]["requested_duration_seconds"] = 3899
    assert media_long_assertions(stress, restart)["scenario_assertions"]["long_fixture_minimum_3600_seconds"] is False

    stress, restart = media_long_fixture()
    stress["phases"][0]["uploads"].pop()
    assert media_long_assertions(stress, restart)["scenario_assertions"]["parallel_multi_account_upload"] is False

    stress, restart = media_long_fixture()
    stress["phases"][1]["final_state"]["jobs"][0]["status"] = "failed"
    assert media_long_assertions(stress, restart)["scenario_assertions"]["hls_terminal_ready"] is False

    stress, restart = media_long_fixture()
    stress["phases"][2]["measurements"][0]["variants"][0]["playlist"]["status"] = 500
    assert media_long_assertions(stress, restart)["scenario_assertions"]["master_variant_segment_measurement"] is False

    stress, restart = media_long_fixture()
    stress["phases"][3]["shares"][0]["revoke"]["post_revoke_playback_status"] = 200
    assert media_long_assertions(stress, restart)["scenario_assertions"]["password_wrong_password_and_revoke"] is False

    stress, restart = media_long_fixture()
    restart["restart"]["started"]["new_pid"] = restart["restart"]["stopped"]["old_pid"]
    assert media_long_assertions(stress, restart)["scenario_assertions"]["primary_planned_restart"] is False


def test_final_ui_selector_requires_real_navigation_load_images_and_invariants() -> None:
    selected = final_ui_assertions(*final_ui_fixture())

    assert all(selected["scenario_assertions"].values())
    assert all(selected["terminal_assertions"].values())
    assert all(selected["cleanup_assertions"].values())


def test_final_ui_selector_rejects_small_touch_target_and_missing_load() -> None:
    ui, gate, invariants, load_context, process_cleanup = final_ui_fixture()
    ui["roles"][1]["modules"][0]["observation"]["undersized"] = [
        {"name": "tiny", "width": 43, "height": 44},
    ]
    load_context["latest_target_sample_at_load"] = False

    selected = final_ui_assertions(ui, gate, invariants, load_context, process_cleanup)

    assert selected["scenario_assertions"]["critical_touch_targets_minimum_44px"] is False
    assert selected["scenario_assertions"]["all_feature_navigation_under_load"] is False


def test_final_ui_selector_rejects_chain_failure_and_orphan_process() -> None:
    ui, gate, invariants, load_context, process_cleanup = final_ui_fixture()
    invariants["points_verify_latest"]["body"]["verification"]["errors"] = ["broken"]
    process_cleanup["new_descendant_pids"] = [456]

    selected = final_ui_assertions(ui, gate, invariants, load_context, process_cleanup)

    assert selected["scenario_assertions"]["final_db_log_chain_finance_and_pointschain_invariants"] is False
    assert selected["cleanup_assertions"]["no_new_descendant_processes"] is False


def wallet_incident_fixture() -> tuple[dict, dict, dict, dict, dict]:
    realistic = {
        "users": {
            "victim": {"username": "qa_victim_1", "wallet": "pcw:victim"},
            "attacker": {"username": "qa_attacker_1", "wallet": "pcw:attacker"},
            "merchant": {"username": "qa_merchant_1", "wallet": "pcw:merchant"},
        },
        "incident": {
            "theft_tx_hash": "a" * 64,
            "attacker_spend_tx_hash": "b" * 64,
            "claimed_amount": 60,
        },
        "dispute": {"status": "approved"},
        "blocked_after_freeze": True,
        "blocked_after_freeze_reason": "wallet frozen",
        "attacker_wallet_state": {
            "risk_label": {"status": "active"},
            "governance_freeze": {"freeze_type": "governance"},
        },
        "proposals": {
            key: {
                "proposal_uuid": f"proposal-{key}",
                "vote_status": "passed",
                "execution": {"action": key},
            }
            for key in ("recovery", "address_risk", "address_freeze")
        },
        "balances": {
            "after_recovery": {"victim": 95, "attacker": 0, "merchant": 30},
            "victim_recovered_points": 56,
        },
    }
    replay = {
        "fixture_usernames": ["dispute_victim", "dispute_suspect"],
        "replay_status": 400,
        "wrong_purpose_status": 400,
        "wrong_branch_status": 400,
        "redaction": {"create": False, "review": False, "list": False},
    }
    branch = {
        "proposal_uuid": "branch-proposal",
        "branch_uuid": "pcbranch:new",
        "parent_branch_uuid": "main",
        "recovery_seed": {"tainted_remainder_return": {"return_amount": 30}},
        "execution_action": "canonical_recovery_branch_activated",
        "root_vote_status": "open",
        "manager_vote_status": "passed",
    }
    final_state = {
        "readiness": {"ok": True},
        "points_verify_job": {"terminal_status": "succeeded"},
        "points_verify_latest": {
            "body": {"verification": {"ok": True, "errors": [], "financial_ok": True}},
        },
        "theft_explorer": {"status": 200},
    }
    usernames = [
        "qa_victim_1", "qa_attacker_1", "qa_merchant_1",
        "dispute_victim", "dispute_suspect",
    ]
    cleanup = {
        "login_succeeded": True,
        "records": [
            {"username": username, "deleted": True, "residual_exact_count": 0}
            for username in usernames
        ],
    }
    return realistic, replay, branch, final_state, cleanup


def test_wallet_incident_selector_requires_real_votes_branch_verify_and_cleanup() -> None:
    selected = wallet_incident_assertions(*wallet_incident_fixture())

    assert all(selected["scenario_assertions"].values())
    assert all(selected["terminal_assertions"].values())
    assert all(selected["cleanup_assertions"].values())


def test_wallet_incident_selector_rejects_replay_or_cleanup_shortcut() -> None:
    realistic, replay, branch, final_state, cleanup = wallet_incident_fixture()
    replay["replay_status"] = 200
    cleanup["records"][0]["residual_exact_count"] = 1

    selected = wallet_incident_assertions(
        realistic, replay, branch, final_state, cleanup
    )

    assert selected["scenario_assertions"]["double_spend_and_replay_rejection"] is False
    assert selected["cleanup_assertions"]["exact_fixture_accounts_removed"] is False


def backup_restore_fixture() -> tuple[dict, dict, dict, dict, dict, dict]:
    portable = {
        "archive": {
            "readable": True,
            "size_bytes": 4096,
            "sha256": "a" * 64,
            "manifest_file_count": 12,
            "archive_regular_file_count": 13,
            "database_file_count": 4,
            "storage_file_count": 2,
            "unsafe_members": [],
        },
        "extracted_restore": {
            "hash_mismatches": [],
            "all_manifest_files_present": True,
            "sqlite_quick_checks": {"database.db": {"ok": True}},
        },
        "restore_root_removed": True,
    }
    snapshot = {
        "snapshot_status": 200,
        "snapshot_id_present": True,
        "dirty_marker_created": True,
        "dirty_marker_absent_after_restore": True,
        "transfer_survived_restore": True,
        "storage_restored": True,
        "protected_database_skips": {
            "finance": "append_only_financial_restore_disabled",
        },
    }
    live = {
        "backup_command_returncode": 0,
        "restore_command_returncode": 0,
        "archive_size_bytes": 2048,
        "archive_readable": True,
        "protected_finance_hash_preserved": True,
        "storage_preserved": True,
        "restore_policy": {"policy": "append_only_financial_restore_disabled"},
        "dirty_marker_absent_after_restore": True,
        "transfer_survived_restore": True,
        "sqlite_quick_checks": {"database.db": {"ok": True}},
        "pre_restore_runtime_removed": True,
        "storage_marker_removed": True,
    }
    restart = {
        "stopped": {
            "old_pid": 100,
            "master_process_remaining": False,
            "process_group_remaining": False,
        },
        "started": {"new_pid": 200, "readiness_succeeded": True},
    }
    final_state = {
        "readiness": {"ok": True},
        "points_verify_job": {"terminal_status": "succeeded"},
        "points_verify_latest": {
            "body": {"verification": {"ok": True, "errors": [], "financial_ok": True}},
        },
        "snapshot_transfer_explorer": {"status": 200},
        "cli_transfer_explorer": {"status": 200},
    }
    cleanup = {
        "snapshot_deleted": True,
        "snapshot_absent": True,
        "unexpected_pre_restore_paths": [],
    }
    return portable, snapshot, live, restart, final_state, cleanup


def test_backup_selector_requires_archive_restore_restart_and_chain_proof() -> None:
    selected = backup_restore_assertions(*backup_restore_fixture())

    assert all(selected["scenario_assertions"].values())
    assert all(selected["terminal_assertions"].values())
    assert all(selected["cleanup_assertions"].values())


def test_backup_selector_rejects_unreadable_archive_or_live_finance_change() -> None:
    portable, snapshot, live, restart, final_state, cleanup = backup_restore_fixture()
    portable["archive"]["readable"] = False
    live["protected_finance_hash_preserved"] = False

    selected = backup_restore_assertions(
        portable, snapshot, live, restart, final_state, cleanup
    )

    assert selected["scenario_assertions"]["portable_full_runtime_archive"] is False
    assert selected["scenario_assertions"]["storage_restore_and_live_finance_protection"] is False


def server_emergency_fixture() -> tuple[dict, dict, dict, dict]:
    enter = {
        "mode_before": "dev_ready",
        "enter": {
            "status": 200,
            "body": {
                "incident_id": "incident-1",
                "mode": {"current_mode": "incident_lockdown"},
            },
        },
        "status_during": {
            "body": {
                "incident": {"id": "incident-1", "status": "open"},
                "mode": {"current_mode": "incident_lockdown"},
            },
        },
        "root_restricted_operation": {
            "status": 503,
            "body": {"server_mode": "incident_lockdown"},
        },
        "member_restricted_operation": {"status": 503},
        "root_recovery_operation": {"status": 200},
    }
    diagnostics = {
        "integrity_repair": {
            "status": 200,
            "body": {"audit": {"after": {"ok": True}}},
        },
        "audit_after": {
            "body": {"audit_integrity": {"enabled": True, "ok": True, "broken_at": None}},
        },
        "database_after": {"body": {"database": {"ok": True}}},
        "mode_log_after": {
            "body": {"broken_links": 0, "invalid_signatures": [], "result": "PASS"},
        },
    }
    restore = {
        "resolve": {"status": 200, "body": {"ok": True, "incident_id": "incident-1"}},
        "switch": {"status": 200, "body": {"ok": True}},
        "mode_after": {"body": {"mode": {"current_mode": "dev_ready"}}},
        "incident_after": {"body": {"incident": None}},
    }
    final_state = {
        "readiness": {"ok": True},
        "audit_integrity": {
            "body": {"audit_integrity": {"enabled": True, "ok": True}},
        },
        "database_integrity": {"body": {"database": {"ok": True}}},
        "mode_log_chain": {"body": {"result": "PASS", "broken_links": 0}},
        "points_verify_job": {"terminal_status": "succeeded"},
        "points_verify_latest": {
            "body": {"verification": {"ok": True, "errors": [], "financial_ok": True}},
        },
        "trading_verify_job": {"terminal_status": "succeeded"},
        "trading_verify_latest": {"body": {"verification": {"ok": True, "errors": []}}},
        "site_config": {"maintenance_mode": False},
    }
    return enter, diagnostics, restore, final_state


def test_server_emergency_selector_requires_containment_repair_and_restore() -> None:
    selected = server_emergency_assertions(*server_emergency_fixture())

    assert all(selected["scenario_assertions"].values())
    assert all(selected["terminal_assertions"].values())
    assert all(selected["cleanup_assertions"].values())


def test_server_emergency_selector_rejects_unblocked_write_or_open_incident() -> None:
    enter, diagnostics, restore, final_state = server_emergency_fixture()
    enter["root_restricted_operation"]["status"] = 200
    restore["incident_after"]["body"]["incident"] = {"status": "open"}

    selected = server_emergency_assertions(enter, diagnostics, restore, final_state)

    assert selected["scenario_assertions"]["incident_restrictions_effective"] is False
    assert selected["scenario_assertions"]["incident_resolve"] is False
