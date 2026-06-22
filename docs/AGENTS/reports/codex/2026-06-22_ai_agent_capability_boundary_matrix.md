# AI Agent Capability Boundary Matrix

Date: 2026-06-22
Target: `https://127.0.0.1:54384`

## Summary

The tested site backends are mostly functional, but AI Agent write capability is intentionally narrow today. It can use ComfyUI generation and a small set of root write tools, but it cannot yet operate trading, downloader, cloud drive/share/tasks, HLS/media management, rewards/penalties, governance proposals, emergency actions, or broad server repair workflows. In all tested unsupported areas, the Agent rejected unsupported write tools or responded without pretending the action had succeeded.

## Matrix

| Domain | Backend Status | AI Agent Status | Response Health | Fix Direction |
| --- | --- | --- | --- | --- |
| Image generation / ComfyUI | Pass: remote ComfyUI on `8189`, generation, progress tracking, failure restore all verified | Pass: `write_comfyui_generate` works for root; member blocked | 5.466s generation dispatch; 1.037s concurrent follow-up; no repeats | Keep; add longer-running queue stress later. |
| Trading / exchange / bots / liquidation | Pass: direct APIs for markets, orders, bot audit, liquidation scan, matching, synthetic DCA backtest | Gap: no trading write tools; 9/9 candidates rejected | 0.551s NL response; no silent trade | Add scoped trading tools with dry-run, preview, confirmation, idempotency key, ledger reconciliation. |
| Server ops / logs / docs / audit | Pass: readonly resources, attack diag, launch docs/log checks, audit scan | Partial: readonly/check tools exist; no kill/restart/repair/emergency tool | 0.89s NL response; no repeats | Add explicit incident workflow tools with root-only guard, status-before-action, rollback notes, and audit log. |
| Governance / community / member admin | Pass: create thread/reply, member create/update, bug review reward, direct post reward/penalty APIs | Partial: Agent can post/update members/bug review; no reward/penalty, proposal/vote/execute, emergency governance tools | 0.528s NL response; no repeats | Add governance tools around existing moderation/reward APIs with reason, target, and confirmation. |
| Cloud drive / share / task automation | Pass: file list, remote capabilities, shares, jobs, small text file create | Gap: no drive/share/task tools; 10/10 candidates rejected | 0.561s NL response; no silent file/share job | Add tools for create/upload/list/share/revoke/job retry/cancel; define upload file-handle boundary. |
| BT / Direct download | Pass: capabilities expose aria2; SSRF and mode guards verified | Gap: no downloader tools; unsupported tools rejected | 0.567s combined media request; no repeats | Add create/list/control/recover task tools; expose backend status and validation messages to Agent. |
| Albums | Pass: create/detail/update/smart-organize | Gap: no album tools | 0.567s combined media request; no fake success | Add create/update/add/remove/list album tools with owner/visibility checks. |
| Video / HLS / transcoding | Pass: real 4.8 KB MP4 uploaded; HLS background job succeeded; playback `mode=hls` and master manifest available | Gap: no video upload/HLS rebuild/subtitle tools | 0.567s combined media request; no fake success | Add tools for status/retry/rebuild around existing Job Center; uploads should use existing cloud files or user-selected attachments. |

## Confirmed Non-Bugs

- Unsupported Agent domains are currently design gaps, not silent execution bugs.
- ComfyUI progress tracking does not block other Agent input in the tested flow.
- HLS background processing completed successfully for a real generated MP4 fixture.
- Member access to root Agent write tools is blocked with `403`.

## Artifacts

- `/tmp/hackme_ai_agent_frontend_audit_20260622_54384/reports/ai_agent_image_generation_probe_v2.json`
- `/tmp/hackme_ai_agent_frontend_audit_20260622_54384/reports/ai_agent_trading_capability_probe_v1.json`
- `/tmp/hackme_ai_agent_frontend_audit_20260622_54384/reports/ai_agent_server_ops_probe_v2.json`
- `/tmp/hackme_ai_agent_frontend_audit_20260622_54384/reports/ai_agent_governance_capability_probe_v2.json`
- `/tmp/hackme_ai_agent_frontend_audit_20260622_54384/reports/ai_agent_drive_share_task_probe_v2.json`
- `/tmp/hackme_ai_agent_frontend_audit_20260622_54384/reports/ai_agent_media_downloader_probe_v2.json`

## Verification

- `python3 -m py_compile scripts/testing/ai_agent_frontend_full_probe.py scripts/testing/ai_agent_capability_boundary_probe.py scripts/testing/ai_agent_image_generation_probe.py scripts/testing/ai_agent_trading_capability_probe.py scripts/testing/ai_agent_server_ops_probe.py scripts/testing/ai_agent_governance_capability_probe.py scripts/testing/ai_agent_drive_share_task_probe.py scripts/testing/ai_agent_media_downloader_probe.py`
- `node --check public/js/37-ai-agent.js`
- `python3 -m pytest tests/frontend/admin/test_frontend_ai_agent.py tests/ai_agent/test_ai_agent_routes.py -q` (`36` passed)
