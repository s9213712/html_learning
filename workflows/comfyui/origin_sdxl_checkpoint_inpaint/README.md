# SDXL Checkpoint Inpaint (Local)

Local diagnostic SDXL checkpoint inpaint workflow.

- Source: `workflows/comfyui/origin/image/edit/sdxl_checkpoint_inpaint_local.json`
- Source Format: `api_prompt`
- Structural Test: `pass` (9 nodes)
- Allowlist Status: `allowlisted`
- Static Unknown Nodes: None
- Live Runtime Check: run `python3 scripts/comfyui/official_workflow_probe.py --preflight-only --only origin_sdxl_checkpoint_inpaint` against a running ComfyUI.
- Note: this workflow is runnable with the local JANKU checkpoint but failed the 2026-06-24 apple-to-plant visual audit; keep it as a diagnostic/manual fallback, not as the default AI Agent inpaint route.
