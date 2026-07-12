from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]


def test_ai_agent_conversation_state_isolated_across_races(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser-state race test")

    source = (ROOT / "public" / "js" / "37-ai-agent.js").read_text(encoding="utf-8")
    prelude = r"""
const assert = require("node:assert/strict");
const nativeSetTimeout = globalThis.setTimeout;
const nativeClearTimeout = globalThis.clearTimeout;
let fakeTimers = null;
function setTimeout(callback, delay) {
  if (fakeTimers) {
    const timer = { callback, delay, cancelled: false };
    fakeTimers.push(timer);
    return timer;
  }
  return nativeSetTimeout(callback, delay);
}
function clearTimeout(timer) {
  if (timer && typeof timer === "object" && "cancelled" in timer) {
    timer.cancelled = true;
    return;
  }
  nativeClearTimeout(timer);
}

let currentScope = "anonymous";
let currentUser = "";
let currentUserId = null;
let currentUserRole = "";
let currentModuleTab = "ai-agent";
const API = "/api";
let apiFetch = async () => { throw new Error("apiFetch was not configured"); };
const frontendFailures = [];
function reportFrontendFailure(scope, error) {
  frontendFailures.push({ scope, message: String(error && error.message || error) });
}
function getCurrentAccountStorageScope() { return currentScope; }
function $(id) { return null; }
function sanitize(value) { return String(value || ""); }
function showAppToast() {}
function setInactivitySuspendState() {}
function fetchCsrfToken() { return Promise.resolve("csrf"); }
function getCsrfToken() { return "csrf"; }
function setCsrfToken() {}
function canAccessModule() { return true; }
function isFeatureEnabledForUi() { return true; }
function requestAnimationFrame(callback) { callback(); }

const localValues = new Map();
const localStorage = {
  getItem(key) { return localValues.has(key) ? localValues.get(key) : null; },
  setItem(key, value) { localValues.set(key, String(value)); },
  removeItem(key) { localValues.delete(key); },
};
const document = {
  addEventListener() {},
  querySelector() { return null; },
  querySelectorAll() { return []; },
  hidden: false,
};
const window = {
  addEventListener() {},
  dispatchEvent() {},
  setTimeout: (...args) => setTimeout(...args),
  clearTimeout: (...args) => clearTimeout(...args),
  confirm() { return false; },
};
"""
    checks = r"""
function response(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "",
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
}
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function flush() {
  await Promise.resolve();
  await Promise.resolve();
}
function resetState(scope) {
  currentScope = scope;
  AI_AGENT_STATE.available = true;
  AI_AGENT_STATE.accountScope = scope;
  AI_AGENT_STATE.messages = [];
  AI_AGENT_STATE.sessionId = "";
  AI_AGENT_STATE.loadingConversation = false;
  AI_AGENT_STATE.conversationLoadToken = 0;
  AI_AGENT_STATE.persistTimer = null;
  AI_AGENT_STATE.persistRetryTimers = {};
  AI_AGENT_STATE.persistRetryCounts = {};
  AI_AGENT_STATE.persistInFlight = {};
  AI_AGENT_STATE.persistControllers = {};
  AI_AGENT_STATE.conversationEpochs = {};
  AI_AGENT_STATE.conversationPersistError = "";
  frontendFailures.length = 0;
  localValues.clear();
}

async function testLateConversationLoadCannotCrossAccounts() {
  resetState("alice");
  const pending = [];
  apiFetch = (url, options = {}) => {
    const item = deferred();
    pending.push({ url, options, ...item });
    return item.promise;
  };

  const aliceLoad = aiAgentLoadConversation("alice");
  currentScope = "bob";
  AI_AGENT_STATE.accountScope = "bob";
  const bobLoad = aiAgentLoadConversation("bob");
  assert.equal(pending.length, 2);

  pending[1].resolve(response({
    ok: true,
    conversation_id: "bob-session",
    payload: { sessionId: "bob-session", messages: [{ role: "user", content: "bob-only" }] },
  }));
  await bobLoad;
  assert.equal(AI_AGENT_STATE.messages[0].content, "bob-only");

  pending[0].resolve(response({
    ok: true,
    conversation_id: "alice-session",
    payload: { sessionId: "alice-session", messages: [{ role: "user", content: "alice-secret" }] },
  }));
  await aliceLoad;
  assert.equal(AI_AGENT_STATE.accountScope, "bob");
  assert.equal(AI_AGENT_STATE.sessionId, "bob-session");
  assert.deepEqual(AI_AGENT_STATE.messages.map((item) => item.content), ["bob-only"]);
}

async function testPersistRetryUsesImmutableAccountSnapshot() {
  resetState("alice");
  fakeTimers = [];
  AI_AGENT_STATE.sessionId = "alice-session";
  AI_AGENT_STATE.messages = [{ role: "user", content: "alice-only" }];
  const requestBodies = [];
  let attempt = 0;
  apiFetch = async (url, options = {}) => {
    requestBodies.push(JSON.parse(options.body));
    attempt += 1;
    return attempt === 1
      ? response({ ok: false, msg: "temporary" }, 500)
      : response({ ok: true });
  };

  assert.equal(await aiAgentPersistConversation("alice"), false);
  assert.equal(fakeTimers.length, 1);
  currentScope = "bob";
  AI_AGENT_STATE.accountScope = "bob";
  AI_AGENT_STATE.sessionId = "bob-session";
  AI_AGENT_STATE.messages = [{ role: "user", content: "bob-only" }];

  const retry = fakeTimers.shift();
  retry.callback();
  await flush();
  assert.equal(requestBodies.length, 2);
  assert.equal(requestBodies[1].conversation_id, "alice-session");
  assert.deepEqual(requestBodies[1].payload.messages.map((item) => item.content), ["alice-only"]);
  fakeTimers = null;
}

async function testClearWaitsForWritesAndInvalidatesRetries() {
  resetState("alice");
  AI_AGENT_STATE.sessionId = "alice-session";
  AI_AGENT_STATE.messages = [{ role: "user", content: "remove-me" }];
  const put = deferred();
  const methods = [];
  apiFetch = async (url, options = {}) => {
    methods.push(options.method || "GET");
    if (options.method === "PUT") return put.promise;
    if (options.method === "DELETE") return response({ ok: true });
    throw new Error(`unexpected method ${options.method}`);
  };

  const persist = aiAgentPersistConversation("alice");
  const clear = clearAiAgentConversation();
  await flush();
  assert.deepEqual(methods, ["PUT"]);
  put.resolve(response({ ok: true }));
  await persist;
  await clear;
  assert.deepEqual(methods, ["PUT", "DELETE"]);
  assert.equal(AI_AGENT_STATE.messages.length, 0);
  assert.equal(AI_AGENT_STATE.sessionId, "");
  assert.equal(AI_AGENT_STATE.persistRetryTimers.alice, undefined);
}

(async () => {
  await testLateConversationLoadCannotCrossAccounts();
  await testPersistRetryUsesImmutableAccountSnapshot();
  await testClearWaitsForWritesAndInvalidatesRetries();
  process.stdout.write("ai-agent conversation race checks passed\n");
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exitCode = 1;
});
"""
    harness = tmp_path / "ai_agent_conversation_races.js"
    harness.write_text(f"{prelude}\n{source}\n{checks}", encoding="utf-8")
    result = subprocess.run(
        [node, str(harness)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ai-agent conversation race checks passed" in result.stdout
