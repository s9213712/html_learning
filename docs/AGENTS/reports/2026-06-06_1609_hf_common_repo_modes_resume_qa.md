# 2026-06-06 16:09 HF common repo / multi-mode resume QA

## 結論

未發現 HF/Diffusers common repo 與 repo mode 偵測的產品回歸。當機前最後提到的重點「同一個 Hugging Face repo 可能同時支援 t2i/i2i/t2t/i2t，而不是只屬於單一模式」已完成：

- 後端會累積 `supported_modes`，可同時回傳 `txt2img`, `img2img`, `t2t`, `i2t` 等模式。
- 後端另外回傳 `runnable_modes`，只列本站目前 HF/Diffusers 生圖器可直接執行的 `txt2img`, `img2img`, `inpaint`。
- 前端會顯示「可用模式」與必要時的「本站可執行」，避免把可辨識但不可直接執行的 t2t/i2t 當成可生圖模式。

2026-06-06 19:43 CST 續跑 04 測試時，Drive bulk 的 move/share/delete/download journey 本體已確認通過；但 Playwright 報告另外抓到首輪 `/api/cloud-drive/remote-download/tasks` 500。已修正 Job Center schema-ready cache 在未提交 transaction/rollback 後誤判 ready 的 race，並補上回歸測試。

2026-06-06 21:05 CST 收尾補驗證：

- `stabilityai/sdxl-turbo` 已重跑 i2i 偵測，前端來源圖卡可見，狀態會提示 repo 支援圖生圖，上傳來源圖後可切換；API 回傳 `runnable_modes=["txt2img","img2img"]`。
- HF 常用清單與多模式偵測對 `FLUX.1-schnell`, `Qwen/Qwen-Image`, `Qwen/Qwen-Image-Edit`, `Tongyi-MAI/Z-Image-Turbo` 通過 API/UI 探測。
- `Qwen/Qwen2.5-VL-3B-Instruct` 以 `image-text-to-text` 辨識為 `i2t`；`openai/gpt-oss-20b` 辨識為文字模型能力。兩者不列入可直接執行的 HF 生圖 common repo。
- `circlestone-labs/Anima-Base-v1.0-Diffusers` 只有 `modular_model_index.json`、沒有 `model_index.json`；本站目前不支援 ModularPipeline，因此維持不可執行並顯示不相容警告。
- 臨時伺服器已從 `/tmp` 副本啟動驗證：listen `0.0.0.0:5000`、不受 Host allowlist 阻擋、`root/root` 登入成功，且 `restart_develop_server.sh` 只產生在實際 runtime 目錄。
- 依 21:xx 追加需求，root 的 Civitai / model import 面板在 local、remote、Diffusers 模式都常駐可見；遠端模式文案明確說明檔案寫入本站設定的 ComfyUI models 目錄。Hugging Face Token 快速設定已從 HF 生圖表單移到右上角螺母的 AI 產圖設定。
- Release ID 已改成分支辨識格式：04 分支使用 `04_2026.06.06-001`，並更新 release policy / pre-push hook 以保留 `04_` / `05_` 前綴。

2026-06-06 23:xx CST 追加 UI 收尾：

- 使用者實測指出 AI 後端設定切到 HF 時仍混入 ComfyUI 相關欄位，且前台不容易看到 Civitai。已將 gear 的 `HF` family 限定為 Hugging Face / Diffusers 欄位；`Civitai API Key`、ComfyUI Account API Key、batch/default-size 等 ComfyUI 專屬欄位只留在 `ComfyUI` family。
- root 前台新增 `Civitai / 模型匯入` 快捷按鈕，並將模型子頁籤改名為 `Civitai / 模型管理`。Civitai 仍是 ComfyUI 模型匯入工具，會在 ComfyUI 本地/遠端模式常駐可見；HF active surface 與 HF 設定 family 不再混入 ComfyUI/Civitai 專屬欄位。
- Live Playwright smoke 抓到切到 HF active surface 後 `Civitai / 模型匯入` 快捷按鈕仍沿用上一個 ComfyUI surface 的 visible 狀態；已補 `setComfyuiView()` 內的 root-panel visibility refresh。
- Release ID 往前推進為 `04_2026.06.06-003`。

## 已補驗證

- 新增 `tests/comfyui/test_diffusers_client.py::test_huggingface_diffusers_metadata_accumulates_multiple_pipeline_capabilities`
  - 覆蓋同一 repo 同時帶有 `text-to-image`, `image-to-image`, `text2text-generation`, `image-to-text` metadata。
  - 預期 `supported_modes == ["txt2img", "img2img", "t2t", "i2t"]`。
- 新增 `tests/comfyui/test_diffusers_client.py::test_huggingface_diffusers_repo_inspection_keeps_multi_mode_supported_and_runnable`
  - 覆蓋 inspect API 保留所有 `supported_modes`，但將 `runnable_modes` 限縮為 `["txt2img", "img2img"]`。
  - 驗證非本站可執行模式會出現在 warning。

## 本輪命令結果

- `./scripts/testing/pytest_in_tmp.sh -q tests/comfyui/test_diffusers_client.py`
  - 44 passed。
  - 1 個 `huggingface_hub` deprecation warning，非本次功能回歸。
- `./scripts/testing/pytest_in_tmp.sh -q tests/frontend/comfyui/test_comfyui_diffusers_repo_ui.py`
  - 16 passed。
- `python3 -m py_compile services/comfyui/huggingface.py services/comfyui/settings.py`
  - passed。
- `node --check public/js/36-comfyui.js`
  - passed。
- `timeout 180s python3 scripts/testing/playwright_deep_site_check.py --runtime-root /tmp/hackme_web_04_drive_bulk_20260606_qa2 --only-drive-bulk`
  - Drive bulk journey 本體全部 PASS：
    - `drive_bulk_selection_desktop_ui`
    - `drive_bulk_selection_mobile_ui`
    - `drive_bulk_move_desktop`
    - `drive_bulk_share_desktop`
    - `drive_bulk_delete_desktop`
    - `drive_bulk_download_desktop`
  - 報告 `ok: true`，但程序 exit 1，原因是 browser console 捕捉到 unrelated `500 /api/cloud-drive/remote-download/tasks`。
  - Evidence: `/tmp/hackme_web_04_drive_bulk_20260606_qa2/reports/qa/playwright_deep_site_check_20260606T112843Z.md`。
- `./scripts/testing/pytest_in_tmp.sh -q tests/platform/test_job_center.py`
  - 初跑暴露日期敏感測試：`test_stale_empty_resumable_upload_jobs_are_expired` 的 fixed `2026-05-01` 已在 2026-06-06 超過 31-day clamp。
  - 已改為相對時間後重跑：14 passed。
- `./scripts/testing/pytest_in_tmp.sh -q tests/storage/test_cloud_drive_attachments.py -k remote_download_task`
  - 6 passed。
- `python3 -m py_compile services/job_center.py`
  - passed。
- `python3 -m py_compile services/comfyui/huggingface.py services/comfyui/settings.py services/job_center.py routes/comfyui_sections/admin_helpers.py services/platform/release_info.py`
  - passed。
- `node --check public/js/36-comfyui.js`
  - passed。
- `node --check public/js/50-admin.js`
  - passed。
- `bash -n test_for_develop.sh hooks/pre-push scripts/storage/setup_transmission_backend.sh`
  - passed。
- Targeted pytest after the Civitai/HF-token/release-prefix changes:
  - `tests/platform/test_release_policy.py tests/platform/test_job_center.py`: 17 passed。
  - `tests/comfyui/test_diffusers_client.py tests/comfyui/test_comfyui_settings_defaults.py`: 46 passed，1 個 `huggingface_hub` deprecation warning。
  - `tests/frontend/comfyui/test_comfyui_diffusers_repo_ui.py tests/frontend/comfyui/test_comfyui_workflow_template_ui.py tests/frontend/comfyui/test_comfyui_idle_retry.py`: 44 passed。
  - `tests/comfyui/civitai/test_comfyui_civitai.py`: 14 passed。
  - `tests/frontend/storage/test_frontend_drive_preview.py`: 14 passed。
  - `tests/frontend/users/test_profile_friends_frontend.py`: 1 passed。
  - `tests/frontend/layout/test_ui_polish.py`: 3 passed。
  - `tests/scripts/prepush/test_prepush_v2.py`: 28 passed。

## Resume 前已完成的相關證據

- 相關 targeted pytest：
  - `tests/comfyui/test_diffusers_client.py`
  - `tests/comfyui/test_comfyui_settings_defaults.py`
  - `tests/frontend/comfyui/test_comfyui_diffusers_repo_ui.py`
  - `tests/frontend/comfyui/test_comfyui_idle_retry.py`
  - `tests/frontend/storage/test_frontend_drive_preview.py`
  - `tests/frontend/users/test_profile_friends_frontend.py`
  - selected ComfyUI integration tests
  - 結果：80 passed。
- Member probe:
  - `/tmp/hackme_web_qa_continue_20260606_1548/member_probe/member_probe.json`
  - 17/17 passed，包含 login、drive preview/share/E2EE、album password share、video password playback、remote URL guard、trading grid fee math。
- HF common repo Playwright probe:
  - `/tmp/hackme_web_qa_continue_20260606_1548/hf_common_repo_probe/result.json`
  - passed。確認內建 common repos 存在、`stabilityai/sd-turbo` 顯示 `文字生圖 / 圖生圖`、custom common repo add/remove 正常。

## 未完成或限制

- Live ComfyUI remote 在本輪 WSL 環境不可用：
  - `192.168.18.18:8188` 連線失敗。
  - `127.0.0.1:8188` 出現 connection reset。
  - 因此本輪沒有把 HF repo 實際送到 live ComfyUI 生成。
- `scripts/testing/playwright_deep_site_check.py` 的 full-site run 曾出現 drive/video UI failures。
  - `drive_bulk_share_desktop` 已確認主要是 QA script race：script 讀取 album 狀態早於前端 `shareAlbum()` PUT 完成。
  - 已調整 script 讓 drive toolbar share/move/delete action 直接 await 對應前端 async function。
  - 後續 `--only-drive-bulk` 已有一輪完成 bulk 本體並確認 share/move/delete/download PASS。
  - 續跑時另發現並修正 Job Center schema cache race：`expire_stale_cloud_remote_download_jobs()` 可能在表被 rollback 後跳過 schema ensure，導致首次 `/api/cloud-drive/remote-download/tasks` 500。
  - 修正後的 Playwright 重跑仍受本 WSL/dev-server harness 穩定性影響：
    - `/tmp/hackme_web_04_drive_bulk_20260606_qa3` 無 remote-download 500/traceback，但在第一個 upload 後超過 180s timeout。
    - `/tmp/hackme_web_04_drive_bulk_20260606_qa4` server 未於腳本 45s ready window 內 ready。
  - `video_share_journey` 的 deep script selector 失敗，但 member probe 已通過 video password share/unlock/playback。

## 待追蹤

- 若需要 Playwright strict green，先提高 `wait_for_server()` ready window，並替 `fetch_multipart()` 加單次 timeout/diagnostic，再重跑 `scripts/testing/playwright_deep_site_check.py --only-drive-bulk`。
- 等本機或遠端 ComfyUI `8188` 恢復後，再做一次 live HF/Diffusers repo generation smoke。
