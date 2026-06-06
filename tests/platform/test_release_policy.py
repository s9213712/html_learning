import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _release_id():
    tree = ast.parse((ROOT / "services" / "platform" / "release_info.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "APP_RELEASE_ID":
                    return node.value.value
    raise AssertionError("APP_RELEASE_ID not found")


def test_release_id_is_synced_to_public_docs():
    release_id = _release_id()
    assert re.fullmatch(r"(?:0[0-9]_)?\d{4}\.\d{2}\.\d{2}-\d{3}", release_id)
    for rel in ("README.md", "docs/README.zh-TW.md", "docs/For_developer.md", "docs/UPDATE_SUMMARY.md"):
        assert release_id in (ROOT / rel).read_text(encoding="utf-8")


def test_branching_policy_documents_numbered_branch_sequence():
    doc = (ROOT / "docs" / "BRANCHING_AND_RELEASE.md").read_text(encoding="utf-8")
    assert "main" in doc
    assert "01.POINTSCHAIN" in doc
    assert "02-WebTerminal-docker" in doc
    assert "02-WebTerminal-qemu" in doc
    assert "03.Economy" in doc
    assert "hackme_web_lite" in doc
    assert "last numeric segment by 1" in doc


def test_root_keeps_only_readme_markdown_and_docs_has_index():
    root_markdown = sorted(path.name for path in ROOT.glob("*.md"))
    assert root_markdown == ["README.md", "SECURITY.md"]

    docs_index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    expected_links = [
        "00_START_HERE.md",
        "01_DEPLOY_QUICKSTART.md",
        "02_DEPLOY_PRODUCTION.md",
        "03_ADMIN_GUIDE.md",
        "04_USER_GUIDE.md",
        "05_FEATURES_OVERVIEW.md",
        "06_SECURITY_MODEL.md",
        "07_POINTSCHAIN.md",
        "08_TRADING_ENGINE.md",
        "09_SNAPSHOT_RESET_RESTORE.md",
        "11_QA_TESTING.md",
        "12_TROUBLESHOOTING.md",
        "For_developer.md",
        "SECURITY.md",
        "WEB.md",
        "trading/TRADING.md",
        "video/VIDEO_PLATFORM.md",
        "ops_boundaries/ENCRYPTION_RUNTIME_BOUNDARY.md",
        "server_mode_v2/SERVER_MODE_V2_PROFILE_MATRIX.md",
        "AGENTS/research/BLOCKCHAIN/README.md",
        "BRANCHING_AND_RELEASE.md",
        "UPDATE_SUMMARY.md",
        "security/PRE_RELEASE_CHECKLIST.md",
        "security/FUNCTIONAL_SMOKE.md",
        "security/FUNCTIONAL_PERMISSION_PENTEST.md",
        "security/PENTEST.md",
    ]
    for link in expected_links:
        assert f"]({link})" in docs_index
