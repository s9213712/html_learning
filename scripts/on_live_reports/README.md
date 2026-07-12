# scripts/on_live_reports/

Shortcut directory for the production-gate reports described in
[docs/11_QA_TESTING.md](../../docs/11_QA_TESTING.md). Each report type maps to a
predictable Python entry point where the report can be generated independently.
The 24-hour campaign is the exception: it is generated under `/tmp` and imported
by the one-shot orchestrator after its source manifest is revalidated.

```
python3 scripts/on_live_reports/<report_type>.py [args...]
```

Most entries are symlinks to the canonical driver under `scripts/security/`;
the remainder are thin Python wrappers that invoke `pytest_in_tmp.sh`, the
relevant API, or compose multiple sub-drivers.

| report_type | entry | underlying driver |
|---|---|---|
| `clean_smoke` | `clean_smoke.py` (symlink) | `scripts/security/server_mode/server_mode_v2_clean_smoke.py` |
| `adversarial` | `adversarial.py` (symlink) | `scripts/security/server_mode/server_mode_v2_adversarial.py` |
| `redteam_l2` | `redteam_l2.py` (symlink) | `scripts/security/server_mode/server_mode_v2_redteam_l2.py` |
| `pytest` | `pytest.py` (wrapper) | `scripts/testing/pytest_in_tmp.sh -q tests` |
| `log_chain_verify` | `log_chain_verify.py` (wrapper) | `GET /api/root/server-mode/logs/verify` |
| `integrity_guard` | `integrity_guard.py` (wrapper) | `tests/security/integrity/test_integrity_guard.py` |
| `stress` | `stress.py` (symlink) | `scripts/security/pentest/trading_stress_pentest.py` |
| `permission` | `permission.py` (symlink) | `scripts/security/pentest/functional_permission_pentest.py` |
| `functional` | `functional.py` (wrapper) | `run_functional_smoke.sh --core-only` + `tests/security/smoke/smoke_suite.py` |
| `pentest` | `pentest.py` (symlink) | `scripts/security/pentest/session_security_pentest.py` |
| `snapshot_restore` | `snapshot_restore.py` (wrapper) | `tests/snapshots/test_snapshots.py` boundary checks; PointsChain ledger backup/restore must stay disabled |
| `points_chain_consistency` | `points_chain_consistency.py` (wrapper) | `tests/points/test_points_chain.py` |
| `cloud_drive_quota_permission` | `cloud_drive_quota_permission.py` (wrapper) | `tests/storage/test_cloud_drive_attachments.py` + `tests/storage/test_storage_albums_schema.py` |
| `ai_agent_boundary` | `ai_agent_boundary.py` (wrapper) | deterministic `tests/ai_agent/test_ai_agent_routes.py` boundary regressions; no LLM call |
| `operational_campaign_24h` | imported by `on_live_reports_make.py` | formal `/tmp/.../operational_campaign_24h.json`; requires at least 86,400 active seconds and matching source manifest |

## One-shot orchestrator

To produce all reports in one run (recommended for a full production gate):

```bash
HACKME_RUNTIME_DIR=/absolute/external/runtime \
HACKME_OPERATIONAL_CAMPAIGN_REPORT=/tmp/<campaign>/reports/operational_campaign_24h.json \
ROOT_PASSWORD='<root-password>' \
MANAGER_PASSWORD='<manager-password>' \
TEST_PASSWORD='<test-user-password>' \
python3 scripts/on_live_reports/on_live_reports_make.py \
    --base-url "https://127.0.0.1:$PORT"
```

This is a symlink to `scripts/security/gate/on_live_reports_make.py`. It also
writes a summary to `$HACKME_RUNTIME_DIR/reports/security/production_gate/`.

For production-gate acceptance, this one-shot generation is necessary but not
sufficient. You must also prove the live server rejects:

- unsigned reports
- invalid JSON
- `report_type` mismatch
- verified reports whose `target_commit` is old/fake

and only accepts the full required-report set when all reports are verified and match
the live server's current `target_commit`.

## Notes

- `stress` covers `trading_stress_pentest.py`. The general HTTP stress driver
  `scripts/security/pentest/stress_test.py` is *not* aliased here because the
  production-gate harness packages both under the single `stress` report type.
  Run `stress_test.py` directly when you need it standalone.
- `integrity_guard` runs the regression test only. The production gate also
  expects a manual API rescan/report fetch (see
  [docs/11_QA_TESTING.md](../../docs/11_QA_TESTING.md)) — capture that JSON
  alongside the test output when assembling the gate package.
- When driving integrity review through
  `scripts/security/gate/on_live_reports_make.py`, the helper now refreshes
  CSRF before `bulk-review` and `rescan` POSTs. If your isolated copy itself
  was modified before the run, you still need to review/approve those expected
  findings before the final `integrity_guard` report can pass.
- These shortcuts intentionally do not move runtime artifacts. Each underlying
  driver still writes to the configured external runtime; standalone QA defaults
  to `/tmp/hackme_web_test_artifacts/` when no deployment runtime is selected.
- When the live server is started through `test_for_develop.sh`, keep
  `HTML_LEARNING_GIT_REPO_DIR` pointed at a real git repo. A `/tmp` copy without
  `.git` will break current-target detection and make `target_commit` gate
  validation meaningless.
