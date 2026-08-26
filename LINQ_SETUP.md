# Linq iMessage intake — setup steps

Tester-only rollout. Complete these in order after merging the Linq channel code.

## 1. Linq account credentials

From [Linq API Tooling](https://dashboard.linqapp.com/api-tooling):

- **API key** → `LINQ_API_KEY`
- **Phone number** assigned to your account → `LINQ_FROM_NUMBER` (E.164, e.g. `+14155551234`)

Add to `/opt/juicebox/.env` (production) or repo `.env` (local):

```bash
LINQ_API_KEY=...
LINQ_FROM_NUMBER=+1...
LINQ_ENABLED=false          # flip to true after webhook secret is set
LINQ_WEBHOOK_SECRET=        # filled in step 3
```

Production: variables must also appear in `docker-compose.prod.yml` `x-app-env` (already
listed as `${LINQ_*:-}`). Recreate `api` + `worker` after editing `.env`.

Verify:

```bash
python deploy/linq_setup.py verify
python deploy/linq_setup.py numbers
```

## 2. Juicebox API reachable from Linq

Linq POSTs webhooks to a **public HTTPS URL**. Production example:

```text
https://app.goforay.io/v1/intake/linq/webhook?version=2026-02-03
```

Local dev needs a tunnel (ngrok, Cloudflare Tunnel, etc.) pointing at `:8000`.

The route is **unauthenticated** but **signature-verified** — same posture as voice
intake. No key configured → every request refused.

## 3. Create the Linq webhook subscription

Pin webhook version **`2026-02-03`** on the URL query string.

```bash
python deploy/linq_setup.py create-webhook \
  --url "https://app.goforay.io/v1/intake/linq/webhook?version=2026-02-03" \
  --phone "+14155551234" \
  --write-env
```

This subscribes to `message.received`, `message.sent`, `message.delivered`,
`message.read`, and `message.failed`, filters to your line, and appends
`LINQ_WEBHOOK_SECRET` + `LINQ_ENABLED=true` to `.env`.

Read receipts (`message.read`) only fire on iMessage/RCS when the recipient has
read receipts enabled. SMS fallback never emits delivered/read — staying at
Sent is expected, not a bug.

**Store `signing_secret` immediately** — Linq returns it once and cannot retrieve it
later.

List / inspect:

```bash
python deploy/linq_setup.py webhooks
python deploy/linq_setup.py status
```

## 4. Register the line in Juicebox

Linq does not know your org. Map the number → org in `intake_numbers`:

```bash
# demo org id from `juicebox seed` output, or your workspace org
juicebox intake-number +14155551234 --org <org_id> --provider linq --label "linq intake"
```

Or print the command:

```bash
python deploy/linq_setup.py print-register --org <org_id> --phone +14155551234
```

List registered numbers:

```bash
juicebox intake-number --list
```

**Important:** do not register the Linq number with `--provider retell`. Voice dial-out
selects `provider == "retell"` only; a second row on the same org with the wrong
provider breaks outbound calling.

## 5. Run the stack

```bash
# local
.venv/bin/python -m juicebox.cli serve --reload          # :8000
cd packages/web && npm run dev                            # :5173 (optional)
.venv/bin/python -m juicebox.cli work                     # drain KIND_INTAKE_TURN

# production
docker compose -f docker-compose.prod.yml up -d api worker
```

Set `LINQ_ENABLED=true` only after steps 1–4 are complete.

## 6. Smoke test

1. Text the Linq number from an **iPhone** (iMessage path).
2. Expect HTTP 200 to Linq within 10 seconds.
3. Check API logs for `KIND_INTAKE_TURN` enqueued.
4. Run worker (or wait for loop): reply arrives over iMessage.
5. Reply with a LinkedIn URL; expect a confirm-or-gap-fill text, then a
   `candidate_preferences` row once the short flow wraps up.
6. Re-send the same message id (Linq retry simulation) → no duplicate turn.

## 7. Seed helper (optional)

If `LINQ_FROM_NUMBER` is set when running `juicebox seed`, the demo org gets an
`IntakeNumber` row with `provider=linq` automatically.

## 8. LinkedIn scrape (LinkedIn-first intake)

Linq threads ask for a LinkedIn URL first, then scrape the profile in a
Playwright browser on the worker. Without a logged-in session the scrape fails
open and the thread asks 2–3 leftover questions instead.

On the worker host:

```bash
# optional extra; the API image does not need this
pip install 'packages/core[linkedin]'
playwright install chromium

# session cookie — Playwright storage_state JSON, or a raw li_at=... string
# never commit this
LINKEDIN_SESSION_COOKIE='[{"name":"li_at","value":"...","domain":".linkedin.com","path":"/"}]'
# or
LINKEDIN_SESSION_PATH=/opt/juicebox/secrets/linkedin-cookies.json
```

Add the same vars to `/opt/juicebox/.env` and recreate `worker`. LinkedIn ToS /
account-ban risk is operator-owned; rotate the session when scrapes start
returning auth-wall errors.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| 401 on webhook | Wrong/missing `LINQ_WEBHOOK_SECRET`, or body re-serialised before verify |
| 404 on webhook | No `intake_numbers` row for this E.164 with `provider=linq` and `active=true` |
| 503 on webhook | `LINQ_ENABLED=false` or missing `LINQ_API_KEY` / `LINQ_FROM_NUMBER` |
| No reply | Worker not running; `LinqChannel` not configured; missing `provider_thread_id` |
| Stuck at Sent, never Read | SMS fallback, or the recipient has read receipts off — expected |
| Scrape skipped, extra questions | Missing `LINKEDIN_SESSION_*`, Playwright, or an auth wall |
| Voice calls wrong number | Linq number registered with `--provider retell` — fix provider column |

## Out of scope (accepted gaps)

- **Android / SMS fallback** until Linq registration model is confirmed
- **Conversational opt-out** beyond keyword STOP/HELP
- **Twilio SMS** until TFV clears — see [`SMS_ENABLEMENT.md`](SMS_ENABLEMENT.md)

## Resume attachments

Inbound attachments are handled: the webhook enqueues an `intake.attachment`
job, the worker downloads each media part from Linq's CDN (with one refresh
retry through `GET /v3/attachments/{id}` for expired ephemeral-tier URLs) and,
when a file sniffs as a resume (PDF or DOCX by magic bytes), stores it as the
thread's `CandidateDocument` and queues the ordinary `docs.parse` → promote →
confirm path. A resume sent before the thread has an email is held at a
thread-scoped storage path and attached the moment identity capture links a
candidate. Non-resume media (photos, video) get an honest "can't read that"
reply with the upload-page link while the profile question is open, and are
ignored otherwise. OCR on scanned images remains out of scope.
