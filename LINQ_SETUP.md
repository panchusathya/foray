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
| Voice memo answered "can you type it instead" | `INTAKE_TRANSCRIPTION_ENABLED` or `ASSEMBLYAI_API_KEY` unset, or the format is not in the accepted list |
| Voice memo gets no reply at all | Worker not running — `intake.attachment` is queued, not inline |
| Job cards arrive as text only | `intake_threads.last_service` is empty or `SMS`; it is set from the first inbound or `message.sent` |
| Card has no logo | No `hiringOrganization.logo`, `og:image` or favicon on the posting page, or the host blocked us. Check `company_brands.error` |

## 9. Inbound voice memos (optional)

A candidate on iMessage will reply with a voice memo, and until this existed the
message was dropped on the floor. Two variables, and **both** are needed:

```bash
INTAKE_TRANSCRIPTION_ENABLED=true
ASSEMBLYAI_API_KEY=...
ASSEMBLYAI_TIMEOUT_SECONDS=90   # optional
```

The flag without the key, or the key without the flag, leaves the
`NullTranscriber` installed. That is a supported state, not a broken one: an
audio message then gets exactly one reply asking the person to type it instead.
Silence is what the channel used to do, and it reads as being ignored.

How it runs: a voice memo is an attachment, so it takes the same
`intake.attachment` job a resume does — audio is just recognised one branch
earlier, before the resume sniffer. The worker downloads it, transcribes it,
writes the words onto `intake_messages.body` with `body_source='transcript'`,
and then queues the turn. So the worker has to be running for a voice memo to
get an answer, the same as any other turn.

**A spoken STOP stops the thread.** `looks_like_stop` reads a message body, and
an audio message has none until the worker fills one in — so the opt-out check
runs on the transcript, before the turn is queued. Nothing else in the
compliance path changed.

Formats accepted: MP3, M4A, AAC, CAF, WAV, AIFF, AMR — the set Linq itself
takes, checked as a closed list rather than `audio/*`, so a format the vendor
would refuse never costs a download. Size is capped by `MAX_UPLOAD_BYTES`, the
same ceiling the upload page uses.

## 10. Branded job cards

Roles now go out as a rendered card image alongside the text, on iMessage and
RCS only — an image over SMS is an MMS, and `IntakeThread.last_service` (set
from what Linq reports on inbound and on `message.sent`) is what decides. An
unknown protocol falls back to text.

No configuration. The logo and palette come from the employer's own posting
page (`enrich/brand.py`), cached in `company_brands`; no vendor and no key. A
posting with no discoverable logo gets a neutral Foray-coloured card, which is
still a real card.

To look at one before shipping a layout change:

```bash
juicebox job-card --sample --out /tmp/cards
```

```bash
juicebox job-card <posting_id> --out /tmp/cards
```

**The iMessage poll is gone.** It read as a survey rather than a job, it was
iMessage-only, and its option text was a fragile join key. Tapbacks stay: a
thumbs-up on a card bubble is still an apply, resolved through
`JobProposal.message_id`.

## Out of scope (accepted gaps)

- **Android / SMS fallback** until Linq registration model is confirmed
- **Conversational opt-out** beyond keyword STOP/HELP
- **Twilio SMS** until TFV clears — see [`SMS_ENABLEMENT.md`](SMS_ENABLEMENT.md)
- **Outbound voice memos.** `POST /v3/chats/{id}/voicememo` is not implemented.
  The attachment upload it would need already exists (`linq.upload_attachment`,
  built for card images), so it is a small addition whenever it is wanted.
## Resume attachments

Inbound attachments are handled: the webhook enqueues an `intake.attachment`
job, the worker downloads each media part from Linq's CDN (with one refresh
retry through `GET /v3/attachments/{id}` for expired ephemeral-tier URLs) and,
when a file sniffs as a resume (PDF or DOCX by magic bytes), stores it as the
thread's `CandidateDocument` and queues the ordinary `docs.parse` -> promote ->
confirm path. A resume sent before the thread has an email is held at a
thread-scoped storage path and attached the moment identity capture links a
candidate. Non-resume media (photos, video) get an honest "can't read that"
reply with the upload-page link while the profile question is open, and are
ignored otherwise. OCR on scanned images remains out of scope.

**A voice memo takes the same job, one branch earlier.** Audio never sniffs as
a resume, so `ingest_inbound_attachments` checks for it before the resume loop
and routes to transcription instead — one dispatcher, not two, which is what
keeps "exactly one conversational consequence per inbound message" true.
