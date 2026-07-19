"""Fail-closed model safety for the exact graph submitted to ComfyUI.

The formal campaign materialises several workflows dynamically (GGUF profile
selection, multi-checkpoint comparison, and user input overrides).  Auditing a
template or a parameter object before those rewrites is insufficient: this
module verifies the canonical graph copy that will actually be posted to
``/prompt``.

Enforcement is activated by the campaign-specific backend URL/models-root
environment pair.  If either half is present without the other, submission is
rejected.  Normal non-campaign ComfyUI use is intentionally unaffected by the
formal campaign's immutable 2 GiB/file and 4 GiB/graph limits.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from services.comfyui.workflow.summary import (
    GRAPH_LOADER_DEPENDENCY_INPUTS,
    extract_embedding_names_from_text,
    extract_graph_dependency_contract,
)


GIB = 1024 * 1024 * 1024
FINAL_MODEL_MAX_FILE_BYTES = 2 * GIB
FINAL_WORKFLOW_MODEL_MAX_BYTES = 4 * GIB
FINAL_MODEL_SAFETY_SCHEMA_VERSION = "hackme.comfyui-final-model-safety/v1"
FINAL_MODEL_SAFETY_MARKER_SCHEMA_VERSION = "hackme.comfyui-final-model-safety-marker/v1"
FINAL_MODEL_SAFETY_BACKEND_BINDING_SCHEMA_VERSION = (
    "hackme.comfyui-final-model-safety-backend-binding/v1"
)
FINAL_MODEL_SAFETY_EXTRA_DATA_KEY = "hackme_final_model_safety"
CAMPAIGN_COMFYUI_API_URL_ENV = "HACKME_CAMPAIGN_COMFYUI_API_URL"
CAMPAIGN_COMFYUI_MODELS_ROOT_ENV = "HACKME_CAMPAIGN_COMFYUI_MODELS_ROOT"


class FinalModelSafetyError(RuntimeError):
    """The exact prompt graph cannot be proven safe under campaign caps."""


_GENERIC_MODEL_INPUT_NAMES = frozenset({
    "model", "model_name", "checkpoint", "ckpt", "ckpt_name", "lora", "lora_name",
    "controlnet", "control_net", "control_net_name", "clip", "clip_name", "clip_name1",
    "clip_name2", "clip_name3", "vae", "vae_name", "unet", "unet_name", "encoder",
    "text_encoder",
})


def _looks_like_loader_dependency_input(value: Any) -> bool:
    key = str(value or "").strip().lower()
    return key in _GENERIC_MODEL_INPUT_NAMES or (
        key.endswith("_name")
        and any(
            token in key
            for token in ("model", "checkpoint", "ckpt", "lora", "control", "clip", "vae", "unet", "encoder")
        )
    )


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FinalModelSafetyError(f"final graph is not canonical JSON: {exc}") from exc


def _canonical_workflow_copy(workflow: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    if not isinstance(workflow, Mapping) or not workflow:
        raise FinalModelSafetyError("final graph must be a non-empty object")
    encoded = _canonical_json_bytes(workflow)
    decoded = json.loads(encoded.decode("utf-8"))
    if not isinstance(decoded, dict) or not decoded:
        raise FinalModelSafetyError("final graph canonical copy is not a non-empty object")
    return decoded, hashlib.sha256(encoded).hexdigest()


def _normalise_backend_origin(value: Any, *, label: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise FinalModelSafetyError(f"{label} must be an exact http(s) origin")
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError as exc:
        raise FinalModelSafetyError(f"{label} has an invalid port") from exc
    default_port = 443 if parsed.scheme == "https" else 80
    netloc = host if port in {None, default_port} else f"{host}:{port}"
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def _assert_no_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return
        except OSError as exc:
            raise FinalModelSafetyError(f"cannot lstat dependency path component {current}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise FinalModelSafetyError(f"symlink dependency path is forbidden: {current}")


def _validated_models_root(value: Any) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise FinalModelSafetyError(f"{CAMPAIGN_COMFYUI_MODELS_ROOT_ENV} is required")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise FinalModelSafetyError("campaign ComfyUI models root must be absolute")
    absolute = Path(os.path.abspath(os.fspath(candidate)))
    _assert_no_symlink_components(absolute)
    try:
        root_stat = os.lstat(absolute)
    except OSError as exc:
        raise FinalModelSafetyError(f"cannot lstat campaign ComfyUI models root: {exc}") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise FinalModelSafetyError("campaign ComfyUI models root is not a real directory")
    real = Path(os.path.realpath(absolute))
    if real != absolute:
        raise FinalModelSafetyError(f"campaign ComfyUI models root crosses a symlink: {absolute} -> {real}")
    return absolute


def _normalise_dependency_name(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if (
        not raw
        or raw.startswith("/")
        or re.match(r"^[A-Za-z]:", raw)
        or "://" in raw
        or "\x00" in raw
        or any(ord(char) < 32 for char in raw)
    ):
        raise FinalModelSafetyError(f"unsafe dependency path: {value!r}")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise FinalModelSafetyError(f"unsafe dependency path: {value!r}")
    return "/".join(parts)


def _dependency_folders(*, category: str, kind: str) -> tuple[str, ...]:
    if category == "loras":
        return ("loras",)
    if category == "controlnets":
        return ("controlnet",)
    if category != "models":
        raise FinalModelSafetyError(f"unsupported dependency category: {category!r}")
    mapping = {
        "checkpoint": ("checkpoints",),
        "diffusion_model": ("diffusion_models", "unet"),
        "vae": ("vae",),
        "clip": ("text_encoders", "clip"),
        "clip_vision": ("clip_vision",),
        "upscale": ("upscale_models",),
        "latent_upscale": ("latent_upscale_models", "upscale_models"),
        "embedding": ("embeddings",),
    }
    folders = mapping.get(kind)
    if not folders:
        raise FinalModelSafetyError(f"unsupported model dependency kind: {kind!r}")
    return folders


def _final_graph_references(workflow: dict[str, Any]) -> list[dict[str, str]]:
    contract = extract_graph_dependency_contract(workflow)
    contract_errors = [str(item) for item in contract.get("errors") or [] if str(item)]
    if contract_errors:
        raise FinalModelSafetyError("final graph dependency contract failed: " + "; ".join(contract_errors))

    references: list[dict[str, str]] = []
    for node_id, node in sorted(workflow.items(), key=lambda item: str(item[0])):
        if not isinstance(node, dict):
            raise FinalModelSafetyError(f"final graph node {node_id} is not an object")
        class_type = str(node.get("class_type") or "").strip()
        inputs = node.get("inputs")
        if not class_type or not isinstance(inputs, dict):
            raise FinalModelSafetyError(f"final graph node {node_id} lacks class_type/object inputs")
        mapped_dependency_class = any(
            mapped_class == class_type
            for mapped_class, _mapped_input_name in GRAPH_LOADER_DEPENDENCY_INPUTS
        )
        for input_name, raw_value in sorted(inputs.items(), key=lambda item: str(item[0])):
            key = (class_type, str(input_name))
            mapped = GRAPH_LOADER_DEPENDENCY_INPUTS.get(key)
            if mapped:
                if not isinstance(raw_value, str) or not raw_value.strip():
                    raise FinalModelSafetyError(
                        f"mapped loader dependency must be a literal non-empty path: "
                        f"node {node_id} {class_type}.{input_name}"
                    )
                category, kind = mapped
                semantic_kind = kind or (
                    "lora" if category == "loras" else (
                        "controlnet" if category == "controlnets" else ""
                    )
                )
                references.append({
                    "node_id": str(node_id),
                    "class_type": class_type,
                    "input_name": str(input_name),
                    "category": category,
                    "kind": semantic_kind,
                    "name": _normalise_dependency_name(raw_value),
                })
            elif _looks_like_loader_dependency_input(input_name):
                lower_class = class_type.lower()
                # A model-bearing custom node is not necessarily named
                # ``*Loader`` (some packs use ``Load*Model``). Literal model
                # paths on any unmapped input are dependencies, never harmless
                # strings. Node links remain valid for ordinary consumers such
                # as KSampler, while an unknown loader/load-model node is
                # rejected even when its input was disguised as a node link.
                # A literal is always an unclassified dependency, including
                # custom consumers that fetch/cache a model internally.  A
                # node link, however, is only an opaque dependency when it is
                # on an otherwise unknown loader.  Known mapped loaders such
                # as LoraLoader legitimately receive upstream MODEL/CLIP
                # links; the referenced upstream loader is audited at its own
                # node and must not be mistaken for a second model path here.
                if isinstance(raw_value, str) or (
                    not mapped_dependency_class
                    and (
                        "loader" in lower_class
                        or ("load" in lower_class and "model" in lower_class)
                    )
                ):
                    raise FinalModelSafetyError(
                        "unmapped loader dependency input in final graph: "
                        f"node {node_id} {class_type}.{input_name}"
                    )
            if str(input_name) == "text" and isinstance(raw_value, str):
                for embedding_name in extract_embedding_names_from_text(raw_value):
                    references.append({
                        "node_id": str(node_id),
                        "class_type": class_type,
                        "input_name": "text:embedding",
                        "category": "models",
                        "kind": "embedding",
                        "name": _normalise_dependency_name(embedding_name),
                    })

    expected: set[tuple[str, str]] = set()
    for item in contract.get("models") or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise FinalModelSafetyError(f"malformed model dependency contract item: {item!r}")
        expected.add((str(item[0] or ""), str(item[1] or "")))
    expected.update(("lora", str(item)) for item in contract.get("loras") or [])
    expected.update(("controlnet", str(item)) for item in contract.get("controlnets") or [])
    observed = {
        (
            "lora" if item["category"] == "loras" else (
                "controlnet" if item["category"] == "controlnets" else item["kind"]
            ),
            item["name"],
        )
        for item in references
    }
    if expected != observed:
        raise FinalModelSafetyError(
            "final graph dependency extraction mismatch: "
            f"missing={sorted(expected - observed)}, extra={sorted(observed - expected)}"
        )
    return references


def _existing_regular_candidate(path: Path, *, models_root: Path) -> tuple[Path, os.stat_result] | None:
    _assert_no_symlink_components(path)
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FinalModelSafetyError(f"cannot lstat dependency candidate {path}: {exc}") from exc
    if stat.S_ISLNK(path_stat.st_mode):
        raise FinalModelSafetyError(f"symlink model file is forbidden: {path}")
    if not stat.S_ISREG(path_stat.st_mode):
        raise FinalModelSafetyError(f"dependency candidate is not a regular file: {path}")
    real = Path(os.path.realpath(path))
    try:
        real.relative_to(models_root)
    except ValueError as exc:
        raise FinalModelSafetyError(f"dependency path crosses models root: {path} -> {real}") from exc
    if real != path:
        raise FinalModelSafetyError(f"dependency path is not canonical: {path} -> {real}")
    return path, path_stat


def _resolve_reference(reference: dict[str, str], *, models_root: Path) -> tuple[Path, os.stat_result]:
    candidates = [
        models_root / folder / reference["name"]
        for folder in _dependency_folders(category=reference["category"], kind=reference["kind"])
    ]
    found: list[tuple[Path, os.stat_result]] = []
    for candidate in candidates:
        match = _existing_regular_candidate(candidate, models_root=models_root)
        if match is not None:
            found.append(match)
    if not found:
        raise FinalModelSafetyError(
            "cannot resolve exact dependency path for "
            f"node {reference['node_id']} {reference['class_type']}.{reference['input_name']}="
            f"{reference['name']!r} under {models_root}"
        )
    if len(found) != 1:
        raise FinalModelSafetyError(
            f"ambiguous dependency path for {reference['name']!r}: "
            f"{[str(path) for path, _path_stat in found]}"
        )
    return found[0]


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return all(
        getattr(left, key) == getattr(right, key)
        for key in (
            "st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns"
        )
    )


def _open_exact_dependency_without_symlinks(
    path: Path,
    *,
    models_root: Path,
) -> tuple[int, list[int], str]:
    try:
        relative_parts = path.relative_to(models_root).parts
    except ValueError as exc:
        raise FinalModelSafetyError(f"dependency path crosses models root: {path}") from exc
    if not relative_parts:
        raise FinalModelSafetyError(f"dependency path has no file component: {path}")

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fds: list[int] = []
    try:
        absolute_root = Path(os.path.abspath(os.fspath(models_root)))
        directory_fds.append(os.open(absolute_root.anchor, directory_flags))
        # Open the models-root chain component-by-component.  O_NOFOLLOW on an
        # absolute models_root only protects its final component; a raced
        # symlink in /home/... or /mnt/... would otherwise still be followed.
        for component in absolute_root.parts[1:]:
            directory_fds.append(
                os.open(component, directory_flags, dir_fd=directory_fds[-1])
            )
        for component in relative_parts[:-1]:
            directory_fds.append(os.open(component, directory_flags, dir_fd=directory_fds[-1]))
        file_fd = os.open(relative_parts[-1], file_flags, dir_fd=directory_fds[-1])
    except OSError as exc:
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)
        raise FinalModelSafetyError(
            f"cannot securely open dependency without symlink traversal {path}: {exc}"
        ) from exc
    return file_fd, directory_fds, relative_parts[-1]


def _hash_exact_regular_file(
    path: Path,
    expected: os.stat_result,
    *,
    models_root: Path,
) -> tuple[str, os.stat_result]:
    fd, directory_fds, leaf_name = _open_exact_dependency_without_symlinks(
        path,
        models_root=models_root,
    )
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not _same_file_snapshot(expected, before):
            raise FinalModelSafetyError(f"dependency file changed before hashing: {path}")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
        if not _same_file_snapshot(before, after):
            raise FinalModelSafetyError(f"dependency file changed while hashing: {path}")
        try:
            linked_after = os.stat(
                leaf_name,
                dir_fd=directory_fds[-1],
                follow_symlinks=False,
            )
        except OSError as exc:
            raise FinalModelSafetyError(
                f"dependency path disappeared while hashing: {path}: {exc}"
            ) from exc
        if not _same_file_snapshot(after, linked_after):
            raise FinalModelSafetyError(f"dependency path changed while hashing: {path}")
    finally:
        os.close(fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)
    _assert_no_symlink_components(path)
    try:
        final_path_stat = os.lstat(path)
    except OSError as exc:
        raise FinalModelSafetyError(f"dependency file disappeared after hashing: {path}: {exc}") from exc
    if not _same_file_snapshot(after, final_path_stat):
        raise FinalModelSafetyError(f"dependency path changed after hashing: {path}")
    final_realpath = Path(os.path.realpath(path))
    if final_realpath != path:
        raise FinalModelSafetyError(
            f"dependency path crosses a symlink after hashing: {path} -> {final_realpath}"
        )
    return digest.hexdigest(), after


def _stat_exact_regular_file_without_symlinks(
    path: Path,
    *,
    models_root: Path,
) -> os.stat_result:
    """Take a path-bound stat snapshot without following any symlink.

    This deliberately reopens the complete directory chain.  Comparing only
    ``Path.lstat()`` after hashing would miss a parent-directory swap, while
    retaining only the original file descriptor would miss a path relink.
    """

    fd, directory_fds, leaf_name = _open_exact_dependency_without_symlinks(
        path,
        models_root=models_root,
    )
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise FinalModelSafetyError(f"dependency candidate is not a regular file: {path}")
        try:
            linked = os.stat(
                leaf_name,
                dir_fd=directory_fds[-1],
                follow_symlinks=False,
            )
        except OSError as exc:
            raise FinalModelSafetyError(
                f"dependency path disappeared during final snapshot: {path}: {exc}"
            ) from exc
        if not _same_file_snapshot(opened, linked):
            raise FinalModelSafetyError(f"dependency path changed during final snapshot: {path}")
    finally:
        os.close(fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)
    _assert_no_symlink_components(path)
    try:
        final_path_stat = os.lstat(path)
    except OSError as exc:
        raise FinalModelSafetyError(
            f"dependency file disappeared after final snapshot: {path}: {exc}"
        ) from exc
    if not _same_file_snapshot(opened, final_path_stat):
        raise FinalModelSafetyError(f"dependency path changed after final snapshot: {path}")
    final_realpath = Path(os.path.realpath(path))
    if final_realpath != path:
        raise FinalModelSafetyError(
            f"dependency path crosses a symlink after final snapshot: {path} -> {final_realpath}"
        )
    return opened


def _receipt_stat_matches(actual: os.stat_result, expected: Mapping[str, Any]) -> bool:
    fields = {
        "device": actual.st_dev,
        "inode": actual.st_ino,
        "mode": actual.st_mode,
        "link_count": actual.st_nlink,
        "size_bytes": actual.st_size,
        "mtime_ns": actual.st_mtime_ns,
        "ctime_ns": actual.st_ctime_ns,
    }
    try:
        return all(int(expected.get(key)) == int(value) for key, value in fields.items())
    except (TypeError, ValueError, OverflowError):
        return False


def revalidate_final_model_safety_receipt_files(
    receipt: Mapping[str, Any],
    *,
    models_root: str | os.PathLike[str],
) -> list[dict[str, Any]]:
    """Revalidate that receipt paths still name the exact hashed file states.

    The receipt digest is checked by its consumer.  This function supplies the
    independent filesystem binding needed at terminal state, after ComfyUI has
    consumed the queued graph.
    """

    root = _validated_models_root(models_root)
    if str(receipt.get("models_root_realpath") or "") != str(root):
        raise FinalModelSafetyError("final model safety receipt models root mismatch")
    rows = receipt.get("model_files")
    if not isinstance(rows, list):
        raise FinalModelSafetyError("final model safety receipt model_files is not a list")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise FinalModelSafetyError(f"final model safety receipt model file {index} is malformed")
        raw_relative_path = str(row.get("relative_path") or "")
        relative_path = _normalise_dependency_name(raw_relative_path)
        if relative_path != raw_relative_path or relative_path in seen:
            raise FinalModelSafetyError(
                f"final model safety receipt path is noncanonical or duplicated: {raw_relative_path!r}"
            )
        seen.add(relative_path)
        expected_stat = row.get("stat")
        if not isinstance(expected_stat, Mapping):
            raise FinalModelSafetyError(
                f"final model safety receipt stat is malformed: {raw_relative_path!r}"
            )
        path = root / relative_path
        actual = _stat_exact_regular_file_without_symlinks(path, models_root=root)
        if not _receipt_stat_matches(actual, expected_stat):
            raise FinalModelSafetyError(
                f"dependency file changed after receipt creation: {raw_relative_path}"
            )
        if int(row.get("size_bytes", -1)) != int(actual.st_size):
            raise FinalModelSafetyError(
                f"dependency receipt size disagrees with exact file state: {raw_relative_path}"
            )
        actual_sha256, hashed_stat = _hash_exact_regular_file(
            path,
            actual,
            models_root=root,
        )
        if not _receipt_stat_matches(hashed_stat, expected_stat):
            raise FinalModelSafetyError(
                f"dependency file changed while terminal receipt was revalidated: {raw_relative_path}"
            )
        if actual_sha256 != str(row.get("sha256") or ""):
            raise FinalModelSafetyError(
                f"dependency content hash changed after receipt creation: {raw_relative_path}"
            )
        validated.append({
            "relative_path": relative_path,
            "device": int(actual.st_dev),
            "inode": int(actual.st_ino),
            "size_bytes": int(actual.st_size),
            "mtime_ns": int(actual.st_mtime_ns),
            "ctime_ns": int(actual.st_ctime_ns),
        })
    return validated


def verify_final_graph_model_safety(
    workflow: Mapping[str, Any],
    *,
    models_root: str | os.PathLike[str],
    backend_url: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the immutable graph copy and its exact model safety receipt."""

    canonical_workflow, graph_sha256 = _canonical_workflow_copy(workflow)
    root = _validated_models_root(models_root)
    backend_origin = _normalise_backend_origin(backend_url, label="ComfyUI backend URL")
    references = _final_graph_references(canonical_workflow)

    resolved_references: list[dict[str, Any]] = []
    files: dict[str, dict[str, Any]] = {}
    pending_stats: dict[str, os.stat_result] = {}
    for reference in references:
        path, path_stat = _resolve_reference(reference, models_root=root)
        relative_path = path.relative_to(root).as_posix()
        resolved_references.append({**reference, "relative_path": relative_path})
        if relative_path not in files:
            files[relative_path] = {
                "relative_path": relative_path,
                "size_bytes": int(path_stat.st_size),
            }
            pending_stats[relative_path] = path_stat

    oversized = sorted(
        relative_path
        for relative_path, item in files.items()
        if int(item["size_bytes"]) > FINAL_MODEL_MAX_FILE_BYTES
    )
    total_bytes = sum(int(item["size_bytes"]) for item in files.values())
    empty = sorted(
        relative_path
        for relative_path, item in files.items()
        if int(item["size_bytes"]) <= 0
    )
    if empty:
        raise FinalModelSafetyError(f"final graph model file is empty: {empty}")
    if oversized:
        raise FinalModelSafetyError(
            f"final graph model file exceeds immutable {FINAL_MODEL_MAX_FILE_BYTES}-byte cap: {oversized}"
        )
    if total_bytes > FINAL_WORKFLOW_MODEL_MAX_BYTES:
        raise FinalModelSafetyError(
            "final graph distinct model total exceeds immutable "
            f"{FINAL_WORKFLOW_MODEL_MAX_BYTES}-byte cap: {total_bytes}"
        )

    for relative_path in sorted(files):
        path = root / relative_path
        sha256, file_stat = _hash_exact_regular_file(
            path,
            pending_stats[relative_path],
            models_root=root,
        )
        files[relative_path].update({
            "sha256": sha256,
            "stat": {
                "device": int(file_stat.st_dev),
                "inode": int(file_stat.st_ino),
                "mode": int(file_stat.st_mode),
                "link_count": int(file_stat.st_nlink),
                "size_bytes": int(file_stat.st_size),
                "mtime_ns": int(file_stat.st_mtime_ns),
                "ctime_ns": int(file_stat.st_ctime_ns),
            },
        })

    # A multi-model graph creates a gap between hashing the first and last
    # file.  Reopen every exact path after all hashes complete so an earlier
    # file cannot be swapped while a later model is being read.
    for relative_path in sorted(files):
        path = root / relative_path
        final_stat = _stat_exact_regular_file_without_symlinks(path, models_root=root)
        if not _same_file_snapshot(pending_stats[relative_path], final_stat):
            raise FinalModelSafetyError(
                f"dependency file changed after complete graph hashing: {path}"
            )
        try:
            final_sha256, final_hashed_stat = _hash_exact_regular_file(
                path,
                final_stat,
                models_root=root,
            )
        except FinalModelSafetyError as exc:
            raise FinalModelSafetyError(
                f"dependency file changed after complete graph hashing: {path}: {exc}"
            ) from exc
        if (
            final_sha256 != str(files[relative_path].get("sha256") or "")
            or not _same_file_snapshot(final_stat, final_hashed_stat)
        ):
            raise FinalModelSafetyError(
                f"dependency content changed after complete graph hashing: {path}"
            )

    receipt: dict[str, Any] = {
        "schema_version": FINAL_MODEL_SAFETY_SCHEMA_VERSION,
        "ok": True,
        "enforcement": "campaign_final_graph_pre_prompt_fail_closed",
        "backend_origin": backend_origin,
        "models_root_realpath": str(root),
        "graph_sha256": graph_sha256,
        "limits": {
            "max_model_file_bytes": FINAL_MODEL_MAX_FILE_BYTES,
            "max_workflow_model_total_bytes": FINAL_WORKFLOW_MODEL_MAX_BYTES,
            "limits_can_only_tighten": True,
        },
        "reference_count": len(resolved_references),
        "distinct_model_file_count": len(files),
        "distinct_model_total_bytes": total_bytes,
        "references": sorted(
            resolved_references,
            key=lambda item: (
                item["node_id"], item["class_type"], item["input_name"], item["relative_path"]
            ),
        ),
        "model_files": [files[key] for key in sorted(files)],
    }
    receipt["receipt_sha256"] = hashlib.sha256(_canonical_json_bytes(receipt)).hexdigest()
    return canonical_workflow, receipt


def enforce_campaign_final_graph_model_safety(
    workflow: Mapping[str, Any],
    *,
    backend_url: str,
    environ: Mapping[str, str] | None = None,
) -> tuple[Mapping[str, Any], dict[str, Any] | None]:
    """Enforce campaign safety when either campaign binding variable is set."""

    env = os.environ if environ is None else environ
    expected_backend = str(env.get(CAMPAIGN_COMFYUI_API_URL_ENV) or "").strip()
    models_root = str(env.get(CAMPAIGN_COMFYUI_MODELS_ROOT_ENV) or "").strip()
    if not expected_backend and not models_root:
        return workflow, None
    if not expected_backend or not models_root:
        missing = CAMPAIGN_COMFYUI_API_URL_ENV if not expected_backend else CAMPAIGN_COMFYUI_MODELS_ROOT_ENV
        raise FinalModelSafetyError(f"campaign final graph safety binding is incomplete: {missing} is required")
    expected_origin = _normalise_backend_origin(expected_backend, label=CAMPAIGN_COMFYUI_API_URL_ENV)
    actual_origin = _normalise_backend_origin(backend_url, label="actual ComfyUI backend URL")
    if actual_origin != expected_origin:
        raise FinalModelSafetyError(
            f"actual ComfyUI backend origin does not match campaign binding: {actual_origin} != {expected_origin}"
        )
    return verify_final_graph_model_safety(
        workflow,
        models_root=models_root,
        backend_url=actual_origin,
    )


def final_model_safety_prompt_marker(receipt: Mapping[str, Any]) -> dict[str, str]:
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema_version") != FINAL_MODEL_SAFETY_SCHEMA_VERSION
        or receipt.get("ok") is not True
        or not str(receipt.get("graph_sha256") or "")
        or not str(receipt.get("receipt_sha256") or "")
    ):
        raise FinalModelSafetyError("cannot bind an invalid final model safety receipt")
    unsigned = dict(receipt)
    supplied_receipt_sha256 = str(unsigned.pop("receipt_sha256", ""))
    recomputed_receipt_sha256 = hashlib.sha256(_canonical_json_bytes(unsigned)).hexdigest()
    if supplied_receipt_sha256 != recomputed_receipt_sha256:
        raise FinalModelSafetyError("cannot bind a tampered final model safety receipt")
    return {
        "schema_version": FINAL_MODEL_SAFETY_MARKER_SCHEMA_VERSION,
        "graph_sha256": str(receipt["graph_sha256"]),
        "receipt_sha256": str(receipt["receipt_sha256"]),
    }


def verify_final_model_safety_backend_history_binding(
    history_record: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    prompt_id: str,
) -> dict[str, Any]:
    """Prove the backend history contains the exact graph and receipt marker."""

    expected_marker = final_model_safety_prompt_marker(receipt)
    prompt_entry = history_record.get("prompt") if isinstance(history_record, Mapping) else None
    if not isinstance(prompt_entry, (list, tuple)) or len(prompt_entry) < 4:
        raise FinalModelSafetyError("ComfyUI history lacks the bound prompt tuple")
    history_prompt_id = str(prompt_entry[1] or "")
    if history_prompt_id != str(prompt_id or ""):
        raise FinalModelSafetyError(
            f"ComfyUI history prompt id mismatch: {history_prompt_id!r} != {prompt_id!r}"
        )
    history_graph = prompt_entry[2]
    if not isinstance(history_graph, Mapping):
        raise FinalModelSafetyError("ComfyUI history prompt graph is not an object")
    _canonical_graph, history_graph_sha256 = _canonical_workflow_copy(history_graph)
    if history_graph_sha256 != str(receipt.get("graph_sha256") or ""):
        raise FinalModelSafetyError(
            "ComfyUI history prompt graph does not match the final model safety receipt"
        )
    extra_data = prompt_entry[3]
    if not isinstance(extra_data, Mapping):
        raise FinalModelSafetyError("ComfyUI history prompt extra_data is not an object")
    actual_marker = extra_data.get(FINAL_MODEL_SAFETY_EXTRA_DATA_KEY)
    if actual_marker != expected_marker:
        raise FinalModelSafetyError(
            "ComfyUI history prompt final model safety marker is missing or changed"
        )
    return {
        "schema_version": FINAL_MODEL_SAFETY_BACKEND_BINDING_SCHEMA_VERSION,
        "ok": True,
        "prompt_id": history_prompt_id,
        "graph_sha256": history_graph_sha256,
        "receipt_sha256": str(receipt["receipt_sha256"]),
        "history_prompt_tuple_minimum_fields": 4,
        "history_graph_verified": True,
        "history_marker_verified": True,
    }


__all__ = [
    "CAMPAIGN_COMFYUI_API_URL_ENV",
    "CAMPAIGN_COMFYUI_MODELS_ROOT_ENV",
    "FINAL_MODEL_MAX_FILE_BYTES",
    "FINAL_MODEL_SAFETY_BACKEND_BINDING_SCHEMA_VERSION",
    "FINAL_MODEL_SAFETY_EXTRA_DATA_KEY",
    "FINAL_MODEL_SAFETY_MARKER_SCHEMA_VERSION",
    "FINAL_MODEL_SAFETY_SCHEMA_VERSION",
    "FINAL_WORKFLOW_MODEL_MAX_BYTES",
    "FinalModelSafetyError",
    "enforce_campaign_final_graph_model_safety",
    "final_model_safety_prompt_marker",
    "revalidate_final_model_safety_receipt_files",
    "verify_final_model_safety_backend_history_binding",
    "verify_final_graph_model_safety",
]
