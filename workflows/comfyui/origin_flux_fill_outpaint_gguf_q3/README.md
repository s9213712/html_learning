# Flux Fill Outpaint GGUF Q3

Low-VRAM outpaint route derived from the official Flux Fill outpaint workflow.

This bundle keeps the official outpaint graph but replaces the stock
`UNETLoader` with ComfyUI-GGUF `UnetLoaderGGUF`.

Required files:

- `models/unet/flux1-fill-dev-Q3_K_S.gguf` or extra-path equivalent
- `models/text_encoders/clip_l.safetensors` or extra-path equivalent
- `models/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors` or extra-path equivalent
- `models/vae/ae.safetensors` or extra-path equivalent

Status: created for local validation after the official gated safetensors Flux
Fill model was unavailable in this runtime.

- Source: `workflows/comfyui/origin/image/outpaint/flux_fill_outpaint_gguf_q3.json`
- Source Format: `ui_graph`
- Structural Test: `pass` (13 nodes)
- Allowlist Status: `allowlisted`
- Static Unknown Nodes: None
- Live Runtime Check: run `python3 scripts/comfyui/official_workflow_probe.py --preflight-only --only origin_flux_fill_outpaint_gguf_q3` against a running ComfyUI.
- Regenerate: `python3 scripts/comfyui/materialize_system_workflows.py`
