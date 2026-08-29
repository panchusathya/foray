# Evidence inventory

Every provable claim, with its source. **Nothing goes into an application that isn't on
this page, and nothing on this page gets left out of every application it fits.**

The SPC application was rejected while describing a company far weaker than the one that
actually exists. This file is the fix.

Last verified against the repo at `7d056d5`, 29 Aug 2026.

---

## 1. Product — what is actually built and running

| Claim | Source |
|---|---|
| Own sourcing engine: parse → filter → retrieve → score → rerank, with evidence attached to every score | `packages/foray/`, `README.md` |
| 200K-profile talent index, acquired 19 Aug 2026 | Bright Data; notes 8/19 |
| Hybrid retrieval: Postgres `tsvector` (GIN) + pgvector cosine, fused with reciprocal rank fusion, LLM rerank on top | `README.md` "How search works"; `packages/core/juicebox/search/` |
| Voice agent — live, taking real intake calls | `packages/core/juicebox/intake/`, `VOICE_INTAKE_PLAN.md` |
| Text agent — live | `packages/core/juicebox/messaging/`, `MESSAGING_INTAKE_PLAN.md` |
| Voice, text and email unified on one CRM: context given in any channel is known to the agent in the others | `packages/core/juicebox/crm.py`, `integrations/sync.py` |
| Browser agent that fills and submits job applications, built end-to-end | `packages/core/juicebox/applications/`, `AUTO_APPLY_SETUP.md` |
| **Multiple applications submitted successfully in testing.** Not yet live; no application submitted on behalf of a real candidate. `GAP: how many test submissions, against which ATSs, and what is the live date?` | confirmed 29 Aug |
| Two shipped front-ends: recruiter SPA and candidate portal | `packages/web/`, `packages/portal/` |
| Gmail, Outlook and GitHub connectors shipped | `packages/core/juicebox/connectors/google.py`, `microsoft.py`, `github.py` |
| Durable job queue with `FOR UPDATE SKIP LOCKED`, backoff and parking | `README.md` "Scale notes"; `juicebox/worker.py` |
| ~161K lines of code (140,141 Python + 21,210 JS/TS) | `wc -l` over `packages/` |
| 259 commits, 24–28 Aug 2026 | `git log --since` |

## 2. Engineering rigor — the part that separates this from a demo

| Claim | Source |
|---|---|
| **Offline evaluation harness reporting nDCG@10, MRR, P@k and R@10** | `eval/run_eval.py` |
| Ground truth expressed as *rules over candidate attributes*, not a frozen id list, so labels stay valid as the corpus grows | `eval/queries.yaml` header |
| Separate job-match eval | `eval/run_job_match.py`, `eval/job_match.yaml` |
| 185 test files | `find packages -name 'test_*.py' -o -name '*.test.js' -o -name '*.spec.js'` |
| CI on every push | `.github/workflows/ci.yml` |
| Architectural boundaries enforced in CI, not by convention | `packages/foray/.importlinter` |
| Secret scanning and pre-commit hooks | `.gitleaks.toml`, `.pre-commit-config.yaml` |
| Runs fully offline without an API key — the model improves quality, it is not a dependency | `README.md` |
| Compliance designed in, not bolted on: AI disclosure and recording consent, salary history never asked or used, work authorisation reduced to exactly two questions, protected categories, TCPA consent, minors, connected-app scopes | `INTAKE_COMPLIANCE.md`, 11 sections |
| Published honest limits rather than hiding them | `README.md` "Scale notes — read before production" |
| **No protected-category inference anywhere in the product.** The ranking prompts instruct "NEVER infer or reference gender, ethnicity, age, or nationality"; the intake extraction schema has *no field* such data could land in — structural, not policy. EEO self-identification fields on Ashby and Greenhouse are parsed only to be declined correctly. | `packages/foray/archetypes.yaml:45`, `INTAKE_COMPLIANCE.md` §4, `packages/core/juicebox/docgen/answers.py`, `applications/submit/browser/questions.py` |

### Public artifact — link this

**`github.com/panchusathya/ForayInstinct`** — MIT-licensed, public, **247 commits.** A
self-hosted iMessage assistant with an encrypted credential vault: a browser-capable agent
that can act on your behalf while **secrets are encrypted before storage so the model never
sees them.** Next.js on Vercel, Postgres via Neon, Drizzle with versioned migrations, Turbo
monorepo, Vitest, Oxlint, one-click deploy, Bright Data browser API with residential proxies,
Google Workspace integration, and a GoForay candidate-deployment variant wired into JuiceBox.

This is the single best "things you made exist" link either founder has. The
encrypt-before-storage decision is the detail to name — it shows security judgement rather
than a demo.

## 3. The data position — the thing nobody else has

This is the strongest single technical claim in the company and it appeared in no prior
application.

| Claim | Source |
|---|---|
| Every application carries a unique inbound alias (`anna+<token>@`), so every reply routes back to one application | `packages/core/juicebox/models.py` |
| Every inbound employer reply is classified `verification \| otp \| acknowledged \| rejected \| interview \| assessment \| other`, with `received_at`, joined to candidate and req | `models.py` (`kind` field) |
| Full application state machine: `draft → prepared → awaiting_consent → submitting → submitted → acknowledged → interviewing → offer`, with `hired` on the candidate | `packages/core/juicebox/applications/state.py` |
| Live funnel analytics endpoint | `packages/core/juicebox/routers/analytics.py` (`/v1/analytics/funnel`) |

**Why it matters, in one line:** an ATS sees one company, a job board hands off at the
click, a sourcing tool stops at the intro. Sitting at the origin of the application *and*
holding the return channel is the only position from which hiring outcomes can be measured
across companies — and outcome data is the one asset in recruiting that compounds.

## 4. Commercial traction

Eight companies have received candidates from us.

| Company | What happened |
|---|---|
| **Kyber** | Head of Engineering — **closed Aug 2026.** A referral we made, not a search we ran; connection made Jan 2026 via the SingleSprout team. A real outcome and a real seven-month cycle that is itself the problem statement, but **it is a referral, not a placement, and must never be written as one.** |
| **OneRobot** | 3–4 confirmed applications from ~50 cold outbound; 2-day turnaround; founder confirmed he wants more |
| **Baseten** | 6 candidates delivered to the head of talent for the BizOps team, 5 sourced by cold outbound; 1 interested candidate |
| **LangChain** | Chief of Staff candidate — **interview in process** |
| **Vela** | 1 connection from an X post, 3 intros offered from ~10 inbound; **$10K referral fee on offer in writing** |
| **Aaru** | 2 connections made to the CEO for Deployment Lead |
| **NeoSigma** | 2 connections for Founding Business Generalist |
| **Vapi** | 1 connection for Chief of Staff |

**~20 candidates delivered across 8 companies in under six weeks**, all under pilot / POC
arrangements, several done to earn the business. **No money has changed hands.**

Separately, and not counted above: referrals made through the network during the day job.
**10+ referrals, and every one has converted to an interview.** That is the best conversion
number in the pack and the only one with a clean denominator.

Inbound: none yet. Referrals have come from outbound.

## 5. Market research

- **~15 recruiter conversations**, 8 recorded in depth: Corky Ribakoff and Nick Tashima
  (True Search), Patrick Kellenberger (Betts), Mitchell Barrett and Juan (Daversa), Shlok
  Punjabi (Quantum Talent), Kay Kakish (Candidate Labs), Dan Grosh (Fleming Martin),
  Dwayne Nonales (Kyth).
- Competitive deep-dive across AI recruiting products and agency firms by ICP.
- Standing cadence of 4–5 recruiter coffees a week.

**The finding that shaped the product** — six of eight named the same two bottlenecks, in
different words:

> Dwayne: "Top-of-funnel is the real bottleneck."
> Nick: "Biggest unmet need is auto-scheduling for inbound replies."
> Mitchell: "Bottlenecks: sourcing *and* scheduling."
> Shlok: "Soft signals and mid-funnel engagement beat sourcing tooling."
> Kay: "The moat is recruiters and brand, not AI."
> Corky: "Exec-level AI sourcing is still noisy."

Sourcing is crowded and commoditising. The middle of the funnel — qualify, keep warm,
chase, schedule — is where every recruiter bleeds candidates and where no product exists.

## 6. Founders

**Tushar Gupta** — Investment Associate, WestBridge Capital ($8B, ex-Sequoia India);
youngest on the team, sourced 6 deals that resulted in strategic acquisitions (Prior Labs,
Natoma, Hathora). Previously **first hire and Chief of Staff at Nova Intelligence**
(SAP/Accel/Conviction/Chemistry-backed, $40M raised) — joined pre-revenue, scaled the team
>5×, built and led every non-engineering function, managed 60+ CIO relationships.
Before that **top-performing analyst in J.P. Morgan's M&A group across SF and NY, where he
led campus and lateral recruiting** — the founder–market-fit fact that appeared in no prior
application. First hire at iNRI (YC W23). Interned at Macquarie, Battery Ventures, Belong
Home. USC, BS Mathematics & Economics, **completed in three years**. Angel investor in
three YC companies (OneRobot, Prana, Soren).

**Sathya Panchu** — Investment Analyst, WestBridge Capital, covering Freshworks (NASDAQ:
FRSH), Nexthop AI and Postman; formed the firm's cloud-infrastructure thesis; represented
the firm at **NeurIPS and ICML**; **built the investment team's internal sourcing tooling —
a VC Twitter follower bot and an AI-generated outreach deck system.** Return offer from the
2024 WestBridge summer analyst program ($SNOW long thesis; 3 diligences, 2 self-sourced).
Return offer from **Formidable Asset Management** ($900M hedge fund), where he drove a
nine-holding ETF rebalance across KONG and FORH.
**UC Berkeley — BS Business Administration (Honors, Haas) *and* BA Computer Science
(CDSS)**, class of 2025. **Chief Investment Officer of Capital Investments at Berkeley**
(student-run fund, $100K+ AUM), taught 20+ analysts. Co-founded YDelta, a finance
editorial, at 19.
Case competitions: **Dodge & Cox FA23 — 1st**; MSD Partners FA22 — 2nd; Goldman Sachs Tech
IB SP24 — finalist; Undergraduate Investment Competition — finalist.
**FIRST robotics — coder, then lead programmer, then captain.** Built the **first four-wheel
dead-wheel odometry system in FIRST** (four encoders fused through a Kalman filter with a
decision algorithm), winner of the **Johns Hopkins Applied Physics Laboratory Software Innovation
Award** at the Maryland Tech Invitational. Then built **simulation software for the team's own
local testing and scaled it to 10,000 users across FTC teams** — the only distribution number
either founder has, earned before college, and stronger than any prize on this page because
adoption is chosen rather than awarded. **All repos public.** `GAP: the URLs.`
Two World Championships (one as captain), Inspire Award
winners in Virginia 2020 and Ohio 2022 (the program's highest team honour), multiple state
season-scoring records. Self-funded the team through Gene Haas and Siemens Foundation grants,
awarded to fewer than 50 teams nationally.

**Tushar — the mid-funnel work he has already done by hand.** At J.P. Morgan, ~30–35 recruiting
calls with undergraduate juniors, several of whom he referred and who were hired. At Nova he
watched the team go from **3 people including him to ~15 in under a year**, and his own part was
the mid-funnel: outbound to interesting candidates, working alongside the in-house Conviction
recruiter, coordinating and scheduling calls, and taking engineering candidates to closing
dinners. **That is precisely the stretch of the funnel Foray automates, done manually, at a
company that 5×'d around him.** It belongs in A5 as the sharpest founder–market-fit line either
founder has: he has personally been the system he is now building.

**Together — WestBridge:**

- **Both ranked top of their level** — Tushar the top-ranked and youngest associate, Sathya the
  top-ranked analyst and **the first hire out of college for the WestBridge office.** A clean,
  symmetric line worth using verbatim.
- **Sourcing record — 21 companies Tushar sourced at WestBridge, and where they ended up.**
  Seven acquired: **Common Room → Zoom**, **Natoma → Snowflake**, **Prior Labs → SAP**,
  **Metis → DoorDash**, **AnswersAI → Quizlet**, **Hathora → Nitrado**, **Offset → Rogo**.
  Fourteen more went on to raise from the top of the market: **Ornn** (a16z led the A),
  **Merendil** (a16z led the seed), **Tessera** (a16z Series A), **Trajectory** (Bessemer and
  Radical seed+, Sequoia led the A), **Broccoli** and **Sanjay Kalra** (Khosla), **Sycamore**
  (Coatue/Lightspeed), **Doss** (Madrona B), **Triomics** (Battery A), **Accordance**
  (Altimeter A), **Anything** (Footwork A), **Actively** (TCV and First Harmonic B),
  **JustAI** (Base10 A), **Integral** (Venrex A).
  **All 21 were sourced by Tushar**, not merely evaluated — confirmed 29 Aug. That makes this
  an origination record, not an anti-portfolio, and it is the strongest single piece of
  evidence for the sourcing claim in A5: *21 companies sourced in a year; 7 acquired; 14 that
  went on to raise from the top of the market.*
  `GAP: attribution — Sathya's sourcing record is separate (3 diligences with 2 self-sourced
  from his own thesis work, plus the Twitter follower bot). Don't merge the two into a "we".`
  **Tactical note for the a16z form specifically:** three of these took a16z money after he
  sourced them. *"I sourced Ornn, Merendil and Tessera before you funded them"* is checkable,
  flattering and high-signal — use it there and nowhere else.
- **Built Biz2Credit's partnerships business from the ground up**, including its credit-card
  partnership with **Mastercard** and its healthcare-provider partnership with **Optum /
  UnitedHealth.**
- **Worked alongside WestBridge's recruiting on internal hiring** — coordinating closely with
  recruiters, maintaining touchpoints with portfolio recruiting teams, and watching how long and
  painful those processes were up close. **This is observation and coordination, not running
  hiring.** The only place either founder has personally *run* a hiring process is Tushar's
  campus and lateral recruiting remit at J.P. Morgan; that one stands on its own and doesn't
  need a second example propped beside it.
- **Sourced the Head of Applied AI Research at OpenAI as a speaker for an event** — access and
  network reach, **not a recruiting placement.** Usable as evidence of reach; never as a hire.
- ~1K companies spoken to and 2–3× that outreached in a year; monitoring APIs across X; web
  agents tracking fundraising announcements; 8–10 sourcing tools run in production. **Sourcing
  candidates is the same muscle pointed at a different graph** — and they started with it built.

## 10. Claims that need precision before use

Both are strong. Both are also the kind of thing a16z and Greylock can check in one message,
and these two firms are more likely than anyone to know these exact deals.

- **$2.5B: drop the number, keep the list. Decided 29 Aug.** Of the seven acquisitions, only
  **Prior Labs → SAP** is publicly announced with terms. An aggregate that cannot be sourced is
  worse than no aggregate: the first question is "how do you get there," and "I can't say" costs
  more than the number was ever worth. **The verifiable version is stronger anyway** — the *fact*
  of each acquisition is public even where terms aren't, and every funding round is:
  *"21 companies sourced in a year. Seven acquired — Prior Labs by SAP, Common Room by Zoom,
  Natoma by Snowflake, Metis by DoorDash. Fourteen more went on to raise from a16z, Sequoia,
  Khosla, Bessemer, Coatue and Madrona."* Every clause of that survives a check, and the list
  reads bigger than the number did.

- **"Will be running" versus "have run" — the same distinction, one week later.** See §11.
  This is now the most valuable unverified claim in the pack, so it gets the most care.
- **The OpenAI sourcing claim.** "Sourced the Head of Applied Research at OpenAI" means one
  thing if a person was recruited into a role and a very different thing if they were sourced
  as a speaker or contact for an event. Both are usable; they are not the same sentence.

## 7. Talent pipeline

- **Dwayne Nonales** — founder of Kyth, Conviction-embedded recruiter. ~50–60% of his
  placements come from referrals; ~40% DM response rate when he has mutuals. Has asked to
  join.
- **Arkash Jain** — engineer, employee #2 running an AI-for-services company's entire
  engineering organisation; bootstrapped a services business from $150K to $3M ARR in ten
  months. Has asked to join.
- **Ansh Gupta** — **Applied AI, Startups @ OpenAI**, San Francisco. Previously founding AI
  engineer at **Anything** (formerly Create.xyz) building autonomous agents that debug, refactor
  and ship production software — multi-agent orchestration, agentic debugging, memory
  architectures, prompt caching. Before that founding AI engineer at **Harper (YC W25)**, whose
  multi-agent systems cut insurance processing from days to minutes on the way to $1.2M ARR and
  600+ customers. Before that, enterprise multi-agent systems at Blue Health Intelligence
  handling **150K calls a day**, and hallucination detection at **Amazon's AGI org**. Dartmouth
  MS Data Science, Boston University CS. Ships side projects (TubeChat AI, on the Chrome Web
  Store). His own profile cites voice-enabled interfaces that lift conversion — directly the
  Foray stack.
  **The checkable connection worth naming: "Anything" is on Tushar's own sourcing list** — he
  sourced the company Ansh was a founding engineer at, before Footwork led its Series A.
  ⚠️ **Care:** he currently works at OpenAI. Name him as someone you'd recruit and say why he is
  exceptional; do not assert in writing that he is leaving OpenAI for you. That is a claim about
  a third party's employment intentions, and applications circulate.
- **Niki Nguyen and Vivian Nguyen** — recruiters at Conviction. Tushar was the first hire at
  a Conviction-backed startup and both were instrumental in the hires that followed.
- **Abhay Singhal** — MTS at Factory AI; agent performance and evals background.

## 8. Honest gaps — stated, never hidden

- **Revenue: $0.** Everything delivered so far has been free — pilots and POCs, several run to
  earn the business.
- **Neither founder is full-time yet. Both go full-time at the end of the year.** They are at
  WestBridge and building around the job. **Write it exactly that way — flatly, with the date.**
  "We're basically full-time" is not an answer to a binary question; it reads as hedging and
  invites the follow-up you least want. The flat version converts the weakness into evidence:
  *"not full-time until year end — and in six weeks around the day job we shipped a sourcing
  engine, a voice agent, a text agent, a browser agent, a CRM and two front-ends."* The output
  is the proof. The hours are not: grind-signalling reads badly to every one of these five, and
  259 commits in five days already says it better.
- **Dwayne has expressed interest but has not committed.** A good data point, not a co-founder,
  and not to be written as one.
- Cold email response rate 2–3%, inbox previously flagged for spam; migrated to Zoho.
  We know this channel is our weakest and the recruiter interviews say LinkedIn beats it
  10–15× (Patrick: 30–40% InMail; Dwayne: ~40% DM with mutuals).

## 9. Do not link, do not mention

- **`github.com/panchusathya/VC_Portcos`** — a funding-announcement monitor that screens
  founders through **Namsor name-origin detection to target Indian-origin individuals**, and
  fires alerts only when that screening passes. Ethnicity inferred from names, automated, in a
  **public repo on the technical co-founder's account**, while pitching five accelerators on an
  AI recruiting firm. Diligence at a16z and Greylock includes reading a technical co-founder's
  GitHub. **Make it private today.** (Foray itself is clean — see §2 — and that contrast is
  exactly why this shouldn't be sitting next to it.)
- **`rottenpotatoes`, `chips7.7`, `chips8.5`** — Berkeley CS169 coursework. RottenPotatoes is
  the canonical assignment from Armando Fox's Engineering Software as a Service course and any
  a16z reviewer will recognise it on sight. Listing coursework as a side project reads as not
  knowing the difference.

An accelerator will find all four of these. Volunteering them costs nothing and buys the
credibility that carries every other claim on this page.

## 11. Client-side executive search experience

**Corrected 29 Aug.** The earlier reading of this — Foray holding mandates to run C-suite
search at portfolio companies — was wrong, and the real version is a different and smaller
claim that is still worth having.

What actually happened: across WestBridge portfolio companies — **Duplo (CMO, CFO), Biz2Credit
(CFO), Innovaccer (multiple C-suite), Turing (multiple)** — they **supported completed
executive searches while representing WestBridge**, on contingency and retained engagements
where **the recruiting firms were their clients.** These are finished roles, not Foray
mandates. No Foray revenue, no engagement letters, no pipeline.

**What it is worth, stated correctly:** they have sat on the *buyer's* side of a dozen
executive searches at real scale — writing the brief, judging the slates, watching the cycle
run for months. That is genuine founder–market fit and a much better claim than most people
building recruiting software can make. It belongs in A4 (how they landed on the problem) and
A5 (why them), where it is strong.

**What it is not:** demand, traction, revenue, or pipeline. It does not go in A3 and it never
gets written as "we run hiring for portfolio companies."

## 12. Positioning — settled 29 Aug

Not executive search. Current work is **early GTM/ops roles at AI startups plus engineering
hires**, and the plan is to run high volume and funnel the highest-quality candidates into
client work. That is the two-tier thesis from the August notes, and it is coherent with both
what has been built and what has been delivered.

So the three-segment problem resolves cleanly: **volume is the engine, quality is the product,
executive search is background experience rather than a line of business.**

## 13. Tushar — DroneCo (2023)

**Upgraded 29 Aug on seeing the deck.** This is not "a fixed-wing aircraft prototype," and it
is not a 3D-printing hobby either. It is a designed product with a business model and a market
thesis.

- **Ultra-fast autonomous delivery drones**, drone-as-a-service, with a named partner model.
- Two SKUs — **X** (lightweight, <2 lb payload) and **Y** (standard, <6 lb).
- Engineered spec: fixed-wing foam airframe, 8 lift propellers plus 2 front-facing, downward and
  navigation sensors, **tether-based release system**; 100 km/h cruise, 30 km on a single charge,
  1 hour flight time, 400 ft operational ceiling, 5 ft wingspan, 12 m LiDAR range, 2 camera
  sensors.
- A designed consumer flow: order → live track with ETA → *"we gently lower your package at your
  doorstep."*
- A real market thesis: India's post-2021 drone liberalisation, Swiggy/Instamart delivery trials,
  Tata 1mg medical delivery in Dehradun.
- **Sole founder.** He did the designing, the CAD, the Raspberry Pi work, taught himself
  aerodynamics from a book, wrote the grants — and **recruited master's students onto the team**
  as an undergraduate, which is the earliest instance of him recruiting for something he was
  building. Team of four at peak, multiple advisors, ~**$10K in grant funding.** **ABC Innovation
  Prize** winner and **Blackstone Launchpad finalist.**
- **They assembled the plane. It never flew.**

His own read: *"I was not a hardware specialist. I always had a passion for drones, but I quickly
learnt I was better off nerding about them than building them."*

⚠️ **Handle that line carefully.** It is honest and it is the right failure answer — but Tushar
is the non-technical founder, so "better off nerding than building" can be misread as a general
statement about himself rather than a specific one about hardware. **Land it as the lesson it
actually is:** he learned to choose problems where his own edge is real. At DroneCo his edge was
nothing; at Foray his edge is the network, the sourcing muscle and having run hiring from the
employer's chair, and Sathya is the builder. DroneCo is not a story about failing to build — it
is the story of learning what he is for.

**How to use it.** The deck itself is the artifact — a16z asks for things you made that exist,
and a product spec with a consumer flow and a go-to-market is exactly that. Attach it or host it.
And it is still the **Z-Fellows risk-and-failure answer**, now a much better one: he didn't fail
for lack of ambition or rigour — the product was properly specified — he failed on
founder–market fit and diagnosed it himself. That is the precise lesson that makes the current
company credible.

## 14. Tushar — the pattern underneath the resume

Volunteered 29 Aug, and it is the through-line the whole application was missing:

- **Eight schools in sixteen years**, moving constantly as a child. This is the origin of
  everything below it: someone who was the new kid eight times learns to manufacture access from
  nothing, which is precisely what the offer ladder, the sourcing record and the conference hack
  all are. **It turns a list of hustle anecdotes into a person**, and it belongs early in the
  a16z and Z-Fellows answers.
- **First software at 14 or 15: "Traqde In"** — a marketplace for listing and buying second-hand
  phones. It made real exchanges. Worth one clause of notice: **at fourteen he built a two-sided
  marketplace matching supply and demand for something people can't easily find or value.** That
  is the same shape as the company he is building now, and it is a better answer to a16z's "when
  did you start building software on your own volition" than anything else in his history.
- Loved number crunching as a kid; studied mathematics at USC alongside economics and **finished
  in three years.**
- **Sat and ACT ten times, improving by more than 450 points.** Ten sittings.
- **Laddered into banking:** won a Moelis offer, used it to get Evercore, used that to get
  J.P. Morgan. Youngest full-time analyst there. `GAP: he says 20 — the YC form gives DOB
  31 Jan 2002 and a June 2023 start, which is 21. Pin this down; it is exactly the kind of
  detail that gets checked.`
- **Talked his way into a technical Chief of Staff seat at Nova Intelligence as employee one**,
  then built GTM, hiring and sales from nothing.
- Sourced 21 companies, sourced speakers, built lab relationships, ~20 investor relationships.
- At SAP Sapphire, couldn't afford a booth, so built a scheduling app for attendees instead.

**The pattern: he treats systems everyone accepts as fixed as things you can simply run more
iterations against.** A standardised test is not a one-shot event, it is ten. A banking offer is
not an outcome, it is leverage for the next one. A conference is not booth space, it is
attention. That is one trait, stated five ways, and it is directly load-bearing for this company
— **recruiting is the largest system everybody accepts as irreducibly slow.**

Use it as the spine of B6, B7 and B9, and echo one clause of it in A5. Sathya's equivalent
through-line is navigation without instruments; Tushar's is more iterations than the system
expects. Two coherent founders beat two lists of accomplishments.

**Founder–market fit nobody has noticed yet:** the Moelis → Evercore → J.P. Morgan ladder is him
**hacking the recruiting funnel from the candidate side**, years before deciding to rebuild it.
Paired with the J.P. Morgan campus and lateral recruiting remit — running the same funnel from
the employer side — that is a genuinely unusual pair of vantage points. It belongs in A4 and A5.

**What drives him, in his words:** hunger to learn a great deal in a short time, by being in
places where people his age traditionally aren't, in order to have broad impact.

**Nerdiest thing:** he operates a computer without touching a mouse, and can build most financial
models on keyboard shortcuts alone. Small, true, specific — and quietly on-pattern: removing
friction to raise throughput is the same instinct as sitting the SAT ten times.

