"""Frontend checks for ComfyUI history apply/rerun actions."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_history_apply_returns_to_generate_form_and_reports_errors():
    js = _read("public/js/36-comfyui.js")

    assert "function comfyuiHistoryItemId(item)" in js
    assert "item?.id ?? item?.history_id ?? item?.historyId" in js
    assert "function setComfyuiHistoryActionMessage" in js
    assert 'setComfyuiView("generate");' in js
    assert "ComfyUI 歷史套回表單失敗" in js
    assert "找不到這筆 ComfyUI 歷史紀錄，請重新整理歷史。" in js


def test_history_rerun_opens_generate_view_for_visible_progress():
    js = _read("public/js/36-comfyui.js")

    assert "async function rerunComfyuiHistory(historyId)" in js
    assert "這筆 ComfyUI 歷史缺少可重跑 ID，請重新整理歷史。" in js
    assert 'API + `/comfyui/history/${encodeURIComponent(targetId)}/rerun`' in js
    assert "setComfyuiMessage(\"正在建立 ComfyUI 重跑工作...\", true);" in js


def test_history_delete_removes_legacy_and_workflow_runs():
    js = _read("public/js/36-comfyui.js")
    runtime_routes = _read("routes/comfyui_sections/runtime_routes.py")

    assert 'data-comfyui-history-delete="${sanitize(historyId)}"' in js
    assert "async function deleteComfyuiHistory(historyId)" in js
    assert "這筆 ComfyUI 歷史缺少可刪除 ID，請重新整理歷史。" in js
    assert 'API + `/comfyui/workflow-runs/${encodeURIComponent(workflowRunId)}`' in js
    assert 'API + `/comfyui/history/${encodeURIComponent(legacyId)}`' in js
    assert 'method: "DELETE"' in js
    assert '@app.route("/api/comfyui/history/<int:history_id>", methods=["DELETE"])' in runtime_routes
    assert '@app.route("/api/comfyui/workflow-runs/<int:run_id>", methods=["DELETE"])' in runtime_routes
