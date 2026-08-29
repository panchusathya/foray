# Positioning — the intent graph

Supersedes the earlier "infra thesis, firm engine" framing, which was correct and boring. The
five candidate positions considered are kept at the bottom.

---

## The redirect that fixed it

*"Recruiting is a boring market to begin with. What's more interesting is we're owning
proprietary distribution with the consumer play, and then we have the ability to funnel
proprietary candidates. It's closer to LinkedIn 2.0."*

That's right, and it's the 8/23 thesis from the original notes. Everything since has been an
argument about which recruiting company to be. **The interesting company isn't a recruiting
company at all.**

LinkedIn isn't a recruiting company either. It's a network that monetises through recruiting, at
roughly $15–16B a year, with one incumbent everybody hates. That is not a boring market. It's a
market with a structural hole in it.

---

## The structural hole

**On LinkedIn you cannot express interest without leaking it.**

Update your profile, flip on "open to work", even browse a competitor — your boss, your reports
and three recruiters you never wanted to hear from all find out. So the people worth $30K a
placement stay silent, and the entire industry fights over the small, visible, mostly junior
slice willing to be seen looking.

The consequence nobody names: **LinkedIn has a profile graph — where you have been. Nobody has an
intent graph — where you would go, and under what conditions.**

Profile data is a commodity. **You proved that yourself: you bought 200K profiles from Bright
Data with a credit card.** That is LinkedIn's supposed moat, purchasable, on a Tuesday. Intent
data cannot be bought, cannot be scraped, and cannot be inferred — because it is never published
anywhere. It exists only where someone chose to tell you privately.

**LinkedIn knows where you've worked. We know where you'd go.**

---

## The company

**An intent graph for careers** — the private, standing record of what people would actually move
for, built through conversation rather than a profile page.

You text or call a number. You say what would move you: comp floor, stage, the two companies
you'd never join, "only if it's a founding role", "only if it's within a year of a Series A."
Nothing is published. Nobody is told. It sits dormant — **that is the point, not a flaw** — and
you get pinged only when something clears your bar, with comp intel attached.

Four things fall out of that, and they're the four things a consumer supply play normally can't
get:

**1. A reason for passive, senior people to be in the system.** Active job seekers are cheap and
near-worthless; employed senior people are expensive and the only supply anyone pays for. The
private brief is the first product that gives them optionality at zero leak risk. Everything else
in the market requires them to be visibly looking.

**2. Retention that isn't tied to job-seeking.** Every consumer job product has the worst
retention curve in consumer, because success means the user leaves. **A standing brief is
valuable precisely when you are not looking.** It sits there for three years and is worth more
each year, not less.

**3. Supply that is proprietary by construction.** Not "proprietary" as in we scraped it first.
Proprietary as in **it is structurally unscrapeable** — a competitor starting tomorrow cannot
acquire it at any price, because it never leaves the private channel it was given in.

**4. Why now, with a real answer.** An intent graph requires a real conversation with a million
people. That was impossible until about two years ago. LinkedIn is a feed company and cannot
retrofit it; a voice and text agent on a phone number is exactly the shape that can.

---

## The funnel, which is the actual business model

**Acquire free → retain private → monetise demand.**

- **Acquisition: free applications, and showing up at the rejection.** Applications are free
  forever — that's a weapon against every mass-apply company, not a concession. And the
  highest-intent moment in a job seeker's life is the rejection email, where nobody is standing.
  You already shipped `connectors/google.py`: candidate connects Gmail once, and minutes after a
  rejection lands, four roles at the same stage and band. Near-perfect relevance, zero cold
  outreach — which is the cold-start problem you've been stuck on for two weeks.
- **Retention: the standing brief.** Free applications get them in the door. The brief is why
  they're still there in three years when they're senior and employed and worth something.
- **Monetisation: the demand side pays.** Companies pay for access to intent that exists nowhere
  else — not resumes, not "open to work" flags, but *this person would move for this, right now,
  and nobody else knows it.*

**And the firm is how you seed demand while supply accumulates.** Not a compromise, not a
fallback: a two-sided network needs one side warm on day one, and running the searches yourself
is how you have companies and live roles to match the first briefs against. It also generates
the outcome data that makes matching better than anyone else's.

---

## Why this isn't the consumer graveyard

LazyApply, Simplify, Sonara and the rest all died the same three deaths. Name them and show the
design already avoids each — this is the paragraph a sharp investor is waiting for:

| How consumer job products die | Why this doesn't |
|---|---|
| Churn is structural — success means the user leaves | The brief is valuable when you're *not* looking. Retention isn't coupled to job-seeking. |
| Adverse selection — the people who show up are the ones companies don't want | Nothing is published, so the product is *only* attractive to people who can't afford to be seen looking. It selects for exactly the opposite population. |
| Willingness to pay is near zero, and the ones who'd pay are least employable | Candidates never pay. Charging them would re-introduce the adverse selection you just designed out. |

---

## The one-liner

> LinkedIn knows where you've worked. Nobody knows where you'd go — because on LinkedIn you can't
> say it without your boss finding out. We're building the intent graph: a private standing brief
> of what would actually move you, held in confidence, matched silently. Applications are free
> and always will be. Companies pay for access to intent that exists nowhere else, and we run the
> searches ourselves so there's demand on day one.

**The proof point that lands hardest, and it's yours:** *we bought 200,000 LinkedIn profiles from
Bright Data with a credit card. That's the incumbent's moat, for sale, on a Tuesday. You cannot
buy what someone told you privately.*

---

## The bear case, priced honestly

- **Cold start on the supply side is brutal**, and you have been stuck on it for two weeks. The
  rejection trigger is the best answer available and it's days of work on connectors you already
  shipped. If that doesn't move, the thesis is in trouble and you'll know fast.
- **Two-sided marketplaces are the hardest thing to build.** True. It's also why it's worth a lot
  if it works, and why the firm funds the attempt rather than the attempt funding itself.
- **Density before leverage.** Intent data is worth nothing at 1,000 briefs and enormous at
  100,000. You need to pick a segment narrow enough to reach density — engineering is the right
  one, which is what you said.
- **Nobody is paying for "intent" today.** True, and it's why the firm is the revenue line while
  the graph accumulates. Say that plainly; it reads as clarity, not hedging.

---

## What this changes in the pack

- **A1** — the problem is no longer "hiring is slow." It's *the people worth hiring can't say
  they're open.*
- **A2** — an intent graph, with the firm as the demand seed. Voice, text and the CRM stop being
  a mid-funnel tool and become **the collection mechanism for intent.**
- **A6** — competitors are LinkedIn and the mass-apply graveyard, not Juicebox and Gem.
- **A7** — the 12-week plan becomes: prove the rejection trigger works, get N briefs, and show
  that a brief converts to a match at a rate a resume never does.
- **Sathya's technical weight goes up.** An intent graph is a data and systems company.

---

## Appendix — the five positions considered

1. **The firm** — matches traction exactly; services multiple; reads as an agency with tooling.
2. **System of record for hiring outcomes** — genuine insight, uncopyable seat, but the data
   doesn't exist yet and the category takes years.
3. **Execution rails** — infra-shaped and partly built, but the worst GTM answer of the five:
   ATSs won't buy it and agencies won't pay for seats.
4. **Mid-funnel agent** — exactly what's built, clear buyer, but it's a feature an ATS could ship.
5. **Verification and outcome layer** — timely and true, and it survives inside the intent-graph
   thesis as the *mechanism* rather than the pitch.

The intent graph subsumes the useful parts of 2 and 5 and gives them a market big enough to be
worth the risk.

---

## The blurb — 29 Aug, after the intent-capture sharpening

The crux is a **data problem, not a speed problem.** Nobody has an accurate view of who is
actually in the market, so matching fails and everyone compensates with volume. And the reframe
that unifies the existing stack: **every surface already built is an intent sensor** — outbound
(a reply is a signal), inbound, the messaging agent, and the applications sent on a candidate's
behalf, which are the purest revealed intent there is and which we originate.

**Why a profile graph can never become an intent graph** — this is the structural argument, and
it's a data-model one rather than a product one. Intent is:
- **private** — say it on LinkedIn and your boss finds out
- **conditional** — "I'd move for a founding role, above $X, not into an enterprise sales org"
- **perishable** — someone open in March isn't in June

None of those are profile fields. LinkedIn's data model cannot represent them at all.

### 50 characters
> The intent graph for hiring.

### One sentence
> Hiring is a matching problem gated by a missing dataset — nobody knows who is actually in the
> market — so we capture that intent and monetise it.

### Short blurb (~90 words)
> Hiring looks like a speed problem. It's a data problem. Nobody has an accurate, current view of
> who's actually in the market, so matching fails and everyone compensates with volume — more
> outreach, more applications, more noise.
>
> LinkedIn is a profile graph. It records where you've been. It can't hold intent, because intent
> is private, conditional and perishable — none of which are profile fields.
>
> We're building the intent graph. Every surface we run is an intent sensor: outbound, inbound,
> the messaging agent, and the applications we send on a candidate's behalf. We capture who's in
> the market and on what terms, and monetise the matching that only becomes possible once you
> actually know.

### Longer blurb (~140 words) — for forms with room
> Hiring looks like a speed problem — months to close, agencies churning, candidates applying
> into the void. It's a data problem underneath. Nobody has an accurate, current view of who is
> actually in the market, so matching fails, and every player compensates with volume: more
> outreach, more applications, more noise. The noise is the symptom.
>
> LinkedIn is a profile graph. It records where you've been, and it's a commodity — we bought
> 200,000 profiles from a data broker on a credit card. What nobody has is an intent graph: who
> would move, on what terms, and when. Intent is private, conditional and perishable, so a
> profile page structurally cannot hold it.
>
> Every surface we run is an intent sensor — outbound, inbound, our messaging agent, and the
> applications we submit on a candidate's behalf. We capture intent, and we monetise the matching
> that only becomes possible once you know.

---

## Taking it to the next level — the infra layer

The blurb is right and it stops one level too early. It describes a *dataset*. Infrastructure is
a dataset with an **interface**, that other systems **call**, and that they eventually **depend
on**. Three levels:

### Level 1 — the graph *(where you are)*
Capture intent, match better than anyone can with profiles. A product.

### Level 2 — the intent layer *(the infra move)*
Three components, and you have the beginnings of all three:

**a) Capture — the sensors.** Voice, text, outbound, inbound, and the applications you submit on
someone's behalf. Already built.

**b) Represent — the intent object, and this is the part nobody has.** There is no standard
representation of career intent. LinkedIn's "open to work" is a **boolean** — the crudest
possible encoding of something that is actually conditional, private, timestamped and decaying:

> comp floor · stage · timing window · no-go list · what would have to be true · confidence ·
> **half-life**

**Freshness is the technical differentiator worth naming.** Intent is perishable — someone open
in March isn't in June — and nobody models decay. An intent record with a confidence score and a
half-life is real systems work, it's defensible, and it's the kind of primitive that sounds like
infrastructure because it is. *Segment didn't win by owning data; it won by owning the event
schema everyone standardised on.*

**c) Serve — and split this into two products, because it solves the density problem.**

| | Aggregate | Individual |
|---|---|---|
| What | *"At $200K for a founding engineer, 12 people who'd fit are open in the next six months. You're 15% below the bar."* | A consented introduction to a specific person |
| Names | None | Yes, with consent |
| Needs marketplace density | **No** | Yes |
| Sellable | **Today** | Later |

**The aggregate layer is the wedge**, and it directly answers the bear case: intent data is worth
nothing at 1,000 briefs *for matching* — but it's worth real money at 1,000 briefs as **market
truth**. A founder deciding a comp band will pay for that before any marketplace exists.

### Level 3 — the framing that makes the market interesting

**The labor market is the largest market on earth with no price discovery and no availability
discovery.** Equities have a tape. Housing has comps. Freight has spot rates. Labour has
Glassdoor and vibes — self-reported, stale, and silent on the only question that matters: *who
would actually move, and for what.*

That's not a recruiting pitch. It's a market-structure pitch, and it's why the intent graph is
worth building rather than merely useful.

> **We're building the tape for the labour market.**

---

## Two moves that make it infrastructure rather than a database

**1. "Apply with Foray" — the write path, and the best growth mechanic available.**
One button on any careers page: click, and a verified human arrives with structured intent
attached instead of a parsed PDF. For the company it kills the form and the slop. For the
candidate it's one click instead of forty fields. For you it's **an acquisition surface on
someone else's website** — which is how Stripe and Plaid actually grew. It turns every employer
you serve into a sensor, and it's the inverse of scraping: they pull you in.

**2. The agent-facing API — and this is your "why now."**
Hiring is moving to agents on both sides: candidates' agents applying, companies' agents
screening. **An agent cannot ask ten thousand people whether they're open. It has to call
something.** When both sides are generated, verified intent is the only signal left — the slop
problem *creates* the need for an authoritative source.

So: a read API (and an MCP server) over consented, decayed intent, that hiring agents call the
way they call a search index today. *You already think this way — the Strata pitch made exactly
this move with market data.*

---

## The upgraded blurb

> Hiring looks like a speed problem. It's a data problem. Nobody has an accurate, current view of
> who's actually in the market, so matching fails and everyone compensates with volume.
>
> LinkedIn is a profile graph. It records where you've been. It can't hold intent — intent is
> private, conditional and perishable, and none of those are profile fields. "Open to work" is a
> boolean.
>
> We're building the intent layer. Every surface we run is a sensor — outbound, inbound, the
> voice and messaging agents, and the applications we submit on a candidate's behalf. We capture
> who's in the market and on what terms, hold it as a decaying, consented record, and serve it
> two ways: aggregate market truth that any employer can query today, and consented matches once
> there's density. As hiring moves to agents on both sides, that's the thing they have to call.
>
> The labour market is the largest market on earth with no price or availability discovery. We're
> building the tape.

---

## Honesty guardrails on all of the above

Everything in Level 2 and 3 is **roadmap, not shipped**, and the pack's discipline holds: the
thesis can be ambitious as long as the traction section is exact. Say it as *"here's what we're
building toward and here's what runs today"* and it reads as vision. Blur the two and it reads as
the SPC application again.

What is genuinely real today: the sensors (voice, text, outbound, applications), the intake
schema, the reply classification, the funnel state machine, the 200K index and the eval harness.
What is not: the intent object as a schema, decay scoring, the aggregate product, the API, and
"Apply with Foray."

**The falsifiable near-term milestone**, worth putting in A7: *N standing briefs captured, and a
brief converts to a match at a rate a resume never does.* That is the whole thesis in one
measurable claim, and twelve weeks is enough to prove or kill it.
