# Production Gate 驗收紀錄（2026-05-09，歷史）

> **歷史註記（2026-07-30）**：本紀錄反映當時 report gate 會阻止 production entry 的行為。現行版本保留同一份報告驗證與顯示，但改為 advisory；明確 `GO_LIVE` 不再由 report 結果阻止。

這份文件不是規格書，而是一次完整 production gate 驗收的實作紀錄。目的有兩個：

1. 讓後續 root / QA 能照同一條流程重做一次，不必重新猜測操作順序。
2. 把這次實際遇到的坑留下來，避免只看單元測試就誤判「已驗完」。

搭配閱讀：

- [03_production_gate_playbook.md](./03_production_gate_playbook.md)
- [SERVER_MODE_V2_PROFILE_MATRIX.md](./SERVER_MODE_V2_PROFILE_MATRIX.md)
- [11_QA_TESTING.md](../11_QA_TESTING.md)

## 驗收目標

驗證三件事必須同時成立：

1. `runtime/reports/security/production_gate/*.json` 的 filesystem auto-detect 只會把 **最新已驗證且 target 一致** 的報告當成可信來源。
2. 13 份 production gate reports 不完整、未驗證、target 不一致時，`/api/root/server-mode/requirements` 必須維持 `ok=false`，`/api/root/production/enter` 必須拒絕。
3. 13 份 reports **全部 verified 且 target_commit / target_branch / server_mode 一致** 時，才允許切到 `production`，而且 production profile 要實際套用。

## 本次使用的隔離環境

- 專案副本：`/tmp/hackme_web_qa_gate_20260509_092620/hackme_web`
- 驗收 runtime：`/tmp/hackme_web_qa_gate_client_runtime`
- 驗收站：`https://127.0.0.1:50861`
- 驗收證據：`/tmp/hackme_web_qa_gate_client_runtime/reports/server_mode_gate_client_20260509T102355Z`

後續又補了一輪真正收斂 `live target_commit mismatch` 的驗收，使用：

- 專案副本：`/tmp/hackme_web_gate_verify7_50909/hackme_web`
- 驗收站：`https://127.0.0.1:50909`
- 證據：`/tmp/hackme_web_gate_verify7_50909/hackme_web/runtime/reports/server_mode_gate_live_20260509T161241Z`

注意：

- 這次所有 13 份 gate 操作都在 `/tmp` runtime 內進行。
- 不碰正式 repo runtime，也不依賴正式 5000 port。

## 實際操作流程

### 1. 先製造壞情境

先故意在 `runtime/reports/security/production_gate/` 放入：

- unsigned report
- invalid JSON
- `report_type` mismatch
- replay 舊 commit 的 signed report

預期：

- `requirements.ok = false`
- failed card 會帶出可讀 warning reason
- `production enter` 拒絕

本次結果：

- `01_warnings_requirements.json`
- `01_warnings_enter.json`

### 2. 再測「13 份中有 1 份 unverified」

13 份檔案都存在，但故意讓其中 1 份 `signature_valid=false`。

預期：

- `requirements.ok = false`
- `missing = []`
- `failed` 只列那份 unverified report
- `production enter` 仍拒絕

本次結果：

- `02_one_unverified_requirements.json`
- `02_one_unverified_enter.json`

### 3. 再測「13 份都已簽，但其中 1 份是舊 commit replay」

這一關要證明「只看有簽章還不夠」，target 也必須一致。

預期：

- `trust_level = verified`
- 但 `target_match = false`
- `target_verification_reason = target_commit_mismatch`
- `production enter` 拒絕

本次結果：

- `03_one_old_commit_requirements.json`
- `03_one_old_commit_enter.json`

### 4. 最後放入 13 份全對報告

條件：

- signature 驗證通過
- `target_commit`、`target_branch`、`server_mode` 全部與當前 runtime 一致
- 13 份都 `pass`

預期：

- `requirements.ok = true`
- `failed = []`
- `production enter` 成功
- current mode 轉成 `production`

本次結果：

- `04_all_green_requirements.json`
- `04_all_green_enter.json`
- `05_final_mode.json`

## 這次實際遇到的問題

### 問題 1：舊 DB PASS 會被新的 FAIL 漏接

舊邏輯曾出現這個錯誤場景：

- DB 內已有 verified PASS
- filesystem 有較新的 FAIL
- API 卻仍吃 DB PASS

這次已修成：

- unsigned / unverified 新檔：不可蓋過 DB verified report
- verified 且 target 完全一致的新檔：可依較新時間接管

### 問題 2：filesystem auto-detect 容易被誤當成可信來源

如果只看 `mtime`，會出現這些風險：

- touch 一個新檔就蓋過 DB
- invalid JSON 也被當最新
- `report_type` mismatch 仍被當成同類 report
- 舊 commit replay 仍讓 gate 轉綠

這次修正後：

- filesystem report 預設 `trust_level=unverified`
- 只有 `_verify_production_report_signature()` 成功才會升級成 `verified`
- `verified` 永遠優先於 `unverified`
- old commit / branch / mode mismatch 只顯示 warning，不得放行

### 問題 3：live 驗收站曾出現 SQLite lock 與讀取失敗

這不是 production gate 規則 bug，而是驗收過程中碰到的整合坑：

- `production_requirements()` 補 retry 時漏掉 `import time`
- `get_current_mode()` 沒 retry，`security-center` 會因 SQLite lock 回 500

修正後：

- `production_requirements()` 可正確 retry
- `get_current_mode()` 也會在 lock 時短暫重試
- 50861 站的上線前檢查頁可正常讀取

## 前端應看到什麼

當 report 不可信時，前端不應顯示成綠燈，而應帶 warning reason，例如：

- `unsigned_report`
- `invalid_report_json`
- `report_type_mismatch`
- `target_commit_mismatch`
- `target_branch_mismatch`
- `server_mode_mismatch`
- `未驗證報告(unverified)`

這些 reason 來自：

- `services/snapshots/server_mode.py`
- `public/js/50-admin.js`

## 驗收證據索引

位於：

- `/tmp/hackme_web_qa_gate_client_runtime/reports/server_mode_gate_client_20260509T102355Z`

主要檔案：

- `SUMMARY.md`
- `00_context.json`
- `00_login.json`
- `01_warnings_requirements.json`
- `01_warnings_enter.json`
- `02_one_unverified_requirements.json`
- `02_one_unverified_enter.json`
- `03_one_old_commit_requirements.json`
- `03_one_old_commit_enter.json`
- `04_all_green_requirements.json`
- `04_all_green_enter.json`
- `05_final_mode.json`

## 收斂結論

這次驗收後，可以把 production gate 視為：

1. **Filesystem auto-detect 只輔助顯示，不是單獨可信來源。**
2. **未驗證 / 錯類型 / 舊 commit / 無效 JSON 的報告，只能形成 warning。**
3. **13 份 verified 且 target 一致，才是唯一可切到 production 的條件。**

## 補充：live `50909` target-commit mismatch 收斂

前一輪 client/runtime 驗收確認了規則方向，但真正 live server 還缺最後一個 gap：
`test_for_develop.sh` 啟動時若把 `HTML_LEARNING_GIT_REPO_DIR` 指到無 `.git` 的
`/tmp` copy，server 端看到的 current target commit 會變空值。

後續修正後，在 `https://127.0.0.1:50909` 的 live 驗收結果是：

1. `verified + old/fake target_commit` 的 13 份 reports：
   - `requirements ok = false`
   - `production enter = rejected`
   - reason 明確顯示 `target_commit_mismatch`
2. `verified + current target_commit` 的 13 份 reports：
   - `requirements ok = true`
   - `production enter = 200`
   - mode 成功切到 `production`

live 證據檔：

- `00_target.json`
- `03_one_old_commit.json`
- `04_all_green.json`
- `05_final_mode.json`
- `FINAL_REPORT.md`

這輪也順便證明：

- `production requirements API`
- `production enter API`
- filesystem report loader
- DB verified report loader

都已使用同一個 current target commit 判定來源。

## 補充：live `5000` focused happy-path + lock 驗收

focused live 驗收已在隔離副本完成，證據位於：

- `/tmp/hackme_web_dev_20260509_171716_15908/hackme_web/runtime/reports/server_mode_gate_live_20260509T092856Z`

結論：

1. 13 份 signed reports 放入 canonical filesystem 後，`requirements ok=true`。
2. 同批 reports 上傳 DB 後，即使 canonical filesystem 被較新的 invalid JSON 壞檔覆蓋，gate 仍回退到 DB verified report。
3. `POST /api/root/production/enter` 成功，mode 切到 `production`。
4. production 後 root 預設帳號會被要求改密，改密後可重新登入。
5. 針對 `/api/root/server-mode/requirements` 與 `/api/admin/security-center` 做並發 hammer，未出現 `database is locked`。

## 補充：live `5000` full-generator 驗收

這輪不是 focused payload，而是把 13 類 production report 的**真實 generator**
全部跑完，再驗 gate 與 `GO_LIVE`。

隔離副本與證據：

- 專案副本：`/tmp/hackme_web_dev_20260509_171716_15908/hackme_web`
- 驗收站：`https://127.0.0.1:5000`
- full-generator 證據：`/tmp/hackme_web_dev_20260509_171716_15908/hackme_web/runtime/reports/server_mode_gate_full_generators_20260509T115237Z`

最終結論：

1. 13 類 generator 全部完成，最後 `generators_failed = []`。
2. `run_target_meta` 與 `final_target_meta` 一致，這輪沒有再出現 target commit drift。
3. 13 份 reports 都驗到：
   - `report_type_correct=true`
   - `target_commit_correct=true`
   - `target_branch_correct=true`
   - `server_mode_correct=true`
   - `signature_valid=true`
   - `trust_level_verified=true`
4. `old commit`、`report_type mismatch`、`invalid JSON` 仍不會放行；同一輪證據保留在各 report 子目錄的 `04/05/06/11/12/13` 檔案。
5. 真正擋住第一輪 `GO_LIVE` 的唯一原因是 `integrity_guard`：
   - 隔離副本在驗收前已更新了 `routes/system_admin.py`、`run_functional_smoke.sh`、`on_live_reports_make.py`、`full_generator_live_validate.py`、`chess_seed_train.py` 等檔
   - integrity scan 正確報出 pending findings
   - approve 這批 expected findings 後，再 rerun integrity report 即可轉綠
6. approve findings 後：
   - `98_final_requirements_after_integrity_review.json` 顯示 `ok=true`
   - `100_enter_production_after_integrity_review.json` 顯示 `http_status=200`
   - `106_mode_after_production_password_rotation.json` 與 `107_security_center_after_production_password_rotation.json` 皆為 `200`
7. production profile 最終實值符合預期：
   - `allow_register=False`
   - `audit_chain_enabled=True`
   - `browser_only_mode_enabled=True`
   - `integrity_guard_strict_mode=True`
   - `server_ssl_enabled=True`
8. 這輪 server log 掃描 `database is locked` 計數為 `0`。

這輪也補出兩個 harness 級修正：

- `scripts/security/gate/full_generator_live_validate.py`
  - 固定整輪 `target_commit/target_branch`
  - 每一份 report 之前 refresh root session，避免長跑後半段 upload 變 `401 未登入`
- `scripts/security/gate/on_live_reports_make.py`
  - integrity `bulk-review` 與 `rescan` 前先刷新 CSRF，避免 report 已變綠但 helper 因 `csrf_invalid` 仍把 payload 簽成 fail

### full-generator 實際產製順序

這輪在隔離副本實際採用的順序如下：

1. 先登入 root，保存 `00_browser_login.json`，再抓 `01_starting_mode.json` 確認起點仍在 `development`。
2. 重設 gate 狀態與帳號密碼相關前置條件，保存 `03_account_reset.json` 與 `04_gate_state_reset.json`。
3. 在整輪開始時凍結 target，保存 `05_run_target_meta.json`，後續 13 份 report 都以這個 `target_commit/target_branch` 為準，不允許中途漂移。
4. 逐項產製 13 類真實 generator 報告。每一類都先保存 raw generator output，再正規化、簽章、上傳 DB，最後回讀 requirements 驗證 gate 對這份 report 的判定。
5. 若 `integrity_guard` 因隔離副本內的預期變更先失敗，先 review pending findings，再 rerun integrity report，之後再做 final requirements 與 `GO_LIVE`。
6. 在所有 13 類都轉綠後，保存 `90_final_requirements_before_go_live.json`，再呼叫 `92_enter_production.json`。
7. production 後再做 browser login、強制改密、security-center 與 DB settings 對帳，最後保存 `97_final_target_meta.json` 與 `SUMMARY.json`。

### full-generator 每份 report 要看哪些證據

除了根目錄的 `SUMMARY.json` / `SUMMARY.md`，每一類 report 都在
`reports/<report_type>/` 下保留同一套最小證據：

- `00_context.json`
  這份 report 當下的 base URL、mode、target 與 driver context。
- `00_session_refresh_before_generator.json`
  長跑前重新登入 root 的結果。若後面 upload 失敗，先看這份是否已失效。
- `01_raw_report.json`
  generator 的原始輸出。判斷問題是在 generator 本身還是 gate 包裝層，先看這份。
- `02_normalized_payload.json`
  上傳到 gate 前的正規化 payload。
- `03_signature_metadata.json`
  簽章與摘要資訊，確認 `signature_valid` 對應的是哪一版 payload。
- `04_preupload_old_commit_requirements.json`
  舊 commit 報告不得放行的檢查。
- `05_preupload_report_type_mismatch_requirements.json`
  `report_type` mismatch 不得放行的檢查。
- `06_preupload_invalid_json_requirements.json`
  invalid JSON 不得放行的檢查。
- `07_preupload_real_requirements.json`
  真實 payload 在 upload 前的 requirements snapshot。
- `08_upload_response.json`
  DB upload API 回應。`401`、`403`、schema 錯誤都先看這份。
- `09_db_row_verification.json`
  回讀 DB record，確認 `report_type`、`target_commit`、`target_branch`、`server_mode`、`trust_level=verified`。
- `10_postupload_requirements.json`
  真實 verified DB report 上傳後的 gate 狀態。
- `11_postupload_old_commit_requirements.json`
  上傳後再驗一次 old commit 不得放行。
- `12_postupload_report_type_mismatch_requirements.json`
  上傳後再驗一次 `report_type` mismatch 不得放行。
- `13_postupload_invalid_json_requirements.json`
  上傳後再驗一次 filesystem 壞檔不得蓋掉 verified DB report。
- `14_validation_summary.json`
  這一類 report 的總結。先判斷是否是 generator fail、upload fail、DB verify fail，直接看這份最快。

`integrity_guard` 另有 rerun 證據，因為這輪第一個 `GO_LIVE` blocker 就在這裡：

- `15_pending_findings_before_review.json`
- `16_bulk_review_response.json`
- `17` 到 `23` 的 rerun 組
- `24` 到 `28` 的 rerun2 組

### full-generator 除錯方法

- `upload_response.json` 出現 `401 未登入`
  先看 `00_session_refresh_before_generator.json`，再回頭檢查 harness 是否在每份 report 前 refresh root session。
- `upload_response.json` 出現 `403` 或 `csrf_invalid`
  先看 `03_signature_metadata.json` 是否對的是同一份 payload；若是 integrity review 流程，確認 helper 是否在 `bulk-review` / `rescan` 前重新取 CSRF。
- 某份 report raw output 已經是綠的，但 `14_validation_summary.json` 仍是 fail
  先比對 `01_raw_report.json`、`02_normalized_payload.json`、`08_upload_response.json`、`09_db_row_verification.json`，通常是包裝層欄位或登入態失效，不是 generator 本身壞掉。
- requirements 一直 `ok=false`
  先看各 report 的 `14_validation_summary.json`，再看根目錄 `90_final_requirements_before_go_live.json` 或 `98_final_requirements_after_integrity_review.json`，可以直接定位是哪一類 report 還未滿足 gate。
- 懷疑 target commit 漂移
  比對 `05_run_target_meta.json` 與 `97_final_target_meta.json`。若不同，這輪驗收無效，因為一部分 report 不是對同一個 target 產生。
- `integrity_guard` 單獨卡住
  先看 `reports/integrity_guard/15_pending_findings_before_review.json`。若裡面是這輪預期修改的 protected files，就先 review/approve，再看 `16_bulk_review_response.json`、`23_rerun_validation_summary.json` 或 `28_rerun2_postupload_requirements.json` 是否轉綠。
- 懷疑 security-center 顯示值和 production DB 不一致
  先比對 `95_security_center_after_go_live.json` / `103_security_center_after_integrity_review_go_live.json` 與 `96_security_center_db_settings.json` / `104_security_center_db_settings_after_integrity_review_go_live.json`，不要只看單一 API payload。
- 懷疑上線後 operator flow 有問題
  依序看 `93_browser_login_after_go_live.json`、`94_mode_after_go_live.json`、`105_browser_login_after_production_password_rotation.json`、`106_mode_after_production_password_rotation.json`、`107_security_center_after_production_password_rotation.json`。
- 懷疑 SQLite 又互鎖
  這輪最後是掃 isolated runtime 的 `server.log`，確認 `database is locked` 計數為 `0`。只看 API `200` 不夠，仍要掃 log。

同一天又補了一輪更貼近 root 日常操作的 focused live 驗收，直接用隔離
`test_for_develop.sh` 站做完整 happy-path：

- 專案副本：`/tmp/hackme_web_dev_20260509_171716_15908/hackme_web`
- 驗收站：`https://127.0.0.1:5000`
- 驗收證據：
  `/tmp/hackme_web_dev_20260509_171716_15908/hackme_web/runtime/reports/server_mode_gate_live_20260509T092856Z`

這輪刻意驗四件事：

1. **13 份 signed filesystem reports 就能先讓 launch-check 讀到綠燈。**
   - `02_requirements_files_only.json`：`ok=true`
   - 證明 `runtime/reports/security/production_gate/<report_type>_report.json`
     的 auto-detect 會接受「已驗簽 + target 一致」的檔案。
2. **同一批 13 份上傳進 DB 後，較新的壞 filesystem 檔不會蓋掉 verified DB record。**
   - `04_requirements_after_upload.json`：`ok=true`
   - 接著把 `stress_report.json` 改成 invalid JSON
   - `05_requirements_after_tamper.json` 仍然 `ok=true`
   - `stress.id` 變成 DB 的 `prodrep_*`，`trust_level=verified`
3. **全部通過後才允許 `GO_LIVE`。**
   - `07_enter_production.json`：HTTP `200`，`ok=true`
   - `17_mode_after_password_change.json`：`current_mode=production`
4. **production profile 與 post-production operator flow 符合預期。**
   - `13_browser_login_before_password_change.json`：root 首次登入被要求改密
   - `15_password_change.json`：改密成功
   - `16_browser_relogin_after_password_change.json`：用新密碼重新登入成功
   - `18_security_center_after_password_change.json`：audit/readiness/anomaly 全部正常

### focused 驗收的設定檢查

`security_center` payload 本身沒有回傳全部 `system_settings` 鍵，因此只看
`19_production_profile_check_after_password_change.json` 會看到 4 個 `null`
mismatch：

- `allow_register`
- `captcha_mode`
- `production_single_account_ip_lock_enabled`
- `production_single_ip_account_lock_enabled`

這不是 production 沒套上，而是該 endpoint 沒把這四個值帶出來。實際查
runtime DB 的 `system_settings`，值為：

- `allow_register=False`
- `captcha_mode=math`
- `production_single_account_ip_lock_enabled=False`
- `production_single_ip_account_lock_enabled=False`
- `audit_chain_enabled=True`
- `browser_only_mode_enabled=True`
- `integrity_guard_strict_mode=True`
- `server_ssl_enabled=True`

也就是 production hardening 實際已生效。

對應證據檔：

- `19_production_profile_check_after_password_change.json`
- `23_system_settings_db_check.json`
- `24_final_summary.json`

### focused 驗收的 SQLite lock 結果

production 後用瀏覽器 UA 對兩條 launch-check 關鍵 API 做並發 hammer：

- `GET /api/root/server-mode/requirements`
- `GET /api/admin/security-center`

結果：

- `20_lock_hammer_after_password_change.json`
  - `total_requests=120`
  - `failure_count=0`
- `21_server_lock_scan_after_password_change.json`
  - `lock_line_count=0`

也就是這輪 focused live 驗收中，**沒有再出現 `database is locked`**，而且
browser-only + 強制改密的 production operator flow 也可正常完成。

如果之後再改：

- `production_requirements()`
- `_latest_production_report_file_record()`
- `_normalize_production_report_record()`
- `_prefer_newer_production_report_record()`
- root 後台上線前檢查卡片文案

就必須重跑這份驗收流程，不可只看單元測試。
