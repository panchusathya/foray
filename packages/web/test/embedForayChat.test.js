import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

// The embed widget ships as a raw static file with no build step, so the test
// evaluates the file the way a browser would and exercises the pure core it
// exposes under the test hook. The DOM boot path is deliberately skipped: the
// hook returns before any document access.

const here = path.dirname(fileURLToPath(import.meta.url));
const source = await readFile(
  path.join(here, "..", "public", "embed", "foray-chat.js"),
  "utf8"
);

function loadCore() {
  const context = { crypto: undefined, console };
  context.globalThis = context;
  context.__FORAY_CHAT_TEST__ = true;
  vm.createContext(context);
  new vm.Script(source, { filename: "foray-chat.js" }).runInContext(context);
  assert.ok(context.__forayChat, "test hook did not export the pure core");
  return context.__forayChat;
}

class FakeStorage {
  constructor() {
    this.map = new Map();
  }
  getItem(k) {
    return this.map.has(k) ? this.map.get(k) : null;
  }
  setItem(k, v) {
    this.map.set(k, String(v));
  }
  removeItem(k) {
    this.map.delete(k);
  }
}

test("the file parses and exposes the pure core under the test hook", () => {
  const core = loadCore();
  for (const name of [
    "apiBase",
    "storageKey",
    "clientMessageId",
    "startPayload",
    "messagePayload",
    "savedSession",
    "persistSession",
    "visibleMessages",
    "errorText",
  ]) {
    assert.equal(typeof core[name], "function", name + " missing");
  }
});

test("apiBase strips trailing slashes so paths concatenate cleanly", () => {
  const core = loadCore();
  assert.equal(core.apiBase({ api: "https://app.example.com/" }), "https://app.example.com");
  assert.equal(core.apiBase({ api: "https://app.example.com" }), "https://app.example.com");
  assert.equal(core.apiBase({}), "");
});

test("storageKey is scoped to the org half of the widget token", () => {
  const core = loadCore();
  const a = core.storageKey({ token: "org-a.signature-one" });
  const b = core.storageKey({ token: "org-b.signature-two" });
  assert.notEqual(a, b);
  assert.ok(a.includes("org-a"));
  assert.ok(!a.includes("signature-one"), "signature must not leak into storage keys");
});

test("message payload carries the thread token and a fresh idempotency key", () => {
  const core = loadCore();
  const session = { thread_id: "t1", thread_token: "tok-1" };
  const one = core.messagePayload(session, "hello");
  const two = core.messagePayload(session, "hello");
  assert.equal(one.token, "tok-1");
  assert.equal(one.body, "hello");
  assert.ok(one.client_message_id.length > 0);
  assert.notEqual(
    one.client_message_id,
    two.client_message_id,
    "a retry of a NEW message must not collide with the previous one"
  );
});

test("sessions persist and resume through storage, and garbage stays out", () => {
  const core = loadCore();
  const storage = new FakeStorage();
  const config = { token: "org-a.sig" };

  assert.equal(core.savedSession(storage, config), null);

  core.persistSession(storage, config, {
    thread_id: "t1",
    thread_token: "tok-1",
    messages: [{ body: "should not be persisted" }],
  });
  const resumed = core.savedSession(storage, config);
  // The core runs in a vm realm, so compare values rather than prototypes.
  assert.equal(resumed.thread_id, "t1");
  assert.equal(resumed.thread_token, "tok-1");
  assert.equal(Object.keys(resumed).length, 2, "message bodies must not be persisted");

  storage.setItem(core.storageKey(config), "{not json");
  assert.equal(core.savedSession(storage, config), null);

  storage.setItem(core.storageKey(config), JSON.stringify({ thread_id: "t2" }));
  assert.equal(core.savedSession(storage, config), null, "a token-less save must not resume");
});

test("only messages with a body render, and known statuses get friendly copy", () => {
  const core = loadCore();
  const shown = core.visibleMessages({
    messages: [{ body: "hi" }, { body: "" }, null, { body: "there" }],
  });
  assert.deepEqual(
    shown.map((m) => m.body),
    ["hi", "there"]
  );
  assert.ok(core.errorText(429).includes("busy"));
  assert.ok(core.errorText(401).includes("expired"));
  assert.ok(core.errorText(500).length > 0);
});
