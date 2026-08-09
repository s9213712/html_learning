# I2I, Outpaint and HF/Diffusers Validation

This runbook validates a delivered image rather than merely a ComfyUI queue
state. It applies to product i2i, inpaint, outpaint, upscale and blend, and to
the separate Hugging Face/Diffusers backend family.

## Delivery contract

1. A job must reach its terminal success state and return one accessible final
   image reference.
2. The output must decode, have the requested/aligned canvas dimensions, and
   not be blank. These are mechanical gates, not the final acceptance test.
3. A reviewer must inspect the saved PNG at 100% and, where relevant, compare
   it with the source: subject identity/pose, edit locality, text integrity,
   edge halos, outpaint seam continuity, upscale artifacts and blend joins.
4. Record the prompt, selected backend/model/workflow, source hash, delivered
   file, review decision and any defect. Do not approve an output only because
   a report says `ok`.

Keep all disposable reports and images outside the checkout, for example under
`/tmp/hackme_i2i_review/`. Do not put source images, generated outputs or
runtime databases into Git.

## Semantic outpaint behavior

The `flux_fill_sam3_subject_gguf` family generates a clean expanded background
and a SAM3 RGBA foreground. The application verifies the configured expansion,
canvas alignment, usable foreground alpha and final upload before composing the
foreground onto the background server-side. The API returns only that final
`semantic_composite` image. Missing nodes, empty/invalid alpha, inconsistent
sizes or failed upload are errors, not a fallback to one of the intermediate
images.

The legacy source-rectangle paste-back option is unsuitable as a seam repair:
an opaque original rectangle can visibly frame a flat or studio background.
Use the fully blended or semantic workflow and review the whole boundary.

## Product HF/Diffusers i2i soak

Use an authorized test account and an image that may be retained as QA
evidence. The script logs in to the product, submits real HF/Diffusers i2i
jobs, downloads every delivered preview, and writes a JSON result plus PNGs.

```bash
python3 scripts/comfyui/hf_diffusers_i2i_soak.py \
  --base-url https://127.0.0.1:5000 \
  --username <qa-user> \
  --source-image /absolute/path/to/source.png \
  --runs 3 \
  --out /tmp/hackme_i2i_review/hf_i2i_soak.json \
  --artifacts-dir /tmp/hackme_i2i_review/hf_i2i_images
```

Pass the password through the permitted QA environment rather than storing it
in a shell history, report or repository file. Choose a model/variant already
authorized and available to the deployment. A successful run still requires
opening every saved image and recording a visual judgement.

## Offline cached Diffusers i2i check

This direct probe does not call the product API and does not download a model.
It is useful for separating a Diffusers/model/runtime fault from a product
integration fault.

```bash
python3 scripts/comfyui/standalone_hf_diffusers_i2i.py \
  --source-image-path /absolute/path/to/source.png \
  --model <cached-hf-repo> \
  --huggingface-cache-root /absolute/path/to/hf-cache \
  --output /tmp/hackme_i2i_review/direct_i2i.png \
  --report /tmp/hackme_i2i_review/direct_i2i.json
```

The default is offline. A missing cache/model is an inconclusive environment
failure, not permission to silently download production dependencies or to
declare the product image path healthy.

## Before approving an outpaint fix

- Test at least one simple/flat backdrop and one detailed background.
- Expand on more than one side and inspect all four source-to-new-canvas joins.
- Verify that the final returned image is `semantic_composite` when using the
  SAM3 family, not an intermediate background or foreground artifact.
- Check at 100% for rectangular seams, white/black matte fringes, alpha holes,
  subject duplication and accidental source cropping.
- Repeat after restart or backend reconnect if the reported issue was
  intermittent. Retain the failed and corrected artifacts outside the repo.

Related references: [COMFYUI_ADMIN.md](COMFYUI_ADMIN.md),
[README.md](README.md), and [../11_QA_TESTING.md](../11_QA_TESTING.md).
