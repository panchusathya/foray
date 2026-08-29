# Block A — the seven company answers

Long form. a16z and Greylock impose no length limits, so these are written to the length the
idea deserves; every shorter cut (Z-Fellows at 500 characters, HF0's "briefly") is trimmed
**from** these, never drafted short from scratch.

Every factual claim traces to `evidence.md`.

---

## A1 — The problem

**Short (HF0 "briefly", Z-Fellows 500ch source):**

Hiring is the highest-stakes decision a company makes and nobody owns the outcome. Time to
close runs months — our own last internal hire took roughly seven months across multiple
agencies — because the work is split across recruiting ops, agencies, referrals, inbound and
several ATS workflows, and a candidate leaks out at every handoff.

**Full:**

Hiring is the highest-stakes repeated decision a company makes, and structurally nobody owns
the outcome. Recruiting ops owns the process, agencies own a slice of sourcing, referrals
arrive through nobody in particular, inbound piles up in an ATS, and each handoff is a place
a good candidate goes quiet. Time to close runs months and sometimes years. Our own last
internal hire took about seven months even while working with multiple agencies, and a role that
closed this month off a referral we made started as a connection back in January.

Discovery has gotten worse, not better. LinkedIn is saturated with AI-generated outreach that
has trained everyone to ignore it. Candidates fire applications into the void on Indeed and
Handshake with no signal about whether a human will ever read one. Agencies surface mediocre
candidates months into a search because they are still running boolean queries over the same
index everyone else has. The right engineer and the right company cannot find each other, and
both sides have concluded the other isn't trying.

The specific place value leaks is not where the industry thinks. We interviewed ~15 recruiters
and recorded eight in depth — True Search, Betts, Daversa, Quantum, Candidate Labs, Fleming
Martin, Kyth. Six of them named the same two bottlenecks in different vocabularies: top of
funnel, and the middle. Sourcing has a dozen well-funded tools. **The middle — qualifying,
keeping warm, chasing, scheduling, reading the soft signals that say a candidate is about to
take another offer — has none.** That is where every recruiter loses candidates, and it is the
part of the job that is pure high-frequency communication, which is exactly what an agent is
good at and a human at 40 reqs is not.

---

## A2 — What we're building

**Short:**

An AI-native recruiting firm — what Crosby is doing for legal, for hiring. We run the firm and
own the outcome; the software is what lets one recruiter do the work of ten. Sourcing, the
mid-funnel and application execution are all built ground-up and already running.

**Full:**

We are building an AI-native recruiting firm. Not a tool we sell to recruiters — the firm
itself, where we own the outcome end to end, and where the software is what makes the unit
economics work. The comparison we use internally is Crosby in legal: take a services business
with a known price and a known bottleneck, and rebuild the delivery layer so throughput per
person moves by an order of magnitude.

Three layers, all shipped and running today:

**Sourcing.** Our own engine — parse the brief, filter, retrieve, score, rerank — over a
200K-profile index, with the evidence behind every score attached to the result. Retrieval is
hybrid: Postgres full-text and pgvector cosine fused with reciprocal rank fusion, then an LLM
rerank. It has an offline evaluation harness reporting nDCG@10, MRR, P@k and R@10, with ground
truth expressed as rules over candidate attributes rather than a frozen label list so the
labels stay valid as the corpus grows.

**The middle — the part nobody else has built.** A voice agent, a text agent and email, all
unified on one CRM, so context a candidate gives in any channel is known to the agent in the
others. It qualifies, keeps candidates warm, chases, and schedules. Compliance is designed in
rather than bolted on: AI disclosure and recording consent, salary history never asked or
used, work authorisation reduced to exactly two lawful questions, TCPA consent captured at
the right moment.

**Execution.** A browser agent that fills and submits applications end to end. It has submitted
applications successfully in testing and goes live shortly. Plus a recruiter SPA and a candidate
portal.

**Where we start and where we're going.** Today we work early GTM and ops roles at AI startups
alongside engineering hires. The model is volume first, quality second: run high volume through
the system, and funnel the highest-quality candidates into client work where the fee is. Engineering
is where that compounds hardest — highest volume, worst existing process — so it is the destination,
and the seed-stage GTM and ops roles are the beachhead, because that is where our fund network gives
us unfair access. We would rather earn the right to the big segment from real traction in a small one
than claim it on day one.

---

## A3 — Traction

> **Write this LAST.** It is the weakest answer in the pack and two of five forms hinge on it.
> Refresh with anything that lands during the week — a signed fee, a closed placement, a
> second interview — before submitting.

**Current state, honest:**

**~20 candidates delivered to eight companies in under six weeks** — Kyber, OneRobot, Baseten,
LangChain, Vela, Aaru, NeoSigma and Vapi — under pilot and POC arrangements, several run to earn
the business. **No money has changed hands yet, deliberately: we wanted the right to go back with
a price, and that conversation is happening now.**

- **LangChain** — Chief of Staff candidate currently **in interview process.**
- **Kyber** — Head of Engineering, **closed this month.** That one came through a referral we made
  rather than a search we ran, and the connection dates to January: a seven-month cycle that is
  itself our problem statement.
- **OneRobot** — 3–4 confirmed applications from ~50 cold outbound, two-day turnaround, founder has
  asked for more.
- **Baseten** — six candidates to the head of talent for BizOps, five cold-sourced; one interested.
- **Vela** — three intros offered from ~10 inbound, with a **$10K referral fee on offer in writing.**
- **Aaru, NeoSigma, Vapi** — five further connections across Deployment Lead, Founding Business
  Generalist and Chief of Staff.

Separately, and not counted in the twenty: referrals made through our own network alongside the day
job. **Every one of those has converted to an interview.**

On the product side, in the same window: a sourcing engine over a 200K-profile index with an offline
eval harness reporting nDCG@10, MRR and P@k; a voice agent and a text agent unified with email on one
CRM; a browser agent submitting applications successfully in testing and going live shortly; a
recruiter SPA and a candidate portal. ~161K lines, 185 test files, CI. 259 commits in the five days
to 28 August — while both of us were still working full-time jobs.

**Revenue is $0** and **neither of us is full-time yet.** Both are true, both are about to change,
and we would rather you hear them from us.

## A4 — How we landed on this problem space

*(Greylock is explicitly testing whether we arrived here honestly, so this answer is a
chronology, not a thesis.)*

We are both investors at WestBridge, an $8B fund. The problem found us three times before we
went looking for it.

**First, as investors.** We supported hiring at Turing, Innovaccer, Biz2Credit and Nightfall
AI, and watched it drain founder bandwidth in the same way every time — weeks of back-and-forth
with search firms about what we actually wanted, slates that arrived late and off-brief, and
founders spending their scarcest resource on a process nobody owned. Our own last internal hire
took about seven months with multiple agencies involved.

**Second, from the inside of recruiting.** Tushar led campus and lateral recruiting for J.P.
Morgan's M&A group, so he has run the hiring process from the employer's chair at volume. He
was then the first hire at a Conviction-backed startup and watched its recruiters build a team
from nothing — their velocity, their resume books, their candidate quality. That contrast is
what convinced us the binding constraint is process, not talent: the same market that takes a
mediocre agency seven months takes a great recruiter with a warm network three weeks.

**Third, by going and asking.** We spoke to ~15 recruiters and recorded eight in depth. We went
in expecting to hear that sourcing was the problem, because that is what every tool in the
category is sold against. Instead six of eight described the same second bottleneck — the
middle of the funnel — and none of them had a product for it. Shlok at Quantum put it most
directly: soft signals and mid-funnel engagement beat sourcing tooling. Kay at Candidate Labs
told us the moat is recruiters and brand, not AI, which we took as the real challenge: build
the thing that makes one recruiter worth ten, and the moat argument inverts.

We also built our way in rather than theorising. We ran on Juicebox first, decided we needed
our own system, and stood one up. Sathya had already built the equivalent muscle at the fund —
a VC Twitter follower bot and an AI-generated outreach deck system used by the investment team.
Sourcing candidates turned out to be the same muscle pointed at a different graph.

---

## A5 — Why we're the ones to build it

**Sourcing at volume is our day job.** Between us, in a year at a high-volume fund, we have
spoken to roughly 1,000 companies and outreached to two to three times that. We set up
monitoring APIs across X, built web agents that track fundraising announcements, and ran 8–10
sourcing tools in production to find what works. Sourcing candidates is that muscle pointed at
a different graph, and we started with it already built.

**We have sat on the buying side of hiring, repeatedly.** Tushar led campus and lateral recruiting
at J.P. Morgan and was the first hire at a Conviction-backed startup that scaled its team more than
5× around him. Between us we have supported executive searches across WestBridge portfolio companies
— Duplo, Biz2Credit, Innovaccer, Turing — writing briefs, judging slates and coordinating with the
search firms we had engaged, across roles from CFO to CMO. Very few people building recruiting
software have spent that much time in the seat where the requisition is written, the slate is
rejected and the cycle drags past month four.

**We can build it.** Sathya holds a BA in Computer Science from Berkeley alongside his Haas
honours degree, built the first four-wheel odometry system in FIRST robotics — four dead wheel
encoders, a Kalman filter and a decision algorithm, which won the Johns Hopkins Applied Physics
Laboratory Software Innovation Award — and ships production tooling at the fund today. What is
running now is not a prototype: it has an eval harness, 185 test files, CI, enforced
architectural boundaries and a written compliance posture.

**We have the network the product needs to be pointed at.** This industry forced us to build
real access into Cognition, Cursor and OpenAI circles, so we can aim attention at high-quality
candidates rather than casting wide. Strong recruiters get three to four referrals from every
person they place; that compounding is the whole game.

**And we are designing with a practitioner, not guessing.** Dwayne Nonales — founder of Kyth,
Conviction-embedded, ~50–60% of his placements sourced from referrals and ~40% DM response when he
has mutuals — has been close to this from the start and has expressed interest in joining. What we
are automating is a desk like his, built against how he actually works.

**We move fast.** In the first two days on this idea we cold-outreached ~45 engineering
candidates and got two inbound. In the six weeks since we have built and shipped the sourcing
engine, the voice agent, the text agent, the browser agent, the CRM and two front-ends, and put
candidates in front of eight companies.

---

## A6 — Competitors, and what we understand that they don't

*(Only Z-Fellows asks this outright. It is written early anyway, because it is what stops A2,
A4, A7 and B12 from reading generic, and it is the right content for "anything else.")*

**The field, honestly:**

- **Sourcing tools** — Juicebox, Gem, SeekOut, Contrario. Good products. But a Juicebox
  engineer described their own system to us as closer to a search index, like Exa — and that is
  the correct description. An index is worth the same to the first buyer and the thousandth.
- **Agencies** — Betts, Daversa, True Search, Candidate Labs, Quantum. Real moats, correctly
  identified by Kay at Candidate Labs: recruiters and brand. Which is exactly why they cannot
  scale past headcount.
- **Marketplaces** — Paraform. Genuinely good at putting incentivised recruiters on a req
  instantly; the matching and the entire middle of the funnel stay manual.
- **Consumer auto-apply** — LazyApply, Simplify and the rest. A race to the bottom where success
  means the customer stops paying, and where per-application pricing selects for the least
  employable candidates.
- **ATS-adapter players** — Tsenta, who told us they hand-build adapters for 29 ATSs and
  hand-roll HTTPS to avoid captcha and spam detection. It works until it doesn't; we chose
  browser agents because generality beats coverage over any horizon that matters.

**What we understand that they don't:**

**One — everyone is competing on the commoditised half.** The entire category is sold against
sourcing, because sourcing is legible and demos well. But six of the eight recruiters we
recorded named the middle of the funnel as where they actually lose candidates, and not one of
their vendors has built for it. We built the middle first.

**Two — outcome data is the only asset in recruiting that compounds, and exactly one position
on the board can collect it.** Every application we send carries its own inbound alias, and
every employer reply is classified — acknowledged, rejected, interview, assessment — with a
timestamp, joined to the candidate and the req. An ATS sees one company. A job board hands off
at the click. A sourcing tool stops at the intro. Being the origin of the application *and*
holding the return channel is the only seat from which hiring outcomes can be measured across
companies. A search index is identical for everyone; this gets better with every application we
send, and is worth nothing to whoever starts tomorrow.

**Three — that data eventually lets us price risk, and nobody else can follow.** Every agency
quotes ~20% with a 90-day guarantee, not because it's right but because none of them can
underwrite. With conversion data we can offer a longer guarantee at a lower fee at the same
expected margin, and win every head-to-head on price.

**Four — the candidates worth $30K are passive, and LinkedIn structurally cannot serve them,**
because you cannot express interest there without leaking it to your boss, your reports and
three recruiters you didn't want to hear from. So the best people stay silent and the industry
fights over the small, active slice willing to be seen looking. Mass-apply products are loud.
The people worth placing are quiet.

---

## A7 — What we'd accomplish in twelve focused weeks

*(HF0's framing: twelve months of progress in twelve weeks. So the answer has to name what
would otherwise take twelve months and why concentration collapses it.)*

What would take us twelve months at current velocity is not the software — we have shipped an
enormous amount in six weeks while both working full time. It is **the commercial loop**: price,
close, deliver, measure, and turn measured outcomes back into pricing. Every step of that loop
is gated on being able to give it full attention during business hours, which is precisely what
we do not have today.

**Weeks 1–4 — revenue and the entity.** Incorporate, paper Dwayne, and convert the eight
companies already holding our free work onto a price sheet: 18% on a hire, or $1,000 per
completed interview capped at $12K per hire, buyer's choice. Target: five signed agreements and
first cash collected. Interview five hiring managers about what they actually pay today —
something we have never done, having so far interviewed only recruiters.

**Weeks 5–8 — prove the multiple.** Run Dwayne's entire desk on our system and measure the one
number that decides whether this is a company: placements per recruiter-month against his own
manual baseline. A good contingent recruiter runs one to one-and-a-half. If the system takes one
recruiter to four, that multiple is the whole business, and everything else is packaging.
Target: 25+ qualified interviews delivered and a defensible throughput ratio.

**Weeks 9–12 — turn the data position into a product.** By then we will have enough classified
replies to publish what nobody in hiring can currently measure: per-company response latency and
what share of applications ever reach a human. That becomes three things at once — a consumer
feature no competitor can copy, a content engine that fixes our weakest channel, and the warmest
cold email in the market. Target: 20 paying companies and the first cohort of outcome data large
enough to price against.

**Why HF0 specifically.** Two of the three phases above are blocked on undivided attention
rather than on capital or headcount, and the residency format is the only thing on our list that
directly buys that.
