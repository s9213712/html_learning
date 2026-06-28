# Qwen Image Edit 2509 Anything2Real

Official Qwen Image Edit 2509 workflow variant for anime or illustration to
realistic photograph conversion.

This bundle keeps the base Qwen Image Edit 2509 graph and applies the
`QWEN\Anything2RealAlpha.safetensors` LoRA after the standard Lightning LoRA.
It is intended for semantic img2img edits where the source image should remain
recognizable while the rendering style shifts toward photorealism.

Recommended prompt shape:

```text
transform the image to realistic photograph. Preserve the same subject, pose,
composition, outfit, and background.
```

Default fast profile:

- Resolution: 1024x1024
- Steps: 4
- CFG: 1
- Anything2Real LoRA strength: 0.85
- Source image node: 78
- Optional reference image node: 79
