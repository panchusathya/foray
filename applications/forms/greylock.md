# Greylock Edge — draft for review

Greylock never asks what we're building. All three questions are track-record and provenance
tests, so Q1 is written role-forward — what shipped and what each of us personally owned — and Q2
is a chronology rather than a thesis. The product description lives inside Q2 where it belongs.

**Note:** Greylock is an investment programme, so this is the form most likely to diligence
commercial reality. Q3 states the $0 revenue and the reasoning explicitly. If we can get one price
quoted and the answer recorded before submitting — even a no — it goes in Q3 and materially
strengthens this specific form.

---

# Q1 — Products you have built and launched, and your role

## Tushar Gupta

**Foray** *(current)* — co-founder. Owns the commercial side: the eight client relationships,
the recruiter research programme (~25 conversations, eight recorded in depth), pricing, and the
outbound sequences that produced 840 contacted candidates and 85 engaged. Not the technical
founder — I'd rather say that plainly than have the org chart read wrong.

**Nova Intelligence** — **Chief of Staff and first hire.** An AI coding-agent platform for SAP
migrations, backed by SAP, Accel, Conviction and Chemistry, $40M raised. I joined pre-revenue as
employee one and built and led every non-engineering function — GTM, hiring, sales, partnerships —
while the team scaled more than 5×. Managed 60+ CIO and IT-executive relationships and worked
alongside Google DeepMind researchers and the co-inventor of SAP HANA.

What I shipped there myself: **the SAP Sapphire scheduling app.** We needed Fortune 100 CIOs at the
one conference that mattered and couldn't afford a booth. The scarce resource at a conference isn't
booth space — it's attendee attention and their inability to navigate hundreds of sessions. So we
built a personalised scheduling app for attendees instead. It took off, and it put us in front of
exactly the CIOs we had no other route to.

**DroneCo** *(USC, 2022–23)* — **sole founder.** Autonomous drone delivery, drone-as-a-service. I
did the industrial design and the CAD, the Raspberry Pi work, taught myself aerodynamics out of a
book, wrote the grants, and recruited master's students onto the team as an undergraduate. Two
specced SKUs by payload, a fixed-wing foam airframe with eight lift propellers, a tether-based
release system, 100 km/h cruise and 30 km range. ABC Innovation Prize, Blackstone Launchpad
finalist, roughly $10K in grant funding, a team of four at peak.
**We assembled the plane. It never flew.** I wasn't a hardware specialist — I loved drones, but
loving a thing isn't an edge. What I took from it is to pick problems where my edge is actually
real, and it's the reason I'm confident about this one.

**Walmart procurement tool** — built with one colleague in under a week after finding that Walmart
still ran procurement largely by hand: vendor catalog, demo profiles for the key procurement
stakeholders, an approval/denial workflow. Currently being showcased to Walmart's head of
procurement. 🔗

**Traqde In** *(age 14)* — a two-sided marketplace for second-hand phones. Sole builder. Real
exchanges went through it.

**J.P. Morgan** — M&A Analyst, top-performing in the group across SF and NY. **Led campus and
lateral recruiting initiatives** — roughly 30–35 candidate calls and multiple referred hires. Sole
or lead analyst on 10+ transactions.

**WestBridge Capital** — Investment Associate; top-ranked and youngest on the team. Sourced 21
companies in my first year: seven acquired (Prior Labs → SAP, Common Room → Zoom, Natoma →
Snowflake, Metis → DoorDash, AnswersAI → Quizlet, Hathora → Nitrado, Offset → Rogo), fourteen went
on to raise from a16z, Sequoia, Khosla, Bessemer, Coatue and Madrona. Also built Biz2Credit's
partnerships business from nothing, including its credit-card partnership with Mastercard and its
healthcare partnership with Optum.

## Sathya Panchu

**Foray** *(current)* — co-founder, and the builder. Owns the sourcing engine, the retrieval stack,
the eval harness, the voice and messaging agents, the browser agent and the CRM. Specifically:
hybrid retrieval over a 200K-profile index (Postgres full-text plus pgvector cosine, fused with
reciprocal rank fusion, then an LLM rerank), an offline eval harness reporting nDCG@10, MRR, P@k and
R@10 against ground truth expressed as rules over candidate attributes rather than a frozen label
list, and the reply-classification layer that gives every application its own inbound alias.

**WestBridge Capital internal sourcing infrastructure** — built and shipped, in production use by
the investment team: a **Twitter monitoring system** that surfaces signal before it reaches the
usual channels, and an **AI-generated outreach deck pipeline.** Built in my first year out of
college. It's the same shape as Foray — an instrument watching a market for signal before it's
announced — pointed at companies instead of people.

**`github.com/panchusathya/ForayInstinct`** — sole author. Public, MIT, 247 commits. A self-hosted
iMessage assistant with an **encrypted credential vault: secrets are encrypted before storage, so
the model never sees them.** Next.js on Vercel, Postgres via Neon, Drizzle with versioned
migrations, Turbo monorepo, Vitest, Bright Data browser API with residential proxies, Google
Workspace integration.

**FIRST Robotics, Team 8719 "Quantum Leap"** — **captain and primary programmer** in the 2021–22
season; before that I was on the team that built what we believed was the first four-wheel
dead-wheel odometry localizer in FIRST — four encoders fused through a Kalman filter, holding under
0.2 inches of drift over thirty seconds across ten of ten trials, against three to eight inches for
the standard three-wheel setup. In my own season I owned the autonomous system: hardening the
localizer, starting to build vision into the robot, improving PID tuning, and pushing for a more
customised mechanical base, because the software was only ever as good as the hardware it
controlled.
Inspire Award — FIRST's highest team honour — Virginia 2020 and Ohio 2022. Johns Hopkins Applied
Physics Laboratory Software Innovation Award. Multiple state scoring records. 19th in the world at
Detroit. I also cold-called the sponsors that funded it: at sixteen I carried the largest
prospecting list on the team and closed four, and ran a national $25,000 grant application end to
end. 🔗 *repo URL*

**Finance tooling portfolio** — several tools built to streamline investment workstreams at
WestBridge. 🔗

**AI-native procurement app** — built with Tushar, out of firsthand family experience in
manufacturing where teams spent whole days logging simple tasks into a paper book. 🔗

**Formidable Asset Management** ($900M+ hedge fund) — equity research inside the KONG and FORH ETFs
leading to a strategic end-of-quarter rebalance that **replaced nine holdings.** Return offer.

**Capital Investments at Berkeley** — **Chief Investment Officer** of a $100K+ student-run fund;
built and taught the new-analyst education programme to 20+ students.

**YDelta** — **co-founder**, at 19. Weekly investment and finance editorial; ran two and a half
years.

**UC Berkeley** — BS Business Administration (Honors), Haas, *and* BA Computer Science.

---

# Q2 — How we landed on this problem space

We're both investors at WestBridge, an $8B crossover fund. The problem found us three times before
we went looking for it, and then the thesis changed once we started asking questions.

**First, as investors, from the buying side.** We supported completed executive searches across
portfolio companies — Duplo (CMO, CFO), Biz2Credit (CFO), Innovaccer and Turing (multiple C-suite)
— on engagements where the search firms worked for us. We wrote briefs, judged slates, and watched
the cycle run for months. It drained founder bandwidth the same way every time: weeks of
back-and-forth about what we actually wanted, slates arriving late and off-brief, and founders
spending their scarcest resource on a process nobody owned. **Our own last internal hire took about
seven months with multiple agencies involved.** A role that closed for one of our clients this month
started as a connection back in January — a seven-month cycle that is itself the problem statement.

**Second, from inside recruiting.** Tushar led campus and lateral recruiting for J.P. Morgan's M&A
group, so he'd run the process from the employer's chair at volume. He was then the first hire at a
Conviction-backed startup and watched its recruiters build a team from nothing — their velocity,
their resume books, their candidate quality. That contrast is what convinced us the binding
constraint is process and access, not talent: the same market that takes a mediocre agency seven
months takes a great recruiter with a warm network three weeks. Worth adding that Tushar had
already hacked this funnel from the *candidate* side years earlier — he won a Moelis offer, used it
to get Evercore, used that to get J.P. Morgan. Having run the same funnel from both chairs is an
unusual pair of vantage points and it's most of why we saw this.

**Third, by going and asking — and this is where our thesis changed.** We spoke to about 25
recruiters and recorded eight in depth: True Search, Betts, Daversa, Quantum Talent, Candidate
Labs, Fleming Martin, Kyth. We went in expecting to hear that sourcing was the bottleneck, because
that is what every product in the category is sold against.

That isn't what they said. What they said was that **they are all reaching out to the same
candidates as everyone else, and none of them has any proprietary distribution.** Every recruiter
fishes the same pond — LinkedIn — with the same tools, so the entire industry competes on who emails
first. That's a market-structure answer, not a workflow complaint, and it reframed the company.

**Where it landed.** The problem isn't that hiring is slow. Slow is a symptom. The problem is that
**nobody knows who is actually in the market**, so matching fails and every player compensates with
volume. LinkedIn is a *profile* graph: it records where you've been, and that data is a commodity —
we bought 200,000 profiles from a broker on a credit card. What nobody has is an **intent graph**:
who would move, on what terms, and when. Intent is private (say it on LinkedIn and your boss, your
reports and three recruiters you didn't want to hear from all find out), conditional ("only for a
founding role, above $X, not into an enterprise sales org"), and perishable (open in March, not in
June). None of those are profile fields. "Open to work" is a boolean.

So the people worth $30,000 a placement stay silent, and the industry fights over the small,
visible, mostly junior slice willing to be seen looking.

We're building the intent graph — a private standing record of what would actually move someone,
captured by conversation rather than a profile page, held in confidence and matched silently. Every
surface we run is a sensor: outbound, inbound, a voice agent, a messaging agent, and the
applications we submit on a candidate's behalf, which are the purest revealed intent there is and
which we originate. And we run the searches ourselves, because a two-sided market needs one side
warm on day one — the firm is how we have live roles to match the first briefs against, and it
generates the outcome data that makes our matching better than anyone else's.

**We also built our way in rather than theorising.** We ran on Juicebox first, decided we needed our
own system, and stood one up in six weeks around two full-time jobs. Sathya already had the muscle
from the fund's Twitter monitoring system. Sourcing candidates turned out to be the same muscle
pointed at a different graph.

---

# Q3 — Anything else you'd like us to know?

**Four things, and the last two are the ones you'd find anyway.**

**One — what we know that the category doesn't.** Every product in AI recruiting is sold against
sourcing, because sourcing demos well. But sourcing is the commoditised half: an index is worth the
same to the first buyer and the thousandth — a Juicebox engineer described their own system to us as
closer to a search index. The recruiters told us the real gap is proprietary supply, which none of
them has. Agencies do have real moats — Kay at Candidate Labs told us plainly that the moat is
recruiters and brand, not AI — which is exactly why they cannot scale past headcount. We took that
as the challenge rather than the objection.

**Two — the position we're accumulating, and only one seat on the board can occupy it.** Every
application we send carries its own inbound alias, and every employer reply is classified —
acknowledged, rejected, interview, assessment — with a timestamp, joined to candidate and req. An
ATS sees one company. A job board hands off at the click. A sourcing tool stops at the intro. Being
the origin of the application *and* holding the return channel is the only position from which
hiring outcomes can be measured across companies. That data compounds with every application and is
worth nothing to whoever starts tomorrow. Eventually it's what lets us underwrite: every agency
quotes ~20% with a 90-day guarantee, not because it's correct but because none of them can price
risk. With conversion data we can offer a longer guarantee at a lower fee at the same expected
margin, and win every head-to-head.

**Three — where we are commercially, stated plainly.** Six weeks: 840 candidates contacted, 85
engaged and qualified (10.1% against a ~5% industry baseline for cold outbound, and that's a
qualified rate not a reply rate), ~20 delivered to eight companies — Kyber, OneRobot, Baseten,
LangChain, Vela, Aaru, NeoSigma, Vapi. A Chief of Staff candidate in interview at LangChain. A $10K
referral fee on offer in writing from Vela. Separately, 10+ referrals through our own network,
every one converted to an interview. A candidate waitlist opened on 31 August with signups inside
24 hours.

**Revenue is $0, and that's a decision rather than a failure to close.** We're delivering
high-quality candidates free to earn letters of intent, because charging now would gate every req
behind a contract and cap the candidate volume we can move — and the graph, not this quarter's
invoices, is the asset. The pricing exists in the product; we've chosen not to invoice. We know
that's the thing you'll want tested, and we'd rather name it than have it discovered.

**Four — the honest bear case, and what we're not.** Neither of us is full-time yet: we're at
WestBridge through the end of 2026 and both full-time on Foray from 1 January. Decided, not
aspirational. Cold start on the supply side is the hard part and we're in it — a standing brief is
worth little at a thousand and enormous at a hundred thousand. Our best answer is the rejection
moment, the highest-intent moment in a job seeker's life, where nobody is standing: a candidate
connects their inbox once, and minutes after a rejection lands they get four roles at the same
stage and band. We've shipped the connector. If it doesn't work we'll know quickly. And nobody is
paying for "intent" today, which is exactly why the firm is the revenue line while the graph
accumulates.
