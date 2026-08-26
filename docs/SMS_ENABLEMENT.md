# SMS enablement — why carrier SMS is not live, and what Linq is for

## The number today

Voice and text both run on **+1 (628) 386-5454**, a local number on our own
Twilio account, brought into the `Foray` Retell workspace over a SIP trunk.
Retell owns the agent runtime; Twilio owns the number, the carrier relationship
and the registration. That number is correct for **inbound and outbound voice**
today. It is not yet a path for carrier SMS.

*(It replaced the toll-free `+1 (888) 260-0142` on 2026-08-23. That matters here
because the registration regime follows the number: toll-free would have meant
Toll-Free Verification, and a local number means A2P 10DLC. Anything you find
elsewhere describing TFV as the blocker predates the move.)*

## Why Twilio SMS is deferred

**A2P 10DLC — brand plus campaign, filed under the legal entity.** That is the
whole blocker, and it is not an engineering one:

1. **Nothing can be filed until the entity exists.** The Delaware entity has to
   be filed, the EIN issued, and the EIN propagated to the databases the
   campaign registries actually query — 30 to 90 days, and none of it is ours to
   speed up. `deploy/NUMBER_CHECKLIST.md` tracks the sequence.
2. **Unregistered A2P traffic is filtered, not queued.** Messages are dropped or
   heavily throttled by carriers, and the failure is largely invisible from our
   side.
3. **Voice is unaffected.** 10DLC gates SMS only. Voice intake has never
   depended on texting, which is why the consent capture went in first: the
   agent has been asking *"Can I text you updates at this number?"* and latching
   `intake_calls.sms_opt_in` since before there was anything to send, so lawful
   opt-ins accumulate in the meantime.

A draft Twilio adapter is kept on `origin/cursor/messaging-intake-sms-5fd9` as
the checklist for when registration clears. It is not wired to anything.

## Why Linq is the interim path

**iMessage rides Apple's network over IP**, not the carrier A2P regime, so it
needs neither 10DLC nor toll-free verification. Linq exposes a Partner API
(`https://docs.linqapp.com/`) with:

- `message.received` webhooks (Standard Webhooks signing)
- Outbound replies via `POST /v3/chats/{chatId}/messages`
- Automatic iMessage / RCS / SMS fallback on their infrastructure (the fallback
  registration is theirs, not ours — check their docs against their blog if
  Android matters)

Juicebox implements this as **`LinqChannel`** + `POST /v1/intake/linq/webhook`,
reusing the same intake gate, extraction and `CandidatePreference` write as web
chat and voice.

## Scope of the Linq rollout

| Decision | Rationale |
|---|---|
| Tester-only | Number not advertised until the production gates are verified |
| **iMessage-only for now** | Android / SMS fallback is an open question on Linq's registration model |
| **Inbound-first + replies** | The candidate texts first; we never cold-text a sourced Foray profile |
| **Separate `IntakeNumber.provider`** | `linq` vs `retell`, so voice dial-out cannot pick the Linq line |

## When Twilio SMS lands

Re-enable the SMS branch checklist in
[`MESSAGING_INTAKE_PLAN.md`](MESSAGING_INTAKE_PLAN.md) — items 5 through 7 of it
(the suppression phone column, HELP, and quiet hours) are already done; 1
through 4 are not.

- `SmsChannel` + `POST /v1/intake/sms/webhook` (Twilio signature)
- 10DLC brand and campaign approved under the legal entity, with the use case
  filed as recruiting / application-status updates
- `SMS_ENABLED=true`, and carrier HELP/STOP compliance verified on the live line
- **Re-review the opt-out copy before going live, not after.** The greetings no
  longer recite STOP/HELP — see [`INTAKE_COMPLIANCE.md`](INTAKE_COMPLIANCE.md)
  §6a for why, and for the exposure it creates. The keyword *handling* is
  unchanged and tested; what is missing is the language in the message body,
  which is exactly what 10DLC campaign review and the audits after it look for.
  If review asks for it, restore it in the **first outbound message only** — not
  in every greeting, and not in the voice wrap-up, which is not an SMS. Getting
  this wrong throttles a shared sender for every tenant on it, so treat it as a
  blocker on enablement rather than a copy tweak afterwards.

Until then, follow [`LINQ_SETUP.md`](LINQ_SETUP.md) for the working text intake
path.
