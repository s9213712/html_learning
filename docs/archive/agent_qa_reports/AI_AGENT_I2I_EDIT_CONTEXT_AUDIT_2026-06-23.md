# AI Agent Image-to-Image Edit Context Audit - 2026-06-23

Target: live `https://127.0.0.1:5000`

Scope:
- Natural-language AI Agent routing for ComfyUI image-to-image edits.
- Recent generated image reuse through message `images[].image_ref`.
- `img2img` style change, `outpaint`, and `inpaint` missing-mask boundary.
- Intercepted planner/UI probe plus real ComfyUI execution through live `:5000` and remote ComfyUI `http://127.0.0.1:8189`.

Final evidence:
- JSON: `/tmp/hackme_ai_agent_i2i_edit_context_20260623_live_5/report.json`
- Screenshot: `/tmp/hackme_ai_agent_i2i_edit_context_20260623_live_5/ai_agent_i2i_edit_context.png`
- Direct API real img2img job: `78b8cf8c5892589057a2c08e`, output `hackme_web_00066_.png`, history id `32`.
- Frontend AI Agent real img2img report: `/tmp/hackme_ai_agent_real_i2i_edit_20260623_clean/report.json`
- Frontend AI Agent real img2img screenshot: `/tmp/hackme_ai_agent_real_i2i_edit_20260623_clean/ai_agent_real_i2i_edit.png`
- Probe: `scripts/testing/ai_agent_i2i_edit_context_probe.py`
- Real execution probe: `scripts/testing/ai_agent_real_i2i_edit_probe.py`

Final result: PASS

Checks:
- `style_uses_comfyui_generate`: PASS
- `style_uses_img2img`: PASS
- `style_uses_recent_source_ref`: PASS
- `style_keeps_denoise`: PASS
- `outpaint_uses_comfyui_generate`: PASS
- `outpaint_mode_and_source`: PASS
- `outpaint_edges`: PASS
- `inpaint_missing_mask_does_not_write`: PASS
- `inpaint_missing_mask_clarifies`: PASS
- `no_browser_errors`: PASS
- Real output-ref materialization: PASS. Output ref `hackme_web_00065_.png` was converted to input ref before ComfyUI `LoadImage`, then img2img completed.
- Natural-language frontend real execution: PASS. AI Agent planned `img2img`, sent `write_comfyui_generate`, tracked progress to completion, and produced `hackme_web_00071_.png`.
- Prompt rewrite quality: PASS after repair. The write payload used `prompt: "淡透明水彩風格，保留構圖"` instead of blindly reusing the previous image prompt.

Issues found and fixed:
- AI Agent write-tool schema did not expose ComfyUI edit fields such as `generation_mode`, `source_image_ref`, `mask_image_ref`, `denoise_strength`, and `outpaint_*`.
- Frontend planner context did not expose recent generated image refs, so "edit the previous image" could silently lose the source image.
- Frontend normalization could drop valid image-edit arguments.
- LLM sometimes returned a correct `img2img` plan with an empty `prompt`; frontend then stopped with a generic "need to know what to draw" message. The frontend now falls back to the user's natural-language edit request as prompt when an image-edit mode and source image are present.
- Real ComfyUI execution initially failed with `LoadImage image - Invalid image file: hackme_web_00065_.png` because generated output refs were passed directly to `LoadImage`. The generate route now verifies ownership and materializes non-input image refs through ComfyUI `/view` + `/upload/image` before workflow submission.
- The LLM could reuse `last_comfyui_args.prompt` for style edits, losing the requested style. Frontend normalization now replaces stale image-edit prompts with the current user request, and the planner prompt explicitly forbids copying the old prompt unless requested.
- When the LLM chose `action=write_tool` with `tool=write_comfyui_generate`, the generic write path submitted the job but did not attach the ComfyUI progress watcher. Generic write execution now detects that tool and calls the same job watcher used by `action=comfyui_generate`.
- Test fake preview/job routes originally produced expected 404 noise; the probe now stubs those endpoints so browser errors represent real frontend issues.

Notes:
- The live AI Agent backend was `openai_compatible` via `http://127.0.0.1:11434/v1`.
- Final observed planner latencies were roughly 6-10 seconds per case in these runs.
- The unavailable retired vision model still appears in `/models`, but current allowed models exclude it from frontend selection.
