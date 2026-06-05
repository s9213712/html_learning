# ComfyUI History, Rerun, GGUF, and HF Token Notes

This note documents the current contract for ComfyUI history restore/rerun,
ComfyUI-GGUF workflow profiles, and the Hugging Face token shortcut on the
HF / Diffusers frontend tab.

## History and Rerun Contract

- `GET /api/comfyui/history` returns only the current actor's regular
  generation history and workflow runs.
- Workflow history rows include the saved `workflow_json` snapshot. The
  frontend uses that snapshot to restore template-specific node inputs, not
  only the generic prompt / seed / size controls.
- `POST /api/comfyui/workflow-runs/{run_id}/rerun` may only rerun a workflow
  run whose `actor_user_id` is the current user. Public or official workflow
  preset visibility does not grant permission to rerun another user's saved
  run snapshot.
- Regular generation history rerun continues to load through the actor-scoped
  history loader.

QA checklist:

1. Log in as the run owner and confirm `/api/comfyui/history` lists the
   workflow run.
2. Log in as a different account and confirm `/api/comfyui/history` does not
   list that workflow run.
3. From the different account, POST
   `/api/comfyui/workflow-runs/{owner_run_id}/rerun` and expect `403`.
4. From the owner account, apply the history item to the form and confirm the
   restored form matches the saved prompt, negative prompt, seed, sampler,
   scheduler, size, batch size, GGUF profile/variant, and any editable direct
   template fields.

## Template Snapshot Restore

Workflow history restore follows this sequence:

1. Select and load the saved workflow preset with `applyDefaults=false`.
2. Apply the generic generation payload to the shared form fields.
3. Rehydrate template field overrides from the saved `workflow_json` node
   snapshot.
4. Render the template panels again so direct fields and special controls show
   the historical values.

This is required because a workflow run can differ from the current preset
defaults even when the preset id is unchanged.

## ComfyUI-GGUF Rerun Safety

ComfyUI-GGUF runs must use GGUF loader nodes such as `UnetLoaderGGUF`.
Historical or legacy snapshots may contain the invalid combination
`CheckpointLoaderSimple.ckpt_name = *.gguf`. The rerun path repairs that
legacy shape before submitting to ComfyUI:

- infer the official GGUF profile/variant from saved params or the GGUF file
  name;
- reapply the official profile to the preset workflow;
- preserve non-model runtime inputs such as prompt, negative prompt, seed,
  sampler, scheduler, cfg, steps, width, height, and batch size;
- keep model inputs mapped through the official profile's UNet / CLIP / VAE
  companion definitions.

If ComfyUI returns `value_not_in_list` for `CheckpointLoaderSimple.ckpt_name`
with a `.gguf` value, treat it as a regression in this repair path.

## HF / Diffusers Token Shortcut

The HF / Diffusers generation tab exposes a root-only Hugging Face API Token
shortcut. It writes to the existing admin setting through
`PUT /api/admin/settings`.

- Leaving the token input blank does not change the saved token.
- Checking "clear" removes the saved token.
- The token is not stored in generation drafts, history, or generation payloads.
- Non-root users use the already configured server token or public Hugging Face
  access; they cannot update the token from the frontend.


## 2026-06-05 Live QA Record

Live validation used the site frontend against remote ComfyUI
`http://192.168.18.18:8188`. The local site runtime was isolated under
`/tmp/hackme_comfy_front_20260605`; QA artifacts were written to
`/tmp/hackme_comfy_front_20260605/reports/qa/`.

Verified results:

- `/system_stats`, `/queue`, `object_info/UnetLoaderGGUF`, and
  `object_info/CheckpointLoaderSimple` responded from `192.168.18.18:8188`.
- `CheckpointLoaderSimple` did not list `illustrious-q4_0.gguf`, confirming
  the original `value_not_in_list` failure would still occur if a GGUF file
  were submitted through `ckpt_name`.
- A real rerun of workflow run `#4` returned HTTP 200, created workflow run
  `#5`, and completed with one output image. The prompt sent to ComfyUI used
  `UnetLoaderGGUF.unet_name = illustrious-q4_0.gguf`, not
  `CheckpointLoaderSimple.ckpt_name`.
- The completed output was `hackme_web_sdxl_gguf_00001_.png`, model
  `illustrious-q4_0.gguf`, seed `2026060506`, size `768x768`.
- Firefox/geckodriver frontend validation confirmed the HF / Diffusers token
  field is visible and the HF GGUF profile controls are visible.
- Applying history item `workflow-4` restored prompt, negative prompt, seed,
  width, height, steps, cfg, sampler, scheduler, template id, GGUF profile
  `calcuis_illustrious_sdxl`, and GGUF variant `q4_0`.
- Cross-account checks confirmed a non-owner account received an empty history
  list and `403` when posting to the owner's workflow rerun endpoint.
- Final remote ComfyUI queue state was empty.

Known limitation from that run:

- The generated PNG was downloaded and verified as a nonblank 768x768 PNG, but
  the local `view_image` helper failed because the filesystem sandbox helper
  hit `bwrap: loopback: Failed RTM_NEWADDR`. No local vision model was
  installed, so semantic image inspection for fine details such as cat ears,
  bed posture, and clothing had to be recorded as not reliably inspectable in
  that environment.

## Live QA Notes

When running against a remote ComfyUI backend:

- verify `/system_stats`, `/object_info`, `/object_info/UnetLoaderGGUF`, and
  `/queue` before submitting a GGUF run;
- confirm the queued GGUF prompt contains `UnetLoaderGGUF` and not
  `CheckpointLoaderSimple` for the GGUF file;
- if the remote backend remains stuck after `/interrupt`, do not enqueue more
  live generation tests until the operator clears that ComfyUI process.
