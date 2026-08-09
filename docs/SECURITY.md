# Security Measures — hackme_web

若你是第一次接手部署或要先看整體安全模型，先讀
[06_SECURITY_MODEL.md](06_SECURITY_MODEL.md)。本文件保留目前已啟用保護的細節與
已知限制。

## 目的與邊界
本文件記錄目前已啟用的保護，並保留仍需持續改善的項目。

## 密碼與 Session
- **Argon2id**：`time_cost=3`、`memory_cost=65536KB`、`parallelism=4`，密碼雜湊不儲存明文。
- **Fernet**：加密並簽章 session token（`session_token` 僅存於 HttpOnly cookie）。
- **Session TTL**：預設 4 小時。
- **登出**：刪除 session 與 csrf cookie。

## CSRF 與請求保護
- CSRF token 可由 `GET /api/csrf-token` 取得；未登入時回傳 anonymous token，登入後回傳目前 session 的 token。
- Token 為 session-scoped，同一個登入 session 期間保持穩定，不會在每次頁面載入或每次 API 請求後消耗。
- Token rotate 時機：登入成功、登出、session 重新建立、密碼變更、角色/權限提升。
- 登入後的 authenticated token rotate 會保留少量近期 token，避免同一 session 的多分頁、
  舊前端或併發請求在短時間內互相打掉 token 並產生誤報；未登入 / 登入流程 token 仍維持
  single-use 行為。
- 只有 `POST`、`PUT`、`PATCH`、`DELETE` 需要 CSRF；`GET`、`HEAD`、`OPTIONS` 不要求 token。
- 前端應透過 `apiFetch()` 或 `X-CSRF-Token` header 傳送 token；後端亦接受表單欄位 `csrf_token`。禁止把 token 放在 URL query string。
- CSRF 驗證失敗固定回 `HTTP 403` 與 `{ "ok": false, "error": "csrf_invalid", "message": "CSRF token expired or invalid" }`，前端 `apiFetch()` 會刷新 token 並重試一次。
- 排查 `csrf_invalid`：先呼叫 `/api/csrf-token` 檢查是否能取得目前 session token；若登出、改密碼、權限提升或多分頁舊頁面仍使用太舊 token，重新整理或讓 `apiFetch()` 自動刷新。

## 速率與封鎖
- 註冊：60 秒 10 次
- 登入：60 秒 30 次
- 過多登入失敗會寫入 `security_events`，可觸發封鎖。
- 環境變數 `IP_BLOCKING_ENABLED=true` 時啟用封鎖。

## 反列舉與時序對策
- `USER` 與 `bad password` 回應保持一致訊息。
- `fake hash` + 固定延遲抖動，降低 timing oracle。

## 審計與完整性
- 審計日誌以 `secure_audit` 寫入 hash-chain。
- 違規計次也維持 hash-chain，支援 integrity 驗證。
- 兼容 legacy repo-root `logs/audit.log` fallback（舊資料可視）；目前正式路徑是 `$HACKME_RUNTIME_DIR/logs/`。
- Integrity Guard 會以簽章 `$HACKME_RUNTIME_DIR/integrity_manifest.json` 監控受保護程式檔。
  啟動與 root rescan 會比對 modified / added / deleted，建立
  `integrity_findings`，不會自動更新 manifest。
- root approve 必須輸入 `APPROVE INTEGRITY UPDATE`；approve / reject /
  ignore 都寫入 audit log。
- `ROOT_INTEGRITY_SIGNING_KEY` 應保存於部署 secret，不可提交到 git。
  若 manifest 簽章錯誤，系統會建立 high risk finding。
- Integrity Guard 可偵測未授權修改，但不能取代 git、CI/CD、備份或主機
  安全；已完全取得主機 root 的攻擊者仍可能干擾 runtime。

## 輸入驗證
- 帳號：3–32 字元，`[a-zA-Z0-9_-]`
- 密碼：8–128 字元，需含大小寫與符號
- 個人欄位有對應格式驗證（`real_name`、`id_number`、`birthdate`、`phone`）

## HTTP 安全
- Flask-Talisman：CSP, HSTS, referrer policy 等
- 限制 `X-Frame-Options`, `nosniff`, `nosniff`, `X-Permitted-Cross-Domain-Policies`

## 已知限制與待做
- 管理與 runtime 路由已拆到 `routes/system_admin_sections/` 等模組，`server.py` 主要負責 app 組裝與相依注入；新增管理功能仍應放進對應 route/service 模組，避免重新集中。
- pre-push 已包含結構、文件、靜態安全與測試檢查；仍應定期檢視動態載入邊界與新外部整合的最小權限設定。
