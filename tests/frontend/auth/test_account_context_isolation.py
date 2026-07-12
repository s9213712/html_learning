from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
JS_ROOT = ROOT / "public" / "js"


def _read(relative_path: str) -> str:
    return (JS_ROOT / relative_path).read_text(encoding="utf-8")


def test_authenticated_requests_are_generation_bound():
    core = _read("00-core.js")

    assert "let accountRequestGeneration = 0;" in core
    assert "function rotateAccountRequestScope()" in core
    assert "accountRequestController.abort();" in core
    assert "function composeAccountRequestSignal" in core
    assert "function assertAccountRequestGeneration" in core
    assert 'err.code = "account_context_changed";' in core
    assert "function isAccountContextAbortError" in core
    assert 'window.addEventListener("unhandledrejection"' in core
    assert "function guardAccountScopedResponse" in core
    assert 'new Set(["arrayBuffer", "blob", "bytes", "formData", "json", "text"])' in core
    assert "const requestGeneration = expectedAccountGeneration === null" in core
    assert "assertAccountRequestGeneration(requestGeneration, accountScoped);" in core
    assert "return guardAccountScopedResponse(response, requestGeneration, accountScoped);" in core
    assert "requestGeneration\n    );" in core
    assert "if (accountScopeChanged) rotateAccountRequestScope();" in core
    assert 'syncActiveAccountStorageScope(previousAccountScope, { requestsRotated: accountScopeChanged });' in core
    csrf_function = core[core.index("async function fetchCsrfToken"):core.index("function abortableWait")]
    assert csrf_function.count("assertAccountRequestGeneration(requestGeneration, true);") >= 3


def test_response_body_cannot_finish_after_account_switch(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the account response race test")

    core = _read("00-core.js")
    start = core.index("function accountRequestAbortError")
    end = core.index("function syncActiveAccountStorageScope", start)
    response_guard_source = core[start:end]
    harness = tmp_path / "account_response_guard.js"
    harness.write_text(
        f"""
const assert = require("node:assert/strict");
const window = {{ addEventListener() {{}} }};
let accountRequestGeneration = 1;
{response_guard_source}

function deferred() {{
  let resolve;
  const promise = new Promise((yes) => {{ resolve = yes; }});
  return {{ promise, resolve }};
}}

function responseWithDeferredJson(pending) {{
  return {{
    status: 200,
    ok: true,
    headers: {{ get() {{ return null; }} }},
    json() {{ return pending.promise; }},
    text: async () => "ok",
    clone() {{ return responseWithDeferredJson(pending); }},
  }};
}}

(async () => {{
  const first = deferred();
  const guarded = guardAccountScopedResponse(responseWithDeferredJson(first), 1, true);
  const bodyRead = guarded.json();
  accountRequestGeneration = 2;
  first.resolve({{ ok: true, secret: "alice" }});
  await assert.rejects(bodyRead, (error) => error && error.name === "AbortError");

  accountRequestGeneration = 3;
  const second = deferred();
  const clone = guardAccountScopedResponse(responseWithDeferredJson(second), 3, true).clone();
  const cloneRead = clone.json();
  accountRequestGeneration = 4;
  second.resolve({{ ok: true, secret: "carol" }});
  await assert.rejects(cloneRead, (error) => error && error.name === "AbortError");

  accountRequestGeneration = 5;
  const publicPending = deferred();
  const publicResponse = guardAccountScopedResponse(responseWithDeferredJson(publicPending), 5, false);
  const publicRead = publicResponse.json();
  accountRequestGeneration = 6;
  publicPending.resolve({{ ok: true, public: true }});
  assert.deepEqual(await publicRead, {{ ok: true, public: true }});
  process.stdout.write("account response guard checks passed\\n");
}})().catch((error) => {{
  console.error(error && error.stack || error);
  process.exitCode = 1;
}});
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(harness)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "account response guard checks passed" in result.stdout


def test_private_module_state_is_cleared_on_account_change():
    contracts = {
        "01-root-quick-settings.js": "resetRootModuleQuickSettingsAccountState",
        "10-users.js": "resetAdminUsersAccountState",
        "20-chat.js": "resetChatAccountState",
        "25-community.js": "resetCommunityAccountState",
        "30-appeals.js": "resetAppealsAccountState",
        "32-notifications.js": "resetNotificationAccountState",
        "35-drive.js": "resetDriveAccountState",
        "36-comfyui.js": "resetComfyuiAccountState",
        "37-bug-report.js": "resetBugReportAccountState",
        "37-ai-agent.js": "handleAiAgentAccountContextChanged",
        "38-games.js": "resetGameAccountState",
        "39-videos.js": "resetVideoAccountState",
        "40-auth-users.js": "resetAuthUsersAccountState",
        "50-admin.js": "resetAdminAccountState",
        "51-admin-server-mode-launch-check.js": "resetServerModeLaunchCheckAccountState",
        "52-admin-trading.js": "resetRootTradingAdminAccountState",
        "53-admin-storage-economy.js": "resetRootStorageEconomyAdminAccountState",
        "55-economy.js": "resetEconomyAccountState",
        "56-trading.js": "resetTradingAccountState",
        "57-platform-centers.js": "resetPlatformCentersAccountState",
        "58-profile-friends.js": "resetProfileAccountState",
    }
    for relative_path, reset_name in contracts.items():
        source = _read(relative_path)
        assert reset_name in source, relative_path
        assert "hackme:account-context-changed" in source, relative_path

    comfy = _read("36-comfyui.js")
    economy = _read("55-economy.js")
    trading = _read("56-trading.js")
    assert "window.resetComfyuiWorkflowAccountState" in comfy
    assert "window.resetEconomyExplorerAccountState" in economy
    assert "window.resetTradingBotsAccountState" in trading


def test_xhr_and_popup_workflows_keep_original_account_ownership():
    drive = _read("35-drive.js")
    videos = _read("39-videos.js")
    trading_editor = _read("trading-workflow-editor.js")
    comfy_editor = _read("comfyui-workflow-editor.js")

    assert "const driveActiveXhrs = new Set();" in drive
    assert "driveActiveXhrs.forEach((xhr) => {" in drive
    assert "const uploadScope =" in drive
    assert "assertUploadScope();" in drive
    assert "const videoActiveXhrs = new Set();" in videos
    assert "videoActiveXhrs.forEach((xhr) => {" in videos
    assert "const uploadScope =" in videos
    assert "assertUploadScope();" in videos
    assert "accountScopedStorageKey(VIDEO_E2EE_LOCAL_TASK_STORAGE_KEY)" in videos

    for source in (trading_editor, comfy_editor):
        assert "const EDITOR_INITIAL_ACCOUNT_SCOPE = editorAccountStorageScope();" in source
        assert "editorAccountAbortController?.abort();" in source
        assert "function assertEditorAccountScope()" in source
        assert "async function editorFetch" in source
        assert "hackme_web:${EDITOR_INITIAL_ACCOUNT_SCOPE}" in source
        assert 'window.addEventListener("storage"' in source
        assert "window.close();" in source


def test_non_http_private_work_is_generation_bound_and_scrubbed():
    drive = _read("35-drive.js")
    videos = _read("39-videos.js")
    economy = _read("55-economy.js")
    profile = _read("58-profile-friends.js")
    auth_users = _read("40-auth-users.js")
    community = _read("25-community.js")
    comfy = _read("36-comfyui.js")
    comfy_workflows = _read("36-comfyui-workflows.js")
    admin = _read("50-admin.js")
    root_trading = _read("52-admin-trading.js")
    games = _read("38-games.js")
    chess = _read("games/chess.js")
    trading = _read("56-trading.js")
    trading_bots = _read("56-trading-bots.js")

    assert "let driveAccountGeneration = 0;" in drive
    assert "const drivePendingAccountCancels = new Set();" in drive
    assert "function driveAssertOperationCurrent" in drive
    assert "drivePendingAccountCancels.forEach((cancel) => cancel());" in drive
    assert "async function storageAction(path, method = \"POST\", body = null, operation = null)" in drive
    assert "driveAssertOperationCurrent(operationContext);" in drive
    assert "new Uint8Array(plaintext).fill(0);" in drive
    assert "new Uint8Array(rawKey).fill(0);" in drive
    assert "shareKeyBytes.fill(0);" in drive
    assert "function clearDriveShareFragments" in drive
    assert "driveShareFragmentStorageKey()" in drive
    assert "async function pollRemoteDownloadTask(taskId, transferId, operation = null)" in drive
    assert "driveAssertOperationCurrent(operationContext);" in drive
    assert "resumeRemoteDownloadTaskPolling({ ...task, status: \"running\" }, operationContext);" in drive

    assert "accountGeneration: 0," in videos
    assert "const videoPendingAccountCancels = new Set();" in videos
    assert "function videoAssertOperationCurrent" in videos
    assert "clearAllVideoE2eeLocalTasks();" in videos
    assert "rawKeyBytes.fill(0);" in videos
    assert "plaintext.fill(0);" in videos
    assert "shareKeyBytes.fill(0);" in videos
    assert "function clearVideoShareFragments" in videos
    assert "videoShareFragmentStorageKey()" in videos

    assert "let economyAccountGeneration = 0;" in economy
    assert "const economyActiveFileReaders = new Set();" in economy
    assert "economyActiveFileReaders.forEach((reader) =>" in economy
    assert "function economyScrubPrivateJwk" in economy
    assert "economyScrubPrivateJwk(privateJwk);" in economy
    assert 'reader.onabort = () => {' in economy
    assert "economyPendingFilePickerCancels.forEach((cancel) => cancel());" in economy

    assert "let profileAccountGeneration = 0;" in profile
    assert "function profileAssertOperationCurrent" in profile
    assert "profileAvatarCropState.objectUrl !== nextUrl" in profile
    assert "profileAccountGeneration += 1;" in profile

    assert "let authUsersAccountGeneration = 0;" in auth_users
    assert "function authUsersAssertOperationCurrent" in auth_users
    assert "avatarCropState.objectUrl !== nextUrl" in auth_users
    assert "authUsersAccountGeneration += 1;" in auth_users

    assert "let communityAccountGeneration = 0;" in community
    assert "function communityAssertOperationCurrent" in community
    assert "communityAssertOperationCurrent(operation);" in community
    assert "communityAccountGeneration += 1;" in community

    assert "let comfyuiAccountGeneration = 0;" in comfy
    assert "function assertComfyuiMaskEditorOperation" in comfy
    assert "const blob = await comfyuiMaskEditorBlob();\n    assertComfyuiMaskEditorOperation(operation);" in comfy
    assert "comfyuiAccountGeneration += 1;" in comfy
    assert "let comfyuiWorkflowAccountGeneration = 0;" in comfy_workflows
    assert "accountGeneration !== comfyuiWorkflowAccountGeneration" in comfy_workflows
    assert "input?.files?.[0] !== file" in comfy_workflows
    assert "comfyuiWorkflowAccountGeneration += 1;" in comfy_workflows
    assert "let comfyuiTemplateDetailLoadGeneration = 0;" in comfy_workflows
    assert "let comfyuiWorkflowPresetsLoadGeneration = 0;" in comfy_workflows
    assert "let comfyuiWorkflowEditorLoadGeneration = 0;" in comfy_workflows
    assert "pollComfyuiJobUntilDone(jobId, controller, workflowTimeoutSeconds" in comfy_workflows
    assert "comfyuiAssertGenerationCurrent(operation, runToken);" in comfy_workflows
    assert "let comfyuiModelsLoadGeneration = 0;" in comfy
    assert "comfyuiModelsLoadPromise === loadPromise" in comfy
    assert "comfyuiGgufProfilesLoadPromise === loadPromise" in comfy
    assert "comfyuiDiffusersInspectInflight.get(cacheKey) === requestPromise" in comfy
    assert "async function pollComfyuiModelDownloadJob(jobId, operation = null, existingPollToken = null)" in comfy
    assert "window.resetComfyUITemplateImporterAccountState" in comfy
    assert "let comfyuiGenerationRunToken = null;" in comfy
    assert "function comfyuiAssertGenerationCurrent" in comfy
    assert "const comfyuiInputAssetHydrationTokens = new Map();" in comfy
    assert "let comfyuiImagePickerLoadGeneration = 0;" in comfy

    assert "let adminAccountGeneration = 0;" in admin
    assert "function adminAssertOperationCurrent" in admin
    assert "waitForRestartOffline(25000, operation)" in admin
    assert "operationContext.requestGeneration" in admin

    assert "let rootTradingAdminAccountGeneration = 0;" in root_trading
    assert "async function pollRootBtcTradeStartJob(jobId, operation = null)" in root_trading
    assert "rootTradingAdminAssertOperationCurrent(operationContext);" in root_trading

    assert "let gameAccountGeneration = 0;" in games
    assert "let chessMoveToken = null;" in games
    assert "const targetMatchId = previousMatch.id;" in chess
    assert "chessMoveToken !== moveToken" in chess

    assert "let tradingAccountGeneration = 0;" in trading
    assert "let tradingDashboardLoadGeneration = 0;" in trading
    assert "tradingLivePriceInFlight === controller" in trading
    assert "renderGridBotPreview({ quiet: true, operation })" in trading_bots

    bootstrap = _read("90-bootstrap.js")
    assert "const requestGeneration = accountRequestGeneration;" in bootstrap
    assert "assertAccountRequestGeneration(requestGeneration, true);" in bootstrap

    comfy_editor = _read("comfyui-workflow-editor.js")
    import_start = comfy_editor.index("async function importJsonFile")
    import_end = comfy_editor.index("function renderNodeCatalogList", import_start)
    import_source = comfy_editor[import_start:import_end]
    assert "const text = await file.text();\n      assertEditorAccountScope();" in import_source
    assert "event.target?.files?.[0] !== file" in import_source


def test_account_reset_clears_passwords_files_and_private_editors():
    drive = _read("35-drive.js")
    videos = _read("39-videos.js")
    economy = _read("55-economy.js")
    profile = _read("58-profile-friends.js")
    auth_users = _read("40-auth-users.js")
    community = _read("25-community.js")

    for field in (
        "drive-e2ee-session-passphrase",
        "drive-upload-mode-passphrase",
        "drive-share-password",
        "drive-new-doc-content",
        "storage-upload-folder",
    ):
        assert field in drive
    for field in ("video-upload-file", "video-cover-file", "video-share-password"):
        assert field in videos
    for field in ("economy-wallet-generated-trade-password", "economy-wallet-file-password"):
        assert field in economy
    for field in ("profile-friend-code", "profile-avatar-cloud-file"):
        assert field in profile
    for field in ("li-pw", "li-internal-test-token", "edit-user-pw"):
        assert field in auth_users
    for field in ("community-thread-content", "community-reply-content", "community-thread-media-input"):
        assert field in community
