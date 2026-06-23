from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_qa_governance_docs_register_gate_scripts_and_validation_vocabulary():
    index = (ROOT / "scripts" / "INDEX.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "security" / "QA_ARCHITECTURE.md").read_text(encoding="utf-8")
    placement = (ROOT / "scripts" / "PLACEMENT_RULES.md").read_text(encoding="utf-8")
    functional_smoke = (ROOT / "docs" / "security" / "FUNCTIONAL_SMOKE.md").read_text(encoding="utf-8")

    for report_type in (
        "clean_smoke",
        "adversarial",
        "redteam_l2",
        "log_chain_verify",
        "integrity_guard",
        "cloud_drive_quota_permission",
        "stress",
        "permission",
        "functional",
        "pentest",
        "snapshot_restore",
        "pytest",
        "points_chain_consistency",
        "ai_agent_boundary",
        "on_live_reports_make",
    ):
        assert f"`{report_type}`" in index

    for required_field in ("Owner", "Purpose", "Artifact", "Failure meaning"):
        assert required_field in index
        assert required_field in architecture

    assert "Every new QA, security, pentest, stress, smoke, or production-gate script must" in index
    assert "registered" in placement
    assert "Focused regression results" in architecture
    assert "must never be summarized as \"full validation passed\"" in architecture
    assert "broad functional smoke, not full production validation" in functional_smoke


def test_functional_smoke_phase_catalog_has_operational_contract_columns():
    architecture = (ROOT / "docs" / "security" / "QA_ARCHITECTURE.md").read_text(encoding="utf-8")

    assert "Functional Smoke Phase Catalog" in architecture
    for column in ("Inputs", "Outputs", "Shared env/state", "Cleanup responsibility"):
        assert column in architecture

    for phase in (
        "Runtime bootstrap",
        "Root auth and account seed",
        "PointsChain and trading gates",
        "ComfyUI and Civitai",
        "Snapshot restore and reset verification",
        "Report finalization",
    ):
        assert phase in architecture


def test_active_docs_do_not_point_to_missing_deployment_scripts():
    production = (ROOT / "docs" / "02_DEPLOY_PRODUCTION.md").read_text(encoding="utf-8")
    updates = (ROOT / "docs" / "UPDATE_SUMMARY.md").read_text(encoding="utf-8")
    comfy_canonical = (ROOT / "docs" / "comfyui" / "COMFYUI_ADMIN.md").read_text(encoding="utf-8")

    active_docs = production + "\n" + updates
    assert "scripts/run_prod.sh" not in active_docs
    assert "scripts/root_recovery.py" not in active_docs
    assert "python3 server.py --doctor" in production
    assert "scripts/admin/root_recovery.py" in updates

    assert "## 管理順序" in comfy_canonical
