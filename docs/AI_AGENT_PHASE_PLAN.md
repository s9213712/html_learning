# AI Agent 分階段實施計畫（04 → 05）

## Phase 1：角色與只讀能力定義（後端）

- 目標：完成個別用戶 / 管理者 / 超級管理者權限邏輯，並加入只讀能力 API。
- 範圍：
  - `routes/ai_agent.py`：`readonly` 端點、角色歸一化、權限判斷、資源/任務快照欄位。
  - `services/ai_agent/hermes.py`：`public_ai_agent_settings` + prompt 文字中帶入角色/任務邊界。
  - 設定 `settings.py`/`settings_metadata.py` 加入 persona、任務開關預設值與欄位描述。
- 成品：`/api/ai-agent/readonly` 可回傳角色對應的只讀摘要。

## Phase 2：前端 UI 與只讀資料渲染

- 目標：AI 助理頁面顯示 `scope`/`權限`/`資源`/`下載與任務快照`。
- 範圍：
  - `public/index.html`：新增只讀資訊面板欄位佔位。
  - `public/js/37-ai-agent.js`：載入 `readonly` payload、渲染。
  - `public/styles.css`：補上對應版面樣式。
- 成品：在 AI Agent 分頁可直接看到個人任務進度與權限視圖。

## Phase 3：驗證與上傳 05

- 目標：新增路由級測試、最小前端驗證更新、分批提交推送到 `05.AI_Agent`。
- 範圍：
  - `tests/ai_agent/test_ai_agent_routes.py`：`status`/`readonly` 權限與資料回傳測試。
  - `tests/frontend/admin/test_frontend_ai_agent.py`（若有新 DOM/API）同步擴充。
  - 以 2~3 個 commit 逐段推到 `05.AI_Agent`。
