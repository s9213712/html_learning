# Flux Fill Inpaint GGUF Q3

Low-VRAM mask inpaint route derived from the official Flux Fill inpaint workflow.

This bundle keeps the official Flux Fill inpaint graph but replaces the stock
`UNETLoader` with ComfyUI-GGUF `UnetLoaderGGUF`. Unlike the upstream example,
source image and mask are separate protected inputs so AI Agent
`source_image_ref` and `mask_image_ref` map to the correct nodes.

Required files:

- `models/unet/flux1-fill-dev-Q3_K_S.gguf` or extra-path equivalent
- `models/text_encoders/clip_l.safetensors` or extra-path equivalent
- `models/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors` or extra-path equivalent
- `models/vae/ae.safetensors` or extra-path equivalent

Status: created for local validation after Qwen semantic img2img repeatedly left
object-removal artifacts. Do not treat as visually verified until a live
inpaint probe passes.
