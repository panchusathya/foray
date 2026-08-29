# Accelerator applications — a16z · Soma · Greylock · HF0 · Z-Fellows

29 questions across five forms reduce to **20 canonical answers**. Write those once, assemble
each form from them. Full plan and reasoning: `/root/.claude/plans/linked-discovering-cocoa.md`.

## Why this pack exists

The SPC application was rejected while describing a company far weaker than the one that
actually exists. Its progress answer was five bullets, three of them "we talked to people," one
volunteering that the outbound doesn't work — and it never mentioned ~20 candidates delivered to
eight AI startups, an interview in process at LangChain, a role closed off their referral at Kyber,
a voice agent, a text agent, a browser agent, a 200K-profile index, or an eval harness reporting
nDCG@10.

The same undersell runs through the founder material. Sathya's resume buries a Berkeley CS
degree, the first four-wheel odometry system in FIRST, and production sourcing tooling shipped
inside an $8B fund. Tushar's YC form gives one clause to a college venture that won the ABC
Innovation Prize, was a Blackstone Launchpad finalist and raised grant funding — and never mentions
that he led campus and lateral recruiting at J.P. Morgan, which is the most on-thesis fact he has.

**The task is not to be more impressive. It is to stop underselling.** Hence `evidence.md`.

## Files

| File | What it is |
|---|---|
| `evidence.md` | **Canonical claim inventory.** Every provable fact with its source. Nothing enters an application that isn't here; nothing here gets omitted from a form it fits. Also §9: what must *not* be linked. |
| `block-a.md` | The 7 company answers, long form. Covers all of HF0, Soma's first question, Greylock's problem-space question, and 4 of Z-Fellows'. |
| `block-b-tushar.md` | Tushar's 10 personal answers. Harvested from the YC application; gaps marked. |
| `block-b-sathya.md` | Sathya's 10 personal answers. Harvested from resume + SPC; gaps marked. |
| `shared.md` | B10 talent density, B11 referrer map, B12 anything-else. |
| `interview.md` | **Open — the remaining questions for both founders.** Start here. |
| `forms/*.md` | The five assembled submissions, at each form's own lengths. Not written yet. |
| `check_lengths.py` | Character-limit assertions for `forms/z-fellows.md`. |

## Status

| | State |
|---|---|
| Evidence inventory | Written |
| Block A (7 company answers) | Written, then **corrected 29 Aug** against the founders' own answers — several drafts were running ahead of the facts |
| Block B — Tushar | Harvested; 6 gaps |
| Block B — Sathya | Harvested; 7 gaps |
| Shared (B10/B11/B12) | Written — B11 referrer map is `TODO` and worth more than any essay |
| The five forms | **Blocked on `interview.md`** |

## Decisions taken

- **Applicants:** Tushar and Sathya. Dwayne appears as the recruiting domain expert, not a
  third applicant.
- **Positioning (settled 29 Aug):** not executive search. Early GTM/ops at AI startups plus
  engineering hires, run high-volume, funnelling the best candidates into client work.
- **Not full-time.** Both still employed at WestBridge. Stated plainly in A3 rather than left
  to be discovered — every form asks about exclusive commitment and HF0 is a residency.
- **Dwayne has expressed interest, not committed.** Never written as a co-founder.
- **Strata does not appear anywhere.** Arkash appears only as a prospective hire.
- **Nobody unsigned is called a co-founder.** Entities are incorporated and equity is 50/50
  between the two founders; naming three more people as co-founders would read as an unsettled
  cap table rather than as talent density.

## Blocking, in order

1. **`VC_Portcos` → private.** Confirmed as agreed; not yet done. See `evidence.md` §9.
2. **`interview.md`** — the remaining questions, mostly Block B personal material and links.
3. **Ansh Gupta's background** — LinkedIn is blocked by this environment's egress policy.
4. **Browser agent status** — test submissions count, ATSs covered, live date.
5. **Referral→interview denominator** — "every referral converted to an interview" is the best
   conversion stat in the pack and it currently has no number attached.

## Verification before submitting

```bash
python applications/check_lengths.py     # z-fellows character caps
grep -ril "strata\|statabank\|VC_Portcos\|rottenpotatoes" applications/forms/   # must be empty
```

Then by hand: every claim traces to `evidence.md`; each founder's three strongest facts appear
in the first third of their answers rather than buried; company counts, candidate counts and
dates read identically across all five forms; and each form read aloud end to end, checking the
two founders' answers don't sound like one person wrote both.
