# Server Mode v2 — Token Usage Examples

This folder contains runnable **教學範例** for the two distinct token classes used by Server Mode v2.

> **The two tokens are not interchangeable.** Read [`SERVER_MODE_V2_PROFILE_MATRIX.md` §Token Types](SERVER_MODE_V2_PROFILE_MATRIX.md#token-types) once before running anything.

## Quick mental model

```
┌────────────────────────────────────────┬───────────────────────────────────────────┐
│ internal_test login token              │ Server Mode v2 tester token               │
│ (door key)                             │ (scoped API key)                          │
├────────────────────────────────────────┼───────────────────────────────────────────┤
│ Lets a tester pass /api/login while    │ Lets a tester call /api/tester/* shadow   │
│ the server is in `internal_test` mode. │ APIs. Per-route, per-method, per-mode,    │
│                                        │ rate-limited, expiring, revocable.        │
│                                        │                                           │
│ Sent in JSON body field:               │ Sent in HTTP header:                      │
│   {"internal_test_token": "..."}       │   X-Tester-Token: ...                     │
│                                        │   (也支援 Authorization 的 Bearer scheme) │
│                                        │                                           │
│ One bound account per issued token.    │ One per tester (multi-issued).            │
│ Rotated by root for a specific tester. │ Created/revoked by root via               │
│                                        │   /api/root/tester-token/*                │
│                                        │                                           │
│ Only verified while                    │ Only valid in `test` / `internal_test`    │
│ current_mode == "internal_test".       │ modes.                                    │
└────────────────────────────────────────┴───────────────────────────────────────────┘
```

A user might need either or both:

- Logging in to look around `internal_test` UI: only the **login token**.
- Running shadow-state automation against `/api/tester/*`: only the **tester token**.
- Doing both (login + run automation): both, used independently in their respective channels.

## Files

| Script / Doc | Demonstrates |
| --- | --- |
| **[`TESTER_HANDBOOK.md`](TESTER_HANDBOOK.md)** | **For testers** — end-to-end playbook covering both tokens, functional UI testing, deep penetration probes, common pitfalls, and a bug-report template. Start here if you are a tester who just received a token. |
| [`01_internal_test_login_token.sh`](01_internal_test_login_token.sh) | How to rotate, distribute, and consume the **internal_test login token**. |
| [`02_tester_token_shadow_api.sh`](02_tester_token_shadow_api.sh) | How root creates a **tester token**, scopes it, and how a tester consumes it against `/api/tester/*`. |
| [`03_production_gate_playbook.md`](03_production_gate_playbook.md) | Pre-production gate report walkthrough (root-side; testers should read but not run in prod). |
| [`04_production_gate_validation_report.md`](04_production_gate_validation_report.md) | 2026-05-09 的實際 production gate 驗收紀錄，包含 warning / unverified / old-commit / all-green 四個情境與踩坑說明，並記錄 live `50909` target-commit mismatch 收斂結果。 |
| [`04_pentest_smv2.sh`](04_pentest_smv2.sh) | Focused SMv2 pentest probes: tester-token boundary checks, confirm-phrase negatives, revoke/replay denial, and root/admin path blocking. |
| [`05_stress_smv2.sh`](05_stress_smv2.sh) | SMv2-specific burst/rate-limit checks for shadow APIs, chain-side credit flow, and mode-log reads. |
| [`06_full_feature_smv2.sh`](06_full_feature_smv2.sh) | End-to-end walkthrough of the main Server Mode v2 admin surfaces, including mode switch, checkpoint, tester-token issue/use/revoke, and isolation checks. |
| [`07_privilege_escalation_smv2.sh`](07_privilege_escalation_smv2.sh) | Negative privilege-escalation probes to prove tester tokens and shadow-role writes cannot become root/admin back doors. |

Each script is self-contained bash + curl + `jq`. Read the `# usage` block at the top.

另外，production gate 的 filesystem auto-detect 規則已收斂：`runtime/reports/security/production_gate/*.json`
只負責輔助顯示最新候選報告。未驗簽、JSON 損壞、`report_type` 不符或 target 不一致的檔案都只會顯示 warning，
不能單獨讓 production gate 轉綠；真正規則以
[`03_production_gate_playbook.md`](03_production_gate_playbook.md) 為準。

針對 live `target_commit` 規則，現在有一條明確驗收要求：

- **verified old-commit reports 不能解鎖 production**
- **verified current-commit reports 才能解鎖 production**

這條規則不可以只靠 unit test 驗證；至少要在隔離 `/tmp` runtime 對
`/api/root/server-mode/requirements` 與 `/api/root/production/enter` 做一次 live
驗收，證據與流程請見
[`04_production_gate_validation_report.md`](04_production_gate_validation_report.md)。

> **First time receiving a token?** Read [`TESTER_HANDBOOK.md`](TESTER_HANDBOOK.md) §0–§2 before
> running any script. It tells you which token you got, what you can and cannot do with it, and
> the exact pre-flight check to confirm the server is in the right mode.

## Wider harness

To run the broader six-script Server Mode v2 tutorial bundle instead of only
the two token examples, use:

```bash
PYTHONPATH=. python3 scripts/security/server_mode/server_mode_v2_full_smoke.py
```

This boots an isolated runtime under `/tmp`, runs `01`, `02`, `04`, `05`,
`06`, and `07` back-to-back, then checks that shadow-table activity did not
leak into production wallet / ledger tables.

In other words, the harness is not only looking for per-script `rc=0`; it also
confirms that shadow-table activity did not leak into production wallet tables
or the production ledger namespace.

## Safety rails

- These scripts are **教學範例**; they are written assuming an **isolated runtime** (e.g. `HACKME_RUNTIME_DIR=/tmp/...`).
- Do **not** run them against a production-mode deployment. The login-token script needs the server to be in `internal_test` mode, which is incompatible with `production`. The tester-token script writes only to shadow tables, but the login probe will still attempt non-prod credentials.
- Never paste real secrets into scripts. The scripts read sensitive values from env vars or interactive prompts.
- Do not log captured tokens. The scripts never echo full token bodies; they only print a fingerprint.

## Prerequisites

- `bash`, `curl`, `jq`, `python3` available on PATH.
- A running hackme_web server reachable at `$BASE_URL` (default `https://127.0.0.1:5000`; the scripts use `-k` for self-signed dev cert).
- A bootstrap root account whose password you know.
- Server is in a non-production mode the script needs (each script asserts and aborts if the mode is wrong).

## Verified end-to-end (2026-05-05)

2026-05-05 的早期實作前 smoke 曾在隔離 runtime 透過 `scripts/security/server_mode/server_mode_v2_token_smoke.py` 跑過兩支腳本；該歷史計畫已移除，結果保留如下：

- `01_internal_test_login_token.sh` → **PASS** (rc=0). Demo flow: root login → forced password change → switch to `internal_test` → rotate login token → tester rejected without token → tester accepted with token → logout.
- `02_tester_token_shadow_api.sh` → **PASS** (rc=0). Demo flow: scoped tester-token created → GET shadow-state → POST shadow-role (manager) → POST shadow-wallet (+100) → re-read state → negative tests on admin/root endpoints (all 401/403) → revoke → revoked-token denial.
- **Isolation check** (post-run DB): `test_shadow_wallets=1`, `test_shadow_transactions=1`, `test_shadow_roles=1`, `points_chain_blocks=0`, `points_wallets=0`, `points_ledger=0`. Shadow modifications did **not** leak into production tables. Confirms internal_test sandbox boundary holds end-to-end through the HTTP stack.

### Known constraints surfaced by the smoke run

These are properties of the **server**, not bugs in the tutorials. The scripts now handle them, but operators should be aware:

1. **`internal_test` mode runs with `browser_only_mode_enabled=true`.** Plain `curl` without a browser-marker User-Agent gets blocked at `/api/*`. The scripts add `User-Agent: Mozilla/5.0 ... HackmeWebTutorialClient/1.0` so the browser-only middleware lets them through. (See `services/security/access_controls.py:BROWSER_UA_MARKERS`.)
2. **`POST /api/admin/access-controls/internal-test-token` requires `confirm:"ROTATE_INTERNAL_TEST_TOKEN"` plus `target_username` or `target_user_id`** in the body, and **`POST /api/root/server-mode/checkpoint` requires `target_mode`**. Missing those gives a 400 with no useful hint — the scripts now send them.
3. **`POST /api/root/tester-token/create` does not accept `ttl_minutes`.** The expiration parameter is `expires_at` (ISO 8601). The server stores and compares it as **naive local time** via `datetime.now().isoformat()`; passing a UTC `Z` timestamp from a TW (`UTC+8`) shell makes the token look already-expired and silently breaks every subsequent tester call. Script 02 now generates the timestamp with `datetime.now() + timedelta(minutes=60)` (naive local) to match the server's comparison.
4. **Default-account `must_change_password=1`.** A freshly-bootstrapped `test` user is flagged for forced password change on first login. The middleware blocks `/api/tester/*` until the password is changed — meaning the tutorial's `test` account would be unusable for shadow-API demos out of the box. The smoke harness clears this flag in DB before running script 02. The tutorial assumes the operator has already handled the forced-change step out of band.
5. **`/api/root/tester-token/list` JSON shape**: the smoke confirmed `label` is among the returned fields, but jq's bare-object shorthand `{label}` collides with jq's reserved `label $name` syntax. Script 02 now uses `{"label": .label}` explicitly.

### Re-run the smaller token smoke at any time

```bash
python3 scripts/security/server_mode/server_mode_v2_token_smoke.py
```

Picks a random free port, spawns an isolated server in `/tmp/hackme_token_smoke_<random>/`, runs both `.sh` scripts back-to-back, asserts shadow-vs-prod table counts, kills the server. Runtime + server log are preserved under the temp dir for inspection.

To run the broader 6-script bundle instead of only token tutorials:

```bash
PYTHONPATH=. python3 scripts/security/server_mode/server_mode_v2_full_smoke.py
```
