# AI Agent Natural Language Audit - 2026-06-12 10:47

## Summary

Scope: live `:5000` AI Agent natural-language behavior, write-tool execution, role boundaries, memory isolation, ComfyUI generation handling, image analysis path, launch checks, audit scan, community, chess, member management, and bug report workflows.

Artifact: `/tmp/hackme_ai_agent_nl_qa_20260612_104751.json`

Result: 36 checks, 35 passed, 1 expected operational gap.

## Findings

1. `write_launch_requirements_check` executed successfully but returned `ok=false` because required pre-release reports are missing:
   `clean_smoke`, `adversarial`, `redteam_l2`, `pytest`, `log_chain_verify`, `integrity_guard`, `stress`, `permission`, `functional`, `pentest`, `snapshot_restore`, `points_chain_consistency`, `cloud_drive_quota_permission`.
   This is a launch-gate data gap, not an AI Agent dispatch failure.

2. ComfyUI is configured as `remote` at `http://127.0.0.1:8188`, but no backend is listening during this audit. The agent now correctly rejects natural-language generation before queueing with: `目前無法讀取 ComfyUI checkpoint 清單，已取消送出產圖：ComfyUI 連線逾時`.

3. Vision/image analysis path reached the AI Agent chat endpoint and returned a controlled 502 for the unavailable vision backend. It did not silently proceed into generation.

## Fixes Verified

- Natural checkpoint shorthand such as `JANKU…..V777` is resolved against `/api/comfyui/models` before submission; unresolved or unavailable model lists cancel generation before creating a fake queued job.
- AI Agent no longer reports generation as done at local queue creation only. The frontend now watches the ComfyUI job until `running`, `completed`, or `error`; completion asks whether to modify parameters, save/favorite, or share as a post.
- Explicit negative image intent such as `請描述這張圖片，不要生圖` is treated as image analysis only.
- Community write-tool maps natural `discussion` post type to the API's `normal` enum.
- AI Agent internal write-tool dispatch no longer leaks backpressure leases; post-fix live verification showed `feature.active=0`, `normal.active=0` after execution.

## Passing Coverage

- Login roles: root/admin/test.
- Frontend natural-language parser and account-scoped conversation isolation.
- Root status, write-tool listing, readonly scopes: resources, ComfyUI, remote download, member management, attack diagnostics.
- Audit status and forced audit scan.
- Natural-language ComfyUI generation refusal when backend unavailable.
- Text chat response path.
- Confirm-required and role-denied write-tool boundaries.
- Community thread creation and reply.
- Member create/update.
- Chess practice creation and move.
- Bug report creation and root review.
- Launch log verification and launch doc read.
- Backpressure leak regression check.

## Verification

- `python3 -m pytest tests/ai_agent/test_ai_agent_routes.py tests/frontend/admin/test_frontend_ai_agent.py` -> 31 passed.
- `python3 -m py_compile routes/ai_agent.py services/server/backpressure.py`
- `node --check public/js/37-ai-agent.js`
- Live low-frequency write-tool retry confirmed chess, bug report, launch log/doc, audit scan, and backpressure release.
