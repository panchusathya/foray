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

**Seven weeks, both of us still holding full-time jobs.**

**We signed our first two clients this week.** Echelon, a Bain Ventures-backed company, on an
enterprise AE search. FlowGen Labs on three roles — forward-deployed engineer, engineering lead,
research engineer. Both at **20% of base as a success fee: roughly $232K of contingent fees across
the four roles** if they close. **The Echelon founder has also offered to angel invest in us.**

Being precise, because it matters: no cash has been collected, nothing closes until someone is
hired, and the terms are agreed rather than papered. We're two days in. We're delivering 10
candidates to FlowGen and 5 to Echelon by Tuesday.

**How we got them is the part worth reading.** We ran two plays the same day. Cold — recycling our
best resumes into interesting-looking roles — got **zero responses.** Warm — founders we already
knew — converted **two out of two.** That's our own thesis tested on ourselves in a day: fishing
the same pond as everyone else caught nothing; proprietary distribution converted at 100%.

**The funnel behind it, with a denominator at every stage:**

| | |
|---|---|
| Profiles indexed | **200,000** |
| Candidates contacted | **840** |
| Engaged **and** a good fit for the role | **85 — 10.1%** |
| Delivered to companies | **~20** |
| Companies served | **8** — Kyber, OneRobot, Baseten, LangChain, Vela, Aaru, NeoSigma, Vapi |

10.1% is against a ~5% baseline for standard recruiter cold outbound, and it's a *qualified* rate,
not a reply rate. Our best-targeted sequence — Baseten's founding BizOps role, ~160 sends — saw
about 70% open and 20% express interest, with interested candidates from Meta, Harvey, Apple,
Databricks and DeepMind. The trajectory matters more than any single number: we started at 2–3% on
a shared inbox that got flagged for spam, and we're at 10% aggregate seven weeks later.

**Also live:** a Chief of Staff candidate in interview process at LangChain. A $10K referral
fee on offer in writing from Vela. OneRobot's founder has asked for more after a two-day
turnaround. Separately, through our own network: 10+ referrals made, every one of which converted
to an interview. And a **candidate-side waitlist live since late August**, with signups and no
spend — the first inbound in the company's history, and the beginning of the supply side.

**Shipped in the same seven weeks:** our own sourcing engine (hybrid full-text + pgvector retrieval,
reciprocal rank fusion, LLM rerank) with an offline eval harness reporting nDCG@10, MRR, P@k and
R@10; a live voice agent and messaging agent unified with email on one CRM; **a browser agent that
fills and submits applications end to end, with submissions landing against Greenhouse, Ashby and
Workday**; a recruiter SPA and a candidate portal; per-application inbound aliases with every
employer reply classified and timestamped. ~161K lines, 185 test files, CI. **259 commits in the
five days to 28 August.**

We chose browser agents over hand-built ATS adapters deliberately. A company doing it the other way
told us they maintain 29 adapters and hand-roll HTTPS to dodge captcha and spam detection — that
works until it doesn't. The general path cost us a week on Workday's captcha and a move to
residential proxies to get through it. We'd make the same call again.

**What we don't have: cash.** Every dollar of that $232K is contingent on a hire, and we're two
days into those engagements. Everything before this week was delivered free on purpose — charging
early would have gated every req behind a contract and capped the candidate volume we could move,
and the graph is the asset. We also know our own search needs work, and we've deliberately
deprioritised fixing it to get candidates in front of these two clients first.

---

# Q3 — Twelve months of progress in twelve weeks

What would take us twelve months isn't the software. We shipped an enormous amount in seven weeks
around two demanding jobs. **What's gated is everything that requires being awake in business
hours and being able to fail in public** — the supply side, and the commercial loop. Both are
attention problems, not capital or headcount problems, which is why the residency format is the
specific thing we want.

**Weeks 1–4 — solve cold start on the supply side.** This is the one that decides whether the
company works, so it goes first. The highest-intent moment in a job seeker's life is the rejection
email, and nobody is standing there. We've already shipped the Gmail connector: a candidate
connects their inbox once, and minutes after a rejection lands they get four roles at the same
stage and band. Near-perfect relevance, zero cold outreach. The browser agent already submits
against Greenhouse, Ashby and Workday, so applications are genuinely free and instant — the work is
widening ATS coverage and making that loop fire at volume for real candidates.
**Target: 1,000 standing briefs and the rejection trigger running in production.**

**Weeks 5–8 — close the four roles we're already on, and prove a brief beats a resume.** We have
two clients and four live roles at 20% of base. **Nothing about this company is real until one of
them closes**, so that comes first, and it turns ~$232K of contingent fees into actual revenue. In
parallel: convert the eight companies holding our free work onto the same terms, and run Dwayne
Nonales' entire recruiting desk on our system to measure placements per recruiter-month against his
own manual baseline. A good contingent recruiter runs one to one-and-a-half. If the system takes
one recruiter to four, that multiple is the business and everything else is packaging. And the
falsifiable version of our whole thesis is one number: *a standing brief converts to a match at a
rate a resume never does.* Twelve weeks is enough to prove or kill it, and we'd rather know.
**Target: first placement and first cash, five more signed agreements, and a defensible throughput
ratio.**

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


---

# Q4 — Most impressive thing you've done (if the form asks)

> **Not in the three questions HF0 gave us** (problem, traction, twelve weeks), so this is here in
> case the live form has the field. It is the same answer submitted to Soma, unchanged.
>
> **It happens to suit HF0 better than the other programmes.** Every beat in it is about
> compressing a timeline: the SAT ten times, college in three years, building the conference app
> instead of buying a booth, and the current build in three weeks. HF0's pitch is twelve months in
> twelve weeks. Do not adapt it, the fit is already there.

I've usually never had a way in, so I've always had to build one.

I went to eight schools in sixteen years. Being the new kid that many times teaches you
something: you don't wait to be let into a room or a group, you find your own way in. At some
point this stopped feeling like a disadvantage and started feeling like a method.

The first system I turned it on was standardized testing. Most people treat the SAT as a one-shot
event. I sat it + the ACT ten times and moved my score more than 450 points. College was the same
idea. Four years is a default, not a rule, so I did my maths degree at USC in three and interned
every semester I was there.

In the middle of that I founded an autonomous drone delivery company. I was an undergrad
recruiting master's students onto the team, which was the first time I had to convince people to
come work on something of mine. I taught myself aerodynamics out of a book, did the design, wrote
the grants. We won the ABC Innovation Prize and made the Blackstone Launchpad finals. We
assembled the plane. It never flew. I wasn't a hardware specialist, and that's the lesson I kept:
pick problems where your edge is real.

Breaking into banking was the same method with better inputs. I had no route into a top group, so
I built one. First I won an offer at Moelis, used it to get Evercore, then used that to get
J.P. Morgan, where I was the youngest analyst to start full-time. Within a year I was ranked
top-performing in the M&A group across SF and New York, and I was helping run campus and lateral
recruiting.

From there I talked my way into a Chief of Staff seat at Nova Intelligence as employee #1, backed
by Accel, Conviction and SAP. I built every non-engineering function while the team scaled >5x,
including our sales motion from nothing.

At WestBridge, an $8B fund, formerly Sequoia India, I'm the youngest & top-ranked associate. I've
sourced 21 companies in a year: seven have been acquired, and fourteen went on to raise from
a16z, Sequoia and others. Five of them I broke into personally, companies backed by Lightspeed,
Accel and YC.

The pattern is the same every time. The system everyone treats as fixed is usually just something
you can run more iterations against. Recruiting is the biggest one left.
