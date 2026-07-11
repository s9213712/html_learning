from services.ai_agent.action_policy import evaluate_action_execution, resolve_action_policy


def test_user_assist_safe_action_is_allowed_but_financial_action_is_not():
    community = evaluate_action_execution(
        "write_community_create_thread",
        "user",
        "assist",
        blueprint={"min_role": "super_admin", "data_scope": "write_tool:community"},
        write=True,
    )
    trading = evaluate_action_execution(
        "write_trading_place_order",
        "user",
        "assist",
        blueprint={"min_role": "super_admin", "data_scope": "write_tool:trading"},
        write=True,
    )

    assert community["allowed"] is True
    assert community["assist_safe"] is True
    assert community["min_role"] == "user"
    assert trading["allowed"] is False
    assert trading["risk_level"] == "high"
    assert trading["min_role"] == "user"


def test_manager_action_requires_write_mode_and_root_action_stays_root_only():
    manager_assist = evaluate_action_execution(
        "write_member_update_user",
        "manager",
        "assist",
        blueprint={"min_role": "super_admin"},
        write=True,
    )
    manager_write = evaluate_action_execution(
        "write_member_update_user",
        "manager",
        "write",
        blueprint={"min_role": "super_admin"},
        write=True,
    )
    root_action = evaluate_action_execution(
        "write_server_restart",
        "manager",
        "write",
        blueprint={"min_role": "super_admin"},
        write=True,
    )

    assert manager_assist["allowed"] is False
    assert manager_write["allowed"] is True
    assert root_action["allowed"] is False
    assert resolve_action_policy(
        "write_server_restart",
        blueprint={"min_role": "super_admin"},
        write=True,
    )["root_only"] is True
