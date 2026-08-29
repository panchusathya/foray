# Block B — Sathya Panchu

Harvested from the resume and the SPC application. `GAP` marks what still needs input.

> ## The pattern — Sathya builds instruments
>
> Tushar's through-line is *more iterations than the system expects.* Sathya's is different and
> just as consistent: **when he can't see something directly, he builds the thing that measures
> it.**
>
> - **Odometry** — measuring position with no external reference
> - **The simulator** — measuring robot behaviour without a robot
> - **The VC Twitter follower bot** — measuring what's moving in a market before it's announced
> - **The finance tooling** — instrumenting his own workflow
> - Even **GeoGuessr and naming every street** — locating yourself from partial signal
>
> **And Foray is an instrument.** The intent graph is a measuring device pointed at a market
> nobody can currently see. That is the "why this person for this company" answer, and it doesn't
> need to be stated — put the facts in order and a reader assembles it themselves.
>
> **The governing note for this whole file.** The resume is a *finance* resume. It leads with
> investment coverage and buries the BA in Computer Science, the FIRST odometry system, and the
> fact that production sourcing tooling was shipped inside a fund. For these five programs that
> ordering is exactly backwards. **Every answer below re-cuts Sathya as a builder who works in
> finance, not an analyst who once did robotics.**

---

## B1 — Career history

**Reverse-chron (a16z format):**

- **WestBridge Capital** — Investment Analyst, VC & Long-Only Equities, Jun 2025–present, San
  Mateo. $8B+ crossover fund, Sequoia Capital India spinoff. Covers Freshworks (NASDAQ: FRSH),
  Nexthop AI and Postman. Formed the firm's cloud-infrastructure thesis. Diligenced startups
  across the GTM stack, infrastructure (dev tools, CI/CD, cybersecurity) and AI apps. On the deal
  team for an unannounced AI-native IT services company (Distyl/Palantir comp). Represented the
  firm at **NeurIPS and ICML**. **Built internal tooling for the investment team to improve
  sourcing quality and efficiency — a VC Twitter follower bot and a custom AI-generated outreach
  deck system.**
- **WestBridge Capital** — Summer 2024 Investment Analyst, Jun–Aug 2024. Return offer received.
  Pitched a two-quarter long thesis on $SNOW arguing that bearish sentiment on Iceberg and
  storage cannibalisation was overstated and Cortex utilisation would drive upside to guidance.
  Staffed on 3 diligences, 2 sourced from his own thesis work, 1 from personal outreach. Built
  top-down and bottoms-up TAM, revenue and IRR/valuation models.
- **Formidable Asset Management** — Summer 2023 Investment Analyst, L/S Equities, May–Aug 2023,
  Cincinnati. $900M+ hedge fund. Return offer received. Equity research within the KONG and FORH
  ETFs leading to a strategic end-of-quarter rebalance — **replaced nine holdings** with
  securities matched to each fund's criteria.
- **Capital Investments at Berkeley** — student-run hedge fund, $100K+ AUM. **Chief Investment
  Officer**, previously Director of Education and External VP, Jan 2023–Jun 2025. Built and
  taught the new-analyst education program to 20+ students. Pitched CoPart ($CPRT) long on a
  total-loss-rate reversion thesis.
- **YDelta** — **Co-Founder** and Financial Editor, Jan 2021–Aug 2023. Weekly investment and
  finance editorial covering markets, public and private equities, earnings and economic news.
  *(Founded at 19, ran for two and a half years.)*
- **UC Berkeley** — **BS Business Administration (Honors), Haas School of Business**, *and* **BA
  Computer Science, College of Computing, Data Science & Society**. Class of 2025.

**Emphasis for a16z and Greylock:** lead with the CS degree and the shipped sourcing tooling, not
the coverage list. The one-line framing that works: *built the sourcing infrastructure an $8B
fund actually runs on, while holding a CS degree and a robotics-software track record.*

## B2 — Self-directed builds

- **Finance tooling portfolio** — multiple tools built at WestBridge to streamline investment
  workstreams. The VC Twitter follower bot and the AI-generated outreach decks are the load
  bearing two, because **they are literally sourcing infrastructure — the same muscle Foray
  runs on, already shipped and in production use by a fund.** `GAP: the portfolio link.`
- **AI-native procurement app** — built with Tushar earlier this year. Came from firsthand
  family experience in manufacturing, where teams spent entire days logging simple tasks into a
  paper book. Their first AI-native build together. `GAP: URL.`
- **FIRST robotics — the repos are public.** Both the odometry stack and the **simulator used by
  10,000 FTC teams.** `GAP: the URLs. This is the single most valuable link in either founder's
  application and it is one paste away.`
- **`github.com/panchusathya/ForayInstinct`** — **link this one first.** Public, MIT, 247
  commits: a self-hosted iMessage assistant with an encrypted credential vault, where secrets
  are encrypted before storage so the model never sees them. Next.js, Postgres/Neon, Drizzle
  migrations, Turbo monorepo, Vitest, Bright Data browser API. Best "things you made exist"
  artifact either founder has.
- **Foray** — the sourcing engine, eval harness and agent stack.

**Do not link:**
- **`VC_Portcos`** — see `evidence.md` §9. Make it private today.
- **`rottenpotatoes`, `chips7.7`, `chips8.5`** — Berkeley CS169 coursework. RottenPotatoes is
  the standard assignment from Armando Fox's ESaaS course; an a16z reviewer recognises it
  instantly, and listing it as a side project reads as not knowing the difference.

## B3 — Awards and competitions

**Investment competitions:**
- **Dodge & Cox FA23 Investment Case Competition — 1st place**
- MSD Partners FA22 Investment Case Competition — 2nd place
- Goldman Sachs SP24 Tech IB Case Competition — Finalist
- Undergraduate Investment Competition (UIC) — Finalist

**FIRST robotics:**
- **World Championship** — one before taking over as captain, one as captain
- **Inspire Award winners, Virginia 2020 and Ohio 2022** — the most prestigious honour a team
  can win in FIRST
- **Johns Hopkins Applied Physics Laboratory Software Innovation Award**, Maryland Tech
  Invitational
- Multiple state records for most points in a season, 2021 and 2022
- Team self-funded through grants including the **Gene Haas Foundation** and the **Siemens
  Foundation** — awarded to fewer than 50 teams nationally

**Academic:** BS Business Administration with Honors; coursework including Introduction to AI (A).

## B4 — Most impressive thing / technical n-of-1

**Confirmed 29 Aug: Sathya captained Team 8719 in 2021–22.** The engineering notebook on file
covers 2020–21 only (where he is listed as a mechanic, grade 11), so the captaincy is
uncontradicted and goes in as stated. The Ohio Inspire Award 2022 and the 2022 state scoring
records fall in his captaincy year.

### The one attribution rule to hold

Role claims and artifact claims carry different risk. The captaincy is his to state. But the
**four-wheel odometry localizer, the Kalman/T265 fusion and the MyBot Simulator are credited to
Eashan Vytla by name in the team's own 243-page notebook**, which is public. So:

- ✅ **"On a team that built the first four-wheel odometry localizer in FIRST — I captained it the
  following season."** True, verifiable, and ~90% of the impact.
- ❌ *"I built the first four-wheel odometry system"* / *"I built the simulator."* Contradicted by
  a public document naming someone else.

`GAP — the one question that upgrades this: what did Sathya personally build or own in the
2021–22 season he captained? Extending the localizer, the auton, the vision stack, whatever it
was. A specific "I" from his own season beats a vague "we" from the season before, and it is
entirely his to claim.`

### Draft — captaincy + team record + the part that is unambiguously his

> I captained FTC 8719 "Quantum Leap" — an independent team, no school affiliation, no district
> budget, families covering $10,000 a year out of pocket. On the technical side we ran what the
> team believed was the first four-wheel dead-wheel odometry localizer in FIRST: four encoders
> fused through a Kalman filter, holding under 0.2 inches of drift over 30 seconds of driving
> across 10 of 10 trials, against 3–8 inches for the standard three-wheel setup. We were #1-ranked
> at every qualifier we entered, took the Inspire Award — FIRST's top honour — in Virginia and
> Ohio, and finished 19th in the world at Detroit.
>
> My own contribution was the part that made it affordable. At 16 I started cold-calling
> companies for parts. I carried the largest prospecting list on the team — 20 companies, more
> than our captain at the time — and closed four: Misumi gave us 30% off the linear slides for the
> shooter, Curbell and Interstate donated the Delrin the shooter was machined from, and I ran our
> application to REV's national $25,000 sponsorship program end to end, which meant consolidating
> a 400-page engineering notebook into a single submission.
>
> Most people who write about robotics write about the robot. **We didn't get to build that robot
> unless someone convinced strangers to pay for it, and that was me.**

**Why this is the right answer for these five, and for this company.** Cold-calling twenty
companies at sixteen and converting four is outbound sourcing — the exact muscle Foray runs on.
It is individually documented, it is checkable, and it is rare: every robotics applicant has the
robot story and almost nobody has the financing story.

### Technical n-of-1 — where the weight now sits

Soma wants technical rarity, and Tushar's YC form says he is not a technical founder. So the
technical claim rests on what is verifiably Sathya's:

- **Berkeley BA Computer Science** alongside the Haas honours degree
- **The VC Twitter follower bot and AI-generated outreach decks** — production sourcing tooling an
  $8B fund runs on, built in his first year out of college. *This is the strongest one and it is
  directly on-thesis: a sensor watching a market for signal before it is announced.*
- **`ForayInstinct`** — 247 commits, MIT, public; encrypt-before-storage credential vault
  `GAP: confirm how much of it is his`
- Whatever he personally owned in the 2021–22 season (see the gap above)

`Notebook data-integrity flags: award lists and dates conflict internally, and "world record 398
points" / "#1 in the world" are the team's own claims. The <0.2" drift figure has a full
trial-by-trial table behind it and is the safest number to quote.`

## B5 — Papers or preprints

`GAP — confirm. Likely none, which is fine.`

## B6 — When you started building unprompted, and what you were doing growing up

Partially implied by robotics, but not answered. `GAP — needed:`
- **a16z's version:** when did you start building *software* on your own volition, and why?
  Robotics is the obvious thread — but was software the entry point, or did it come through the
  hardware? The specific first thing, at a specific age.
- **Z-Fellows' version:** non-traditional things growing up. Building and self-funding a
  championship robotics program through national grants while in high school is already an
  answer; anything before or beside it makes it stronger.

## B7 — What drives you

`GAP — needed. ~40 words (250 characters). The SPC application ends on "the technical growth and
the bonds from those years are still my most fulfilling personal experience," which is a real
sentence and a plausible seed for this. Give me the raw version.`

## B8 — The nerdiest thing about you

**Answered:** he can name every street in his hometown, and refuses to use maps on purpose, just
to test himself.

**Draft:**

> I can name every street in my hometown, and I don't use maps on purpose — I'd rather navigate
> from memory and find out where my mental model breaks. Same reason I play GeoGuessr.

**Use the through-line.** A person who insists on knowing exactly where he is without instruments
went and built a robot localisation system. That is a real and slightly funny coherence between
B8 and B4, it is true, and it is the kind of detail a reader remembers a week later. Worth one
clause in the a16z answer — *"which is probably why I ended up building odometry"* — and worth
nothing if over-explained.

## B9 — A risk or challenge, and what it revealed

`GAP — needed.` Z-Fellows asks explicitly whether you failed or succeeded, so a genuine failure
reads better than a managed one. Candidates:
- Taking on the robotics captaincy after a World Championship win — inheriting a peak and having
  to beat it.
- Self-funding the team: pursuing national grants as a high schooler, and whatever was lost along
  the way.
- Turning down or leaving a hedge-fund track for this.

## B13 — One-minute video

~150 words from B4 + A2 + B6 once those land.
