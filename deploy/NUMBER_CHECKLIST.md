# Number & console checklist — +1 (628) 386-5454

The live line is **+1 (628) 386-5454**, bought on our own Twilio account and
brought into the `Foray` Retell workspace over an elastic SIP trunk
(`deploy/twilio_provision.py` builds the trunk, `retell_agents.py import`
registers it with Retell). Retell owns the agent runtime; Twilio owns the
number, the carrier relationship, and — once texting starts — the 10DLC
registration. The earlier toll-free `+1 (888) 260-0142` was bought *through*
Retell (`retell-twilio` type) and is superseded; keep it detached, and release
it once the 628 has taken a few real calls. This checklist is everything that
lives in a console rather than in this repo. Work top to bottom; the sections
are ordered by blast radius.

## Wiring the number (repo scripts, in order)

- [ ] `python deploy/twilio_provision.py verify` — credentials and standing.
- [ ] `python deploy/twilio_provision.py trunk --write-env` — SIP trunk,
      origination to Retell, credential-list termination auth. Writes the
      trunk secrets to `.env`.
- [ ] `python deploy/twilio_provision.py attach +16283865454` — number onto
      the trunk. From here inbound calls leave Twilio for Retell, and fail
      until the import exists — do the next step immediately.
- [ ] `python deploy/retell_agents.py import --number +16283865454` — tell
      Retell about the trunk. Reads the `.env` values the trunk step wrote.
- [ ] Register the number to the org in the **production** database:
      `juicebox intake-number +16283865454 --org <org-id>` on the VM —
      without this row the webhook refuses the call's events (404).

## Retell console — before real callers

- [ ] **Attach agents to the number** — deliberately not automatic:
      `python deploy/retell_agents.py attach --number +16283865454 --base-url …`.
      Creating agents changes nothing a caller can reach; attaching one makes a
      bot answer real people. Verify with `retell_agents.py list` that inbound
      is `foray-status-inbound` and outbound is `foray-intake-outbound`.
- [ ] **Check `ring hook=` on that number** in `retell_agents.py list`. It is
      the number's `inbound_webhook_url`, and it is what fires `call_inbound`
      while the phone rings — the only moment we can tell the agent who is
      calling. `attach --base-url` sets it; `apply` can only repair a number
      that is *already* attached, so on a first setup `apply` runs too early to
      set anything and never comes back.
      **Unset, nothing looks broken:** the line answers, the agent is
      word-perfect, and `caller_known` is false for every caller forever, so
      returning candidates get introduced to us again. If someone reports "it
      didn't remember we spoke", check this before anything in the database.
- [ ] **Restrict allowed countries to US (+ CA if wanted)** on both inbound and
      outbound. This is the abuse-and-spend control: an unrestricted outbound
      agent on a metered line is somebody else's international calling card.
- [ ] **Set the fallback number** to a real recruiter's line, so concurrency
      overflow and platform failure reach a human instead of dead air.
- [ ] **Leave webhook fields pointed at the current base URL** — they are set
      by `retell_agents.py apply --base-url …`, which is the one command to
      re-run whenever the tunnel or domain moves. If the agent "stops asking
      questions", this is the first thing to check: it is calling a dead host.
- [ ] **Voice check** — test-call the line and listen. `11labs-Adrian` was
      picked from a list, not from a phone; `list-voices` has ~300 options.
      Judge it through a phone speaker, not studio monitors.
- [ ] **Data processing** — get Retell's DPA signed and their transcript
      retention terms in writing; decide on the PII-scrubbing add-on
      (~$0.01/min). Candidate PII flows through their platform on every call.
      Do this before volume, not after.

## Twilio console — trust & deliverability

Both add-ons have lead time (days to weeks); start them before they are needed.

- [ ] **Verified Phone Number** — registers the number and calling identity
      with the carriers' analytics providers, so the line is less likely to
      show as "Spam Likely". A local 628 fares better than toll-free did, but
      an unregistered number placing agent calls still drifts toward the spam
      label as volume grows.
- [ ] **Branded Calling** — displays "Foray" instead of a bare number on
      supported carriers. The outbound agent's whole job depends on the call
      being picked up.
- [ ] **Geo permissions** — mirror the US/CA restriction on the Twilio side
      too; Retell's restriction governs the agent, Twilio's governs the number.

## SMS — blocked on the entity, in this order

Nothing here can start until the Delaware entity exists and the EIN has
propagated (30–90 days). The voice consent capture is already live so that
lawful opt-ins accumulate in the meantime (`intake_calls.sms_opt_in`).

1. [ ] Entity filed → EIN issued → EIN propagated to the IRS databases the
       campaign registries actually query.
2. [ ] **10DLC brand + campaign registration** in Twilio, under the real legal
       entity — the 628 is a local number, so it is the 10DLC path (toll-free
       verification would only apply if texting ever moved to the 888). Submit
       the use case as recruiting/application-status updates with the exact
       opt-in language below.
3. [ ] Opt-in evidence for the verification form: the agent asks, verbatim,
       *"Can I text you updates at this number?"* — and nothing about
       stopping; the question is the consent, and reciting the opt-out
       mechanism turns it into a sales line (INTAKE_COMPLIANCE.md §6a) — captured per call in the transcript, flagged in
       `intake_calls.sms_opt_in`, latched true only on an explicit yes.
4. [ ] **STOP/HELP handling** wired and tested before the first campaign
       message. STOP must actually stop; the database flag is consent to start,
       not permission to ignore a revocation.
5. [ ] First messages: application-status updates only, to opted-in numbers
       only. No marketing on this consent — it was asked for updates, and the
       verification was granted for updates.

## Standing rules

- The number-to-org mapping lives in the database, registered via
  `juicebox intake-number` — never trust a webhook payload for org resolution.
- Consent is per call, per purpose: recording consent and SMS opt-in are
  separate booleans, and neither implies the other.
- The agents never go live on a number as a side effect of `apply`. Attaching
  is always an explicit, separate command.
