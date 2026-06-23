#!/usr/bin/env python3
"""On-live-report driver: ai_agent_boundary.

Runs deterministic AI Agent boundary regressions without calling an LLM.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVER = REPO_ROOT / "scripts/testing/pytest_in_tmp.sh"
REPORT_HINT = "runtime/reports/security/production_gate/ai_agent_boundary_*"


def progress(message: str) -> None:
    print(f"[on-live:ai-agent-boundary] {message}", file=sys.stderr, flush=True)


cmd = [
    "bash",
    str(DRIVER),
    "-q",
    "tests/ai_agent/test_ai_agent_routes.py::test_ai_agent_write_tools_root_only_and_lists_allowed_tools",
    "tests/ai_agent/test_ai_agent_routes.py::test_ai_agent_write_tools_lockdown_blocks_list_and_execute",
    "tests/ai_agent/test_ai_agent_routes.py::test_ai_agent_write_tool_execute_requires_write_mode_for_mutation",
    "tests/ai_agent/test_ai_agent_routes.py::test_ai_agent_launch_preflight_executes_checks_audit_and_switch",
    "tests/ai_agent/test_ai_agent_routes.py::test_ai_agent_chat_blocks_os_filesystem_listing_before_llm",
    "tests/ai_agent/test_ai_agent_routes.py::test_ai_agent_chat_blocks_server_filesystem_mutation_before_llm",
    "tests/ai_agent/test_ai_agent_routes.py::test_ai_agent_write_tool_blocks_server_filesystem_path_args",
    *sys.argv[1:],
]
progress(f"target repo: {REPO_ROOT}")
progress(f"artifact hint: {REPORT_HINT}")
progress("phase pytest-in-tmp started: AI Agent tool and filesystem boundaries")
rc = subprocess.run(cmd, cwd=REPO_ROOT, env={**os.environ}).returncode
progress(f"phase result pytest-in-tmp: exit={rc}")
if rc != 0:
    progress("failure hint: inspect AI Agent write-tool, lockdown, and filesystem-boundary regressions above")
sys.exit(rc)
