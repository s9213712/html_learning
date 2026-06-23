# Server Mode v2 Profile Matrix

> **Status (2026-05-05):** Updated by root direction; aligned with current implementation in `services/snapshots/schema.py` and `services/snapshots/server_mode.py`. Previous "驗收差異 D-1 / D-2 / D-3"（CSRF always-on, 13 production reports, per-mechanism enforcement）皆已併入本主規格；舊 companion acceptance report 已從 tracked docs 移除，必要時查 Git history。
>
> **Companion specs:**
> - [`SERVER_MODE_V2_TRADING_AND_POINTSCHAIN.md`](SERVER_MODE_V2_TRADING_AND_POINTSCHAIN.md) — engineering-grade trading + chain rules per mode.
> - [`SERVER_MODE_V2_TEST_PLAN.md`](SERVER_MODE_V2_TEST_PLAN.md) — phase-by-phase test/validation plan.
> - [`SERVER_MODE_V2_MIGRATION_PLAN.md`](SERVER_MODE_V2_MIGRATION_PLAN.md) — incremental migration plan.

Server Mode v2 makes server mode a first-class operational control, not just a
collection of feature flags. Mode switching is a root-only high-risk operation
with checkpoint creation, independent mode switch logging, and explicit restore
validation.

## Mode Names

| Canonical mode | Legacy name | Purpose | Notes |
| --- | --- | --- | --- |
| `superweak` | none | Deliberately weak security lab mode. | Never for normal development or operations. Dirty state is discarded on exit. |
| `test` | none | **Isolated QA bench** — developers / QA agents run curl / 自動化 / fuzz / 例外輸入 / 壓測. | Must be deployed in an isolated runtime; **never on a public-facing site**; **must not point at production wallet / production ledger**. Different role from `internal_test`. |
| `internal_test` | none | Controlled feature preview for **invited human testers**. | Tighter than `test`; access requires tester token; tester actions go to `test_shadow_*` tables. |
| `dev_ready` | `preprod` | Development-ready / pre-release mode. | `preprod` is accepted only as a legacy alias and is stored/displayed as `dev_ready`. |
| `production` | none | Public online operation. | Requires strict report gate and all mandatory security controls. |
| `maintenance` | previous `maintenance_mode` setting | Operational maintenance, updates, DB migration, repair. | Formal server mode; setting remains an implementation detail. |
| `incident_lockdown` | none | Security incident containment. | Auto-entered on restore failure, chain mismatch, high-risk integrity findings, and failed mode switch. |

## Mode Service Posture

Two orthogonal axes describe what each mode means operationally.

| Mode | Public service axis | Release readiness axis |
| --- | --- | --- |
| `production` | Public, for general users | Released live |
| `maintenance` | Not public; only operator UI | Not released; deferred for upkeep |
| `incident_lockdown` | Not public; only root rescue paths | Not released; recovery-only |
| `internal_test` | Not public; invite-only testers | Not released; pre-release validation |
| `test` | Not public; QA / automation | Not released; QA bench |
| `dev_ready` (alias `preprod`) | Not public; developer environment | Not released; dev rail |
| `superweak` | Not public; weakened lab | Not released; deliberate adversarial bench |

Notes:

- "Public service axis" = whether normal end users can reach the running site through the front door.
- "Release readiness axis" = whether the deployment counts as a live release for governance purposes (audit chain, PointsChain production writes, etc.).
- `production` is the only mode that is `Public` × `Released live`. Any other mode that becomes publicly reachable must immediately fail the production gate.

## Default Mode and Activity Guide

### Default mode: `dev_ready`

Day-to-day development should run in `dev_ready` (alias `preprod`). It keeps a baseline of safety while staying out of the way of normal coding.

```text
SERVER_MODE=dev_ready
```

### What stays on in `dev_ready`

- CSRF (on in every mode except `superweak`; see §Mode Behavior Matrix footnote 1)
- SSL
- Rate limit
- `feature_audit_log_enabled`
- `mode_switch_logs` (always-on across all modes)

### What is off in `dev_ready` by default

- General audit chain (`audit_chain_enabled = false`)
- Integrity Guard
- `browser_only_mode_enabled`
- IP blocking
- Login violation counters
- `feature_economy_enabled`
- `feature_trading_enabled`

### Activity → Mode mapping

| Activity | Mode | Who uses it | Where data lives |
| --- | --- | --- | --- |
| Routine feature work | `dev_ready` | Developer | Local dev DB; economy / trading off by default |
| Automation / curl / QA scripts / fuzz / 例外輸入 / 壓測 | `test` | Developer + QA agent | Isolated runtime DB (must not be production runtime) |
| Invited human testers exercise real flows | `internal_test` | Invited tester | `test_shadow_*` tables (production tables not mutated) |
| Pre-launch production gate rehearsal | `production` | Operator | **Only inside an isolated QA runtime**. Do not point real users at it. |
| Deliberately weakened lab experiments | `superweak` | Researcher | Ephemeral dirty state; snapshot rollback required |

Three closely-related non-production modes split as follows:

```text
dev_ready     │ developer's daily mode; economy/trading off; loose security baseline
              │  → use this when "I'm coding a non-economy feature"
              │
test          │ QA bench; full safeguards on; economy on (in isolated runtime); trading off
              │  → use this when "I want to run pytest / curl / Playwright / fuzz"
              │  → DO NOT use this for inviting real testers
              │  → DO NOT point this at production runtime
              │
internal_test │ invited-tester sandbox; full security on; trading shadow only
              │  → use this when "I'm letting a real human tester exercise the UI"
              │  → tester actions land in test_shadow_*; production tables stay clean
```

The two protective rules:

- **Economy / points / wallet / ledger** experiments must NOT run in `dev_ready`. Switch to `test` or `internal_test` so the writes go through the shadow layer (`test_shadow_wallets`, `test_shadow_transactions`, `test_chain_blocks`).
- **Production gate dry runs** must NOT happen in the live runtime. Use a dedicated isolated QA host so that the production-grade controls do not leak into a real user-facing deployment.

Mode switching follows the §Mandatory Invariants and §Confirmation Phrases sections; nothing in this guide bypasses those gates.

## `test` Mode Hard Rules

`test` is the isolated QA bench. It exists because `dev_ready` is too loose for acceptance and `internal_test` is too tight for automation. To keep the role narrow, the following hard rules apply:

1. **Not for public production sites.** A `test`-mode deployment must run in an isolated runtime (separate `runtime/` dir, separate DB, separate hostname) and must not be reachable from the public production frontend.
2. **No production wallet / production ledger.** Even though `test` mode has `feature_economy_enabled = True` (so QA can exercise wallet / ledger flows in the isolated runtime), the deployment must not be configured against the real `points_wallets` / `points_ledger` / `points_chain_blocks`. Always confirm `HACKME_RUNTIME_DIR` / `HTML_LEARNING_DB_DIR` point at a non-production path before running `test`-mode tests.
3. **TEST MODE banner mandatory.** UI banner must be `orange testing` per §Mode Behavior Matrix; any branch / build that strips the banner is a release blocker.
4. **`browser_only_mode` may be off.** This is by design so that curl / Playwright / fuzz tools can hit the API without needing a real browser User-Agent. Do NOT confuse this with `production` (where browser_only is on).
5. **Password strength / forced-default-password reset may be relaxed.** `optional/off` per §Mode Behavior Matrix. This is so QA fixtures with short test passwords can be reused. **Never reuse such passwords on real users.**
6. **Test accounts and test data may be reset.** A `test`-mode deployment is considered ephemeral; resetting fixtures is normal. By contrast, `internal_test` keeps tester shadow rows immutable until the next mode switch / checkpoint.
7. **Trading remains off in `test`.** `feature_trading_enabled = False` in this profile. If you need to QA trading flows, run them in `internal_test` mode where trading writes land in `test_shadow_*` tables — not in `test` mode where the writes would touch the isolated-but-real `points_ledger`.

The activity → mode mapping below uses these rules to keep `test` and `internal_test` clearly distinct.

## Mandatory Invariants

| Invariant | Required behavior |
| --- | --- |
| Root only | Only root can switch mode, create mode checkpoint, upload production reports, create/revoke tester tokens, enter/resolve incident mode. |
| Two-phase confirmation | Every mode switch requires the mode-specific confirmation phrase plus reason. |
| Independent logging | `mode_switch_logs` is written directly to DB and does not depend on audit chain. No delete API is exposed. |
| Checkpoint gate | Mode switch is rejected if checkpoint creation fails. |
| Restore validation | Restore must verify DB, test content removal, PointsChain hash, Cloud Drive metadata hash, config/security hash, and integrity manifest hash. |
| Production gate | Production entry requires all mandatory reports passing with zero critical/high unresolved findings. |
| Incident safety | Restore validation failure, PointsChain mismatch, high-risk integrity finding, mode switch failure, or production critical file change enters `incident_lockdown`. |

## Mode Behavior Matrix

| Mechanism | superweak | test | internal_test | dev_ready | production | maintenance | incident_lockdown |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **CSRF**¹ | **off** | on | on | on | on | on | on |
| Password strength | off | optional/off | on for non-testers | optional/off | on | on | on |
| Force default password reset | off | optional/off | on for non-testers | off | on | on | on |
| Login IP lock | off | off | on | off | on | on | on |
| Account lock | off | limited | on | limited | on | on | on |
| Rate limit | off | token/request capped | on | on | on | on | strict |
| General audit chain | off | off or shadow | on | off | on | on | on |
| `mode_switch_logs` | on | on | on | on | on | on | on |
| Integrity Guard | off | off | on | off | on strict | on | on strict |
| PointsChain production writes | off | shadow only | production for normal users, shadow for testers | off | on | paused for writes | paused |
| **Trading**² | off | shadow only | shadow only / no production wallet write | off by default | on if feature enabled | paused | paused |
| Cloud Drive | 10MB quota, sandbox | overlay/shadow for testers | overlay/shadow for testers | normal dev data | quota enforced | read-only optional | read-only |
| **Public registration**³ | configurable, ephemeral only | configurable | off | off | off | off | off |
| **`browser_only_mode`**⁴ | off | off | on | off | on | n/a (subsumed by `maintenance_mode`) | n/a (subsumed by `maintenance_mode`) |
| Test vulnerability features | on | token-limited | off by default | off | off | off | off |
| UI banner | red warning | orange testing | orange internal | blue dev | green production | purple maintenance | black/red incident |

**Footnotes:**

1. **CSRF: on in every mode EXCEPT `superweak`.** `superweak` is the deliberate "weakest web mode" used for adversarial / red-team / pentest fuzzing — turning CSRF off there is part of its definition (alongside disabled rate limit, login lock, account lock, password strength, etc.). All other modes (`test` / `internal_test` / `dev_ready` / `production` / `maintenance` / `incident_lockdown`) MUST keep CSRF on; the middleware reads `current_mode` only to test the single `mode == 'superweak'` bypass. Implementation: `services/users/auth.py:require_csrf` + `require_csrf_safe` skip the CSRF check **only** when `current_mode == 'superweak'`. The bypass MUST NOT extend to any other mode for any reason. Audit chain: every superweak entry/exit logged in `mode_switch_logs`; every superweak session is treated as dirty state and rolled back on exit.
2. **`internal_test` Trading: shadow only.** Tester actions go to `test_shadow_wallets` / `test_shadow_transactions` / `test_chain_blocks`. **No production wallet write under any tester path.** See §Shadow Data Boundaries.
3. **`superweak` public registration: ephemeral dirty state only.** When enabled, registrations are part of the dirty state that is **discarded on mode exit**. Implementation must not write `superweak`-mode registrations into production `users` / `points_wallets` / `points_ledger`. Mode exit performs rollback through the `before_superweak` snapshot or equivalent dirty-state cleanup.
4. **`browser_only_mode` in maintenance / incident_lockdown: n/a.** The `maintenance_mode` flag already blocks normal user UI in those two modes, so `browser_only_mode` is redundant. Root / admin rescue endpoints in those modes still require root-only auth, CSRF, and rate limit on top of the normal session checks.

## Confirmation Phrases

| Target mode | Confirmation phrase |
| --- | --- |
| `superweak` | `ENABLE_SUPERWEAK` |
| `test` | `SWITCH_TO_TEST` |
| `internal_test` | `SWITCH_TO_INTERNAL_TEST` |
| `dev_ready` / `preprod` | `SWITCH_TO_DEV_READY` |
| `production` | `GO_LIVE` |
| `maintenance` | `ENTER_MAINTENANCE` |
| `incident_lockdown` | `ENTER_INCIDENT_LOCKDOWN` |

## Production Gate Reports

Production entry requires one latest passing report for each of the **13 report types** defined in `services/snapshots/schema.py:35-49 PRODUCTION_REQUIRED_REPORT_TYPES`.

| # | Report type | Required content |
| --- | --- | --- |
| 1 | `clean_smoke` | Server Mode v2 clean smoke run; baseline state-machine + boot path. |
| 2 | `adversarial` | Server Mode v2 adversarial run; injection / bypass / mode-spoof attempts. |
| 3 | `redteam_l2` | Server Mode v2 red-team Level 2 deeper attack tree. |
| 4 | `pytest` | Full project pytest pass at the target commit. |
| 5 | `log_chain_verify` | `mode_switch_logs` hash chain + audit chain hash verification. |
| 6 | `integrity_guard` | Integrity Guard self-check (no high-risk findings). |
| 7 | `stress` | Controlled traffic / trading stress report. |
| 8 | `permission` | Role and permission pentest report. |
| 9 | `functional` | Full functional smoke report. |
| 10 | `pentest` | Security penetration test report. |
| 11 | `snapshot_restore` | Snapshot/restore regression report. |
| 12 | `points_chain_consistency` | PointsChain verification report. |
| 13 | `cloud_drive_quota_permission` | Cloud Drive quota and permission report. |

All 13 must be **latest passing** with **zero critical findings**, **zero high findings**, and **zero unresolved findings** to enter production. A single missing or stale or failing report blocks entry.

Each report must include `report_id`, `report_type`, `generated_at`,
`target_commit`, `target_branch`, `server_mode`, `test_result`, `pass`,
`critical_findings_count`, `high_findings_count`, `unresolved_findings`,
`tester`, and `signature` or `hash`.

> **Spec history note**: An earlier draft listed only the bottom 7 (`stress` through `cloud_drive_quota_permission`). The implementation has always required the full 13 since the Server Mode v2 phase 2 cut. The spec is now aligned with implementation.

## Shadow Data Boundaries

`test` and `internal_test` testers may be allowed to change their own role,
points, and trading data, but only through:

- `test_shadow_roles`
- `test_shadow_wallets`
- `test_shadow_transactions`
- `test_chain_blocks` / shadow ledger

The shadow layer must not affect production wallet balances, production
PointsChain, production leaderboards, or production trading positions.

## Token Types

Server Mode v2 uses two different token classes. They are intentionally not
interchangeable:

| Token | Where it is used | Purpose | Authority |
| --- | --- | --- | --- |
| `internal_test` login token | `POST /api/login` payload field `internal_test_token` | Lets a root-approved tester log in while the server is in `internal_test` mode. | Login gate only. It does not grant API scopes by itself. |
| Server Mode v2 tester token | `X-Tester-Token` or `Authorization: Bearer ...` | Lets a tester call scoped `/api/tester/*` automation/shadow APIs in `test` or `internal_test`. | Route/method/mode scoped, rate-limited, expiring, revocable, HMAC-signed. It cannot access root/admin/server-mode/snapshot/integrity/audit APIs. |

The login token is a door key. The tester token is a scoped test API key. A
user may need one or both depending on whether the task is simply entering
`internal_test` or actively running shadow-state/API tests.

## Root APIs

| API | Purpose |
| --- | --- |
| `GET /api/root/server-mode` | Read current mode, profiles, production gate status, and incident summary. |
| `POST /api/root/server-mode/checkpoint` | Create a checkpoint before a planned mode operation. |
| `POST /api/root/server-mode/restore-check` | Validate current state against a stored checkpoint. |
| `POST /api/root/server-mode/switch` | Switch mode after confirmation phrase and checkpoint gate. |
| `GET /api/root/server-mode/requirements` | Read production gate report requirements. |
| `GET /api/root/server-mode/logs` | Read independent mode-switch log rows. |
| `POST /api/root/production-report/upload` | Upload a production-entry-compatible report record. |
| `GET /api/root/production-report/status` | Read production report status. |
| `POST /api/root/production/enter` | Shortcut for production entry using the same gate as mode switch. |
| `POST /api/root/tester-token/create` | Create a scoped tester token. |
| `POST /api/root/tester-token/revoke` | Revoke a tester token. |
| `GET /api/root/tester-token/list` | List tester token metadata. Token secret is never returned after creation. |
| `POST /api/root/incident/enter` | Manually enter incident lockdown. |
| `GET /api/root/incident/status` | Read open incident status. |
| `POST /api/root/incident/resolve` | Resolve the open incident after root confirmation. |

### Production gate trust boundary

- `GET /api/root/server-mode/requirements` 會同時讀取資料庫中的 `production_entry_reports` 與
  `runtime/reports/security/production_gate/*.json` 穩定檔。
- 但 filesystem auto-detect **只用來輔助顯示最新候選報告**，不是預設可信來源。
- 任何來自 filesystem 的 report 預設 `trust_level=unverified`；只有驗簽成功且
  `target_commit` / `target_branch` / `server_mode` 與目前 runtime 完全一致，才可視為
  `verified`。
- `verified` record 永遠優先於 `unverified` record。未驗證的 filesystem 報告只能形成
  warning，不能覆蓋已驗證的資料庫 record，也不能單獨滿足 production gate。
- `current target commit` 的來源必須在 live server 上唯一收斂：
  `production requirements API`、`production enter API`、filesystem report loader、
  DB verified report loader 都必須使用**同一個** target 判定來源。
- 對 `test_for_develop.sh` 啟動路徑而言，這表示 `HTML_LEARNING_GIT_REPO_DIR`
  必須指向真實 git repo，而不是沒有 `.git` 的 `/tmp` copy；否則 live target commit
  會變空值，舊 commit 驗證將失真。
- 正式 QA / pre-push / production-gate 驗收必須至少包含一個 live regression：
  **完整 required report set verified 但 old/fake `target_commit` 的 reports 不得解鎖 production。**

## Tester APIs

Tester APIs require `X-Tester-Token` or `Authorization: Bearer <token>`.
State-changing tester APIs still use the normal CSRF protection.

| API | Purpose | Formal data mutation |
| --- | --- | --- |
| `GET /api/tester/shadow-state` | Read tester shadow role, wallet, transactions, and test chain rows. | No |
| `POST /api/tester/shadow-role` | Change own role inside shadow layer. | No |
| `POST /api/tester/shadow-wallet` | Add/subtract own shadow points inside shadow layer. | No |

## Per-Mechanism Enforcement

Server Mode v2 uses an explicit behavior boundary between the central mode profile and each individual mechanism. **Mode-driven enforcement is per-mechanism, not centralized in a single dispatch dict.**

`services/snapshots/schema.py:243` `BUILTIN_SECURITY_PROFILES` carries the **settings-driven** signals (e.g. `audit_chain_enabled`, `integrity_guard_enabled`, `rate_limit_violation_enabled`, `feature_*_enabled`, `maintenance_mode`, `allow_register`, `browser_only_mode_enabled`, `captcha_mode`). These flow through `system_settings` and are read by each mechanism module on demand.

Mechanisms NOT in the central profile dict — they consult `current_mode` (or its derived flags) inside their own module:

- CSRF middleware (on in every mode except `superweak`; bypass is the single allowed exception)
- Password strength validator (consults member level + mode-aware policy)
- Force default password reset (per-user state combined with mode policy)
- Login IP lock (combines IP-blocking enabled flag + mode-specific risk)
- Account lock (per-user state plus mode-driven thresholds)
- Trading shadow routing (`internal_test`/`test` redirect production wallet writes into `test_shadow_*`)
- Cloud Drive overlay (per-tester scope plus mode-aware quota / write target)
- Test vulnerability features (only valid in `superweak` / `test` token-limited paths)
- UI banner (rendered from current mode at request time)

Implication: when a future mechanism needs mode-aware behavior, it adds its own check against `current_mode` (or a derived predicate). It does **not** require adding a new key to `BUILTIN_SECURITY_PROFILES`. The profile dict remains a **stable subset** of mode-driven settings; the rest is enforced per-mechanism by design.

This split keeps the profile dict small, auditable, and migration-safe, while letting individual mechanisms evolve independently. Reviewers should not assume "if it isn't in the profile dict, it isn't enforced". For each mechanism listed above, the canonical definition is the module's source.

## Data Scope (per-mode)

每個 mode 「會碰到的資料範圍」是 spec-level 約束，動工 / QA / agent 看哪個 table 應不應該被動，請以本表為準：

| Mode | data_scope | 解釋 |
|---|---|---|
| `production` | `real` | 對外用戶資料；wallet / ledger / chain 都是真資料 |
| `maintenance` | `real_restricted` | 真資料但寫入暫停；root 維運讀寫受限 |
| `incident_lockdown` | `read_mostly` | 真資料但僅允許讀（救援匯出），不允許寫 |
| `internal_test` | `shadow` | tester 動作只進 `test_shadow_*` table；不碰 production wallet / ledger / chain |
| `test` | `isolated` | 必須在 isolated runtime；DB / 設定 / 檔案系統與 production 隔離 |
| `dev_ready` | `dev_local` | 開發者本機資料；economy / trading 預設關 |
| `superweak` | `ephemeral` | 進場前 `before_superweak` snapshot；退場 dirty state 必須清掉，不可 persist 進 production |

`data_scope` 是 informational 標籤但同時是 hard rule：

- agent / QA tool 必須以 `data_scope` 判斷要不要寫某 table
- 任何 `data_scope != "real"` 的 mode 寫入 production wallet / ledger / chain → **release blocker**

## Routing（global mode + per-request shadow path）

- **Server mode is global.** `current_mode` 影響所有 request；不存在「這個 request 在 production，那個 request 在 test」的 hybrid。
- **Shadow routing is per-request.** 在 `internal_test` mode 下，request 是否走 shadow tables 由 caller role 決定：

```
condition:
  if   mode == "internal_test" AND role == "tester"
  then route_to: test_shadow_*  tables
  else route_to: production tables (read-only for tester roles)
enforcement:
  - tester_token_invalid_in_other_modes: true
  - production_writes_under_tester_role_must_be_rejected: true
```

`tester_token` 在 `production` / `dev_ready` / `superweak` / `maintenance` / `incident_lockdown` 都無效；只在 `test` / `internal_test` 有效。

## Cache / Job / Queue Isolation

跨 mode 共用 cache / queue / job namespace 是事故熱點。Spec 鐵則：

- **Cache key 必須帶 scope prefix**：
  - production: `wallet:prod:{user_id}` / `orderbook:prod:{symbol}`
  - test: `wallet:test:{user_id}` / `orderbook:test:{symbol}`
  - shadow (internal_test): `wallet:tester:{tester_user_id}:{user_id}` / `orderbook:testerA:{symbol}`
- **Background jobs 必須按 mode 分隔 queue**：例如 `bg:matching:prod` vs `bg:matching:shadow` vs `bg:matching:test`
- **Queue / pub-sub channel 不可共用 namespace**：production 的撮合事件 publisher **絕對不可** subscribe shadow 的事件（會跨界觸發清算）

執行時 helper 函式請強制這個 prefix；review 任何「沒帶 scope prefix」的新 cache key 都當 release blocker。

## `mode_switch_logs` Append-Only + Optional Hash Chain

- `mode_switch_logs` 是 append-only 表，已用 `BEFORE DELETE` trigger 鎖死刪除（`services/snapshots/schema.py`）。
- `UPDATE` 在 server-side 也應該避免；雖然目前無強制 trigger 阻擋 UPDATE，但任何修改既有 row 的 PR 都應該 review 拒絕。
- 已實作 hash chain（`row_hash` + `prev_hash`），boot 時驗證；任何 row 被竄改即破鏈，由 `log_chain_verify` production gate report 偵測。
- 未來若要加 `BEFORE UPDATE ON mode_switch_logs RAISE` trigger 也歡迎，這是 spec 友好的補強。

## QA Acceptance Checklist（必過）

進 production / 動 mode-related 源碼前必過：

```
qa_checks:
  - tester_write_does_not_affect_production_wallet
  - tester_write_does_not_affect_points_chain
  - shadow_data_is_isolated_per_tester
  - production_mode_rejects_tester_token
  - restore_failure_enters_incident_lockdown
  - mode_switch_requires_checkpoint
  - superweak_exit_discards_all_changes
```

對應 pytest：見 `tests/test_snapshots.py` + `tests/test_auth_csrf_safe.py` + 後續 trading 整合測試（[`SERVER_MODE_V2_TRADING_AND_POINTSCHAIN.md`](SERVER_MODE_V2_TRADING_AND_POINTSCHAIN.md)）。

## Implementation-Grade YAML Reference

> 本附錄是 root 拍板的 final spec YAML form（root 直接給的）。**spec 衝突時以 markdown 主文 + 本 YAML 為雙重依據；兩者不一致是 release blocker。**

```yaml
server_mode:
  type: global
  source: ENV / DB
  allowed_values:
    - production
    - maintenance
    - incident_lockdown
    - internal_test
    - test
    - dev_ready
    - superweak

rules:
  - csrf_default_on: true             # CSRF on in every mode...
  - csrf_superweak_bypass: true       # ...except superweak (deliberate weakest-web-mode definition)
  - mode_switch:
      require_two_phase_confirmation: true
      require_reason: true
      require_checkpoint: true
      reject_on_checkpoint_failure: true
  - production_gate:
      require_all_reports_passing: true
      forbid_critical_high: true
      report_count: 13
  - restore_validation:
      must_verify:
        - database_state
        - test_data_removed
        - points_chain_hash
        - cloud_drive_metadata_hash
        - config_hash
        - integrity_manifest_hash
      on_failure: enter_incident_lockdown
  - mode_transition:
      forbid:
        - from: incident_lockdown
          to: superweak
  - mode_switch_logs:
      append_only: true
      delete_forbidden: true
      update_forbidden: true

data_scope:
  production: real
  maintenance: real_restricted
  incident_lockdown: read_mostly
  internal_test: shadow
  test: isolated
  dev_ready: dev_local
  superweak: ephemeral

dev_ready:
  csrf: true
  ssl: true
  rate_limit: true
  ip_blocking: false
  login_violation: false
  audit_chain: false
  integrity_guard: false
  browser_only_mode: false
  feature:
    audit_log: true
    economy: false
    trading: false
  data_scope: dev_local

test:
  csrf: true
  ssl: true
  rate_limit: true
  ip_blocking: true
  login_violation: true
  audit_chain: true
  integrity_guard: non_strict
  browser_only_mode: false
  feature:
    audit_log: true
    economy: true
    trading: false
  constraints:
    - must_not_use_production_wallet: true
    - must_not_use_production_ledger: true
    - must_run_in_isolated_runtime: true
  data_scope: isolated

internal_test:
  csrf: true
  ssl: true
  rate_limit: true
  ip_blocking: true
  login_violation: true
  audit_chain: true
  integrity_guard: strict
  browser_only_mode: true
  feature:
    audit_log: true
    economy: true
    trading:
      mode: shadow_only
      production_write: forbidden
  access:
    require_tester_token: true
    reject_if_not_internal_test_mode: true
  data_scope: shadow

maintenance:
  csrf: true
  ssl: true
  rate_limit: true
  ip_blocking: true
  login_violation: true
  audit_chain: true
  integrity_guard: non_strict
  maintenance_mode: true
  browser_only_mode: optional
  feature:
    audit_log: true
    economy: true
    trading: false
    comfyui: false
    games: false
  data_scope: real_restricted

incident_lockdown:
  csrf: true
  ssl: true
  rate_limit: true
  ip_blocking: true
  login_violation: true
  audit_chain: true
  integrity_guard: strict
  maintenance_mode: true
  browser_only_mode: enforced_public
  feature:
    trading: false
    comfyui: false
    games: false
  constraints:
    - forbid_mode_switch_to_superweak: true
  data_scope: read_mostly

production:
  csrf: true
  ssl: true
  rate_limit: true
  ip_blocking: true
  login_violation: true
  audit_chain: true
  integrity_guard: strict
  browser_only_mode: true
  allow_register: false
  feature:
    audit_log: true
    economy: true
    trading: configurable
    comfyui: configurable
  gate:
    require_13_reports: true
  data_scope: real

superweak:
  csrf: false           # superweak is the deliberate weakest web mode (red-team / fuzz / pentest)
  ssl: false
  rate_limit: false
  ip_blocking: false
  login_violation: false
  audit_chain: false
  integrity_guard: false
  browser_only_mode: false
  feature:
    audit_log: false
    economy: false
    trading: false
  constraints:
    - must_create_snapshot_before_entry: true
    - must_discard_all_changes_on_exit: true
    - allow_register:
        allowed: true
        must_not_persist: true
  data_scope: ephemeral

routing:
  condition:
    if:
      mode: internal_test
      role: tester
    then:
      route_to: shadow_tables
  else:
    route_to: production
  enforcement:
    - tester_token_invalid_in_other_modes: true

isolation:
  cache:
    key_format:
      - prod: "wallet:prod:{id}"
      - test: "wallet:test:{id}"
      - shadow: "wallet:tester:{tester_id}:{id}"
  background_jobs:
    must_isolate_by_mode: true
  queues:
    separate_namespaces: true

tokens:
  internal_test_login_token:
    purpose: login
    scope: UI access
  tester_token:
    purpose: API access
    scope: /api/tester/*
    forbidden:
      - /api/admin/*
      - /api/server-mode/*
      - /api/snapshot/*

qa_checks:
  - tester_write_does_not_affect_production_wallet
  - tester_write_does_not_affect_points_chain
  - shadow_data_is_isolated_per_tester
  - production_mode_rejects_tester_token
  - restore_failure_enters_incident_lockdown
  - mode_switch_requires_checkpoint
  - superweak_exit_discards_all_changes
```

> **工程語意速記（root 拍板）**：
> ```
> production         = 真實世界（可對外）
> maintenance        = 真實世界（暫停服務做手術）
> incident_lockdown  = 真實世界（緊急封鎖）
> internal_test      = 模擬真實世界（但資料隔離）
> test               = 測試場（可亂打 API）
> dev_ready          = 開發場（寫 code 用）
> superweak          = 無保護實驗室（可破壞）
> ```
