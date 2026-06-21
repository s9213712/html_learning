from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_pointschain_financial_settlement_architecture_is_formalized():
    architecture = (ROOT / "docs" / "architecture" / "POINTSCHAIN_FINANCIAL_SETTLEMENT_NETWORK.md").read_text(encoding="utf-8")
    pc0_model = (ROOT / "docs" / "architecture" / "PC0_DUAL_RAIL_WALLET_MODEL.md").read_text(encoding="utf-8")
    pointschain = (ROOT / "docs" / "07_POINTSCHAIN.md").read_text(encoding="utf-8")
    docs_index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")

    for required in [
        "Permissioned Financial Settlement Network",
        "PC1",
        "Canonical Settlement Layer",
        "PC0",
        "Operational Wrapped Layer",
        "Bridge",
        "Cross-Ledger Settlement Layer",
        "pc1_settlement_ledger",
        "pc0_operational_ledger",
        "bridge_settlement_events",
        "reserve_audit_snapshots",
        "wrapped_supply <= canonical_locked_reserve",
        "pending settlement must not count as finalized reserve or finalized liability",
        "Reserve Transparency Dashboard Contract",
        "Immutable Daily Audit Snapshots",
        "Forbidden Mixing Patterns",
        "Canonical Reserve",
        "Wrapped Outstanding",
        "Pending Settlement",
        "Invariant Status",
        "liability Merkle root",
        "showing `pc0` operational balances as canonical on-chain assets",
        "using one transaction id as if it were simultaneously a PC1 tx, PC0 op, and",
        "PENDING_LOCK",
        "RELEASED",
    ]:
        assert required in architecture

    assert "traditional single-chain blockchain" in architecture
    assert "Status: draft" not in pc0_model
    assert "POINTSCHAIN_FINANCIAL_SETTLEMENT_NETWORK.md" in pc0_model
    assert "single-chain" in pc0_model
    assert "Closed-loop supply formula" not in pc0_model
    assert "Multi-ledger settlement reconciliation" in pc0_model
    assert "permissioned financial settlement network" in pointschain
    assert "PC1、PC0 與\nBridge 必須在帳務語意上分離" in pointschain
    assert "POINTSCHAIN_FINANCIAL_SETTLEMENT_NETWORK.md" in pointschain
    assert "POINTSCHAIN_FINANCIAL_SETTLEMENT_NETWORK.md" in docs_index


def test_pointschain_ui_and_invariant_names_match_federated_architecture():
    economy_js = (
        (ROOT / "public" / "js" / "55-economy.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "55-economy-explorer.js").read_text(encoding="utf-8")
    )
    service = (ROOT / "services" / "points_chain" / "service.py").read_text(encoding="utf-8")

    for required in [
        "PC1 Canonical Settlement Layer",
        "PC0 Operational Wrapped Layer",
        "Bridge Cross-Ledger Settlement Layer",
        "Audit & Reserve Invariants",
        "多帳本結算控制平面",
        "PC1 Canonical Reserve",
        "PC0 Wrapped Operational Supply",
        "Bridge Settlement / Pending Isolation",
        "Financial Reconciliation",
        "Settlement Explorer",
        "Operational Explorer",
        "Bridge Explorer",
        "Audit Explorer",
    ]:
        assert required in economy_js

    for forbidden in [
        "鏈上/橋外在外流通",
        "pc0出站",
        "入金入站",
        "閉環公式",
        "閉環正常",
    ]:
        assert forbidden not in economy_js

    assert '"model": "pc1_canonical_reserve_pc0_wrapped_operational_v1"' in service
    assert "pc0_operational_ledgers_not_sealed_into_pc1_blocks" in service
    assert "bridge_settlement_integrity" in service
