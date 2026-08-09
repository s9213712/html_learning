# Production Readiness Playbook — Optional Reports

> **目的**：把 server 切到 `production` 前，可選擇產生並驗證完整的 readiness report set，讓操作者有可追溯的風險證據。
> 本檔給操作者一張 step-by-step 對照表：每份 report 是什麼、怎麼產、怎麼上傳、怎麼驗 status、失敗怎麼辦。
>
> **切換規則**：report 結果只作建議，不阻止 root 明確輸入 `GO_LIVE` 的 mode switch；權限、CSRF、確認字串、Integrity Guard、checkpoint 與 mode-switch log-chain 的安全處置仍照常強制。
>
> **依據**：[`SERVER_MODE_V2_PROFILE_MATRIX.md §Production Readiness Reports`](SERVER_MODE_V2_PROFILE_MATRIX.md#production-readiness-reports) + `services/snapshots/schema.py::PRODUCTION_REQUIRED_REPORT_TYPES`。截至 2026-06-23，report set 為 14 份。

---

## 0. 前置認知

每份 report 必須符合 `services/snapshots/server_mode.py:upload_production_report` 的所有檢查，否則 reject：

| 必填欄位 | 規則 |
|---|---|
| `report_type` | 必須 ∈ §1 required report 白名單 |
| `raw_report` | 原始報告 JSON；伺服器會對它做 canonical JSON 後重算 hash |
| `report_hash` | 格式 `sha256:<64 hex>`，且必須等於 `sha256(canonical_json(raw_report))` |
| `target_commit` | 受測 commit hash（git log 得） |
| `target_branch` | 受測 branch 名 |
| `server_mode` | 受測時 server 在哪個 mode（多數 = `dev_ready` 或 `test`） |
| `test_result` | 必須是 `pass` 或 `passed` |
| `passed` | 必須 `true`（boolean）|
| `critical_findings_count` | 必須 `0` |
| `high_findings_count` | 必須 `0` |
| `unresolved_findings` | 必須空陣列 |
| `tester` | 跑測試的人員 / 系統 ID |
| `signature` | 必須是 `hmac_sha256:<hex>`，伺服器會用 production report key 驗簽 |
| `key_version` | 對應簽章用的 key version |

**Replay 防護**：同 `(report_type, report_hash, target_commit)` 三元組已存在 → reject，避免複用過期 artifact。
**可信驗證**：缺少 `raw_report`、`report_hash` 與 `raw_report` 不一致、`signature` 驗證失敗、`key_version` 不符的 report 不會被標為可信。
**Filesystem auto-detect 不是信任來源**：`runtime/reports/security/production_gate/*.json` 只作為後台頁面與 API 的輔助顯示來源。這些檔案預設 `trust_level=unverified`；只有 `_verify_production_report_signature()` 驗證成功，且 `target_commit` / `target_branch` / `server_mode` 與當前 runtime 完全一致時，才會升級成 `verified`。
**舊檔 / 偽造檔警告**：unsigned、invalid JSON、`report_type` mismatch、replay 舊 commit、target 不一致等 filesystem 報告都只應顯示 warning，不能覆蓋資料庫裡已驗證的 report；它們也不會成為切換授權。

**Commit 對齊**：required report set 的 `target_commit` 必須**全部相同**才算「對同一個 commit 都通過」。任一份 commit hash 不一致 → 視為過期，重跑該份。

**Live regression 必測**：不能只靠單元測試確認 target 規則。至少要在隔離
`/tmp` runtime 實測一次：

1. 製造完整 required report set `verified` 但 `target_commit=old/fake` 的 reports。
2. 驗 `GET /api/root/server-mode/requirements` 仍然 `ok=false`。
3. 驗 `POST /api/root/production/enter` 在明確 `GO_LIVE` 下仍可切換，但回應保留
   `production_requirements.ok=false` 與 advisory，明確顯示 `target_commit_mismatch`。
4. 再製造完整 required report set `verified + current target_commit` 的 reports，確認
   `requirements ok=true` 且 `production enter` 成功。

實際已跑過的 live 範例見：
- [04_production_gate_validation_report.md](./04_production_gate_validation_report.md)

---

## 1. Required Report 對照表

| # | report_type | 用途 | Generator | 預期 artifact |
|---|---|---|---|---|
| 1 | `clean_smoke` | Server Mode v2 乾淨 smoke：boot path / state-machine / 基線 endpoints | `scripts/security/server_mode/server_mode_v2_clean_smoke.py` | JSON 結果檔 |
| 2 | `adversarial` | Mode v2 對抗測試：injection / bypass / mode-spoof | `scripts/security/server_mode/server_mode_v2_adversarial.py` | JSON 結果檔 |
| 3 | `redteam_l2` | Mode v2 red-team Level 2 攻擊樹 | `scripts/security/server_mode/server_mode_v2_redteam_l2.py` | JSON 結果檔 |
| 4 | `pytest` | 全專案 pytest pass | `scripts/testing/pytest_in_tmp.sh -q tests` | pytest junit XML 或 stdout log |
| 5 | `log_chain_verify` | `mode_switch_logs` + audit chain hash 完整性 | 走 `/api/admin/health/audit-chain` 驗 + `services/server_mode_v2_log_chain_verify` 自寫 | JSON 結果檔 |
| 6 | `integrity_guard` | IntegrityGuard 自檢、無 high-risk finding | `python -c "from server import integrity_guard; print(integrity_guard.run_self_check())"` 或 `/api/admin/integrity/repair?dry_run=true` | JSON 結果檔 |
| 7 | `stress` | 流量 / trading 壓測 | `scripts/security/pentest/stress_test.py` + `scripts/security/pentest/trading_stress_pentest.py` | JSON 結果檔 |
| 8 | `permission` | role / permission pentest | `scripts/security/pentest/functional_permission_pentest.py` | JSON 結果檔 |
| 9 | `functional` | 全功能 smoke | `scripts/security/pentest/run_functional_smoke.sh` 或 `tests/security/smoke/smoke_suite.py` | JSON 結果檔 |
| 10 | `pentest` | 安全滲透測試 | `scripts/security/pentest/run_pentest.sh` + `scripts/security/pentest/session_security_pentest.py` | JSON 結果檔 |
| 11 | `snapshot_restore` | snapshot/restore regression | `tests/snapshots/test_snapshots.py` 全綠 + 手動跑 1 次 create→restore→verify | JSON 結果檔 |
| 12 | `points_chain_consistency` | PointsChain 一致性 | `tests/points/test_points_chain.py` + `services/points_chain.verify_chain()` | JSON 結果檔 |
| 13 | `cloud_drive_quota_permission` | Cloud Drive quota & 權限 | `tests/storage/test_cloud_drive_attachments.py` + `tests/storage/test_storage_albums_schema.py` | JSON 結果檔 |
| 14 | `ai_agent_boundary` | AI Agent tool / filesystem / launch-preflight 邊界 | `scripts/on_live_reports/ai_agent_boundary.py`；不呼叫 LLM | JSON 結果檔 |

> **附註**：1–6 號是 Server Mode v2 phase 2 cut 才加的「平台層」報告；7–13 號是更早的 functional / security domain 報告；14 號是 AI Agent 寫工具與站內邊界擴張後新增的 deterministic gate。

---

## 2. 產 report 的標準流程

每份 report artifact 應該是個 JSON 檔，至少含：

```json
{
  "report_id": "<uuid 或 timestamp+hex>",
  "report_type": "clean_smoke",
  "generated_at": "2026-05-05T11:00:00Z",
  "target_commit": "9273da5a...",
  "target_branch": "03b.strategy_workflow",
  "server_mode": "dev_ready",
  "test_result": "pass",
  "passed": true,
  "critical_findings_count": 0,
  "high_findings_count": 0,
  "unresolved_findings": [],
  "tester": "claude-acc-2026-05-05",
  "signature": "hmac_sha256:..."
}
```

**簽章**用內部 HMAC key（root 持有）對 JSON canonical form 計算。範例（pseudo）：

```bash
artifact_path=/tmp/report.json
hash=$(sha256sum "$artifact_path" | awk '{print $1}')
signature=$(openssl dgst -sha256 -hmac "$REPORT_SIGN_KEY" "$artifact_path" | awk '{print $2}')
```

`report_hash` 上傳時填 `sha256:$hash`；`signature` 填 `hmac_sha256:$signature`。

---

## 3. 上傳流程

### 3.1 確認 server 不在 production / incident_lockdown

upload report 不需要 server 已經在 production；通常是在 `dev_ready` 或 `test` 跑出 report，再上傳。但 `incident_lockdown` 期間禁止上傳（會被 mode policy 擋）。

### 3.2 用 root 帳號 + CSRF + POST

```bash
csrf=$(curl -sk -c jar -b jar "$BASE_URL/api/csrf-token" | jq -re '.csrf_token')

curl -sk -b jar -H "Content-Type: application/json" -H "X-CSRF-Token: $csrf" \
  -X POST "$BASE_URL/api/root/production-report/upload" \
  -d "$(jq -n \
    --arg rt clean_smoke \
    --arg rh "sha256:$hash" \
    --arg tc "$target_commit" \
    --arg tb "$target_branch" \
    --arg sm "dev_ready" \
    --arg tr "pass" \
    --arg ts "claude-acc-2026-05-05" \
    --arg sig "hmac_sha256:$signature" \
    --arg c "$csrf" \
    '{
      report_type: $rt, report_hash: $rh,
      target_commit: $tc, target_branch: $tb,
      server_mode: $sm, test_result: $tr, passed: true,
      critical_findings_count: 0, high_findings_count: 0,
      unresolved_findings: [],
      tester: $ts, signature: $sig,
      csrf_token: $c
    }')"
```

成功會回 `{"ok": true, "report_id": "prodrep_..."}`。

### 3.3 驗證 status

```bash
curl -sk -b jar "$BASE_URL/api/root/production-report/status" | jq
```

回傳會列出 required report type 中各自是否「最新通過」。任一 type `latest_passing == false` → 必須補。

```json
{
  "ok": true,
  "required": ["clean_smoke", "adversarial", ..., "ai_agent_boundary"],
  "status": {
    "clean_smoke": {"latest_passing": true, "latest_report_id": "prodrep_..."},
    "adversarial": {"latest_passing": false, "reason": "no report uploaded yet"},
    ...
  },
  "ready_for_production": false
}
```

### 3.4 選用：報告全綠後再 enter production

```bash
csrf=$(curl -sk -c jar -b jar "$BASE_URL/api/csrf-token" | jq -re '.csrf_token')
curl -sk -b jar -H "Content-Type: application/json" -H "X-CSRF-Token: $csrf" \
  -X POST "$BASE_URL/api/root/production/enter" \
  -d "{\"confirm\":\"GO_LIVE\",\"reason\":\"prod cut for 2026-05-05 release\",\"csrf_token\":\"$csrf\"}"
```

---

## 4. 失敗排錯

### 4.1 「report_type 不在 production gate 清單」
typo；確認名稱在 §1 required report set 之一。

### 4.2 「report_hash 必須是 sha256:<64 hex>」
格式：`sha256:` + `64 hex chars`，缺一不可。

### 4.3 「production report 必須明確 pass」
- `test_result` 必須是 `pass` 或 `passed`（小寫）
- `passed` 必須 `true`（boolean，不是字串）

### 4.4 「不允許 critical/high finding」
report 內部如果還有 critical 或 high finding（`*_findings_count > 0` 或 `unresolved_findings` 非空），就先把 issue 處理掉再重跑、重產、重上傳。

### 4.5 「production report replay detected」
同 `(report_type, report_hash, target_commit)` 已上傳過。
- 改了東西 → `target_commit` 會變
- 沒改 → 直接複用既有 report；不需重上傳
- 想強制重新上傳 → 重新跑 generator 拿到新的 hash

### 4.6 required set 中部分 commit 不一致
全部 required reports 的 `target_commit` 應該是同一個。最常見原因：commit 動了之後沒重跑全套；補跑該份就好。
查方法：

```bash
curl -sk -b jar "$BASE_URL/api/root/production-report/status" \
  | jq -r '.status | to_entries[] | "\(.key): \(.value.target_commit // "MISSING")"'
```

只挑跟主流不同的 commit 重跑。

如果是 live 驗收，要另外確認：

- 這個 `target_commit` 來源和 live server 自己看到的 `current target commit`
  是同一個來源。
- `test_for_develop.sh` 不可把 `HTML_LEARNING_GIT_REPO_DIR` 指向沒有 `.git`
  的 `/tmp` copy，否則 old commit 驗證會退化成空 target 判定。

### 4.7 server 在 incident_lockdown 不能 enter production
先解 incident：

```bash
curl -sk -b jar "$BASE_URL/api/root/incident/status" | jq
# 解決事故後
csrf=$(curl -sk -c jar -b jar "$BASE_URL/api/csrf-token" | jq -re '.csrf_token')
curl -sk -b jar -H "Content-Type: application/json" -H "X-CSRF-Token: $csrf" \
  -X POST "$BASE_URL/api/root/incident/resolve" \
  -d "{\"reason\":\"<post-incident summary>\",\"csrf_token\":\"$csrf\"}"
```

---

## 5. 完整 required-report 跑表

建議使用真實 orchestrator，而不是手抄舊骨架：

```bash
HACKME_RUNTIME_DIR=/absolute/external/runtime \
HACKME_OPERATIONAL_CAMPAIGN_REPORT=/tmp/<campaign>/reports/operational_campaign_24h.json \
ROOT_PASSWORD='<root-password>' MANAGER_PASSWORD='<manager-password>' TEST_PASSWORD='<test-user-password>' \
python3 scripts/security/gate/on_live_reports_make.py \
  --base-url https://127.0.0.1:5000
```

穩定捷徑是：

```bash
HACKME_RUNTIME_DIR=/absolute/external/runtime \
HACKME_OPERATIONAL_CAMPAIGN_REPORT=/tmp/<campaign>/reports/operational_campaign_24h.json \
ROOT_PASSWORD='<root-password>' MANAGER_PASSWORD='<manager-password>' TEST_PASSWORD='<test-user-password>' \
python3 scripts/on_live_reports/on_live_reports_make.py \
  --base-url https://127.0.0.1:5000
```

所有單項 wrapper 登錄在 `scripts/INDEX.md`，列表在 `scripts/on_live_reports/README.md`。需要單獨重跑某份時，用：

```bash
python3 scripts/on_live_reports/ai_agent_boundary.py
```

## 6. 實際行動指南

1. **先看當前狀態**：
   ```bash
   curl -sk -b jar "$BASE_URL/api/root/production-report/status" | jq
   ```
   會列出 required report type 各自最新狀態。
2. 跑 `scripts/on_live_reports/on_live_reports_make.py` 產生完整 required set。
3. 若有 report fail，修正對應 domain 後只重跑該 wrapper，再重新跑 requirements。
4. 完整 set 全綠 + `ready_for_production = true` 後，再 `POST /api/root/production/enter`，confirm phrase = `GO_LIVE`。

---

*Playbook end. 對應 spec：[`SERVER_MODE_V2_PROFILE_MATRIX.md §Production Readiness Reports`](SERVER_MODE_V2_PROFILE_MATRIX.md#production-readiness-reports). 配套腳本：本資料夾 `01_internal_test_login_token.sh` + `02_tester_token_shadow_api.sh`。*
