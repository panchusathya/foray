# SMS enablement — why toll-free is blocked and what Linq is for

## The number today

Voice intake uses **+1 (888) 260-0142**, a toll-free line imported into Retell from
Twilio. Retell shows `Provider: Twilio`. That number is correct for **inbound and
outbound voice**; it is **not** a path for carrier SMS on the current account.

## Why Twilio SMS is deferred

Toll-free SMS does **not** use A2P 10DLC. It uses **Toll-Free Verification (TFV)** —
a separate registration with its own lead time and approval criteria.

On this stack specifically:

1. **Retell cannot file TFV from their dashboard** for the imported toll-free number.
   Verification has to happen on the Twilio side, and the number's lifecycle is
   split across Retell (agent runtime) and Twilio (PSTN + SMS capability flags).
2. **Unverified toll-free SMS is blocked outright** by carriers — not filtered, not
   deprioritised. Messages simply do not deliver.
3. **Voice is unaffected.** TFV gates SMS only. Phase 2 voice intake does not depend
   on texting.

The Twilio SMS adapter drafted on `origin/cursor/messaging-intake-sms-5fd9` is kept
as the checklist for when TFV clears or a separate SMS-capable local number is
provisioned. It is not wired to the Retell toll-free line.

## Why Linq is the interim path

**iMessage rides Apple's network over IP**, not the carrier SMS A2P/TFV regime. Linq
exposes a Partner API (`https://docs.linqapp.com/`) with:

- `message.received` webhooks (Standard Webhooks signing)
- Outbound replies via `POST /v3/chats/{chatId}/messages`
- Automatic iMessage / RCS / SMS fallback on their infrastructure (fallback
  registration is theirs, not ours — see Linq docs vs blog if Android matters)

Juicebox implements this as **`LinqChannel`** + `POST /v1/intake/linq/webhook`, reusing
the same intake gate, extraction, and `CandidatePreference` write as web chat and voice.

## Scope of the Linq rollout

| Decision | Rationale |
|---|---|
| Tester-only | Number not advertised until production gates are verified |
| **iMessage-only for now** | Android / SMS fallback is an open question on Linq's registration model |
| **Inbound-first + replies** | Candidate texts first; we never cold-text sourced Foray profiles |
| **Separate `IntakeNumber.provider`** | `linq` vs `retell` so voice dial-out cannot pick the Linq line |

## When Twilio SMS lands

Re-enable the SMS branch checklist in [`MESSAGING_INTAKE_PLAN.md`](MESSAGING_INTAKE_PLAN.md):

- `SmsChannel` + `POST /v1/intake/sms/webhook` (Twilio signature)
- TFV approved on a **dedicated** SMS-capable number (likely local, not the toll-free voice line)
- `SMS_ENABLED=true` and carrier HELP/STOP compliance
- **Re-review the opt-out copy before going live, not after.** The greetings no
  longer recite STOP/HELP — see [`INTAKE_COMPLIANCE.md`](INTAKE_COMPLIANCE.md)
  §6a for why, and for the exposure it creates. The keyword *handling* is
  unchanged and tested; what is missing is the language in the message body,
  which is exactly what 10DLC campaign review and the audits after it look
  for. If review asks for it, restore it in the **first outbound message only**
  — not in every greeting, and not in the voice wrap-up, which is not an SMS.
  Getting this wrong throttles a shared sender for every tenant on it, so treat
  it as a blocker on enablement rather than a copy tweak afterwards.

Until then, follow [`LINQ_SETUP.md`](LINQ_SETUP.md) for the working text intake path.
