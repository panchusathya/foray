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

## 8. LinkedIn profile lookup (LinkedIn-first intake)

Linq threads ask for a LinkedIn URL first, then look the profile up. **The paid
provider goes first, and the free browser scrape is off by default.**

That inverts the obvious cost order for a reason measured on a live intake. The
scrape cannot return anything without a logged-in session, and with none it does
not fail fast: Chromium launches, the navigation to linkedin.com from a
datacentre IP burns its 30s timeout, and `_playwright_fetch` retries the whole
thing once. Roughly a minute spent proving a miss, with the paid lookup that was
always going to answer still to come — and a candidate watching a typing
indicator for all of it. Free is only cheaper than paid when the two cost the
same wait.

So the default needs no LinkedIn setup at all: set `APIFY_API_TOKEN` (or
`SCRAPIN_API_KEY` with `PROFILE_PROVIDER=scrapin`) and the lookup answers in
one hop.

To put the free scrape back in front, on the worker host:

```bash
# optional extra; the API image does not need this
pip install 'packages/core[linkedin]'
playwright install chromium

LINKEDIN_SCRAPE_ENABLED=true
# session cookie — Playwright storage_state JSON, or a raw li_at=... string
# never commit this
LINKEDIN_SESSION_COOKIE='[{"name":"li_at","value":"...","domain":".linkedin.com","path":"/"}]'
# or
LINKEDIN_SESSION_PATH=/opt/juicebox/secrets/linkedin-cookies.json
```

Both are required together. `LINKEDIN_SCRAPE_ENABLED=true` with no session is
ignored — that is a configuration that cannot succeed, and obeying it would
restore exactly the wait the default exists to remove. Add the vars to
`/opt/juicebox/.env` and recreate `worker`. LinkedIn ToS / account-ban risk is
operator-owned; rotate the session when scrapes start returning auth-wall
errors.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| 401 on webhook | Wrong/missing `LINQ_WEBHOOK_SECRET`, or body re-serialised before verify |
| 404 on webhook | No `intake_numbers` row for this E.164 with `provider=linq` and `active=true` |
| 503 on webhook | `LINQ_ENABLED=false` or missing `LINQ_API_KEY` / `LINQ_FROM_NUMBER` |
| No reply | Worker not running; `LinqChannel` not configured; missing `provider_thread_id` |
| Stuck at Sent, never Read | SMS fallback, or the recipient has read receipts off — expected |
| Profile lookup empty, extra questions | No `APIFY_API_TOKEN` / `SCRAPIN_API_KEY`; profile not public |
| Scrape never runs | Expected: `LINKEDIN_SCRAPE_ENABLED` is false by default, and needs a session cookie too |
| LinkedIn step takes ~a minute | The free scrape is enabled without a working session — unset `LINKEDIN_SCRAPE_ENABLED` |
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

## Group-chat referrals

Off by default. A candidate can refer a friend by putting our line and the
friend in an iMessage group — the whole gesture is three taps, no code and no
form.

```bash
LINQ_GROUP_REFERRALS_ENABLED=false   # flip to true per deployment
REFERRAL_BONUS_APPLICATIONS=5        # what a confirmed referral is worth
```

Its own flag, separate from `LINQ_ENABLED`, because this is the one path where
a message of ours reaches somebody who never texted us. What the flag turns on:

1. A group `message.received` opens a thread with `entry_point=linq_group`,
   `mode=referral`, and queues one `intake.referral` job. No turn is ever run on
   a group — the intake agent asks one person about their own preferences, and
   there is more than one person in the room.
2. The job reads the participants (`GET /v3/chats/{id}`), writes one `referrals`
   row per newcomer, and sends **one** message into the group: AI disclosure,
   our number, what the intro is worth, notice that we will step out once the
   newcomer messages us, and no link.
3. **We stay in the chat.** Handing somebody a number and leaving in the same
   breath gives them no way to check they had the right one. A later ping in the
   group gets exactly one redirect — "text me separately, happy to help
   one-on-one" — and after that we go quiet and stay put. Two outbound messages
   is the ceiling (`prompts.MAX_GROUP_REPLIES`).
4. Nothing is ever sent one-to-one to the referred friend. The row stays
   `pending` until **they** text our number; that inbound is both the
   confirmation and the consent. It is what tells the referrer their intro
   landed, and it is what makes us leave: a short sign-off naming nobody, then
   `POST /v3/chats/{id}/leave`, then the thread closes.

A group with nobody new in it is the one case we leave immediately — there is
no one to wait for, so it gets an honest "I keep things one-on-one" and we go.

**Consequence worth knowing:** if the newcomer never texts, we stay in that
group indefinitely (quiet after the first redirect). There is no timer. If that
turns out to matter, the event-driven place to add an exit is the redirect cap
in `referrals.redirect_group` — being pinged twice with no private message is a
reasonable signal to step out.

The bonus is **recorded, not yet spendable**: `referrals.earned_bonus` sums
confirmed rows and nothing reads it, so `applications.limits.may_submit` still
enforces 5 submissions a week for everyone. The group copy says "banked" for
exactly that reason — wire the bonus into `may_submit` and update the copy in
the same change.

Evidence record: `messaging/consent.py` grants these threads
`SOURCE_REFERRAL_GROUP` with a versioned disclosure that states what is sent
into a group and what is not. STOP works in a group as anywhere else.

Because the group thread stays `active` while we are in the chat, it carries the
referrer's own number in `from_address` — so `threads.find_active_thread`
excludes `MODE_REFERRAL` outright. Without that, the referrer's next private
message would resume the *group* thread and every reply to them would be
delivered in front of the friend they just introduced. Do not remove that filter.

### Group smoke test

1. `LINQ_GROUP_REFERRALS_ENABLED=true`, recreate `api` + `worker`.
2. From an iPhone, create a group with our Linq number and a second phone that
   is **not** already a candidate. Send one message.
3. Expect: one reply into the group, and that we are **still in it**. Check
   `select status, referee_address from referrals` → one `pending` row for the
   second phone.
4. Send another message into the group: expect one "text me separately" reply.
   Send a third and a fourth: expect silence, and we are still in the chat.
5. Text our number from that second phone. Now expect the sign-off in the group
   and "left the conversation". The row flips to `confirmed` with
   `referee_thread_id` set, and the referrer gets the credit text if they have a
   `managing` thread.

Worth confirming against your Linq account before the first real run:
`GET /v3/webhook-events` for the authoritative event list, and that `/leave` is
POST — the docs pages disagree, and `leave_chat` is best-effort either way, so a
wrong verb shows up as us staying in groups rather than as an error.

## Contact card (iMessage identity)

Instead of "Maybe: +1 415…", the candidate sees **Foray** and a one-tap
banner offering to save us. This is Apple's own name-and-photo sharing, not a
`.vcf` attachment — nothing arrives as a file, and there is nowhere on the
card for an email or a website. Those belong on a vCard media part, which is
a separate piece of work.

Two steps, and configuring a card shares it with nobody.

### 1. Set the card (once, per number)

```bash
python deploy/linq_setup.py set-contact-card --first-name Foray
```

`--image-url` defaults to `$BASE_URL/v1/intake/linq/contact-card.png`, which
the app serves: the Foray mark on `#0A0A11`, drawn at 1024px and sized to fill
the circle Apple crops it to. Pass `--image-url` to point at something else,
or run with `BASE_URL` unset for a name-only card. Linq fetches the URL when
the card is set, so it must be the deployed host — not localhost.

Check it with:

```bash
python deploy/linq_setup.py contact-card
```

### 2. Turn on sharing

```bash
LINQ_CONTACT_CARD_ENABLED=true
```

Default off. A share against a number with no card is error 2012 on *every*
outbound, so the switch is what keeps a half-provisioned line quiet.

### When a candidate is asked

`messaging/contact_card.py` offers the card after a successful outbound, and
only when all of these hold:

| Gate | Why |
|---|---|
| They replied to us first | An inbound *after* one of our outbounds. Someone who texted once and went quiet has not earned a name in their contacts. |
| iMessage, not RCS | Apple feature. RCS renders job-card images but cannot show a contact card, so this splits what `cards.py` treats alike. |
| `to_address` is `LINQ_FROM_NUMBER` | A recruiter line added later must not inherit the candidate agent's face. |
| Under 3 attempts, ≥24h apart | Linq documents that a share may not take effect and recommends retrying daily. Three, because a banner that returns forever is nagging. |

State lives on `thread.record["_contact_card"]` (`attempts`, `last_attempt_at`,
`shared_at`). Every failure path is swallowed — a missing banner must never
cost a turn.

### Known limits

- Whether Apple actually drew the banner is not observable from our side. The
  bool we record only says Linq accepted the call.
- Re-running `set-contact-card` replaces the card; recipients who already
  saved us keep the old name and photo in their contacts.
