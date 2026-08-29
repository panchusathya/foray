# Accelerator applications — a16z · Soma · Greylock · HF0 · Z-Fellows

29 questions across five forms reduce to **20 canonical answers**. Write those once, assemble
each form from them. Full plan and reasoning: `/root/.claude/plans/linked-discovering-cocoa.md`.

## Why this pack exists

The SPC application was rejected while describing a company far weaker than the one that
actually exists. Its progress answer was five bullets, three of them "we talked to people," one
volunteering that the outbound doesn't work — and it never mentioned candidates delivered to
eight AI startups, an interview in process at LangChain, a placement closing at Kyber, a voice
agent, a text agent, a browser agent that autonomously submitted a real application, a
200K-profile index, or an eval harness.

The same undersell runs through the founder material. Sathya's resume buries a Berkeley CS
degree, the first four-wheel odometry system in FIRST, and production sourcing tooling shipped
inside an $8B fund. Tushar's YC form describes founding a drone company that won awards and
grant funding as *"prototyped a fixed-wing aircraft."*

**The task is not to be more impressive. It is to stop underselling.** Hence `evidence.md`.

## Files

| File | What it is |
|---|---|
| `evidence.md` | **Canonical claim inventory.** Every provable fact with its source. Nothing enters an application that isn't here; nothing here gets omitted from a form it fits. Also §9: what must *not* be linked. |
| `block-a.md` | The 7 company answers, long form. Covers all of HF0, Soma's first question, Greylock's problem-space question, and 4 of Z-Fellows'. |
| `block-b-tushar.md` | Tushar's 10 personal answers. Harvested from the YC application; gaps marked. |
| `block-b-sathya.md` | Sathya's 10 personal answers. Harvested from resume + SPC; gaps marked. |
| `shared.md` | B10 talent density, B11 referrer map, B12 anything-else. |
| `interview.md` | **Open — 67 questions still needed from both founders.** Start here. |
| `forms/*.md` | The five assembled submissions, at each form's own lengths. Not written yet. |
| `check_lengths.py` | Character-limit assertions for `forms/z-fellows.md`. |

## Status

| | State |
|---|---|
| Evidence inventory | Written |
| Block A (7 company answers) | Written — **A3 traction to be refreshed last** |
| Block B — Tushar | Harvested; 6 gaps |
| Block B — Sathya | Harvested; 7 gaps |
| Shared (B10/B11/B12) | Written — B11 referrer map is `TODO` and worth more than any essay |
| The five forms | **Blocked on `interview.md`** |

## Decisions taken

- **Applicants:** Tushar and Sathya. Dwayne appears as the recruiting domain expert, not a
  third applicant.
- **Positioning:** engineering hires are the destination; founding GTM/ops at seed AI startups
  is the beachhead. Makes existing traction on-thesis rather than off-thesis.
- **Strata does not appear anywhere.** Arkash appears only as a prospective hire.
- **Nobody unsigned is called a co-founder.** With no entity and no papered equity, "three
  people asked to be co-founders" reads as an unsettled cap table, not as talent density.

## Blocking, in order

1. **`VC_Portcos` → private.** Public repo on the technical co-founder's account that screens
   founders by inferred ethnicity. See `evidence.md` §9.
2. **`interview.md`** — 67 questions, both founders, answerable in bullets.
3. **Ansh Gupta's background** — LinkedIn is blocked by this environment's egress policy.
4. **Entity + Dwayne's agreement** — A5 is a materially different answer with a signed
   recruiting co-founder than with an unnamed advisor.

## Verification before submitting

```bash
python applications/check_lengths.py     # z-fellows character caps
grep -ril "strata\|statabank\|VC_Portcos\|rottenpotatoes" applications/forms/   # must be empty
```

Then by hand: every claim traces to `evidence.md`; each founder's three strongest facts appear
in the first third of their answers rather than buried; company counts, candidate counts and
dates read identically across all five forms; and each form read aloud end to end, checking the
two founders' answers don't sound like one person wrote both.
