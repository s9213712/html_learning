# 2026-06-05 Backpressure and ComfyUI Resume QA

## Findings

- **Medium - User-specified ComfyUI IP path is still unreachable from WSL.**
  The resumed QA run generated a valid `1024x1024` image through `origin_sdxl_gguf_txt2img`. After the user confirmed ComfyUI was still running, a re-check at `2026-06-05 20:14 +0800` showed `http://127.0.0.1:8188/system_stats` and `http://localhost:8188/system_stats` both returned Windows ComfyUI `0.22.0`, while `http://192.168.18.18:8188/system_stats` still returned curl 7 from WSL. ComfyUI itself is healthy; the remaining issue is the requested LAN/IP route.
  Evidence: `/tmp/hackme_web_gguf_template_frontend_qa_resume2_20260605/results.json`; generated image `/tmp/hackme_web_gguf_template_frontend_qa_resume2_20260605/images/01_origin_sdxl_gguf_txt2img_01.png`.

- **Low - GGUF template dependency badge still reports raw-template missing models before runtime profile overlay.**
  The preset detail still reports missing `WAI-NSFW-Illustrious-v140-Q8_0.gguf` and `sdxl_vae.safetensors` from the raw bundled workflow. The actual run succeeds because `gguf_workflow` applies the selected official GGUF profile at execution time and then reports `dependency_status.available=true`. This can confuse operators even though generation works.
  Evidence: `/tmp/hackme_web_gguf_template_frontend_qa_resume2_20260605/results.json`.

## Verified Passes

- Confirmed online branch status before continuing: `origin/04.BLOCKCHAIN_RC1` was fetched successfully, local branch and origin differed by `0 0`, latest remote commit `ad53bbe Keep login bootstrap on backpressure fast lane`.
- Confirmed the latest live GGUF workflow records preserve the prompt verbatim: `prompt`, `params_json.prompt`, and workflow node `6.inputs.text` matched for recent runs containing quantity prompts such as `2girls` / `2 girls`. This points the observed quantity-adherence failure at model/prompt behavior rather than prompt loss in the app layer.
- Added owner-scoped ComfyUI history deletion:
  - `DELETE /api/comfyui/history/<id>` deletes only the caller's legacy generation history.
  - `DELETE /api/comfyui/workflow-runs/<id>` deletes only the caller's workflow run history.
  - The frontend history list now shows a `刪除` button next to `套回表單` and `一鍵重跑`.
- Verified account isolation for ComfyUI history with targeted tests: another account cannot list, rerun, or delete a user's legacy generation history or workflow run history.
- Verified frontend history deletion with Playwright against the live `0.0.0.0:5000` runtime. A test history row was visible in the history tab, the `刪除` button removed it through the UI, the list updated to empty, and the DB row was gone. Screenshot: `/tmp/hackme_comfyui_history_delete_after.png`.
- Fixed `scripts/testing/playwright_comfyui_template_default_qa.py` so the browser QA path sends the same `sdxl_refiner` and `gguf_workflow` run specs as the real frontend button. Before this fix, the QA script falsely rejected the GGUF template against raw model filenames.
- Verified GGUF template generation after the QA fix:
  `passed=1`, `failed=0`, `completed_with_issues=0`, `console_event_count=0`, `page_error_count=0`, `network_error_count=0`.
  Output image analysis: `1024x1024`, mean `69.58`, stddev `47.29`, no blank/black/white flags; manual visual inspection passed.
- Verified the backpressure fix with targeted unit coverage:
  `/home/s92137/hackme_web/tests/security/gates/test_flask_hardening.py` passed `13 passed`.
- Earlier live backpressure stress in the same resumed session reproduced the original fast-lane problem and verified the fix:
  stale configured capacity `6` on actual gunicorn `--threads 4` left root login on `bp:fast_lane` but p95 was about `12.2s`; after capping to live thread capacity and reserving two fast-lane slots, root login under load completed `12/12` with p95 about `1.864s`, no network error/5xx. Lightweight fast-lane probe completed `36/36` with p95 about `847ms`.
- System stress probe on the fixed backpressure code passed with `ok=true`, `degraded=false`, `qos_version` p95 `815ms`, `version` p95 `981ms`; feature-gate `503 server_busy` responses were expected load shedding, not hard 5xx.

## Pending List

- Re-check the user-specified ComfyUI address `192.168.18.18:8188`; it is unreachable from WSL even though `127.0.0.1:8188` and `localhost:8188` are healthy.
- Decide whether the preset list/detail dependency badge should evaluate GGUF profile-overlay dependencies instead of raw bundled workflow defaults.
- Improve prompt UX guidance for anime/Illustrious GGUF models when users need strict cardinality. The app now proves prompt text is delivered; model-side noncompliance still needs prompt/negative-prompt presets or model guidance.

## Commands

- `git -C /home/s92137/hackme_web fetch --prune` - passed.
- `git -C /home/s92137/hackme_web rev-list --left-right --count HEAD...origin/04.BLOCKCHAIN_RC1` - `0 0`.
- `pytest -q /home/s92137/hackme_web/tests/comfyui/generation/test_comfyui_history_delete_and_prompt.py` - 3 passed.
- `pytest -q /home/s92137/hackme_web/tests/frontend/comfyui/test_comfyui_history_ui.py` - 3 passed.
- `pytest -q /home/s92137/hackme_web/tests/comfyui/generation/test_comfyui_generation.py::test_comfyui_history_rerun_reuses_saved_assets` - passed.
- `pytest -q /home/s92137/hackme_web/tests/frontend/comfyui/test_comfyui_diffusers_repo_ui.py` - 14 passed.
- `pytest -q /home/s92137/hackme_web/tests/comfyui/test_sdxl_and_gguf_workflow_options.py` - 2 passed.
- `python3 /tmp/hackme_comfyui_history_delete_playwright.py` - passed.
- `python3 /home/s92137/hackme_web/scripts/testing/playwright_comfyui_template_default_qa.py --base-url https://127.0.0.1:54833 --root-password root --comfyui-api-url http://127.0.0.1:8188 --only origin_sdxl_gguf_txt2img --per-template-timeout 300 --out-dir /tmp/hackme_web_gguf_template_frontend_qa_resume2_20260605` - passed.
- `python3 -m py_compile /home/s92137/hackme_web/scripts/testing/playwright_comfyui_template_default_qa.py` - passed.
- `pytest -q /home/s92137/hackme_web/tests/security/gates/test_flask_hardening.py` - 13 passed.
- `curl http://192.168.18.18:8188/system_stats` - failed with curl 7.
- `curl http://127.0.0.1:8188/system_stats` - passed on re-check after the user confirmed ComfyUI was healthy.
