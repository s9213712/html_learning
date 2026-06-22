# AI Agent Function Audit: Server Ops

Date: 2026-06-22
Target: `https://127.0.0.1:54384`
Artifact: `/tmp/hackme_ai_agent_frontend_audit_20260622_54384/reports/ai_agent_server_ops_probe_v2.json`

## Scope

This pass audited server status, logs, launch checks, audit scan, server issue handling, and emergency response boundaries.

## Result Table

| ID | Item | Result | Evidence |
| --- | --- | --- | --- |
| SRV-01 | Read-only resource/security status | PASS | AI Agent readonly resources and root `attack_diag` returned 200. |
| SRV-02 | Launch checks and log verify | PASS | `write_launch_requirements_check`, `write_launch_logs_verify`, `write_launch_doc_read`, and `audit_scan` all dispatched and returned result payloads. Requirements may report `ok:false` when gates are not all green; that is a gate result, not dispatch failure. |
| SRV-03 | Document/path boundary | PASS | `write_launch_doc_read` rejected `../server.py` with 400. |
| SRV-04 | Dialogue asks for logs/status | PASS | Natural-language status/log/security request routed to readonly output in 0.89s. |
| SRV-05 | Repair/restart/kill handling | GAP | AI Agent has no arbitrary repair, kill, or restart tool. Dialogue did not execute a write request; unsupported `write_server_repair` returned 400. |
| SRV-06 | Emergency incident handling | GAP | `audit_scan` works, but no `write_emergency_incident_handle` or equivalent incident action tool exists. Unsupported tool returned 400. |
| SRV-07 | Permission and overreach boundary | PASS | normal member received scoped readonly response, 403 for write-tool list, and 403 for `audit_scan`. |
| SRV-08 | Response time / repetition | PASS | log/status response 0.89s; repair-boundary response 0.116s; no empty message or adjacent repeat. |

## Capability Boundary

- AI Agent can inspect server/resource/security state and can run launch-check/log-verify/doc-read/audit-scan tools.
- AI Agent cannot resolve server problems beyond reporting and audit scanning. It cannot kill processes, restart services, clear queues, change runtime settings, or declare/resolve incidents.
- This is a deliberate safety boundary today, but it is a product gap if the intended Agent is expected to handle server incidents inside the site.

## Fix Direction

Do not add arbitrary shell or filesystem access. Add a bounded site-internal runbook registry instead:

1. `read_server_status`: aggregate health, queues, logs, and degraded reasons.
2. `audit_scan`: already exists; keep root-only.
3. `draft_incident_response`: explain likely issue and proposed site-local action.
4. `write_server_runbook_action`: execute only pre-registered actions such as `clear_stale_comfyui_jobs`, `restart_site_worker_queue`, or `resume_trading_background_job`, with strict role checks.
5. `write_incident_declare` / `write_incident_resolve`: create auditable incident records without shell access.

Every runbook action should have a dry-run preview, confirmation token, audit row, timeout, idempotency key, and explicit rollback/undo notes.

## Next Function

Recommended next scoped audit: forum/community posting plus governance/member reward and penalty.
