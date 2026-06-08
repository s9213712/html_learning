from urllib.parse import parse_qs, urlparse

from routes.comfyui_sections import admin_helpers as comfyui_admin_helpers
from tests.comfyui._integration_suite import _build_app, _init_db


def test_civitai_image_favorite_import_fetches_meta_and_model_versions(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    storage_root = tmp_path / "storage"
    _init_db(db_path)
    calls = []

    def fake_fetch_json(url, *, headers=None, timeout=20):
        calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        parsed = urlparse(url)
        if parsed.path.endswith("/images"):
            query = parse_qs(parsed.query)
            assert query.get("imageId") == ["124687059"]
            assert query.get("withMeta") == ["true"]
            return {
                "items": [
                    {
                        "id": 124687059,
                        "url": "https://image.civitai.com/xG1nkqKTMzGDvpLrqFT7WA/example/original=true/example.jpeg",
                        "width": 1664,
                        "height": 2432,
                        "nsfw": False,
                        "nsfwLevel": "None",
                        "createdAt": "2026-03-19T17:33:27.289Z",
                        "postId": 27356378,
                        "username": "janxd",
                        "baseModel": "Illustrious",
                        "modelVersionIds": [1833157],
                        "meta": {
                            "id": 124687059,
                            "meta": {
                                "seed": 3865068928,
                                "Model": "JANKUv7.77b-final-full-remove_ema-clip-fix (2)",
                                "steps": 30,
                                "width": 832,
                                "height": 1216,
                                "prompt": "lazypos, 1girl",
                                "sampler": "Euler a",
                                "cfgScale": 3,
                                "resources": [
                                    {
                                        "hash": "88177d224c",
                                        "name": "JANKUv7.77b-final-full-remove_ema-clip-fix (2)",
                                        "type": "model",
                                    }
                                ],
                                "negativePrompt": "lazyneg, lazyhand, lazywet",
                            },
                        },
                    }
                ],
                "metadata": {},
            }
        if parsed.path.endswith("/model-versions/1833157"):
            return {
                "id": 1833157,
                "modelId": 1302719,
                "name": "lazypos",
                "baseModel": "Illustrious",
                "air": "urn:air:sdxl:embedding:civitai:1302719@1833157",
                "trainedWords": ["lazypos"],
                "model": {
                    "name": "Lazy Embeddings",
                    "type": "TextualInversion",
                    "nsfw": False,
                },
                "files": [
                    {
                        "id": 1733353,
                        "name": "lazypos.safetensors",
                        "type": "Model",
                        "sizeKB": 176.1484375,
                        "primary": True,
                        "downloadUrl": "https://civitai.com/api/download/models/1833157",
                        "hashes": {"AutoV2": "3086669265"},
                    }
                ],
            }
        raise AssertionError(f"unexpected Civitai URL: {url}")

    monkeypatch.setattr(comfyui_admin_helpers, "_fetch_json", fake_fetch_json)
    client = _build_app(
        db_path,
        storage_root,
        settings={"comfyui_civitai_api_key": "unit-token"},
    ).test_client()

    response = client.post(
        "/api/comfyui/image-favorites/import-civitai",
        json={"url": "https://civitai.com/images/124687059"},
    )

    assert response.status_code == 200, response.get_json()
    favorite = response.get_json()["favorite"]
    params = favorite["params"]
    assert favorite["civitai_image_id"] == 124687059
    assert params["prompt"] == "<embeddings:lazypos.safetensors>, 1girl"
    assert params["negative_prompt"] == "lazyneg, lazyhand, lazywet"
    assert params["embeddings"] == ["lazypos.safetensors"]
    assert params["model"] == ""
    assert params["source_model_name"] == "JANKUv7.77b-final-full-remove_ema-clip-fix (2)"
    assert params["seed"] == "3865068928"
    assert params["steps"] == 30
    assert params["cfg"] == 3
    assert params["width"] == 832
    assert params["height"] == 1216
    assert params["workflow_system_bundle_id"] == "origin_sdxl_txt2img"
    assert params["workflow_preset_title"] == "SDXL Text-to-Image"
    assert params["civitai"]["base_model"] == "Illustrious"
    assert params["civitai"]["source_model_name"] == "JANKUv7.77b-final-full-remove_ema-clip-fix (2)"
    assert params["civitai"]["model_version_ids"] == [1833157]
    resource = params["civitai"]["model_version_resources"][0]
    assert resource["model_version_id"] == 1833157
    assert resource["model_name"] == "Lazy Embeddings"
    assert resource["model_type"] == "TextualInversion"
    assert resource["trained_words"] == ["lazypos"]
    assert resource["primary_file"]["name"] == "lazypos.safetensors"
    assert calls[0]["headers"]["Authorization"] == "Bearer unit-token"
    assert any("/model-versions/1833157" in call["url"] for call in calls)


def test_civitai_image_favorite_import_rejects_wrong_image_id(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    storage_root = tmp_path / "storage"
    _init_db(db_path)

    def fake_fetch_json(url, *, headers=None, timeout=20):
        return {
            "items": [
                {
                    "id": 999,
                    "url": "https://image.civitai.com/xG1nkqKTMzGDvpLrqFT7WA/example/original=true/example.jpeg",
                }
            ]
        }

    monkeypatch.setattr(comfyui_admin_helpers, "_fetch_json", fake_fetch_json)
    client = _build_app(db_path, storage_root).test_client()

    response = client.post(
        "/api/comfyui/image-favorites/import-civitai",
        json={"url": "https://civitai.com/images/124687059"},
    )

    assert response.status_code == 404
    assert "imageId=124687059" in response.get_json()["msg"]
