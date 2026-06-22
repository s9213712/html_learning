# AI Agent Image Context Audit - 2026-06-23

## Scope

This audit covers the AI Agent frontend image-context flow on the live `:5000` runtime:

- Attach an image and ask an ambiguous natural-language question.
- Attach an image and request prompt generation without image generation.
- Attach an image and explicitly request reverse prompt plus ComfyUI generation.
- Verify model selection is not hardcoded to retired model names.
- Verify settings changed during a live test are restored.

All timestamps below use Asia/Taipei date context unless a provider error explicitly includes another timezone.
For reference, "yesterday" relative to this audit date is 2026-06-22.

## Live Artifacts

- `/tmp/hackme_ai_agent_image_context_20260623_after_fix/report.json`
- `/tmp/hackme_ai_agent_image_context_20260623_dynamic_models/report.json`
- `/tmp/hackme_ai_agent_image_context_20260623_model_refresh/report.json`
- `/tmp/hackme_ai_agent_image_context_20260623_model_refresh/ai_agent_image_context.png`

## Findings

- PASS: attaching an image now switches the effective AI Agent mode to `image`, even if the user did not manually change the mode selector.
- PASS: ambiguous image input such as "這張圖片幫我看一下" is routed as chat and does not execute a write tool.
- PASS: prompt-only image input such as "產生 ComfyUI 提示詞，但不要生圖" is routed as chat and does not execute a write tool.
- PASS: explicit image generation intent is recognized by the planner as `comfyui_generate` with `execute_write=true`.
- PASS: the live probe snapshots `ai_agent_model` and `ai_agent_allowed_models`, temporarily allows live `/models` results for testing, and restores the original settings afterward.
- PASS: browser-side transient JS errors were removed in the dynamic-model run.
- BLOCKED: the only live Ollama cloud vision model available through `/models`, `qwen3-vl:235b-instruct-cloud`, is no longer callable. Direct provider response: `qwen3-vl:235b-instruct was retired at 2026-06-16 00:00:00 -0700 PDT`.

## Repairs Applied

- Added natural-language creative skills to the AI Agent system prompt for visual reference reconstruction and iterative creative comparison without creating a fixed workflow/tool.
- Added prompt boundaries so uploaded images are interpreted by context rather than always treated as prompt-generation or generation requests.
- Updated frontend image handling so attached files force image mode and planner context receives `input_mode=image`.
- Added lightweight status/model refresh before image analysis, avoiding stale allowed-model state after root settings changes.
- Added unavailable-model tracking in the frontend. Provider 410/retired/not-found/unavailable responses now mark the selected model unavailable and remove it from future selectable candidates in the current session.
- Fixed backend provider error parsing so string-form errors such as `{"error":"...retired..."}` are preserved instead of being collapsed to `AI Agent backend HTTP 410`.
- Removed visible legacy "Hermes Agent" naming from frontend settings/status and replaced it with generic local/backend wording while keeping legacy provider values for compatibility.
- Removed hardcoded frontend quick-preset model names; model inputs now depend on `/models` or explicit root settings.

## Verification

Passed focused suite:

```text
python3 -m py_compile scripts/testing/ai_agent_image_context_probe.py services/ai_agent/hermes.py routes/ai_agent.py routes/system_admin_sections/settings_routes.py tests/ai_agent/test_hermes_client.py tests/frontend/admin/test_frontend_ai_agent.py tests/frontend/admin/test_root_quick_settings.py
node --check public/js/37-ai-agent.js
node --check public/js/01-root-quick-settings.js
node --check public/js/50-admin.js
pytest tests/ai_agent/test_hermes_client.py tests/frontend/admin/test_frontend_ai_agent.py tests/frontend/admin/test_root_quick_settings.py tests/security/auth/test_access_controls.py::test_ai_agent_api_key_is_write_only_and_clearable tests/security/auth/test_access_controls.py::test_ai_agent_settings_validate_url_and_key_shape tests/security/auth/test_access_controls.py::test_ai_agent_audit_settings_are_configurable_and_validated -q
```

Result: 48 passed.

Live `:5000` also returned OK from `/api/csrf-token` after reload.

## External Model Note

The current Ollama cloud vision tag is retired, so the image-to-ComfyUI write path cannot complete on the current live provider. Official OpenAI API docs state that current OpenAI models support text and image input and vision. Candidate cloud replacement models should be configured through root settings and validated through `/models`, not hardcoded into frontend code.

Relevant official docs:

- https://developers.openai.com/api/docs/models
- https://developers.openai.com/api/docs/guides/images-vision

