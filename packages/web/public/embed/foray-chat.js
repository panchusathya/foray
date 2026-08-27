/* Foray landing-page intake chat.
 *
 * A single static file the marketing site embeds with one script tag:
 *
 *   <script src="https://app.example.com/embed/foray-chat.js" defer
 *           data-token="<org_id>.<signature>"
 *           data-api="https://app.example.com"></script>
 *
 * It talks to the unauthenticated web-widget intake API
 * (POST /v1/intake/chat/sessions, POST /v1/intake/chat/sessions/{id}/messages,
 * GET /v1/intake/chat/sessions/{id}) and nothing else. The widget token is the
 * signed, org-scoped token from GET /v1/intake/widget-token -- the org is read
 * from its verified half server-side, never from this page.
 *
 * No dependencies and no build step, because the landing site has neither.
 * State that must survive a reload (thread id + thread token) lives in
 * localStorage so a half-finished conversation resumes where it stopped.
 */
(function () {
  "use strict";

  // ---- pure helpers (exported for tests; no DOM below this line) ----------

  function apiBase(config) {
    return String(config.api || "").replace(/\/+$/, "");
  }

  function storageKey(config) {
    // Keyed by the org half of the widget token so two orgs' widgets on one
    // browser never resume each other's threads.
    var token = String(config.token || "");
    return "foray-chat:" + token.slice(0, token.indexOf(".") > 0 ? token.indexOf(".") : 24);
  }

  function clientMessageId() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
    return "cm-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
  }

  function startPayload(config) {
    return { token: String(config.token || "") };
  }

  function messagePayload(session, body) {
    return {
      token: session.thread_token,
      body: body,
      client_message_id: clientMessageId(),
    };
  }

  function savedSession(storage, config) {
    try {
      var raw = storage.getItem(storageKey(config));
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (!parsed || !parsed.thread_id || !parsed.thread_token) return null;
      return parsed;
    } catch (err) {
      return null;
    }
  }

  function persistSession(storage, config, session) {
    try {
      storage.setItem(
        storageKey(config),
        JSON.stringify({ thread_id: session.thread_id, thread_token: session.thread_token })
      );
    } catch (err) {
      /* private mode: the conversation just does not resume */
    }
  }

  function visibleMessages(session) {
    return (session && session.messages ? session.messages : []).filter(function (m) {
      return m && m.body;
    });
  }

  function errorText(status) {
    if (status === 429) return "we're a little busy right now. give it a minute and try again";
    if (status === 401) return "this conversation expired. send a message to start a fresh one";
    return "something went wrong on our side. try again in a moment";
  }

  // ---- test hook: expose the pure core, skip the DOM boot -----------------

  if (typeof globalThis !== "undefined" && globalThis.__FORAY_CHAT_TEST__) {
    globalThis.__forayChat = {
      apiBase: apiBase,
      storageKey: storageKey,
      clientMessageId: clientMessageId,
      startPayload: startPayload,
      messagePayload: messagePayload,
      savedSession: savedSession,
      persistSession: persistSession,
      visibleMessages: visibleMessages,
      errorText: errorText,
    };
    return;
  }

  if (typeof document === "undefined") return;

  var script = document.currentScript;
  if (!script) return;
  var config = {
    token: script.getAttribute("data-token") || "",
    api: script.getAttribute("data-api") || "",
    accent: script.getAttribute("data-accent") || "#6A50C8",
  };
  if (!config.token) {
    if (typeof console !== "undefined") console.warn("[foray-chat] data-token missing; widget not mounted");
    return;
  }

  // ---- network -------------------------------------------------------------

  function call(method, path, body) {
    return fetch(apiBase(config) + path, {
      method: method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (res) {
      if (!res.ok) {
        var err = new Error("foray-chat http " + res.status);
        err.status = res.status;
        throw err;
      }
      return res.json();
    });
  }

  function startSession() {
    return call("POST", "/v1/intake/chat/sessions", startPayload(config));
  }

  function resumeSession(saved) {
    return call(
      "GET",
      "/v1/intake/chat/sessions/" + encodeURIComponent(saved.thread_id) +
        "?token=" + encodeURIComponent(saved.thread_token)
    );
  }

  function sendMessage(session, body) {
    return call(
      "POST",
      "/v1/intake/chat/sessions/" + encodeURIComponent(session.thread_id) + "/messages",
      messagePayload(session, body)
    );
  }

  // ---- UI -------------------------------------------------------------------

  var css =
    ".fjx-launcher{position:fixed;bottom:22px;right:22px;z-index:9999;width:58px;height:58px;border-radius:50%;border:none;cursor:pointer;background:ACCENT;color:#fff;box-shadow:0 10px 26px rgba(20,20,40,.3);display:grid;place-items:center}" +
    ".fjx-launcher:hover{transform:translateY(-2px)}" +
    ".fjx-panel{position:fixed;bottom:92px;right:22px;z-index:9999;width:344px;max-width:calc(100vw - 32px);height:480px;max-height:calc(100vh - 120px);background:#fff;border:1px solid rgba(20,20,40,.14);border-radius:18px;box-shadow:0 28px 70px rgba(20,20,40,.28);display:flex;flex-direction:column;overflow:hidden;font:14px/1.5 system-ui,-apple-system,sans-serif;color:#1c1c28}" +
    ".fjx-head{display:flex;align-items:center;gap:8px;padding:12px 16px;background:ACCENT;color:#fff;font-weight:600}" +
    ".fjx-head small{margin-left:auto;font-weight:400;opacity:.85}" +
    ".fjx-log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:8px}" +
    ".fjx-msg{max-width:85%;padding:8px 12px;border-radius:15px;white-space:pre-wrap;word-break:break-word}" +
    ".fjx-in{align-self:flex-start;background:#ECECF1;border-bottom-left-radius:4px}" +
    ".fjx-out{align-self:flex-end;background:ACCENT;color:#fff;border-bottom-right-radius:4px}" +
    ".fjx-note{align-self:center;font-size:11.5px;color:#8a8a99}" +
    ".fjx-bar{display:flex;gap:8px;padding:10px;border-top:1px solid rgba(20,20,40,.1)}" +
    ".fjx-bar input{flex:1;min-width:0;border:1px solid rgba(20,20,40,.18);border-radius:999px;padding:9px 14px;font:inherit;outline:none}" +
    ".fjx-bar input:focus{border-color:ACCENT}" +
    ".fjx-send{width:38px;height:38px;border-radius:50%;border:none;cursor:pointer;background:ACCENT;color:#fff;display:grid;place-items:center;flex:0 0 auto}" +
    ".fjx-dots{display:inline-flex;gap:4px;align-self:flex-start;background:#ECECF1;border-radius:15px;border-bottom-left-radius:4px;padding:12px 14px}" +
    ".fjx-dots i{width:6px;height:6px;border-radius:50%;background:#8E8E93;animation:fjx-b 1s ease-in-out infinite}" +
    ".fjx-dots i:nth-child(2){animation-delay:.15s}.fjx-dots i:nth-child(3){animation-delay:.3s}" +
    "@keyframes fjx-b{0%,60%,100%{transform:translateY(0);opacity:.4}30%{transform:translateY(-3px);opacity:1}}";

  var style = document.createElement("style");
  style.textContent = css.split("ACCENT").join(config.accent);
  document.head.appendChild(style);

  var state = { session: null, open: false, busy: false };

  var launcher = document.createElement("button");
  launcher.className = "fjx-launcher";
  launcher.setAttribute("aria-label", "Chat with foray");
  launcher.innerHTML =
    '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 8.5-8.5 8.38 8.38 0 0 1 8.5 8.5Z"></path></svg>';

  var panel = document.createElement("div");
  panel.className = "fjx-panel";
  panel.style.display = "none";
  panel.innerHTML =
    '<div class="fjx-head"><span>foray</span><small>replies in minutes</small></div>' +
    '<div class="fjx-log" role="log" aria-live="polite"></div>' +
    '<div class="fjx-bar"><input type="text" placeholder="text foray anything" aria-label="Message" maxlength="4000">' +
    '<button class="fjx-send" aria-label="Send"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"></path><path d="m5 12 7-7 7 7"></path></svg></button></div>';

  var log = panel.querySelector(".fjx-log");
  var input = panel.querySelector("input");
  var send = panel.querySelector(".fjx-send");

  function note(text) {
    var el = document.createElement("span");
    el.className = "fjx-note";
    el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
  }

  function render() {
    log.textContent = "";
    visibleMessages(state.session).forEach(function (m) {
      var el = document.createElement("div");
      el.className = "fjx-msg " + (m.direction === "inbound" ? "fjx-out" : "fjx-in");
      el.textContent = m.body;
      log.appendChild(el);
    });
    if (state.busy) {
      var dots = document.createElement("span");
      dots.className = "fjx-dots";
      dots.innerHTML = "<i></i><i></i><i></i>";
      log.appendChild(dots);
    }
    log.scrollTop = log.scrollHeight;
  }

  function adopt(session) {
    state.session = session;
    persistSession(window.localStorage, config, session);
    render();
  }

  function ensureSession() {
    if (state.session) return Promise.resolve(state.session);
    var saved = savedSession(window.localStorage, config);
    var attempt = saved
      ? resumeSession(saved).catch(function () {
          return startSession();
        })
      : startSession();
    return attempt.then(function (session) {
      adopt(session);
      return session;
    });
  }

  function submit() {
    var body = input.value.trim();
    if (!body || state.busy) return;
    input.value = "";
    state.busy = true;
    ensureSession()
      .then(function (session) {
        // Show the outbound immediately; the server echo replaces it.
        var mine = document.createElement("div");
        mine.className = "fjx-msg fjx-out";
        mine.textContent = body;
        log.appendChild(mine);
        render();
        return sendMessage(session, body);
      })
      .then(function (session) {
        state.busy = false;
        adopt(session);
      })
      .catch(function (err) {
        state.busy = false;
        render();
        if (err && err.status === 401) {
          try {
            window.localStorage.removeItem(storageKey(config));
          } catch (e) { /* ignore */ }
          state.session = null;
        }
        note(errorText(err && err.status));
      });
  }

  launcher.addEventListener("click", function () {
    state.open = !state.open;
    panel.style.display = state.open ? "flex" : "none";
    if (state.open) {
      ensureSession()
        .then(function () {
          input.focus();
        })
        .catch(function (err) {
          note(errorText(err && err.status));
        });
    }
  });
  send.addEventListener("click", submit);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") submit();
  });

  document.body.appendChild(panel);
  document.body.appendChild(launcher);
})();
