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
**Every one of those referrals has converted to an interview.** `GAP: how many? A denominator
turns this from a nice line into the best conversion stat in the pack.`

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
season-scoring records. Built the **first four-wheel odometry Self-funded the team through Gene Haas and Siemens Foundation grants, awarded to fewer than
50 teams nationally.

**Together — WestBridge:**

- **Both ranked top of their level** — Tushar the top-ranked and youngest associate, Sathya the
  top-ranked analyst and **the first hire out of college for the WestBridge office.** A clean,
  symmetric line worth using verbatim.
- **The anti-portfolio — companies evaluated at WestBridge, and where they ended up.**
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

- ~~"Sourced" vs "evaluated"~~ **Resolved 29 Aug: all 21 were sourced by Tushar.** Use the
  origination framing. Still open: whether **$2.5B** is offers received or deals closed, and
  across which subset — that number stays out until it's pinned to something checkable.

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
- **Ansh Gupta** (Dartmouth) — *background pending.*
- **Niki Nguyen and Vivian Nguyen** — recruiters at Conviction. Tushar was the first hire at
  a Conviction-backed startup and both were instrumental in the hires that followed.
- **Abhay Singhal** — MTS at Factory AI; agent performance and evals background.

## 8. Honest gaps — stated, never hidden

- **Revenue: $0.** Everything delivered so far has been free — pilots and POCs, several run to
  earn the business.
- **Neither founder is full-time.** Both are still employed at WestBridge and working on this
  around the job. They intend to go full-time. **Every one of the five forms asks about
  exclusive commitment, and HF0 is a residency**, so this gets stated plainly with the trigger
  for going full-time attached, rather than left for someone to discover.
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

## 13. Tushar — the drone company, as actually told

College venture project, four people including **master's students he recruited onto the team**
(worth noting: the earliest instance of him recruiting for something he was building).
3D-printed drones, prototyped in CAD, multiple advisors. **~$10K in grant funding.** Won the
**ABC Innovation Prize** and was a **Blackstone Launchpad finalist.** They assembled the plane;
**it never flew.**

His own read: *"I think it wasn't the best founder–market fit. I realised that as I built it."*

**How to use it.** Not as "I founded a company" — four people, $10K and a plane that didn't fly
is a serious college project, and calling it more invites a correction. Use it where it is
genuinely strong: **this is the Z-Fellows risk-and-failure answer**, and a good one, because it
is specific, it failed, and the lesson is exactly the one that makes the current company
credible — he has now chosen a problem where the founder–market fit is real, and he knows the
difference because he got it wrong once. Programs reward that far more than a clean win.

`GAP: name of the project, year, and any photos or CAD renders — a link would still help.`
