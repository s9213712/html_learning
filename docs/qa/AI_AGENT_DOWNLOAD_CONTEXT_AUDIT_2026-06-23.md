# AI Agent Download Context Audit - 2026-06-23

## Scope

This audit covers AI Agent natural-language handling for the downloader features on the live `:5000` runtime:

- Direct download creation.
- BT/magnet download creation.
- Download progress lookup without write execution.

The test used the real frontend and real AI Agent planner. Final write execution was intercepted at `/api/ai-agent/write-tools/execute` to avoid creating real download jobs.

## Live Artifacts

- Initial pass with hidden issues: `/tmp/hackme_ai_agent_download_context_20260623_v1/report.json`
- After write gate and alias fix: `/tmp/hackme_ai_agent_download_context_20260623_after_gate/report.json`
- After site-config retry fix: `/tmp/hackme_ai_agent_download_context_20260623_after_site_retry/report.json`
- Final screenshot: `/tmp/hackme_ai_agent_download_context_20260623_after_site_retry/ai_agent_download_context.png`

## Findings

- PASS: Direct download natural-language request selected `write_remote_download_direct`.
- PASS: Direct download payload used canonical `url` and `filename`.
- PASS: BT/magnet natural-language request selected `write_remote_download_bt`.
- BUG FIXED: the first BT run selected the right tool but emitted `magnet_uri` instead of required `url`. Backend write-tool dispatch now normalizes `magnet_uri`, `magnet`, `torrent_url`, `download_url`, and `source_url` into `url` before required-field validation.
- BUG FIXED: the first BT run returned `execute_write=false`, but the frontend still executed `write_tool`. The frontend now refuses to execute any `write_tool` plan unless `execute_write=true`.
- PASS: after planner prompt tightening, the BT request emitted `execute_write=true` and canonical `url`.
- PASS: "查一下目前下載器和遠端下載任務進度，不要新增下載" routed to readonly `remote_download` and did not execute any write tool.
- BUG FIXED: repeated probes exposed a transient `site config load failed TypeError: Failed to fetch` console error during frontend startup. `loadSiteConfig()` now retries once and logs a warning instead of a browser error for non-critical site-config load failure.

## Final Verification

Final live checks:

```text
direct_download_wrote_direct_tool: PASS
bt_download_wrote_bt_tool: PASS
status_did_not_write: PASS
no_browser_errors: PASS
```

Focused suite:

```text
python3 -m py_compile scripts/testing/ai_agent_download_context_probe.py routes/ai_agent.py services/ai_agent/hermes.py tests/ai_agent/test_ai_agent_routes.py tests/frontend/admin/test_frontend_ai_agent.py
node --check public/js/00-core.js
node --check public/js/37-ai-agent.js
pytest tests/ai_agent/test_ai_agent_routes.py::test_ai_agent_remote_download_bt_aliases_magnet_uri_to_url tests/frontend/admin/test_frontend_ai_agent.py tests/ai_agent/test_hermes_client.py -q
```

Result: 39 passed.

