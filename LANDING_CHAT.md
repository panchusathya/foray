# Landing-page chat — wiring goforay.io to the intake API

The goforay.io landing page has a live "try it now" chat in its hero (the
phone mock accepts typed messages). This document is the contract for pointing
that front end at the real backend instead of the scripted demo, using the
web-widget intake that already ships in `routers/messaging.py`.

Nothing here adds a new API. The work is one static embed file, one token, and
two lines of deploy configuration.

## What already exists (server side)

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /v1/intake/widget-token` | recruiter bearer token | Mint the signed, org-scoped token the marketing site embeds. One-time, at integration setup. |
| `POST /v1/intake/chat/sessions` | widget token in body | Open a conversation; returns `thread_id`, `thread_token`, and the greeting. |
| `POST /v1/intake/chat/sessions/{id}/messages` | thread token in body | One inbound message, one synchronous turn. Idempotent on `client_message_id`. |
| `GET /v1/intake/chat/sessions/{id}?token=` | thread token | Resume a conversation after a reload, even days later. |

Safety posture is already handled where it belongs:

- **Org resolution** comes from the verified half of the widget token, never
  from the request body (`_org_from_widget_token`).
- **Rate limits**: nginx `limit_req` on `/v1/intake/chat/` at the edge, plus
  durable per-org and per-sender thread caps in the router.
- **Consent** is recorded per thread (`SOURCE_WEB_WIDGET`) the moment the
  visitor starts typing — the landing page has nothing to implement.
- **No score, rank, or recommendation** crosses this surface (see
  `INTAKE_COMPLIANCE.md`).

## What this change adds

`packages/web/public/embed/foray-chat.js` — a dependency-free, build-free
widget the landing site loads with one tag. Vite copies `public/` into the
built app verbatim, so the file is served by the existing web container at
`/embed/foray-chat.js` with no new infrastructure.

```html
<script src="https://app.<api-host>/embed/foray-chat.js" defer
        data-token="<org_id>.<signature>"
        data-api="https://app.<api-host>"
        data-accent="#6A50C8"></script>
```

Behavior: floating launcher, opens a panel, starts a session on first use,
optimistic send with typing indicator, resumes from `localStorage`
(`thread_id` + `thread_token` only — message bodies are never persisted
client-side), friendly copy on 429/401, fresh `client_message_id` per message
so double-taps cannot double-extract.

## Integration steps (deploy checklist)

1. **Mint the token once** as any recruiter user of the Foray org:
   `GET /v1/intake/widget-token` → embed the returned value as `data-token`.
   Rotating `SECRET_KEY` invalidates it; re-mint and redeploy the site.
2. **CORS**: the landing page is a different origin, so it has to be allowed.
   Set it in the production `.env` **and redeploy** —
   `CORS_ORIGINS=https://app.goforay.io,https://goforay.io,https://www.goforay.io`.
   List the app domain explicitly: the variable replaces the default rather
   than adding to it, so omitting it locks the app out of its own API.
3. **Serve the file**: ships automatically with the next web image build.
4. **Landing site**: replace the scripted demo's send handler with the embed,
   or load the embed as the floating chat and keep the hero mock as pure
   theater. The current design mock maps 1:1: first typed message →
   `POST /sessions` then `POST /messages`; every later send → one
   `POST /messages`; reload → `GET /sessions/{id}`.

## Non-goals

- The SMS (628 number), voice, and email intake paths are untouched.
- The widget does not render job cards or tapbacks yet — the conversation is
  plain text both ways, which is what the API speaks today. If `run_turn`
  later returns structured matches, extend `ChatMessageOut` first and the
  widget second.
- No analytics beacon. The thread itself is the funnel record.
