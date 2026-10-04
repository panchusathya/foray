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

> Rewritten 4 Oct with the actual sequence. **The research step is what makes this answer work**:
> he didn't reframe the problem in his head, he went and asked people what had gone wrong in prior
> years and found the gap empirically. That also sets up the closing parallel, which is true and
> is the best thing in the answer.

I was employee #1 at Nova Intelligence. We'd raised a fraction of what the company has now, and we
were pre-revenue, so there was effectively no budget. My job was to get us in front of Fortune 100
CIOs at SAP Sapphire, one of the biggest events in the SAP world and the one week all of those
people are in the same place.

My first instinct was the obvious one: negotiate. Find the cheapest way onto the floor, push for a
rate on the smallest sponsorship package we could get. I spent time on that before accepting it
wasn't going to work. Even at the best rate I could argue for, we'd have been the smallest booth at
a conference where the companies we were competing with were buying stages. Being present and being
noticed are different things, and we could only afford the first one.

So I went and asked instead. I spoke to people who'd attended in previous years about how the event
actually runs and what had gone wrong for them. Almost all of them said the same thing: scheduling.
Hundreds of overlapping sessions across several days, no good way to work out where to be, and the
organisers didn't solve it for you.

That's where the app came from. I built a personalised scheduling tool for Sapphire attendees and
gave it away. I had no affiliation with the organising team, which turned out not to matter, because
my job wasn't to run the conference, it was to get Nova in front of the right people at it. People
used it, told us nothing like it existed, and it put us in front of exactly the CIOs we had no
other route to reach.

What I'd point at isn't the app. It's that I stopped trying to buy a worse version of what everyone
else had, and went and found out what was actually broken. That's the same method behind the
company I'm building now: we interviewed 25 recruiters expecting to hear that sourcing was the
bottleneck, and heard something else entirely.

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
