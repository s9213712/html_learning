"""Prompt queue, progress, and image generation orchestration helpers."""

import json
import inspect
import mimetypes
import os
import time
import urllib.parse
import uuid

from services.comfyui.workflow.final_model_safety import (
    FINAL_MODEL_SAFETY_EXTRA_DATA_KEY,
    FinalModelSafetyError,
    enforce_campaign_final_graph_model_safety,
    final_model_safety_prompt_marker,
    verify_final_model_safety_backend_history_binding,
)
from services.comfyui.semantic_composite import finalize_generation_result_if_needed


QUEUE_TIMEOUT_EXTENSION_SECONDS = 1800
QUEUE_MAX_TIMEOUT_SECONDS = 21600
BACKEND_UNRESPONSIVE_FAIL_SECONDS = max(
    60,
    int(os.environ.get("COMFYUI_BACKEND_UNRESPONSIVE_FAIL_SECONDS", "7200") or "7200"),
)
OUTPUT_REF_KEYS = {
    "images": "images",
    "video": "videos",
    "videos": "videos",
    "gifs": "videos",
    "audio": "audio",
    "audios": "audio",
    "file": "other",
    "files": "other",
}
OUTPUT_NODE_CLASS_PRIORITY = {
    "SaveImage": 0,
    "PreviewImage": 10,
    "SaveVideo": 0,
    "VHS_VideoCombine": 0,
    "SaveAudio": 0,
    "SaveAudioMP3": 0,
    "PreviewAudio": 10,
    "MaskPreview": 90,
}
VIDEO_OUTPUT_CLASS_TYPES = {"SaveVideo", "VHS_VideoCombine"}
AUDIO_OUTPUT_CLASS_TYPES = {"SaveAudio", "SaveAudioMP3", "PreviewAudio"}
PREVIEW_IMAGE_OUTPUT_CLASS_TYPES = {"PreviewImage", "MaskPreview"}
VIDEO_OUTPUT_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".gif"}
AUDIO_OUTPUT_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"}
TRANSIENT_ERROR_MARKERS = (
    "timed out",
    "timeout",
    "temporarily unavailable",
    "connection reset",
    "connection aborted",
    "connection refused",
    "network is unreachable",
    "http 502",
    "http 503",
    "http 504",
    "逾時",
    "連線失敗",
    "暫時",
)


def extract_history_error_message(record):
    status = record.get("status") if isinstance(record, dict) else {}
    messages = status.get("messages") if isinstance(status, dict) else []
    if not isinstance(messages, list):
        return ""
    for item in reversed(messages):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        event_name, payload = item[0], item[1]
        if event_name != "execution_error" or not isinstance(payload, dict):
            continue
        exc_type = str(payload.get("exception_type") or "").strip()
        exc_message = str(payload.get("exception_message") or "").strip()
        node_id = str(payload.get("node_id") or "").strip()
        node_type = str(payload.get("node_type") or "").strip()
        parts = []
        if exc_type or exc_message:
            parts.append(f"{exc_type}: {exc_message}".strip(": "))
        if node_id or node_type:
            parts.append(f"node {node_id or '-'} {node_type or ''}".strip())
        return "；".join(part for part in parts if part)[:1000]
    return ""


def queue_prompt_with_client_id(client, workflow, *, client_id=None, extra_data=None, error_cls):
    client_id = str(client_id or uuid.uuid4().hex)
    try:
        submitted_workflow, safety_receipt = enforce_campaign_final_graph_model_safety(
            workflow,
            backend_url=str(getattr(client, "base_url", "") or ""),
        )
    except Exception as exc:
        if isinstance(exc, FinalModelSafetyError):
            detail = str(exc)
        else:
            detail = f"{exc.__class__.__name__}: {exc}"
        raise error_cls(f"ComfyUI final graph safety rejected before /prompt: {detail}") from exc
    prompt_extra_data = dict(extra_data) if isinstance(extra_data, dict) else {}
    if safety_receipt is not None:
        if FINAL_MODEL_SAFETY_EXTRA_DATA_KEY in prompt_extra_data:
            raise error_cls(
                "ComfyUI final graph safety rejected before /prompt: "
                f"reserved extra_data key collision ({FINAL_MODEL_SAFETY_EXTRA_DATA_KEY})"
            )
        prompt_extra_data[FINAL_MODEL_SAFETY_EXTRA_DATA_KEY] = final_model_safety_prompt_marker(
            safety_receipt
        )
    payload = {"prompt": submitted_workflow, "client_id": client_id}
    if prompt_extra_data:
        payload["extra_data"] = prompt_extra_data
    data = client._json_request("/prompt", method="POST", payload=payload)
    prompt_id = data.get("prompt_id") if isinstance(data, dict) else None
    if not prompt_id:
        raise error_cls("ComfyUI 未回傳 prompt_id")
    return {
        "prompt_id": str(prompt_id),
        "client_id": client_id,
        "final_model_safety": safety_receipt,
        "_submitted_workflow": submitted_workflow,
    }


def queue_prompt(client, workflow, *, extra_data=None, error_cls):
    return queue_prompt_with_client_id(client, workflow, client_id=None, extra_data=extra_data, error_cls=error_cls)["prompt_id"]


def interrupt(client, *, timeout_seconds=None):
    return client._json_request(
        "/interrupt",
        method="POST",
        payload={},
        timeout=timeout_seconds,
        allow_non_json=True,
    )


def get_queue(client, *, timeout_seconds=None):
    return client._json_request(
        "/queue",
        timeout=timeout_seconds,
    )


def delete_queue_items(client, prompt_ids, *, timeout_seconds=None):
    ids = [str(item) for item in (prompt_ids or []) if str(item or "").strip()]
    if not ids:
        return {}
    return client._json_request(
        "/queue",
        method="POST",
        payload={"delete": ids},
        timeout=timeout_seconds,
        allow_non_json=True,
    )


def emit_progress(progress_callback, snapshot):
    if not progress_callback:
        return
    progress_callback({key: value for key, value in dict(snapshot).items() if not str(key).startswith("_")})


def _workflow_node_count(workflow):
    if not isinstance(workflow, dict):
        return 0
    return sum(
        1
        for node in workflow.values()
        if isinstance(node, dict) and str(node.get("class_type") or "").strip()
    )


def _progress_node_id(value):
    text = str(value or "").strip()
    return text or None


def _ensure_total_progress_state(snapshot, *, total_node_count=None):
    nodes = snapshot.setdefault("_node_progress", {})
    if not isinstance(nodes, dict):
        nodes = {}
        snapshot["_node_progress"] = nodes
    if total_node_count is not None:
        try:
            count = int(total_node_count or 0)
        except Exception:
            count = 0
        if count > 0:
            snapshot["_total_node_count"] = max(int(snapshot.get("_total_node_count") or 0), count)
    return nodes


def _mark_progress_node(snapshot, node_id, ratio):
    node_id = _progress_node_id(node_id)
    if not node_id:
        return
    nodes = _ensure_total_progress_state(snapshot)
    try:
        value = float(ratio or 0)
    except Exception:
        value = 0.0
    value = max(0.0, min(1.0, value))
    nodes[node_id] = max(float(nodes.get(node_id) or 0.0), value)


def _total_progress_percent(snapshot):
    nodes = _ensure_total_progress_state(snapshot)
    configured_total = int(snapshot.get("_total_node_count") or 0)
    known_nodes = len(nodes)
    # Without a workflow node count, keep room for at least one later node so a
    # single node's 100% step progress does not masquerade as whole-prompt 100%.
    estimated_total = known_nodes + (0 if snapshot.get("completed") else 1)
    total_nodes = max(configured_total, known_nodes, estimated_total, 1)
    total_ratio = sum(max(0.0, min(1.0, float(value or 0.0))) for value in nodes.values())
    percent = max(0, min(99, round((total_ratio / total_nodes) * 99)))
    previous = int(float(snapshot.get("_last_total_percent") or snapshot.get("percent") or 0))
    if str(snapshot.get("phase") or "").lower() not in {"completed", "error"}:
        percent = max(previous, percent)
    snapshot["_last_total_percent"] = percent
    snapshot["current"] = total_ratio
    snapshot["max"] = total_nodes
    return percent


def _output_ref_key(bucket, item):
    if not isinstance(item, dict):
        return (bucket, "", "", "", "")
    return (
        bucket,
        str(item.get("output_node_id") or ""),
        str(item.get("filename") or ""),
        str(item.get("subfolder") or ""),
        str(item.get("type") or ""),
    )


def _merge_output_collections(existing, found):
    merged = {}
    for bucket in ("images", "videos", "audio", "other"):
        merged[bucket] = []
        seen = set()
        for source in (existing, found):
            values = source.get(bucket) if isinstance(source, dict) else []
            if not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, dict):
                    continue
                key = _output_ref_key(bucket, item)
                if key in seen:
                    continue
                seen.add(key)
                merged[bucket].append(dict(item))
    return merged


def _output_collection_count(collection):
    if not isinstance(collection, dict):
        return 0
    return sum(
        len(collection.get(bucket) or [])
        for bucket in ("images", "videos", "audio", "other")
        if isinstance(collection.get(bucket) or [], list)
    )


def _update_partial_outputs(snapshot, found, prompt_id):
    if not isinstance(found, dict) or not _output_collection_count(found):
        return False
    existing = snapshot.get("partial_outputs") if isinstance(snapshot.get("partial_outputs"), dict) else {}
    previous_count = _output_collection_count(existing)
    merged = _merge_output_collections(existing, found)
    next_count = _output_collection_count(merged)
    if next_count <= previous_count:
        return False
    merged["prompt_id"] = str(prompt_id)
    snapshot["partial_outputs"] = merged
    snapshot["partial_output_count"] = next_count
    snapshot["updated_at"] = time.time()
    return True


def _is_transient_comfy_error(exc):
    message = str(exc or "").strip().lower()
    if not message:
        return False
    return any(marker in message for marker in TRANSIENT_ERROR_MARKERS)


def _queued_deadline(now, *, start_time, deadline, extension_seconds=None, max_seconds=None):
    extension_seconds = QUEUE_TIMEOUT_EXTENSION_SECONDS if extension_seconds is None else extension_seconds
    max_seconds = QUEUE_MAX_TIMEOUT_SECONDS if max_seconds is None else max_seconds
    extension_seconds = max(int(extension_seconds or 0), 0)
    refresh_window = max(5, min(60, extension_seconds // 10 if extension_seconds else 5))
    if deadline - now > refresh_window:
        return deadline, False
    max_deadline = start_time + max(int(max_seconds or 0), 0)
    if max_deadline <= start_time:
        return deadline, False
    extended = min(max_deadline, now + extension_seconds)
    new_deadline = max(deadline, extended)
    return new_deadline, new_deadline > deadline


def apply_ws_message_to_progress(snapshot, message, prompt_id, *, total_node_count=None, workflow=None):
    if not isinstance(message, dict):
        return False
    _ensure_total_progress_state(snapshot, total_node_count=total_node_count)
    msg_type = str(message.get("type") or "")
    data = message.get("data") if isinstance(message.get("data"), dict) else {}
    if data.get("prompt_id") and str(data.get("prompt_id")) != str(prompt_id):
        return False
    updated = False
    snapshot["last_event"] = msg_type
    snapshot["updated_at"] = time.time()
    if msg_type == "status":
        exec_info = data.get("status") if isinstance(data.get("status"), dict) else data.get("exec_info")
        if isinstance(exec_info, dict):
            queue_remaining = exec_info.get("queue_remaining")
            if queue_remaining is not None:
                snapshot["queue_remaining"] = int(queue_remaining)
                updated = True
    elif msg_type == "executing":
        snapshot["phase"] = "running"
        node_id = _progress_node_id(data.get("node") or data.get("display_node_id"))
        previous_node = _progress_node_id(snapshot.get("_active_node"))
        if previous_node and previous_node != node_id:
            _mark_progress_node(snapshot, previous_node, 1.0)
        if node_id:
            snapshot["_active_node"] = node_id
            _mark_progress_node(snapshot, node_id, 0.0)
            snapshot["current_node"] = node_id
            snapshot["node_current"] = 0
            snapshot["node_max"] = 0
            snapshot["node_percent"] = 0
            snapshot["detail"] = f"正在執行節點 {node_id}"
        else:
            snapshot["current_node"] = None
            snapshot["node_current"] = 0
            snapshot["node_max"] = 0
            snapshot["node_percent"] = 0
        snapshot["percent"] = _total_progress_percent(snapshot)
        updated = True
    elif msg_type == "execution_cached":
        snapshot["phase"] = "running"
        nodes = data.get("nodes") if isinstance(data.get("nodes"), list) else []
        for node_id in nodes:
            _mark_progress_node(snapshot, node_id, 1.0)
        snapshot["percent"] = _total_progress_percent(snapshot)
        snapshot["detail"] = f"使用快取節點 {len(nodes)} 個"
        updated = True
    elif msg_type == "progress":
        value = data.get("value")
        maximum = data.get("max")
        if isinstance(value, (int, float)) and isinstance(maximum, (int, float)) and maximum:
            snapshot["phase"] = "running"
            node_ratio = float(value) / float(maximum)
            node_id = _progress_node_id(data.get("node") or data.get("display_node_id") or snapshot.get("_active_node"))
            if node_id:
                snapshot["_active_node"] = node_id
                _mark_progress_node(snapshot, node_id, node_ratio)
            snapshot["node_current"] = float(value)
            snapshot["node_max"] = float(maximum)
            snapshot["node_percent"] = max(0, min(100, round(node_ratio * 100)))
            snapshot["percent"] = _total_progress_percent(snapshot)
            snapshot["current_node"] = node_id
            snapshot["detail"] = f"節點 {node_id or '-'}：{int(value)}/{int(maximum)}"
            updated = True
    elif msg_type == "progress_state":
        nodes = data.get("nodes") if isinstance(data.get("nodes"), dict) else {}
        active_node = None
        for node_id, node in nodes.items():
            if not isinstance(node, dict):
                continue
            if node.get("prompt_id") and str(node.get("prompt_id")) != str(prompt_id):
                continue
            node_max = node.get("max")
            node_value = node.get("value")
            if isinstance(node_max, (int, float)) and float(node_max) > 0:
                node_key = node.get("display_node_id") or node.get("node_id") or node_id
                _mark_progress_node(snapshot, node_key, min(float(node_value or 0), float(node_max)) / float(node_max))
                if active_node is None and float(node_value or 0) < float(node_max):
                    active_node = node
                    active_node["node_id"] = node_key
        if nodes:
            snapshot["phase"] = "running"
            snapshot["percent"] = _total_progress_percent(snapshot)
            if active_node:
                node_label = active_node.get("node_id") or "-"
                snapshot["current_node"] = node_label
                snapshot["detail"] = f"節點 {node_label}：{int(active_node.get('value') or 0)}/{int(active_node.get('max') or 0)}"
            updated = True
    elif msg_type == "executed":
        snapshot["phase"] = "running"
        node_id = _progress_node_id(data.get("node") or data.get("display_node_id"))
        if node_id:
            _mark_progress_node(snapshot, node_id, 1.0)
            snapshot["current_node"] = node_id
        output = data.get("output") if isinstance(data.get("output"), dict) else {}
        if node_id and output:
            found = collect_output_refs({"outputs": {node_id: output}}, workflow=workflow)
            if _update_partial_outputs(snapshot, found, prompt_id):
                count = int(snapshot.get("partial_output_count") or 0)
                snapshot["detail"] = f"已產生 {count} 個輸出，仍在繼續執行"
        snapshot["percent"] = _total_progress_percent(snapshot)
        updated = True
    return updated


def _sorted_output_items(outputs, workflow=None):
    items = list(outputs.items())
    if not isinstance(workflow, dict):
        return items

    def sort_key(indexed_item):
        index, (node_id, _output) = indexed_item
        node = workflow.get(str(node_id)) if isinstance(workflow, dict) else None
        class_type = str((node or {}).get("class_type") or "").strip()
        return (OUTPUT_NODE_CLASS_PRIORITY.get(class_type, 50), index)

    return [item for _index, item in sorted(enumerate(items), key=sort_key)]


def _output_ref_bucket(raw_key, normalized_key, *, filename="", class_type=""):
    class_type = str(class_type or "").strip()
    if class_type in VIDEO_OUTPUT_CLASS_TYPES:
        return "videos"
    if class_type in AUDIO_OUTPUT_CLASS_TYPES:
        return "audio"
    lower_name = str(filename or "").strip().lower()
    extension = ""
    if "." in lower_name.rsplit("/", 1)[-1]:
        extension = "." + lower_name.rsplit(".", 1)[-1]
    if extension in VIDEO_OUTPUT_EXTENSIONS:
        return "videos"
    if extension in AUDIO_OUTPUT_EXTENSIONS:
        return "audio"
    return normalized_key


def _output_ref_mime_type(file_ref, *, bucket="other", fallback="application/octet-stream"):
    filename = ""
    if isinstance(file_ref, dict):
        filename = str(file_ref.get("filename") or "").strip()
    guessed = mimetypes.guess_type(filename)[0]
    if guessed:
        return guessed
    if bucket == "videos":
        return "video/mp4"
    if bucket == "audio":
        return "audio/mpeg"
    return fallback or "application/octet-stream"


def _workflow_suppresses_preview_image_outputs(workflow):
    if not isinstance(workflow, dict):
        return False
    classes = {
        str((node or {}).get("class_type") or "").strip()
        for node in workflow.values()
        if isinstance(node, dict)
    }
    return bool(classes & VIDEO_OUTPUT_CLASS_TYPES) and "SaveImage" not in classes


def collect_output_refs(record, workflow=None):
    outputs = (record or {}).get("outputs") or {}
    found = {"images": [], "videos": [], "audio": [], "other": []}
    seen = set()
    suppress_preview_images = _workflow_suppresses_preview_image_outputs(workflow)
    for _node_id, output in _sorted_output_items(outputs, workflow=workflow):
        if not isinstance(output, dict):
            continue
        source_node_id = str(_node_id)
        source_node = workflow.get(source_node_id) if isinstance(workflow, dict) else None
        source_class_type = str((source_node or {}).get("class_type") or "").strip()
        source_meta = source_node.get("_meta") if isinstance(source_node, dict) and isinstance(source_node.get("_meta"), dict) else {}
        output_label = str(source_meta.get("title") or source_meta.get("label") or "").strip()
        for raw_key, normalized_key in OUTPUT_REF_KEYS.items():
            refs = output.get(raw_key)
            if not isinstance(refs, list):
                continue
            for item in refs:
                if not isinstance(item, dict):
                    continue
                filename = str(item.get("filename") or "").strip()
                if not filename:
                    continue
                bucket = _output_ref_bucket(
                    raw_key,
                    normalized_key,
                    filename=filename,
                    class_type=source_class_type,
                )
                if (
                    bucket == "images"
                    and suppress_preview_images
                    and source_class_type in PREVIEW_IMAGE_OUTPUT_CLASS_TYPES
                ):
                    continue
                payload = {
                    "filename": filename,
                    "subfolder": str(item.get("subfolder") or "").strip(),
                    "type": str(item.get("type") or "output").strip() or "output",
                }
                if isinstance(source_node, dict):
                    payload["output_node_id"] = source_node_id
                if output_label:
                    payload["output_label"] = output_label
                for extra_key in ("format", "frame_rate", "duration", "workflow"):
                    if extra_key in item:
                        payload[extra_key] = item.get(extra_key)
                dedupe = (bucket, source_node_id, payload["filename"], payload["subfolder"], payload["type"])
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                found[bucket].append(payload)
    return found


def wait_for_images(
    client,
    prompt_id,
    *,
    timeout_seconds=1800,
    poll_interval=1.0,
    expected_count=1,
    total_node_count=None,
    websocket_conn=None,
    progress_callback=None,
    error_cls,
    websocket_module=None,
):
    outputs = wait_for_outputs(
        client,
        prompt_id,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        expected_count=expected_count,
        total_node_count=total_node_count,
        websocket_conn=websocket_conn,
        progress_callback=progress_callback,
        error_cls=error_cls,
        websocket_module=websocket_module,
    )
    if not outputs.get("images"):
        raise error_cls("ComfyUI 沒有回傳圖片輸出")
    return outputs["images"]


def wait_for_outputs(
    client,
    prompt_id,
    *,
    timeout_seconds=1800,
    poll_interval=1.0,
    expected_count=1,
    wait_until_completed=False,
    workflow=None,
    total_node_count=None,
    websocket_conn=None,
    progress_callback=None,
    error_cls,
    websocket_module=None,
    final_model_safety_receipt=None,
):
    start_time = time.time()
    timeout_value = max(0, int(timeout_seconds or 0))
    unlimited_timeout = timeout_value <= 0
    deadline = float("inf") if unlimited_timeout else start_time + timeout_value
    last_status = None
    expected = max(1, int(expected_count or 1))
    running_started = False
    snapshot = {
        "prompt_id": str(prompt_id),
        "phase": "queued",
        "percent": 0,
        "current": 0,
        "max": 0,
        "current_node": None,
        "queue_remaining": None,
        "detail": "已送出至 ComfyUI 佇列",
        "completed": False,
        "_total_node_count": int(total_node_count or 0) if total_node_count else 0,
        "timeout_seconds": 0 if unlimited_timeout else timeout_value,
        "timeout_unlimited": unlimited_timeout,
        "timeout_extended": False,
        "updated_at": time.time(),
    }
    next_history_poll = 0.0
    history_error_count = 0
    last_history_error = None
    backend_unresponsive_since = None
    while True:
        now = time.time()
        if snapshot.get("phase") == "running" and not running_started:
            running_started = True
            if not unlimited_timeout:
                deadline = max(deadline, now + timeout_value)
                snapshot["timeout_seconds"] = max(int(snapshot.get("timeout_seconds") or 0), int(deadline - start_time))
            emit_progress(progress_callback, snapshot)
        if not unlimited_timeout and not running_started and snapshot.get("phase") == "queued":
            deadline, extended = _queued_deadline(now, start_time=start_time, deadline=deadline)
            if extended:
                snapshot["timeout_extended"] = True
                snapshot["timeout_seconds"] = max(int(snapshot.get("timeout_seconds") or 0), int(deadline - start_time))
                snapshot["detail"] = "仍在 ComfyUI 佇列中，已自動延長等待時間"
                emit_progress(progress_callback, snapshot)
        if not unlimited_timeout and now >= deadline:
            break
        if websocket_conn is not None and websocket_module is not None:
            for _ in range(20):
                try:
                    raw = websocket_conn.recv()
                except websocket_module.WebSocketTimeoutException:
                    break
                except websocket_module.WebSocketConnectionClosedException:
                    websocket_conn = None
                    break
                except Exception:
                    websocket_conn = None
                    break
                if not isinstance(raw, str):
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if apply_ws_message_to_progress(
                    snapshot,
                    message,
                    prompt_id,
                    total_node_count=total_node_count,
                    workflow=workflow,
                ):
                    emit_progress(progress_callback, snapshot)
        now = time.time()
        if now >= next_history_poll:
            try:
                history = client._json_request(f"/history/{urllib.parse.quote(str(prompt_id))}", timeout=client.timeout)
                history_error_count = 0
                last_history_error = None
                backend_unresponsive_since = None
                snapshot.pop("backend_unresponsive", None)
                snapshot.pop("history_error_count", None)
                snapshot.pop("last_history_error", None)
                snapshot.pop("backend_unresponsive_seconds", None)
            except error_cls as exc:
                if not _is_transient_comfy_error(exc):
                    raise
                history_error_count += 1
                last_history_error = str(exc)
                if backend_unresponsive_since is None:
                    backend_unresponsive_since = now
                backend_silence = max(0, int(now - backend_unresponsive_since))
                snapshot["phase"] = "backend_unresponsive" if running_started else snapshot.get("phase", "queued")
                snapshot["backend_unresponsive"] = True
                snapshot["backend_unresponsive_seconds"] = backend_silence
                snapshot["history_error_count"] = history_error_count
                snapshot["last_history_error"] = last_history_error[:240]
                snapshot["updated_at"] = time.time()
                snapshot["detail"] = (
                    "ComfyUI 暫時沒有回覆結果查詢，可能正在載入大模型或輸出檔案；"
                    "已保留工作並持續補抓結果。"
                )
                emit_progress(progress_callback, snapshot)
                if running_started and backend_silence >= BACKEND_UNRESPONSIVE_FAIL_SECONDS:
                    raise error_cls(
                        "ComfyUI 後端連續 "
                        f"{backend_silence} 秒沒有回覆 history 查詢；已停止等待，請檢查或重啟 ComfyUI 後重試。"
                    )
                next_history_poll = now + max(float(poll_interval), 1.0)
                time.sleep(0.15)
                continue
            record = history.get(prompt_id) if isinstance(history, dict) else None
            if record:
                backend_safety_binding = None
                if final_model_safety_receipt is not None:
                    try:
                        backend_safety_binding = verify_final_model_safety_backend_history_binding(
                            record,
                            final_model_safety_receipt,
                            prompt_id=str(prompt_id),
                        )
                    except Exception as exc:
                        detail = (
                            str(exc)
                            if isinstance(exc, FinalModelSafetyError)
                            else f"{exc.__class__.__name__}: {exc}"
                        )
                        raise error_cls(
                            "ComfyUI final graph safety backend history binding failed: "
                            f"{detail}"
                        ) from exc
                status = record.get("status") or {}
                last_status = status
                if status.get("status_str") == "error" or status.get("completed") is False and status.get("status_str") == "error":
                    detail = extract_history_error_message(record) or "ComfyUI 產圖失敗"
                    raise error_cls(detail)
                found = collect_output_refs(record, workflow=workflow)
                image_count = len(found["images"])
                media_count = len(found["videos"]) + len(found["audio"]) + len(found["other"])
                if (image_count or media_count) and status.get("completed") is not True:
                    if _update_partial_outputs(snapshot, found, prompt_id):
                        count = int(snapshot.get("partial_output_count") or 0)
                        snapshot["phase"] = "running"
                        snapshot["detail"] = f"已產生 {count} 個輸出，仍在繼續執行"
                        emit_progress(progress_callback, snapshot)
                if (image_count or media_count) and status.get("completed") is True:
                    snapshot["phase"] = "completed"
                    snapshot["percent"] = 100
                    snapshot["completed"] = True
                    snapshot["detail"] = f"已完成，輸出 {image_count + media_count} 個檔案"
                    emit_progress(progress_callback, snapshot)
                    result = {**found, "prompt_id": str(prompt_id)}
                    if backend_safety_binding is not None:
                        result["final_model_safety_backend_binding"] = backend_safety_binding
                    return result
                if not wait_until_completed and image_count >= expected:
                    snapshot["phase"] = "completed"
                    snapshot["percent"] = 100
                    snapshot["completed"] = True
                    snapshot["detail"] = f"已完成，共 {len(found['images'][:expected])} 張"
                    emit_progress(progress_callback, snapshot)
                    result = {
                        **found,
                        "images": found["images"][:expected],
                        "prompt_id": str(prompt_id),
                    }
                    if backend_safety_binding is not None:
                        result["final_model_safety_backend_binding"] = backend_safety_binding
                    return result
            next_history_poll = now + float(poll_interval)
        time.sleep(0.15)
    detail_parts = []
    if last_status:
        detail_parts.append(f"最後狀態：{last_status}")
    if last_history_error:
        detail_parts.append(f"最後 ComfyUI API 狀態：{last_history_error}")
    detail = f"；{'；'.join(detail_parts)}" if detail_parts else ""
    raise error_cls(f"ComfyUI 產圖逾時{detail}")


def wait_for_first_image(client, prompt_id, *, timeout_seconds=1800, poll_interval=1.0, error_cls, websocket_module=None):
    return wait_for_images(
        client,
        prompt_id,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        expected_count=1,
        error_cls=error_cls,
        websocket_module=websocket_module,
    )[0]


def _fetch_output_with_retries(fetcher, file_ref, *, error_cls, progress_callback=None, label="輸出檔", max_attempts=3):
    attempts = max(1, int(max_attempts or 1))
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fetcher(file_ref)
        except error_cls as exc:
            last_exc = exc
            if attempt >= attempts or not _is_transient_comfy_error(exc):
                raise
            emit_progress(progress_callback, {
                "phase": "fetching_output",
                "percent": 99,
                "completed": False,
                "backend_unresponsive": True,
                "detail": f"ComfyUI 已完成生成，但{label}讀取暫時失敗，正在重試 {attempt + 1}/{attempts}。",
                "last_output_fetch_error": str(exc)[:240],
                "updated_at": time.time(),
            })
            time.sleep(min(1.5 * attempt, 5.0))
    raise last_exc


def _output_refs_without_preview_data(output_refs, *, prompt_id, fetch_error):
    image_refs = list(output_refs.get("images") or [])
    media_refs = {key: list(output_refs.get(key) or []) for key in ("videos", "audio", "other")}
    warning = str(fetch_error or "ComfyUI output preview fetch failed")[:500]
    primary_ref = image_refs[0] if image_refs else next(item for values in media_refs.values() for item in values)
    return {
        "prompt_id": prompt_id,
        "image_ref": primary_ref,
        "mime_type": "image/png" if image_refs else _output_ref_mime_type(primary_ref, bucket="other"),
        "data": b"",
        "preview_warning": warning,
        "output_fetch_failed": True,
        "images": [
            {
                "image_ref": image_ref,
                "mime_type": "image/png",
                "data": b"",
                "size_bytes": 0,
                "output_node_id": image_ref.get("output_node_id") or "",
                "output_label": image_ref.get("output_label") or "",
                "preview_warning": warning,
            }
            for image_ref in image_refs
        ],
        "media": {
            key: [
                {
                    "file_ref": file_ref,
                    "mime_type": _output_ref_mime_type(file_ref, bucket=key),
                    "data": b"",
                    "size_bytes": 0,
                    "preview_warning": warning,
                }
                for file_ref in values
            ]
            for key, values in media_refs.items()
        },
    }


def generate_from_workflow(
    client,
    workflow,
    *,
    timeout_seconds=1800,
    expected_count=1,
    progress_callback=None,
    extra_data=None,
    fetch_outputs=True,
    wait_until_completed=False,
    error_cls,
    websocket_module=None,
    image_fetcher,
):
    websocket_conn = None
    client_id = uuid.uuid4().hex
    final_model_safety = None
    submitted_workflow = workflow
    try:
        if progress_callback:
            try:
                websocket_conn = client._open_progress_socket(client_id, timeout=min(5, client.timeout))
            except Exception:
                websocket_conn = None
        queued = queue_prompt_with_client_id(
            client,
            workflow,
            client_id=client_id,
            extra_data=extra_data,
            error_cls=error_cls,
        )
        prompt_id = queued["prompt_id"]
        final_model_safety = queued.get("final_model_safety")
        submitted_workflow = queued.get("_submitted_workflow") or workflow
        total_node_count = _workflow_node_count(submitted_workflow)
        safety_marker = (
            final_model_safety_prompt_marker(final_model_safety)
            if final_model_safety is not None
            else None
        )
        queued_progress = {
            "prompt_id": prompt_id,
            "phase": "queued",
            "percent": 0,
            "current": 0,
            "max": 0,
            "total_node_count": total_node_count,
            "current_node": None,
            "queue_remaining": None,
            "detail": "已送出至 ComfyUI 佇列",
            "completed": False,
            "updated_at": time.time(),
        }
        if safety_marker is not None:
            queued_progress["final_model_safety"] = safety_marker
        emit_progress(progress_callback, queued_progress)
        output_refs = wait_for_outputs(
            client,
            prompt_id,
            timeout_seconds=timeout_seconds,
            expected_count=expected_count,
            wait_until_completed=wait_until_completed,
            workflow=submitted_workflow,
            total_node_count=total_node_count,
            websocket_conn=websocket_conn,
            progress_callback=progress_callback,
            error_cls=error_cls,
            websocket_module=websocket_module,
            final_model_safety_receipt=final_model_safety,
        )
    finally:
        try:
            if websocket_conn is not None:
                websocket_conn.close()
        except Exception:
            pass
    if not fetch_outputs:
        image_refs = list(output_refs.get("images") or [])
        media_refs = {key: list(output_refs.get(key) or []) for key in ("videos", "audio", "other")}
        if not image_refs and not any(media_refs.values()):
            raise error_cls("ComfyUI 沒有回傳可用輸出檔")
        primary_ref = image_refs[0] if image_refs else next(item for values in media_refs.values() for item in values)
        result = {
            "prompt_id": prompt_id,
            "image_ref": primary_ref,
            "mime_type": "image/png" if image_refs else "application/octet-stream",
            "data": b"",
            "images": [
                {
                    key: value
                    for key, value in {
                        "image_ref": image_ref,
                        "mime_type": "image/png",
                        "data": b"",
                        "size_bytes": 0,
                        "output_node_id": image_ref.get("output_node_id") or "",
                        "output_label": image_ref.get("output_label") or "",
                    }.items()
                    if value or key in {"image_ref", "mime_type", "data", "size_bytes"}
                }
                for image_ref in image_refs
            ],
            "media": {
                key: [
                    {
                        "file_ref": file_ref,
                        "mime_type": _output_ref_mime_type(file_ref, bucket=key),
                        "data": b"",
                        "size_bytes": 0,
                    }
                    for file_ref in values
                ]
                for key, values in media_refs.items()
            },
        }
        if final_model_safety is not None:
            result["final_model_safety"] = final_model_safety
            result["final_model_safety_backend_binding"] = output_refs.get(
                "final_model_safety_backend_binding"
            )
        return result
    image_refs = list(output_refs.get("images") or [])
    media_refs = {key: list(output_refs.get(key) or []) for key in ("videos", "audio", "other")}
    try:
        images = [
            _fetch_output_with_retries(
                image_fetcher,
                image_ref,
                error_cls=error_cls,
                progress_callback=progress_callback,
                label="圖片",
            )
            for image_ref in image_refs
        ]
        media_outputs = {}
        for media_key in ("videos", "audio", "other"):
            fetcher = getattr(client, "fetch_file", None) or image_fetcher
            media_outputs[media_key] = [
                _fetch_output_with_retries(
                    fetcher,
                    file_ref,
                    error_cls=error_cls,
                    progress_callback=progress_callback,
                    label="媒體輸出",
                )
                for file_ref in media_refs.get(media_key) or []
            ]
    except error_cls as exc:
        if not image_refs and not any(media_refs.values()):
            raise
        emit_progress(progress_callback, {
            "phase": "completed",
            "percent": 100,
            "completed": True,
            "backend_unresponsive": True,
            "preview_warning": str(exc)[:240],
            "detail": "ComfyUI 已完成生成，但預覽檔讀取暫時失敗；已保留輸出檔引用，可稍後重新載入預覽。",
            "updated_at": time.time(),
        })
        fallback = _output_refs_without_preview_data(output_refs, prompt_id=prompt_id, fetch_error=exc)
        if final_model_safety is not None:
            fallback["final_model_safety"] = final_model_safety
            fallback["final_model_safety_backend_binding"] = output_refs.get(
                "final_model_safety_backend_binding"
            )
        return fallback
    if not images and not any(media_outputs.values()):
        raise error_cls("ComfyUI 沒有回傳可用輸出檔")
    primary = images[0] if images else next(item for values in media_outputs.values() for item in values)
    serialized_images = []
    for index, item in enumerate(images):
        ref_meta = image_refs[index] if index < len(image_refs) and isinstance(image_refs[index], dict) else {}
        serialized_images.append({
            "image_ref": {
                "filename": item.filename,
                "subfolder": item.subfolder,
                "type": item.type,
            },
            "mime_type": item.mime_type,
            "data": item.data,
            "output_node_id": ref_meta.get("output_node_id") or "",
            "output_label": ref_meta.get("output_label") or "",
        })
    serialized_media = {
        key: [{
            "file_ref": {
                "filename": item.filename,
                "subfolder": item.subfolder,
                "type": item.type,
            },
            "mime_type": item.mime_type or _output_ref_mime_type({"filename": item.filename}, bucket=key),
            "data": item.data,
        } for item in values]
        for key, values in media_outputs.items()
    }
    result = {
        "prompt_id": prompt_id,
        "image_ref": {
            "filename": primary.filename,
            "subfolder": primary.subfolder,
            "type": primary.type,
        },
        "mime_type": primary.mime_type,
        "data": primary.data,
        "images": serialized_images,
        "media": serialized_media,
    }
    if final_model_safety is not None:
        result["final_model_safety"] = final_model_safety
        result["final_model_safety_backend_binding"] = output_refs.get(
            "final_model_safety_backend_binding"
        )
    return result


def generate_image(
    client,
    params,
    *,
    timeout_seconds=1800,
    progress_callback=None,
    extra_data=None,
    fetch_outputs=True,
    build_generation_workflow_func,
    generate_from_workflow_func=None,
    error_cls,
    websocket_module=None,
    image_fetcher=None,
):
    workflow = build_generation_workflow_func(params)
    expected_count = int(params.get("batch_size") or 1)
    if generate_from_workflow_func is not None:
        kwargs = {
            "timeout_seconds": timeout_seconds,
            "expected_count": expected_count,
            "progress_callback": progress_callback,
        }
        if extra_data:
            try:
                signature = inspect.signature(generate_from_workflow_func)
                accepts_extra_data = (
                    "extra_data" in signature.parameters
                    or any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
                )
            except (TypeError, ValueError):
                accepts_extra_data = True
            if accepts_extra_data:
                kwargs["extra_data"] = extra_data
        try:
            signature = inspect.signature(generate_from_workflow_func)
            accepts_fetch_outputs = (
                "fetch_outputs" in signature.parameters
                or any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
            )
        except (TypeError, ValueError):
            accepts_fetch_outputs = True
        if accepts_fetch_outputs:
            kwargs["fetch_outputs"] = fetch_outputs
        result = generate_from_workflow_func(workflow, **kwargs)
    else:
        if image_fetcher is None:
            raise TypeError("generate_image() requires image_fetcher when generate_from_workflow_func is not provided")
        result = generate_from_workflow(
            client,
            workflow,
            timeout_seconds=timeout_seconds,
            expected_count=expected_count,
            progress_callback=progress_callback,
            extra_data=extra_data,
            fetch_outputs=fetch_outputs,
            error_cls=error_cls,
            websocket_module=websocket_module,
            image_fetcher=image_fetcher,
        )
    return finalize_generation_result_if_needed(client, result, params, error_cls=error_cls)
