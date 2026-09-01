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

### ✅ Answered 1 Sep — the 2021–22 season, and it closes the attribution problem

In his own words: captain **and primary programmer**; maintained most of the codebase; also
carried robot build, sponsor communication, outreach and the engineering notebook. Technical
focus was **autonomous reliability** — hardened the four-wheel odometry localizer, started
building **vision** into the robot, improved **PID tuning**, and pushed for a more customised
mechanical base *because the software was only as good as the hardware it controlled*: a rigid,
intentional base made the odometry usable in practice. The season's field had a large central
structure that was hard to navigate, so precise autonomous movement was the binding constraint.
His summary: *"I was not just writing code for the robot; I was working through the relationship
between design, localization, controls, and match strategy until the system was dependable enough
to use."*

**This is the answer.** It's specific, it's his own season, it's technical, and the last sentence
is the best thing in it — systems thinking across four layers rather than a component claim.

> ⚠️ **One word to cut: "still."** He wrote *"I was captain and still the primary programmer."*
> "Still" imports a claim about **2020–21**, and the public 243-page notebook lists him as a
> mechanic in grade 11 with Eashan Vytla as lead programmer. Write **"I was captain and the
> team's primary programmer"** — true of the season being described, and it doesn't reach back
> into the year a public document contradicts. Everything else stands as written.

> ⚠️ **Also soften the propagation claim.** *"After our core team graduated, the approach became
> more common"* is unverifiable and doesn't need to carry weight. The honest and better version
> is already in his answer: *"it was not universally better — it cost space, design time and
> iteration — but it was better for the robot and the game we were building for."* **That
> sentence is worth more than the adoption claim**, because engineering judgement under tradeoffs
> is exactly what these reviewers are reading for, and almost no applicant volunteers the cost.

### ✅ The localisation context — answered 1 Sep, use it as one clause

- **What everyone else ran:** the standard three-wheel dead-wheel setup — two parallel tracking
  wheels, one perpendicular. Simpler to package, easier to reason about, and it drifted.
- **Why four wheels wasn't the default:** it makes the whole system less forgiving — wheel
  geometry, vector math, calibration, mounting rigidity and code all have to be right at once.
  The prevailing intuition was that *if one tracking wheel can introduce drift, more sensors just
  give you more bad data.*
- **The insight:** with a rigid base and careful calibration, the extra measurements stop being
  noise and start being redundancy.

**That third bullet is the whole answer to "are you technical?"** — it is a correct, non-obvious
claim about sensor fusion, arrived at by a high schooler, with the failure mode named first. One
sentence of it belongs in the Soma n-of-1 answer.

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

✅ **Answered 1 Sep: none.** Answer the field as "None" and move on — it costs nothing.

## B6 — When you started building unprompted, and what you were doing growing up

### ✅ First software — answered 1 Sep

**Age twelve, through robotics.** A small robot/arm setup; he wrote the control logic to make it
move in the directions and sequences he wanted. His words: *"code was not abstract. I could write
something, upload it, and watch a physical object do something different because of it."*

**Draft (a16z, ~50 words):**

> Twelve, through robotics. We'd built a small arm and I wrote the control logic to make it move
> the way we wanted. What hooked me was that code wasn't abstract — I could upload something and
> watch a physical object behave differently because of it. Everything I've built since has been
> some version of that.

**Note the pairing.** Tushar's first build at 14 was a *marketplace* — matching supply and demand.
Sathya's at 12 was a *controller* — making a physical system do what he intended. **Those are the
two halves of this company and they showed up a decade early in each founder.** Worth one clause
somewhere; not worth stating outright, a reader would rather find it.

### ✅ Growing up — answered 1 Sep

Parents moved farther north of the city for a better school district, adding 40–60 minutes of
commuting to *their* day. Little of the usual after-school structure, so he *"learned to be hacky
in almost every sense of the word"* — videos, old parts, half-working projects, whatever he could
get hold of. *"I learned to make things work before I had the vocabulary to explain why they
worked."*

**That last line is the keeper** — it is a specific, unusual, and slightly unflattering
description of how someone learns, which is exactly what makes it credible.

> ⚠️ **This is the weakest answer in either founder's set, and it's worth knowing why.** The
> content is honest and the sentences are good, but the *fact pattern* is common: a family
> sacrificed for schooling and a kid taught himself from the internet. Thousands of applicants
> have that. Tushar's equivalent — **eight schools in sixteen years** — is a fact nobody else in
> the pile has, and it does the same narrative work.
>
> **The fix isn't a better paragraph, it's a harder fact.** The genuinely non-standard thing in
> Sathya's history is already documented and sitting in B4: **at sixteen he cold-called twenty
> companies, closed four sponsorships, and ran a national $25,000 grant application end to end
> for a robotics team with no school funding, while families covered $10,000 a year.** That is
> not standard by any measure. **Lead the upbringing answer with that and use the "hacky" framing
> as the explanation for it** — the commute and the missing structure become *why* a sixteen-year
> old was cold-calling Misumi, instead of being the whole answer.

## B7 — What drives you

### ✅ Answered 1 Sep

His words: *"I have always wanted to get into rooms I was not supposed to be in and become useful
once I got there."* Plus: build things that matter, leave systems better than he found them;
technical growth matters because it expands how much he can contribute; *"it takes a village to
raise someone"* — he's had people make room for him and feels a responsibility to do the same,
*"without keeping score."*

**Draft (250 characters — the Z-Fellows cap):**

> Getting into rooms I wasn't supposed to be in, and being useful once I'm there. People made
> room for me before I'd earned it. Technical growth is how I pay that forward — it's the only
> thing that expands how much I can actually contribute.

*(248 characters.)*

> 🔴 **Collision to fix before submitting.** Tushar's answer to this same question is *"hunger to
> learn a great deal in a short time, by being in places where people my age traditionally
> aren't."* Sathya's is *"get into rooms I was not supposed to be in."* **These are the same
> sentence.** On a form where both founders answer — a16z and Z-Fellows both do — a reviewer
> reads two people with one personality, or worse, one person filling in two boxes.
>
> **They diverge naturally if you take each to its actual end.** Tushar's driver is *access* —
> the room is the goal, and he manufactures entry (ten SATs, the offer ladder, the conference
> hack). Sathya's is *usefulness* — the room is the beginning, and the second half of his answer
> is the real one: **he wants to be the person who makes room for someone else, without keeping
> score.** That is a materially different motive, it's the more unusual of the two, and it's
> already in his raw answer. **Cut "rooms I wasn't supposed to be in" from Sathya's version and
> give the whole thing to the village line.** Tushar keeps access; Sathya takes obligation.
>
> **Alternate draft on that axis (243 characters):**
>
> > It takes a village. People made room for me before I'd done anything to deserve it, and I've
> > never been able to treat that as free. Technical growth is how I make room for the next
> > person — it's the only thing that scales how much I can give back.

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

🔴 **Still open, and it is now the largest hole in Sathya's set.** The 1 Sep batch answered
everything else and skipped this one. Z-Fellows asks explicitly whether you failed or succeeded,
so **a genuine failure reads better than a managed one** — and Tushar has DroneCo, a real
failure with a diagnosed cause, which makes an absence on Sathya's side conspicuous.

Candidates, in order of how well they'd work:
- **The four-wheel odometry bet itself.** He now has the honest version on record — it cost space,
  design time and iteration, and was *not* universally better. **What went wrong while getting
  there?** A season, a match, a competition lost to drift or a miscalibration? A technical bet
  that cost something concrete before it paid is the best possible answer here.
- Taking the captaincy after a World Championship — inheriting a peak and having to beat it.
- Self-funding: chasing national grants as a high schooler, and the ones that were lost. Sixteen
  prospects that said no, out of twenty.
- Leaving the hedge-fund track.

## B10–B12

See `shared.md`.

## Links — the state of play after 1 Sep

| Item | Status |
|---|---|
| **FIRST robotics repo** | 🔴 *"The repository is public: [insert GitHub URL]"* — still a bracket. One paste. |
| **Finance tooling portfolio** | 🔴 *"available here: [insert portfolio URL]"* — still a bracket. |
| `ForayInstinct` | ✅ `github.com/panchusathya/ForayInstinct` — link this first |
| AI-native procurement app | 🟡 URL still needed |
| `VC_Portcos` | ⚠️ confirmed private — do not link, do not mention |

## The VC Twitter follower bot — 🔴 still unanswered, and it is the most expensive gap in the pack

The 1 Sep response to this question came back as **a template with every substantive field left
as a bracket**: `[fund/company]`, `[the inputs it watched]`, `[the exact criteria]`, `[the users]`,
`[the workflow]`, `[what it replaced]`, `[audience and purpose]`. Nothing was answered.

**Why this one matters more than the others.** With the simulator withdrawn and the localizer
team-attributed, this is **the strongest verifiable technical artifact Sathya has as an adult** —
production sourcing infrastructure, running inside an $8B fund, built in his first year out of
college. And it is *directly on-thesis*: a sensor watching a market for signal before it is
announced is precisely what Foray claims to be, pointed at a different graph. **It is the single
best answer available to Soma's technical n-of-1 question and to a16z's "what have you built"** —
and right now it cannot be written at all.

Six specifics needed, one line each:
1. **What does it watch?** Follower deltas on specific VC accounts? Who follows whom? New account
   creation? Founder activity? Keyword/topic pairs? Engagement velocity?
2. **How does it decide something matters?** The actual rule or threshold. *"When three partner
   accounts follow the same new handle inside a week"* is a real answer; *"early signals"* is not.
3. **Who uses it, and how often?**
4. **What did it replace?** Manual browsing, a spreadsheet, ad hoc alerts, nothing?
5. **Has it produced a deal, an outreach, or a meeting?** One instance is worth the whole answer.
6. **The outreach decks** — audience, what's generated, what's human.

`Also still open: how much of ForayInstinct's 247 commits are his?`

## B13 — One-minute video

~150 words from B4 + A2 + B6 once those land.
