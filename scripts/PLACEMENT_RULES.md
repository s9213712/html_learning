# Script Placement Rules

## Purpose

`scripts/` exists for human-operated tooling and repository validation.

The directory is for:

- operator entrypoints
- recovery and maintenance tooling
- smoke tests and pentest tooling
- release and pre-push validation
- subsystem-specific probes and benchmark helpers

It is not for runtime state or product data.

## What May Live Here

Allowed categories:

- deployment and setup entrypoints
- offline repair and recovery tools
- pre-push and release checks
- security smoke, gate, and pentest tooling
- trading probes, validators, and benchmark drivers
- ComfyUI probes and local helper scripts
- thin compatibility wrappers for moved public script paths

## What Must Not Live Here

Disallowed categories:

- runtime reports
- logs
- caches
- temporary files
- generated result JSON or Markdown
- SQLite databases
- user uploads, media, or attachments
- workflow data that belongs to product features
- notebooks or throwaway experiments
- duplicate copies such as `foo (2).py`

If a script generates artifacts, those artifacts must go under `runtime/`.

## Root-Level Policy

Keep `scripts/` root nearly empty.

Root-level files are only acceptable when they are:

- a small number of stable human-facing entrypoints
- shared framework modules used by the `scripts/` package itself
- documentation that explains the directory

Feature-specific tools must not be added directly under `scripts/` root.

## Domain Mapping

Use these homes by default:

- `scripts/admin/`
  Root recovery, offline repair, operator-only maintenance tooling.
- `scripts/comfyui/`
  ComfyUI-specific probes, helper scripts, and ComfyUI-only templates.
- `scripts/prepush/`
  Pre-push framework internals and repository checks.
- `scripts/security/`
  Security validation, pentest, dependency audit, server-mode validation.
- `scripts/trading/`
  Trading validation, benchmark, bridge, and probe tooling.

## Template Rule

Templates should live with their owning subsystem when they are specific to one
feature area.

Example:

- `scripts/comfyui/comfyui_run_in_linux.template.sh`

Only truly shared cross-domain templates should live outside subsystem folders.

## Runtime Artifact Rule

Operational scripts may write to the configured external runtime root:

- `runtime/reports/...`
- `runtime/logs/...`
- other explicit runtime-only paths

Test and QA scripts must use `HACKME_TEST_OUTPUT_ROOT` or
`/tmp/hackme_web_test_artifacts`.

Scripts must not write reports or runtime data back into the source checkout.

Legacy paths like `security/reports/` are forbidden.

## Naming Rule

Script names should describe purpose, not history.

Prefer:

- `feature_probe.py`
- `workflow_template_backtest_benchmark.py`
- `run_functional_smoke.sh`

Avoid:

- `new_script.py`
- `final_script.py`
- `foo (2).py`
- `tmp_check.py`

## Wrapper Rule

When moving a documented script path:

1. move the real implementation into the correct subtree
2. keep a thin wrapper only if users are likely to still call the old path
3. update docs and tests in the same slice
4. remove the wrapper after the compatibility window ends

Do not keep wrappers forever.

## Review Checklist

Before adding or moving a script, check:

- is this a tool rather than product data?
- does it belong to an existing domain subtree?
- will it generate artifacts outside `runtime/`?
- is the filename descriptive?
- is this a real maintained tool rather than a one-off experiment?
- if this is QA/security/pentest/stress/smoke/gate tooling, is it registered
  in [INDEX.md](INDEX.md) with owner, purpose, artifact, and failure meaning?
- if this can be called by the production gate, is it listed in the production
  gate table in [INDEX.md](INDEX.md)?
- if this is only a focused regression, do docs and reports avoid calling it
  full validation?

If the answer is unclear, do not add it to `scripts/` root.
