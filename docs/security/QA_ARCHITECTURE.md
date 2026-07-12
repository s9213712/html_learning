# QA And Security Architecture

This document is the governance rulebook for QA/security tooling. It is not
only a map of the current scripts; it defines rules that should block future
tooling sprawl.

## Canonical Sources

| Topic | Canonical source |
|---|---|
| Documentation index | `docs/README.md` |
| QA layer map and operator flow | `docs/11_QA_TESTING.md` |
| Script registration and production-gate script ownership | `scripts/INDEX.md` |
| Script placement rules | `scripts/PLACEMENT_RULES.md` |
| Functional smoke behavior and options | `docs/security/FUNCTIONAL_SMOKE.md` |
| Permission pentest behavior | `docs/security/FUNCTIONAL_PERMISSION_PENTEST.md` |
| Pentest runner behavior | `docs/security/PENTEST.md` |
| Trading stress behavior | `docs/security/TRADING_STRESS_PENTEST.md` |
| Production gate playbook | `docs/server_mode_v2/03_production_gate_playbook.md` |

If two docs disagree, update the more specific canonical source first, then
update the index or overview that points to it.

## Validation Vocabulary

Use these names consistently in docs, reports, PRs, and issue comments:

| Term | Meaning | Must not be called |
|---|---|---|
| Focused regression | A targeted pytest or script check for a known risk or recent change. | Full validation, full QA, production validation. |
| Unit/integration pytest | A selected pytest suite, possibly broad inside one subsystem. | Production gate, whole-site validation. |
| Functional smoke | `run_functional_smoke.sh` isolated runtime workflow coverage. | Full production validation. |
| Permission pentest | Role/permission abuse matrix. | Functional smoke. |
| Pentest run | `run_pentest.sh` orchestration of built-in and optional external security checks. | Production sign-off unless production-gate requirements are also satisfied. |
| Stress/soak | Runtime or domain load/correctness validation. | Functional validation. |
| Production gate | The required report set and gate verification for production unlock. | A single script run. |

Focused regression results may be reported as "focused regression passed" or
"selected tests passed". They must never be summarized as "full validation passed".

## Script Layering

| Layer | Primary entry | Responsibility | Boundary |
|---|---|---|---|
| Orchestration | `scripts/security/pentest/run_pentest.sh` | Select and run pentest/security checks by ID. | Does not own individual domain assertions. |
| Permission matrix | `scripts/security/pentest/functional_permission_pentest.py` | Verify role-based allow/deny behavior. | Does not prove happy-path product workflows. |
| Functional smoke | `scripts/security/pentest/run_functional_smoke.sh --qa-full` | Start isolated runtime and exercise broad user/admin/product workflows. | Broad QA smoke, not production-gate core validation. |
| Stress/correctness | `scripts/security/pentest/stress_test.py`, `scripts/security/pentest/trading_stress_pentest.py` | Validate load behavior and trading correctness. | Does not replace permission or security scanning. |
| Production gate | `scripts/security/gate/on_live_reports_make.py` | Generate and verify production-gate evidence. | Must consume registered scripts and reject stale/fake/missing reports. |
| Stable wrappers | `scripts/on_live_reports/` | Provide operator-facing stable entry paths. | Must remain thin and registered in `scripts/INDEX.md`. |

Production-gate callers may invoke functional smoke only with `--core-only`
or equivalent `GO_LIVE_CORE_ONLY=1`. QA-only product workflows such as
community/chat, broad storage UX, ComfyUI, video/E2EE share, reports,
moderation, appeals, and game/chess validation stay in QA entrypoints and must
not be folded into the production unlock decision unless a specific security
boundary from that domain is promoted into a focused gate check.

## New QA Script Admission Rules

A new QA/security script is acceptable only when all of these are true:

1. It has a row in `scripts/INDEX.md`.
2. It has an owner, purpose, expected artifact, and failure meaning.
3. If production-gate callable, it appears in the "Production Gate Usable
   Scripts" table in `scripts/INDEX.md`.
4. Its output goes under `runtime/` or to stdout/stderr captured by a caller,
   never to tracked source paths.
5. It names its validation level correctly. Focused regression tests cannot be
   called full validation.
6. It does not duplicate an existing canonical entrypoint unless it is a thin
   wrapper with a documented compatibility reason.

Reviewers should block a new QA/security script that does not meet these rules,
even if the script itself works.

## Production Gate Script Requirements

Every production-gate usable script must define:

| Field | Required meaning |
|---|---|
| Owner | The subsystem or responsibility group expected to fix failures. |
| Purpose | The specific invariant the script proves. |
| Artifact | The report/raw output path that can be audited later. |
| Failure meaning | The operational interpretation of a failure. |
| Scope limit | What the script explicitly does not prove. |

The production gate may aggregate focused or broad checks, but the aggregated
gate result is the production decision. Individual focused checks remain
focused checks in all wording.

## Functional Smoke Phase Catalog

`run_functional_smoke.sh` is large enough that every phase needs an explicit
input/output expectation. When adding checks, place them in the matching phase or update this
catalog in the same change.

| Phase | Inputs | Outputs | Shared env/state | Cleanup responsibility |
|---|---|---|---|---|
| Runtime bootstrap | CLI args, `HOST`, `PORT`, `SMOKE_SCHEME`, new `/tmp/hackme_web_*` `RUNTIME_ROOT`, external `REPORT_ROOT`, smoke credentials. | `RUN_DIR`, `RAW_DIR`, `RESULTS_TSV`, pre-start evidence snapshot, server PID, selected local port. | Runtime path env vars: `HACKME_RUNTIME_DIR`, `HTML_LEARNING_DB_DIR`, `HTML_LEARNING_LOG_DIR`, `HTML_LEARNING_CHAT_DIR`, `HTML_LEARNING_ANCHOR_DIR`, `HTML_LEARNING_STORAGE_DIR`, `HTML_LEARNING_REPORTS_DIR`. | `cleanup` trap kills server and removes its newly created runtime; `--keep-runtime` retains that isolated state for debugging. The evidence snapshot is not used to overwrite a caller path. |
| Public pre-auth checks | `BASE_URL`, curl, no authenticated session. | Raw responses for index, site config, version, password strength, captcha, root recovery CLI help. | No login session should exist yet. | None beyond normal raw artifact cleanup through runtime cleanup. |
| Root auth and account seed | CSRF cookie, root credentials, smoke user credentials. | Root session cookie, possibly changed root password, `SMOKE_USER_ID`, enabled smoke feature flags. | `COOKIE_JAR`, `CSRF_TOKEN`, `ROOT_PASSWORD`, `SMOKE_USER_ID`; feature flags are intentionally mutated. | Later reset/restore and runtime cleanup must remove seeded users/state from the isolated runtime. |
| Baseline community checkpoint | Root session, smoke user id, feature flags, QA full mode. | Baseline category/board/thread ids and `APP_SNAPSHOT_ID`. | Community content becomes the restore sentinel. | Snapshot/restore phase must prove baseline remains after restore and disappears after reset. |
| Admin and security-center checks | Root session, baseline runtime. | Health/readiness/audit/security-center responses, custom profile, server-mode changes. | Server mode and security thresholds may be mutated. | Later mode transitions must return to a usable test state; final runtime cleanup removes state. |
| PointsChain and trading gates | Root session, smoke user session, points credit, maintenance bypass token, tester token. | Points/ledger responses, custom-profile fail-closed proof, test/internal_test diagnostics, trading price/audit responses. | Server mode changes from custom profile to `test` to `internal_test` and back to `test`; tester token stored in env. | Phase must restore a test-compatible mode before later user workflows. Runtime cleanup removes trading mutations. |
| Community, chat, and announcements | Root/smoke sessions, feature flags, QA full mode. | Announcement, category, board, thread, reply, chat, and DM raw responses. | Uses ids created earlier and may create residual content. | Restore/reset phase must prove residual content is removed. |
| Storage and cloud drive | Root/smoke sessions, upload fixture, storage runtime dirs. | Upload ids, album ids, quota/security responses, remote-downloader rejection guidance. | Files are written under isolated storage runtime. | Runtime cleanup removes files; reset/restore checks data cleanup behavior. |
| ComfyUI and Civitai | Root session, ComfyUI settings, optional backend availability, QA full mode. | Status/model responses, workflow/template guard responses, template `text:embeddings` assertion, Civitai missing-key guidance. | ComfyUI backend is optional; settings may be changed in isolated runtime. | No external model download should be required. Runtime cleanup removes isolated settings. |
| Video and E2EE share | Root/smoke sessions, storage upload ids, generated media fixture, QA full mode. | Published video ids, share tokens, playback responses, E2EE unlock/key/revoke responses. | Share tokens and E2EE metadata are intentionally created. | Revoke checks must fail closed; runtime cleanup removes media and metadata. |
| Reports and moderation | Root/smoke sessions, generated report targets, QA full mode. | Bug/report/moderation/appeal/notification raw responses. | May create moderation/report state. | Runtime cleanup removes generated state. |
| Snapshot restore and reset verification | `APP_SNAPSHOT_ID`, baseline ids, residual ids, `RESET_OFFLINE_TIMEOUT`, `RESET_RECONNECT_TIMEOUT`. | Restore proof, residual cleanup proof, reset restart proof, changed `started_at`, post-reset root login proof. | Server process may restart; `BASE_URL`, cookies, and CSRF must be refreshed. | This phase owns reconnect handling and session refresh after restart. |
| Report finalization | `RESULTS_TSV`, raw responses, server output, pass/fail/skip counters. | `00_FUNCTIONAL_SMOKE.md`, final exit code. | None beyond accumulated counters and artifact paths. | Cleanup trap performs final runtime/server cleanup after report generation. |

## When To Add Coverage

| Change type | Minimum expected validation |
|---|---|
| Single bug fix in one helper | Focused regression pytest or script check. |
| User-visible workflow change | Focused regression plus the relevant domain smoke. |
| Auth, permission, root, snapshot, trading, payments, storage, E2EE, or server-mode change | Focused regression plus relevant permission/security script; consider functional smoke. |
| Production-gate or release-unlock change | Focused regression plus production-gate relevant script/report validation. |
| Performance, soak, or concurrency change | Focused regression plus stress/soak evidence with duration and artifact path. |

Passing a focused regression only proves the selected risk did not reproduce.
It does not prove there are no silent failures, races, production-only bugs,
cross-mode contamination, or long-run regressions.
