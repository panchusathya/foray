# Documents

Nothing here is required reading. `AGENTS.md` at the repo root plus the
`AGENTS.md` beside the package you are changing is the required set; these are
what you open when you need the *reasoning*, and module docstrings cite them by
section.

Three kinds of document live here, and the difference matters:

- **Binding** — a rule that constrains what may ship. Read it before changing
  the thing it governs.
- **Design record** — why the system is shaped the way it is, including options
  that were considered and refused. Historical in places; the "Status" block at
  the top of each says what is live.
- **Runbook** — how to turn something on. Current, or it is a bug.

| Document | Kind | What it is |
|---|---|---|
| [`INTAKE_COMPLIANCE.md`](INTAKE_COMPLIANCE.md) | **Binding** | What the intake agents may not do, and where each rule is enforced in code rather than in prose. Cited by section from `applications/bar.py`, `messaging/consent.py`, `messaging/prompts.py`, `messaging/linkedin_flow.py` and four test modules — **do not renumber its sections.** §6a in particular is load-bearing: it records why the opt-out recitals were removed *and* the exposure that creates. |
| [`CANDIDATE_AGENT_PLAN.md`](CANDIDATE_AGENT_PLAN.md) | Design record | The proactive loop: find roles that fit, propose them, and with a per-application yes, apply. §3 quiet hours and §4 the three budgets are cited from `messaging/cadence.py`; §5 the fit bar from `applications/bar.py`; §7 the storage decision from `storage.py`. Phases A–F are built. **§12.1–.3 are still open and need counsel, not engineering.** |
| [`VOICE_INTAKE_PLAN.md`](VOICE_INTAKE_PLAN.md) | Design record | Candidate intake by phone. The recon findings near the top are the most reusable part — particularly why intake must emit `ParsedQuery`-shaped preferences rather than a private vocabulary. |
| [`MESSAGING_INTAKE_PLAN.md`](MESSAGING_INTAKE_PLAN.md) | Design record | The same conversation over text: what changes when the gate paces cadence instead of deciding when to hang up, and why one message too many is worse than one question too many. |
| [`AUTO_APPLY_SETUP.md`](AUTO_APPLY_SETUP.md) | Runbook | Turning on application submission, in the order the decisions actually depend on each other. Everything ships inert (`APPLY_SUBMITTER=console`). |
| [`LINQ_SETUP.md`](LINQ_SETUP.md) | Runbook | Wiring the Linq iMessage intake channel: credentials, webhook subscription, registering the line against an org, and the LinkedIn profile lookup. |
| [`SMS_ENABLEMENT.md`](SMS_ENABLEMENT.md) | Runbook | Why carrier SMS is not live, what Linq is doing in the meantime, and the gates on turning Twilio SMS on. |
| [`archive/MESSAGING_INTAKE_BRIEF.md`](archive/MESSAGING_INTAKE_BRIEF.md) | Historical | The kickoff brief for the messaging build, kept for its reasoning. Superseded by `MESSAGING_INTAKE_PLAN.md`; do not follow its instructions. |

## Operations

These live next to the scripts they describe rather than here.

| Document | What it is |
|---|---|
| [`deploy/README.md`](../deploy/README.md) | Deploying the stack. |
| [`deploy/STAGING.md`](../deploy/STAGING.md) | Rehearsing a risky change against a restored dump before it reaches `main`. |
| [`deploy/NUMBER_CHECKLIST.md`](../deploy/NUMBER_CHECKLIST.md) | Everything about the phone line that lives in a console rather than in this repo — Twilio, Retell, and the 10DLC path. The current source of truth for the number. |
| [`packages/foray/README.md`](../packages/foray/README.md) | Foray's own command reference. |
| [`packages/web/e2e/README.md`](../packages/web/e2e/README.md) | The Playwright suite. |

## Keeping this honest

A design record earns its place by explaining a decision somebody would
otherwise re-litigate. When a plan's phases are all built, the phase list stops
being a plan and becomes a changelog — trim it to the reasoning and say so in
the Status block, rather than leaving a reader to work out which half is still
true. Superseded documents go to `archive/` with a banner, not to the bin: the
argument that was rejected is usually the part worth keeping.
