---
name: hackme-web-qa
description: Run rigorous hackme_web QA for isolated local test environments. Use when asked to test, re-test, verify fixes, perform full-feature QA, heuristic/chaos QA, Playwright/browser QA, trading/economy correctness checks, upload/share/E2EE checks, or write docs/AGENTS/reports findings for the hackme_web project.
---

# Hackme Web QA

Use this skill to behave as a professional QA engineer for `hackme_web`: verify recent fixes, run broad automated coverage, perform heuristic member actions, and write a concise report under `docs/AGENTS/reports`.

## Core Workflow

1. Work from `/home/s92137/hackme_web` unless the user gives another checkout.
2. Inspect recent commits and dirty status before testing. Do not revert unrelated user or teammate changes.
3. Start an isolated server with `test_for_develop.sh`, using a unique run root under `/tmp` and a free port.
4. Run the strongest practical checks for the request:
   - `scripts/testing/playwright_deep_site_check.py` for browser/full-site coverage.
   - `scripts/testing/pytest_in_tmp.sh -q tests` when time allows.
   - Targeted pytest slices for touched domains when full pytest is too slow.
   - `scripts/member_probe.py` from this skill for real member behavior.
5. Treat failing scripts as evidence, not truth. Reproduce or inspect payloads before reporting. Mark false positives clearly.
6. Verify previously reported bugs first, then keep looking for new bugs.
7. Stop every server process started for the QA run.
8. Write or update a report in `docs/AGENTS/reports/YYYY-MM-DD_HHMM_*.md`.

## Skill Sync Rule

When this skill changes, keep the project mirror synchronized at `/home/s92137/hackme_web/docs/AGENTS/skills/hackme-web-qa`. If editing the repo mirror first, copy the same changes back to `/home/s92137/.codex/skills/hackme-web-qa` so future Codex sessions and project docs stay aligned.

## Required Heuristic Coverage

Always include the user-facing feature domains that exist in the current codebase:

- Auth/register/login/session/CSRF.
- Server/admin/security health, launch checks, user governance.
- Member levels, quota/rate limits, permissions.
- Cloud drive uploads, previews, sharing, E2EE, remote direct URL, magnet, `.torrent`.
- Video upload, publish/share/password unlock, HLS/playback, unsupported privacy modes.
- Albums and shared album password flows.
- Forum/community, chat/private messages, notifications.
- Games, especially chess and solo score routes.
- ComfyUI image generation, workflow editor/routes, missing dependency/offline behavior.
- Points wallet/ledger/catalog/admin adjustments.
- Trading exchange, orders, grid bot preview math, bots/backtests, margin/lending/reserve pool.

Use [references/coverage-checklist.md](references/coverage-checklist.md) as a compact checklist when writing reports.

## Member Probe

Run the bundled probe against a live isolated server:

```bash
export HACKME_QA_ROOT_PASSWORD
export HACKME_QA_TEST_PASSWORD
python3 /home/s92137/.codex/skills/hackme-web-qa/scripts/member_probe.py \
  --base-url https://127.0.0.1:PORT \
  --out /tmp/hackme_web_qa_RUN/member_probe/member_probe.json
```

Set both password variables from the isolated server launcher or a secret store.
Do not place live credentials in command arguments or checked-in reports.

The probe creates small fixture files, uploads multiple file types, tests previews, exercises E2EE success/failure cases, share links, album password sharing, remote download SSRF guards, real MP4 upload/HLS/password playback, unsupported video privacy mode, grid fee math, and reserve allocation.

If a probe fails because the account quota was exhausted by repeated probes, restart a clean `test_for_develop.sh` environment and rerun once before reporting.

## Report Rules

Lead with confirmed findings ordered by severity. For each finding include:

- Severity and title.
- Exact behavior and impact.
- Evidence artifact path or command/test name.
- Whether it is a regression, fix-verification failure, or new finding.

Include a coverage section with passes, false positives invalidated, and tests that could not be run. Mention commands and result counts, but keep the report practical for developers.
