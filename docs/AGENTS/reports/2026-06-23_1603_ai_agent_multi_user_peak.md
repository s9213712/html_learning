# AI Agent Multi-User Peak QA

Date: 2026-06-23 16:03 Asia/Taipei
Target: `https://127.0.0.1:5000`
Checkout: `/home/s92137/hackme_web_05_AI_Agent`

## Confirmed Findings

1. **Design limit: `write` mode blocks non-root chat**
   - Evidence: `/tmp/ai_agent_multi_user_peak_probe_round3.json`
   - Current `ai_agent_operation_mode=write` is intentionally root-only for chat according to existing tests.
   - Non-root users receive HTTP 403 with clear message: `AI Agent 目前為執行寫入模式，僅 root 可執行。`
   - Impact: multi-user AI Agent chat cannot operate while root has the global agent in `write` mode. This is safe by design, but it means write mode is not a normal multi-user service mode.

2. **Backpressure protects the server during multi-user AI chat peak**
   - Evidence: `/tmp/ai_agent_assist_mode_peak_probe.json`
   - Procedure: temporarily switched AI Agent from `write` to `assist`, ran 8 logged-in users concurrently, then restored `write`.
   - Result: 4/8 requests completed with HTTP 200; 4/8 were rejected with HTTP 503.
   - The 503 response was explicit: `目前是流量高峰，伺服器正在保護服務品質。請稍候 2 秒後再試。`
   - No gunicorn worker crash was observed; `/api/version` kept responding.

3. **User conversation storage isolation passed**
   - Evidence: `/tmp/ai_agent_multi_user_peak_probe_round3.json`
   - 8 users each saved a private marker under the same conversation id.
   - Each user loaded only their own marker; `cross_leaks=[]`.

4. **Permission isolation passed**
   - Evidence: `/tmp/ai_agent_multi_user_peak_probe_round3.json`
   - Non-root users were denied:
     - `/api/ai-agent/write-tools`
     - `/api/ai-agent/write-tools/execute`
     - `/api/ai-agent/conversation-history`
   - Root could list 74 write tools and view conversation history.

5. **System filesystem isolation passed**
   - Evidence: `/tmp/ai_agent_multi_user_peak_probe_round3.json`
   - Requests to list `/home/s92137` or modify server files returned HTTP 403 before LLM execution.
   - Error message clearly states the station boundary: use site cloud drive/runtime/authorized tools instead of OS filesystem.

6. **Frontend root final exam passed as readonly route**
   - Evidence: `/tmp/ai_agent_multi_user_peak_probe_round3.json`
   - Natural language prompt: `幫用戶完成上線前檢查：檢查 requirements gate、server mode、log chain、AI agent audit 狀態，逐項回報。`
   - Planner chose `action=readonly`, `readonly_scope=all`, confidence `0.95`.
   - Frontend reported progress states: planning, readonly loading, completed.
   - Response included server mode, launch requirements, ComfyUI status, resources, and ComfyUI jobs.

## Performance Snapshot

Assist-mode peak test:

- Users: 8
- Concurrency: 12 worker threads in the probe, 8 total AI chat requests
- HTTP 200: 4
- HTTP 503: 4
- Total tokens reported: 10,618
- Latency p95: 7.405s
- Fastest 503: 0.017s
- Gunicorn RSS before/after remained stable enough for continued service.

Root final-exam frontend test:

- Planner latency: 19.834s
- Total frontend case latency: 22.866s
- Tool path: chat planner -> readonly all -> optional ComfyUI status/resources/models

## Notes

- The initial peak attempt in `write` mode is not a valid LLM capacity test because current policy blocks non-root chat at the AI Agent layer.
- The test temporarily switched to `assist` only for capacity measurement and restored `write` afterward. The final restored mode is recorded in `/tmp/ai_agent_assist_mode_peak_probe.json`.
- During test account creation, the 9th and 10th attempted users received HTTP 403 with no parsed JSON message in the probe. A later manual reproduction using a new username returned HTTP 200, so this is recorded as an intermittent observation, not a confirmed reproducible bug.

## Evidence Files

- `/tmp/ai_agent_multi_user_peak_probe.py`
- `/tmp/ai_agent_multi_user_peak_probe_round3.json`
- `/tmp/ai_agent_assist_mode_peak_probe.py`
- `/tmp/ai_agent_assist_mode_peak_probe.json`
