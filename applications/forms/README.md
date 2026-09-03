# The five forms — review index

All five are drafted end to end. Nothing below is a placeholder answer; the only gaps are links,
one referrer field, and three facts to pin.

| Form | File | State |
|---|---|---|
| a16z speedrun | `a16z.md` | Complete. 6 links to paste. |
| Soma Fellows | `soma.md` | Complete except **Q4 (how did you hear about us) — blocking.** |
| Greylock Edge | `greylock.md` | Complete. 4 links to paste. |
| HF0 | `hf0.md` | Complete. Confirm the cohort start date. |
| Z-Fellows | `z-fellows.md` | Complete, **all 15 answers within their character caps.** |

`python applications/check_lengths.py` → PASS, 15/15.

---

## Updated 3 Sep — what changed

All five forms now carry: **the two signed clients** (Echelon, FlowGen Labs — four roles at 20% of
base, ~$232K contingent, and the Echelon founder's angel offer), **the warm-vs-cold acquisition
result** (cold zero, warm two of two — the distribution thesis tested on themselves), **the browser
agent live against Greenhouse, Ashby and Workday** with the adapters-vs-browser-agents architectural
bet stated as a bet, **~70% open rate** on the Baseten sequence, and **six weeks → seven weeks**
throughout. The waitlist is now "live since late August" rather than a specific date — see the
conflict flagged below.

## Blocking before submission

0. 🔴 **Get both client engagements in writing.** Worth more than any edit in this directory.
   "Agreed to work with us" is not an artifact; "signed at 20% with a defined trigger" is.
1. 🟡 **Waitlist date conflict.** The 29 Aug note says the site and waitlist went live that day; on
   1 Sep it was described as "opened last night." All five forms now say "live since late August" —
   safe, but check and be exact, because it's trivial to verify and exactly what gets asked.
2. 🟡 **Baseten send count:** the demo said 162 in one telling and ~150 in another. Forms say
   "~160". Pin it.
3. 🟡 **Name the agent framework precisely.** The daily note transcribes as "E-Framework" and "BLM
   DOM-based," which won't survive a technical reader. Not currently in any form; keep it out until
   the name is right.

1. 🔴 **Soma Q4 — the referrer field is empty.** One Slack message to three WestBridge partners.
   This is the highest-leverage empty box in the pack and it gates the Soma form entirely.
2. 🔴 **Links.** Sathya: robotics repo, finance tooling portfolio. Tushar: DroneCo deck, Walmart
   Loom, SAP Sapphire app, AI-native procurement app. Plus **a hosted URL for the demo recording** —
   four of five forms have an artifact field and the SPC application left it blank.
3. 🔴 **HF0 cohort start date.** If it starts on or after 1 Jan 2027, the answers stand as written.
   If earlier, Q3's closing sentence has to say acceptance moves the resignation date forward.
4. 🟡 **Tushar's age at J.P. Morgan.** The forms say "youngest investor on the team" rather than
   naming an age, so nothing here is wrong — but the discrepancy (he says 20; YC form gives DOB
   31 Jan 2002 and a Jun 2023 start, which is 21) should be pinned before any interview.
5. 🟡 **The 840 and 85 definitions.** Is 840 all-time or post-Zoho? Is 85 "replied and qualified"
   or "qualified out of a larger reply set"? The second reading is a better number and a different
   sentence. Every form currently states it the conservative way.
6. 🟡 **Sathya's Z92** — drafted from the documented sponsorship campaign because the
   risk-and-failure question came back blank. Submittable as written; his to own or replace.

## Attribution rules held throughout — check these survive any edit

- **Kyber is a referral, never a placement.** Appears as such in every form.
- **Sathya's robotics:** "on a team that built the first four-wheel odometry localizer" + "I
  captained the following season, and in that season I was the team's primary programmer." The
  MyBot Simulator is not claimed anywhere. The word "still" (as in "still the primary programmer")
  is out — it reaches into the season the public notebook attributes to someone else.
- **No 10,000-user simulator claim. No $2.5B aggregate.** Grepped clean.
- **OpenAI:** the Head of Applied AI Research was **sourced as a speaker**, not recruited. Not
  claimed as a hire anywhere.
- **Portfolio-company C-suite searches** appear only as client-side experience in Greylock Q2 and
  a16z Q6 — never as Foray mandates, revenue or pipeline.
- **Browser agent:** now "submissions landing against Greenhouse, Ashby and Workday" — upgraded
  3 Sep from "working in testing." `Confirm before submitting: were any of those on behalf of a
  real candidate for a real role? Both versions are good; they are different sentences.`
- **The two clients:** always *contingent fees on agreed engagements* — never revenue, never
  booked, never ARR. Every form states that no cash has been collected, that nothing closes until
  a hire, and that terms are agreed rather than papered.
- **Echelon and FlowGen Labs are the only clients.** LangChain, Perplexity, Physical Intelligence,
  Clay, LiteLLM, CrustData and Whop are outbound targets and resume books; the original eight
  received free candidates. Three different categories — never merged into one list.
- **Ansh Gupta** is named as someone impressive and someone we'd recruit. Nowhere does any form
  assert he is leaving OpenAI.
- **Dwayne** "has expressed interest in joining." The word co-founder is attached to nobody
  unsigned.
- **Outbound numbers:** 10.1% aggregate is the headline everywhere; 20% is always labelled as the
  best-targeted sequence. Never 20% alone.
- **No Strata.** Grepped clean.

## Deliberate choices you may want to overrule

- **The intent-graph positioning leads all five**, with the firm as the demand seed and revenue
  line — not "an AI-native recruiting firm." `block-a.md` still holds the older firm-first framing
  if you'd rather go back to it, but it reads as an agency with tooling, which is what made
  recruiting look like a boring market in the first place.
- **Shipped vs roadmap is separated explicitly in every form.** The intent object as a formal
  schema, decay scoring, the aggregate product, the read API and "Apply with Foray" are all named
  as roadmap. Blurring the two is what the SPC application did.
- **$0 revenue is stated as a decision with its reasoning**, not omitted and not apologised for.
- **Tushar's Soma n-of-1 answer opens by saying he is not the technical founder.** Deliberate — the
  question is technical, Sathya carries it, and volunteering the division reads as confident rather
  than evasive.
- **The a16z-only line** — Ornn, Merendil and Tessera were sourced before a16z funded them — appears
  in `a16z.md` Q6 and nowhere else.
