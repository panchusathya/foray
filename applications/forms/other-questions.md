# Questions that appear on live forms but weren't in the original 29

Home for answers drafted after the fact. Same rules as the rest of the pack: nothing here that
isn't in `evidence.md`, same attribution guardrails, same compressed register.

---

# "A time you tackled a problem in a novel way and achieved something truly impressive"

> **This is not the most-impressive question, and answering it with that draft wastes it.** It asks
> about the *approach*. So the shape has to be: here was the conventional move, here is why it
> doesn't work, here is what I did instead, here is what it got. Both answers below are restructured
> that way. The existing drafts lead with the outcome, which answers a different question.

## Tushar — the conference we couldn't afford to attend

At Nova Intelligence we needed to be in front of Fortune 100 CIOs, and the one place they all are
in the same week is SAP Sapphire. We were pre-revenue. A booth was out of reach, and so was
outspending anyone on marketing.

The conventional move is to buy presence: booth, sponsorship, dinners. We couldn't, so I looked at
what was actually scarce at a conference, and it isn't floor space. It's attendee attention, and
specifically the fact that nobody can navigate hundreds of overlapping sessions across several
days. Every attendee arrives with the same unsolved problem, and the organiser doesn't solve it for
them.

So instead of buying our way onto the floor, I built a personalised scheduling app for Sapphire
attendees and gave it away. It took off during the conference, and it put us in front of exactly
the CIOs we had no other route to reach.

What I'd point at isn't the app, it's the substitution. We had no budget, so we spent the thing we
did have, which was the ability to build something useful in a week. That trade has repeated:
Walmart still ran procurement by hand, so a colleague and I built a working tool in under a week
rather than writing a deck about it, and it's now being showcased to their head of procurement.

## Sathya — four tracking wheels when everyone ran three

Our autonomous routines needed position accuracy the standard setup couldn't give us. In FIRST,
dead-wheel odometry is three tracking wheels: two parallel, one perpendicular. It's simpler to
package and easier to reason about, and it drifts three to eight inches over thirty seconds of
driving, which is the difference between an auton that scores and one that doesn't.

The obvious fix is more sensors. Nobody did it, and the reason is a good one: four wheels makes the
entire system less forgiving. Wheel geometry, vector math, calibration, mounting rigidity and code
all have to be right at once, and the prevailing intuition was that if a single tracking wheel can
introduce drift, adding another just gives you more bad data.

What we worked out is that the intuition holds only if the base flexes. With a rigid enough chassis
and careful calibration, the fourth wheel stops being an extra source of error and becomes
redundancy: four encoders fused through a Kalman filter, with the disagreement between them
carrying information rather than noise. We held under 0.2 inches of drift over thirty seconds
across 10 of 10 trials.

It wasn't universally better. It cost space, design time and iteration, and I'd still tell a team
with a flexible base not to do it. It was better for the robot and the game we were building for,
which is the only claim worth making about an engineering tradeoff.

## Third option, if the form wants something from the company

> Use this where the programme is judging the current build rather than history. It's the same
> shape and it's three weeks old.

The obvious way to submit applications programmatically is hand-built adapters, one per ATS. We
talked to a company doing exactly that: 29 adapters, plus hand-rolled HTTPS to stay under captcha
and spam detection. It works, and it's a maintenance treadmill that breaks every time a vendor
reshapes a form.

We took browser agents instead, which are slower and more expensive per run, on the view that
generality beats coverage over any horizon that matters. The bet cost us immediately. Workday's
captcha took a week and a move to residential proxies to get through. It now submits end to end
against Greenhouse, Ashby and Workday, and every board after those three is a config change rather
than a new adapter.
