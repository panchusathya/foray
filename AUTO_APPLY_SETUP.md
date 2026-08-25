# Auto-apply: what to set, and in what order

Everything in this feature ships inert. `APPLY_SUBMITTER` defaults to `console`,
which runs the entire path — resolve the ATS, build the context, write the
attempt row, move the status machine, notify the candidate — and logs the
application instead of sending it. You can exercise the whole thing on a laptop
without an employer ever hearing from you.

Turning it on is four decisions, and they are worth making in this order.

---

## 1. Nothing (verify the path works)

No configuration. Apply to something in the demo workspace and watch it move.

```bash
make install && make migrate && make seed
make api      # and, in another shell:
make worker
```

Then, against a prepared application that has a recorded yes:

```bash
curl -XPOST localhost:8000/v1/applications/$APP_ID/submit -H "Authorization: Bearer $KEY"
curl localhost:8000/v1/applications/$APP_ID/submissions  -H "Authorization: Bearer $KEY"
```

The worker log shows a `[console-apply]` line with the role, the address it
would have used, and the documents. The application reaches `submitted` with
`submission_method: "console"`, so a row from this mode is never mistakable for
a real one.

**A 409 here is normal and worth reading.** `no recorded consent`, `submission
budget`, or `this one is a hand-off` (no adapter for that employer's ATS) are
all the system working.

---

## 2. The alias mailbox — the one that actually unblocks things

```bash
APPLY_ALIAS_ADDRESS=anna@goforay.io
APPLY_ALIAS_RELAY=true
```

This is the decision the rest depends on. Every ATS that makes a candidate
register gates on a click in an email, and most then send a one-time code.
Owning the address the application carries is what closes that loop without a
person — and without a Google restricted scope, a vendor's verified OAuth client,
or a CASA assessment.

Each candidate gets `anna+<token>@goforay.io`, where the token is an HMAC of
their id, so inbound mail is attributable before any lookup and the local part
cannot be guessed.

**Two setup steps, and the second is the one people miss:**

1. Set `APPLY_ALIAS_ADDRESS` to a real mailbox on a domain whose DNS you control.
2. **Connect that same mailbox as a mailbox connection** (Settings → Mailboxes).
   Sending is not enough. Verification links and one-time codes arrive *inbound*,
   and if nothing is reading that mailbox the run parks forever at
   `needs_verification`.

Also confirm the provider delivers `+` sub-addressed mail to the base mailbox.
Google and Microsoft both do by default; some hosts do not, and the symptom is
silence.

**What this costs, so it is a decision and not a surprise:**

- We become the addressee of record on every application. The employer can see
  the `goforay.io` domain — so we never claim they cannot tell.
- One shared mailbox is one sending reputation for every candidate on it. Keep
  volume modest until it is proven.
- A candidate who hits Reply in their own mail client answers the recruiter from
  their own address, which orphans the thread at the employer's end. The relay
  footer says so; the real fix is replying from the portal.

Leaving `APPLY_ALIAS_ADDRESS` empty is supported: the candidate's own address
goes on the application. It is honest, and it means Workday and Oracle stall.

---

## 3. Turn on the adapters

```bash
APPLY_SUBMITTER=live
```

Coverage on day one, by vendor:

| Vendor | How | Needs |
|---|---|---|
| Lever | public apply endpoint | nothing |
| Ashby | public application-form API | nothing |
| Greenhouse | board API | `GREENHOUSE_API_KEY` (per board, from the employer) |
| An email address in the JD | SMTP with attachments | `EMAIL_PROVIDER=smtp` + `SMTP_*` |
| Workday | browser adapter | the alias mailbox, and step 4 below |
| Oracle / iCIMS / Taleo / Jobvite / BambooHR / Workable | recognised, no adapter | — (Tier 0 hand-off) |

Recognising a vendor is not the same as being able to submit to it. Anything in
that last row resolves correctly, shows the candidate the real form URL instead
of an aggregator listing, and stays the assisted hand-off it already was.

Greenhouse and SmartRecruiters **refuse rather than posting blind** without a
key, so an unset key is a clean `needs_human`, not a silent failure.

Before flipping this, find out what your own posting book actually resolves to —
it decides which adapter is worth building next:

```bash
.venv/bin/python -c "
from juicebox.db import session_scope
from juicebox.models import JobPosting
from collections import Counter
with session_scope() as db:
    print(Counter(p.ats_vendor or '(none)' for p in db.query(JobPosting).all()))
"
```

---

## 4. The browser tier (Workday)

Playwright is an optional extra, deliberately: the API adapters must not depend
on a Chromium install.

```bash
.venv/bin/pip install -e "packages/core[apply]"
.venv/bin/playwright install --with-deps chromium
```

In Docker, build the worker image with the arg that already exists:

```bash
docker compose build --build-arg INSTALL_PLAYWRIGHT=true worker
```

```bash
APPLY_BROWSER_HEADLESS=true   # false only to watch one run by hand
APPLY_MAX_ATTEMPTS=3
```

A Workday run is two legs. Leg one creates the portal account and parks at
`needs_verification`; the verification mail lands at the alias, the inbox
handler clicks it and queues `application.submit.verify`; leg two signs in,
fills the form and submits. The account is kept in `ats_accounts` (password
encrypted) so the next role at the same employer reuses it.

Every run stores a screenshot and the exact field map it filled. Read them back
with `GET /v1/applications/{id}/submissions`.

---

## Before `APPLY_SUBMITTER=live` in production

Not a checklist of nice-to-haves. These are the ones with an outside party.

- **Counsel on `CANDIDATE_AGENT_PLAN.md` §12.1–.3** — NYC LL144 notice, the
  authority to apply on somebody's behalf, and whether a submission discloses
  that an agent prepared it. §12.3 is no longer optional-feeling: the address on
  the application is ours, so non-disclosure is not a position available to us.
- **SPF, DKIM and DMARC on the alias domain.** Relayed mail is rewritten `From`
  the alias with `Reply-To` pinned to the original sender; without alignment it
  lands in spam and candidates silently stop hearing about interviews.
- **A first live run you watch end to end**, on one posting, then withdraw it.
  `APPLY_BROWSER_HEADLESS=false` for that one.

## Guardrails you cannot turn off with a flag

- No submission without a recorded per-application yes. The gate gates
  `submitting` as well as `submitted`.
- 5 submissions per candidate per 7 days, 2 per company per 30 days, one
  application per posting ever.
- A candidate's first application waits for a person, however it was triggered
  automatically.
- A required field we cannot answer from the record stops the run. Nothing here
  invents an answer.
