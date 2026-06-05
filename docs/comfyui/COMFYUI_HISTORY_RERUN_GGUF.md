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

## Live QA Notes

When running against a remote ComfyUI backend:

- verify `/system_stats`, `/object_info`, `/object_info/UnetLoaderGGUF`, and
  `/queue` before submitting a GGUF run;
- confirm the queued GGUF prompt contains `UnetLoaderGGUF` and not
  `CheckpointLoaderSimple` for the GGUF file;
- if the remote backend remains stuck after `/interrupt`, do not enqueue more
  live generation tests until the operator clears that ComfyUI process.
