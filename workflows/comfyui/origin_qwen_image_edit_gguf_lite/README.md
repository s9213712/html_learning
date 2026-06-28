# Qwen Image Edit GGUF Lite

Qwen Image Edit 2509 workflow adapted for the installed ComfyUI-GGUF runtime.

- Source: `calcuis/qwen-image-edit-gguf` mapping, adapted to the local node names exposed by ComfyUI 0.25.0.
- Source Format: `api_prompt`
- UNet: `qwen-image-edit-q2_k_s.gguf` through `UnetLoaderGGUF`
- Text encoder: `qwen2.5-vl-7b-test-q4_0.gguf` through `CLIPLoaderGGUF`
- VAE: `qwen_image_vae.safetensors` through `VAELoader`
- VaeGGUF: not required for this runtime; `/object_info` does not expose `VaeGGUF`.
- Live Runtime Check: run `python3 scripts/comfyui/official_workflow_probe.py --preflight-only --only origin_qwen_image_edit_gguf_lite` against a running ComfyUI.
