# Soma Fellows — draft for review

Q2 is the one this form turns on, and it's Sathya's. Tushar's honest answer there is not a
competing technical claim — see the note in that section.

---

# Q1 — What are you working on, and what progress has been made to date?

**What we're working on**

Hiring looks like a speed problem. It's a data problem. Nobody has an accurate view of who is
actually in the market, so matching fails and everyone compensates with volume.

LinkedIn is a profile graph — it records where you've been, and that data is a commodity; we bought
200,000 profiles from a broker on a credit card. What nobody has is an **intent graph**: who would
move, on what terms, and when. Intent is private (say it on LinkedIn and your boss finds out),
conditional ("only for a founding role, above $X, not into enterprise sales"), and perishable
(open in March, not in June). None of those are profile fields. "Open to work" is a boolean.

So the people worth $30,000 a placement stay silent and the industry fights over the visible slice
willing to be seen looking. We're building the intent graph: a private standing record of what
would actually move someone, captured by conversation rather than a profile page, held in
confidence, matched silently. Every surface we run is a sensor — outbound, inbound, a voice agent,
a messaging agent, and the applications we submit on a candidate's behalf, which are the purest
revealed intent there is and which we originate. We also run the searches ourselves, because a
two-sided market needs one side warm on day one.

**Progress — seven weeks, both of us still in full-time jobs**

*Commercial:*

- **We signed our first two clients this week.** **Echelon** (Bain Ventures-backed) on an enterprise
  AE search, and **FlowGen Labs** on three roles — forward-deployed engineer, engineering lead and
  research engineer. Both at **20% of base as a success fee**, about **$232K of contingent fees
  across the four roles** if they close. No cash yet, nothing closes until someone is hired, and the
  terms are agreed rather than papered — we're two days in. **The Echelon founder has also offered
  to angel invest.**
- **How we got them matters more than that we did.** Same day, two plays: cold outreach recycling
  our best resumes into interesting roles got **zero responses**; warm outreach to founders we
  already knew converted **two out of two.** That is our own thesis run on ourselves — the pond
  everyone fishes caught nothing, and proprietary distribution converted at 100%.
- Before that: **200,000 profiles indexed → 840 candidates contacted → 85 engaged and a good fit
  (10.1%) → ~20 delivered → 8 companies** on free pilots. The 10.1% is against a ~5% baseline for
  standard recruiter cold outbound, and it's a qualified rate rather than a reply rate. Our
  best-targeted sequence — ~160 sends for Baseten's founding BizOps role — saw **~70% open and ~20%
  express interest**, with interested candidates from Meta, Harvey, Apple, Databricks and DeepMind.
- Eight companies served: Kyber, OneRobot, Baseten, LangChain, Vela, Aaru, NeoSigma, Vapi.
- A Chief of Staff candidate **in interview process at LangChain.** A **$10K referral fee on offer
  in writing** from Vela. OneRobot's founder asked for more after a two-day turnaround.
- Separately, through our own network: **10+ referrals made, every one converted to an interview.**
- **A candidate-side waitlist has been live since late August**, with signups and no spend — the
  first inbound in the company's history, and the start of the supply side.
- **No cash collected.** Every dollar of the $232K is contingent on a hire. Everything before this
  week was delivered free on purpose: charging early would have gated every req behind a contract
  and capped the candidate volume we could move, and the graph is the asset.

*Built and running:*

- Our own sourcing engine — parse the brief, filter, retrieve, score, rerank, with the evidence
  behind every score attached. Hybrid retrieval: Postgres full-text and pgvector cosine fused with
  reciprocal rank fusion, then an LLM rerank.
- **An offline eval harness reporting nDCG@10, MRR, P@k and R@10**, with ground truth expressed as
  rules over candidate attributes rather than a frozen label list, so labels stay valid as the
  corpus grows.
- A **live voice agent** and **messaging agent**, unified with email on one CRM, so context given
  in any channel is known to the agent in the others.
- A **browser agent** that fills and submits applications end to end, **with submissions landing
  against Greenhouse, Ashby and Workday.** We chose browser agents over hand-built ATS adapters on
  purpose: a company doing it the other way told us they maintain 29 adapters and hand-roll HTTPS
  to dodge captcha and spam detection, which works until it doesn't. The general path cost us a
  week on Workday's captcha and a move to residential proxies. We'd make the same call again.
- Per-application inbound aliases, with every employer reply classified — acknowledged, rejected,
  interview, assessment — and timestamped, joined to candidate and req.
- A recruiter SPA and a candidate portal.
- ~161K lines, 185 test files, CI, architectural boundaries enforced by import-linter, and a
  written compliance posture: AI disclosure, recording consent, TCPA, salary history never asked or
  used, and **no protected-category inference anywhere in the stack** — the extraction schema has no
  field such data could land in.
- **259 commits in the five days to 28 August.**

*Research:* about 25 recruiter conversations, eight recorded in depth (True Search, Betts, Daversa,
Quantum Talent, Candidate Labs, Fleming Martin, Kyth). They didn't tell us sourcing was hard. They
told us they're all reaching the same candidates as everyone else and none of them has proprietary
distribution. That's why we're building supply instead of another search tool.

---

# Q2 — Most impressive technical achievements: the n-of-1 claim

## Sathya

I captained FTC 8719 "Quantum Leap" — an independent team, no school affiliation, no district
budget, families covering about $10,000 a year out of pocket.

The technical claim: the team ran what we believed was **the first four-wheel dead-wheel odometry
localizer in FIRST.** Four encoders fused through a Kalman filter, holding **under 0.2 inches of
drift over thirty seconds of driving across ten of ten trials**, against three to eight inches for
the standard three-wheel setup.

The reason nobody had done it is the interesting part. Four wheels makes the entire system less
forgiving — wheel geometry, vector math, calibration, mounting rigidity and code all have to be
right at once — and the prevailing intuition was that if one tracking wheel can introduce drift,
adding sensors just gives you more bad data. **The insight was that with a rigid enough base and
careful calibration, the extra measurements stop being noise and start being redundancy.** It
wasn't universally better; it cost space, design time and iteration. It was better for the robot
and the game we were building for.

In the season I captained I was also the team's primary programmer, and my focus was making
autonomous dependable: hardening the localizer, starting to build vision into the robot, improving
PID tuning, and pushing for a more customised mechanical base — because the software was only ever
as good as the hardware it controlled. That year's field had a large central structure that was
hard to navigate around, so precise autonomous movement was the binding constraint. I wasn't just
writing code for the robot; I was working through the relationship between design, localisation,
controls and match strategy until the system was dependable enough to actually use.

Results: **#1-ranked at every qualifier we entered. Inspire Award — FIRST's highest team honour —
in Virginia 2020 and Ohio 2022. Johns Hopkins Applied Physics Laboratory Software Innovation
Award. Multiple state records for most points in a season. 19th in the world at Detroit.** The
team was self-funded through national grants including the Gene Haas and Siemens Foundations,
awarded to fewer than fifty teams nationally — and I was the one cold-calling for them, at sixteen,
with the largest prospecting list on the team.

Since then: **Berkeley BA Computer Science alongside a Haas honours degree.** At WestBridge I built
the investment team's internal sourcing infrastructure — a Twitter monitoring system that surfaces
signal before it reaches the usual channels, and an AI-generated outreach deck pipeline, both in
production use. That's directly on-thesis for Foray: an instrument watching a market for signal
before it's announced, just pointed at a different graph. And
**`github.com/panchusathya/ForayInstinct`** — public, MIT, 247 commits: a self-hosted iMessage
assistant with an encrypted credential vault, where secrets are encrypted before storage so the
model never sees them.

The through-line, if it's useful: **when I can't see something directly, I build the thing that
measures it.** Odometry measures position with no external reference. The Twitter system measures
what's moving in a market before it's announced. Foray is an instrument pointed at a market nobody
can currently see. (I also refuse to use maps and can name every street in my hometown, which is
probably why I ended up building localisation.)

🔗 *Robotics repo URL and finance tooling portfolio URL — paste before submitting.*

## Tushar

I'm going to answer this honestly rather than compete with the above: I'm not the technical
founder, and my n-of-1 isn't technical.

It's that **I have been graded on my market judgment, and almost nobody at this stage has been.**
In my first year at an $8B fund I sourced 21 companies. Seven have been acquired — Prior Labs by
SAP, Common Room by Zoom, Natoma by Snowflake, Metis by DoorDash, AnswersAI by Quizlet, Hathora by
Nitrado, Offset by Rogo. Fourteen went on to raise from a16z, Sequoia, Khosla, Bessemer, Coatue and
Madrona. That record isn't available to someone through talent alone; it takes a year of decisions
and a year of outcomes, and it's the credential behind the claim that we picked this market on
purpose.

The closest thing I have to a technical n-of-1: at fourteen I built a two-sided marketplace for
second-hand phones and put real exchanges through it. At USC I founded an autonomous drone delivery
company as sole founder — CAD, Raspberry Pi, taught myself aerodynamics from a book, wrote the
grants, recruited master's students onto the team as an undergrad. ABC Innovation Prize,
Blackstone Launchpad finalist, ~$10K in grants. We assembled the plane; it never flew, and the
lesson was to pick problems where my edge is actually real.

---

# Q3 — Two or three of the most impressive people you know

**Ansh Gupta** — Applied AI, Startups at **OpenAI**. Founding AI engineer twice over before that:
at Anything, building autonomous agents that debug, refactor and ship production software, and at
Harper (YC W25), whose multi-agent systems cut insurance processing from days to minutes on the way
to $1.2M ARR and 600+ customers. Before that, enterprise multi-agent systems handling 150,000 calls
a day, and hallucination detection in Amazon's AGI organisation. Dartmouth MS Data Science, BU CS.
*A detail we like: **Anything is on our own sourcing list** — Tushar sourced the company Ansh was a
founding engineer at, before Footwork led its Series A.*
`linkedin.com/in/ansh-gupta-dartmouth`

**Dwayne Nonales** — founder of Kyth, embedded recruiter at Conviction. The most instructive person
we know on this problem: **50–60% of his placements come from referrals, and he gets ~40% response
on cold DMs when he has mutuals** — roughly 15× what the industry gets on email. He's the reason we
believe distribution, not tooling, is the constraint in recruiting. He's been close to this from
the start and has expressed interest in joining.
`linkedin.com/in/dwaynenonales`

**Abhay Singhal** — Member of Technical Staff at **Factory AI**, working on agent performance and
evaluation. One of the most technical people either of us knows, and a friend of Sathya's since high
school. 🔗 *LinkedIn URL needed, or drop him and keep this to two.*

---

# Q4 — How did you hear about us?

🔴 **Blocked — do not submit this form until this is filled in.**

This is the highest-leverage field on the page and it's currently empty. Routes available:

- **Soma is prolific in the YC ecosystem, and Tushar is an angel in three YC companies** (OneRobot,
  Prana, Soren), has attended demo days, and hosted a dinner for the YC W26 cohort.
- **WestBridge partners** — one Slack message to three of them asking who they know at Soma.
- Our eight customer companies and their investors.

**Action: send that message today.** A named referrer with a LinkedIn URL in this box changes how
every other answer above is read.
