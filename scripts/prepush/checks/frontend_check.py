from __future__ import annotations

import os
import subprocess

from scripts.prepush import utils
from scripts.prepush.context import PrepushContext
from scripts.prepush.result import CheckResult


DEFAULT_NODE_CHECK_TIMEOUT_SECONDS = 120
NODE_BATCH_CHECK_SCRIPT = r"""
const fs = require("fs");
const vm = require("vm");
const failures = [];
for (const file of process.argv.slice(1)) {
  try {
    new vm.Script(fs.readFileSync(file, "utf8"), { filename: file, displayErrors: true });
  } catch (error) {
    failures.push({ file, error: String(error && (error.stack || error.message) || error) });
  }
}
if (failures.length) {
  console.error(JSON.stringify(failures));
  process.exit(1);
}
"""


def node_check_timeout_seconds() -> int:
    try:
        requested = int(
            os.environ.get("PREPUSH_NODE_CHECK_TIMEOUT_SECONDS", str(DEFAULT_NODE_CHECK_TIMEOUT_SECONDS)).strip()
        )
    except (TypeError, ValueError):
        requested = DEFAULT_NODE_CHECK_TIMEOUT_SECONDS
    return max(30, min(600, requested))


def run(ctx: PrepushContext) -> CheckResult:
    if not utils.tool_exists("node"):
        if ctx.is_ci:
            return CheckResult.fail("frontend JS syntax", "node is missing in CI", severity="medium", remediation="Install node or disable frontend syntax gate explicitly.")
        return CheckResult.skip("frontend JS syntax", "node is not installed; skipped local JS syntax check")
    js_files = [
        path
        for path in sorted((ctx.repo_root / "public" / "js").rglob("*.js"))
        if ".min." not in path.name
    ]
    timeout_seconds = node_check_timeout_seconds()
    try:
        proc = utils.run_command(
            ["node", "-e", NODE_BATCH_CHECK_SCRIPT, *(str(path) for path in js_files)],
            cwd=ctx.repo_root,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return CheckResult.fail(
            "frontend JS syntax",
            f"Node syntax batch exceeded {timeout_seconds} seconds",
            severity="high",
            details=[{"files": len(js_files)}],
            remediation=(
                "Check host I/O and CPU pressure, then rerun; adjust "
                "PREPUSH_NODE_CHECK_TIMEOUT_SECONDS only within the bounded 30-600 second range."
            ),
        )
    if proc.returncode != 0:
        return CheckResult.fail(
            "frontend JS syntax",
            "Node syntax parser failed",
            severity="high",
            details=[{"output": utils.sanitize_path(proc.stderr)[-4000:]}],
            remediation="Fix JavaScript syntax errors in public/js.",
        )
    return CheckResult.pass_("frontend JS syntax", f"checked {len(js_files)} JS file(s)")
