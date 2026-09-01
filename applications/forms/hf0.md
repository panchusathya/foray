# HF0 — draft for review

All three answers are about the company. HF0 says "briefly," so these are deliberately the
tightest versions in the pack — the long forms are in `block-a.md`.

**One thing to fix before submitting:** HF0 is a live-in residency. Confirm the cohort start date.
If it starts on or after 1 January 2027 we're already full-time and Q3 covers it as written; if it
starts earlier, the sentence in Q3 has to say that acceptance moves our resignation date forward.

---

# Q1 — Briefly, what problem are you solving?

Nobody knows who is actually in the market, so hiring can't match — and everyone compensates with
volume.

LinkedIn is a profile graph: it records where you've been. It cannot hold intent, because intent is
private (say it on LinkedIn and your boss finds out), conditional ("I'd move for a founding role
above $X, not into enterprise sales"), and perishable (open in March, not in June). None of those
are profile fields; "open to work" is a boolean.

So the people worth $30,000 a placement stay silent, and the whole industry fights over the small,
visible slice willing to be seen looking. We interviewed about 25 recruiters. What they told us
wasn't "sourcing is hard" — it was that **they are all reaching the same candidates as everyone
else and none of them has proprietary distribution.** That's a market-structure problem, not a
workflow one.

We're building the intent graph: the private, standing record of what would actually move someone,
captured by conversation instead of a profile page. Profile data is a commodity — we bought 200,000
profiles from a broker on a credit card. You cannot buy what someone told you in confidence.

---

# Q2 — Traction

**Six weeks, both of us still holding full-time jobs.**

**The funnel, with a denominator at every stage:**

| | |
|---|---|
| Profiles indexed | **200,000** |
| Candidates contacted | **840** |
| Engaged **and** a good fit for the role | **85 — 10.1%** |
| Delivered to companies | **~20** |
| Companies served | **8** — Kyber, OneRobot, Baseten, LangChain, Vela, Aaru, NeoSigma, Vapi |

10.1% is against a ~5% baseline for standard recruiter cold outbound, and it's a *qualified* rate,
not a reply rate. Our best-targeted sequence — Baseten's founding BizOps role, 162 sends — ran at
about 20% expressing interest, with interested candidates from Meta, Harvey, Apple, Databricks and
DeepMind. The trajectory matters more than any single number: we started at 2–3% on a shared inbox
that got flagged for spam, and we're at 10% aggregate six weeks later.

**Live right now:** a Chief of Staff candidate in interview process at LangChain. A $10K referral
fee on offer in writing from Vela. OneRobot's founder has asked for more after a two-day
turnaround. Separately, through our own network: 10+ referrals made, every one of which converted
to an interview.

**Last night we opened a candidate-side waitlist** and had signups within 24 hours with no spend.
That's the first inbound in the company's history and the beginning of the supply side.

**Shipped in the same six weeks:** our own sourcing engine (hybrid full-text + pgvector retrieval,
reciprocal rank fusion, LLM rerank) with an offline eval harness reporting nDCG@10, MRR, P@k and
R@10; a live voice agent and messaging agent unified with email on one CRM; a browser agent that
fills and submits applications end to end, working in testing; a recruiter SPA and a candidate
portal; per-application inbound aliases with every employer reply classified and timestamped.
~161K lines, 185 test files, CI. **259 commits in the five days to 28 August.**

**What we don't have:** revenue is $0. That's a choice — we're delivering candidates free to earn
letters of intent, because charging now would gate every req behind a contract and cap the volume
we can move while the graph is the thing we're actually building. The pricing exists in the
product. We haven't invoiced.

---

# Q3 — Twelve months of progress in twelve weeks

What would take us twelve months isn't the software. We shipped an enormous amount in six weeks
around two demanding jobs. **What's gated is everything that requires being awake in business
hours and being able to fail in public** — the supply side, and the commercial loop. Both are
attention problems, not capital or headcount problems, which is why the residency format is the
specific thing we want.

**Weeks 1–4 — solve cold start on the supply side.** This is the one that decides whether the
company works, so it goes first. The highest-intent moment in a job seeker's life is the rejection
email, and nobody is standing there. We've already shipped the Gmail connector: a candidate
connects their inbox once, and minutes after a rejection lands they get four roles at the same
stage and band. Near-perfect relevance, zero cold outreach. Get the browser agent live so
applications are genuinely free and instant, and make that loop fire for real.
**Target: 1,000 standing briefs and the rejection trigger running in production.**

**Weeks 5–8 — prove a brief beats a resume, and get paid.** The falsifiable version of our whole
thesis is one number: *a standing brief converts to a match at a rate a resume never does.* Twelve
weeks is enough to prove or kill it, and we'd rather know. In parallel, convert the eight companies
holding our free work onto a price sheet — 18% on a hire, or $1,000 per completed interview capped
at $12K, buyer's choice — and run Dwayne Nonales' entire recruiting desk on our system to measure
placements per recruiter-month against his own manual baseline. A good contingent recruiter runs
one to one-and-a-half. If the system takes one recruiter to four, that multiple is the business and
everything else is packaging.
**Target: first revenue, five signed agreements, and a defensible throughput ratio.**

**Weeks 9–12 — turn the data position into a product nobody can copy.** By then we'll have enough
classified employer replies to publish what nobody in hiring can currently measure: per-company
response latency, and what share of applications ever reach a human. And enough briefs to ship the
aggregate product — *"at $200K for a founding engineer, twelve people who'd fit are open in the
next six months, and you're 15% below the bar."* That's sellable without marketplace density,
which is the honest answer to our own bear case, and it's simultaneously the content engine that
fixes our weakest channel.
**Target: the aggregate product in front of paying companies, and 20 companies on contract.**

**Why HF0 specifically.** Two of those three phases are blocked on undivided attention rather than
on money, and the residency is the only thing on our list that directly buys it. We're at
WestBridge through the end of 2026 and full-time on Foray from 1 January — decided, not
aspirational. If the cohort starts before then, acceptance moves the date forward. We're not asking
to do this part-time.
