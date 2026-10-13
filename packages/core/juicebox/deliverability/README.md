# Deliverability: seed tests and inbox health

Step 1 of the spam work. **Measure only.** Nothing in this package pauses,
throttles or re-routes a send; step 2 acts on what this records.

## What it measures

- **Seed tests.** Each sending mailbox periodically re-sends one of its own
  recent real emails to a seed inbox we own (Gmail, Yahoo, any IMAP host; later
  Outlook.com). A reader then looks in that seed and records where it landed:
  `inbox`, `other_tab` (Gmail Promotions/Updates/Social/Forums, Outlook
  "Other"), `spam`, or `missing` (not found anywhere after 24h).
- **Signals from real traffic.** Reply rate by first-send week, bounce rate,
  and *block* bounces (a provider refusing on policy/reputation, as opposed to
  a dead address).
- **Domain checks.** SPF, DMARC, DKIM key size, MX, and the public domain
  blocklists (Spamhaus DBL, SURBL).

## Ways it can fail (written before the code; the tests follow from this list)

### Sending a seed test
1. **It bypasses a rail real mail obeys.** A seed send ignoring the org kill
   switch, the sending window, the per-mailbox daily cap or the domain gap
   is extra volume from a domain whose reputation is the thing at risk.
   Seed sends must obey all four, and must *count* against the cap.
2. **It counts nowhere.** If `rotation.sent_in_window` / `frees_at` do not see
   seed sends, a mailbox capped at 5 sends 6.
3. **It sends from a mailbox that is off.** Revoked, errored, `send_enabled`
   false or cap <= 0 means "do not send from this", seeds included.
4. **Content that is not what candidates get.** A canned test body measures
   the canned body. The test re-renders a real recent step from that mailbox
   (prior AI fragments reused, so no model call and the same words). A
   rendering failure falls back to the stored text rather than not testing.
5. **It fakes an open/click on a real candidate's message.** If tracking is on,
   the pixel/links must point at the seed test's id, never the copied
   message's id.
6. **It double-sends on retry.** A worker retrying a seed job must not send a
   second copy once the first was accepted (the test row is the guard).
7. **It never runs or runs constantly.** Cadence is per mailbox: at most one
   scheduled test per interval; "Test now" is explicit and still capped by
   remaining room.
8. **It pollutes other features.** Seed addresses must never be staged by the
   contact-discovery sweep as people we emailed, and a seed test is not a
   `Message` (no candidate, no enrollment, never in the Outbox or reply flow).
9. **Seed rotation starves a provider.** A mailbox should spread its tests
   across seeds (least-tested seed first) so every provider is sampled.

### Reading a seed
10. **Wrong match.** Two tests with the same subject from the same mailbox on
    different days; matching must prefer Message-ID and fall back to
    from + subject + not-before-sent-time, never "any message from this sender".
11. **A miss read as "missing".** A seed we could not log in to, or a timeout,
    is *unknown*, not "not delivered". `missing` is only written after a
    successful search that came back empty and 24h have passed.
12. **The reader changes the seed.** Opening, flagging or moving a message out
    of spam trains that provider's filter and skews every later test. IMAP
    folders are opened read-only (`EXAMINE`) and bodies fetched with `PEEK`.
13. **Spam folder not found.** Spam is `[Gmail]/Spam` on Gmail, `Bulk` on
    Yahoo, `Junk` elsewhere. Use the IMAP SPECIAL-USE `\Junk` flag first, then
    known names; a seed with no detectable spam folder still reports inbox
    hits, and says it cannot see spam.
14. **Gmail tabs mistaken for the Primary inbox.** Promotions is still INBOX
    over IMAP; the category is asked separately (`X-GM-RAW category:...`).
15. **Secrets leak.** Seed passwords are stored encrypted and never returned by
    the API, logged, or echoed in an error.
16. **A bad seed is saved silently.** Adding a seed logs in first; a failed
    login is an error on the form, not a row that fails forever in the
    background.

### Gmail seeds connected with Google sign-in (no app password)
22. **It asks for more than it reads.** A seed needs `gmail.readonly` and
    nothing else; asking for send/compose on an inbox we only read is a
    permission we would never use. And a consent where the person unticked
    `gmail.readonly` must be refused, not saved as a seed that can see nothing.
23. **A seed connect turns into a sending mailbox, or the reverse.** Both go
    through the one registered Gmail callback, so the signed state carries the
    purpose; a seed connect must never create a `MailboxConnection`, and a
    mailbox connect never a seed.
24. **The address is typed, not proven.** The seed's address is the Google
    account's own (userinfo), so a test cannot be aimed at an inbox the
    reader is not signed in to.
25. **An expired or revoked sign-in reads as "missing".** A failed refresh is a
    read error on the seed ("reconnect it"), never a placement.
26. **A refreshed token is thrown away.** Refreshed access tokens are written
    back to the seed, and a reconnect that returns no refresh token keeps the
    stored one -- otherwise the seed dies at the next refresh.
27. **The reader writes.** Only GETs; `gmail.readonly` could not modify
    anyway, and the reader should not try.
28. **Spam is invisible to search.** Gmail search skips Spam and Trash unless
    asked; every search passes `includeSpamTrash=true`.
29. **A revoked sending mailbox can never become a seed.** Only an *active*
    sending mailbox is refused; a disconnected one is just an inbox now.

### Yahoo seeds connected with Yahoo sign-in
Yahoo has no public mail REST API, so a Yahoo seed signs in with OAuth
(`mail-r`, read only) and the same IMAP reader authenticates with that token
(SASL XOAUTH2) instead of an app password. Items 22-27 apply unchanged, plus:
30. **The token goes in as a password.** XOAUTH2 is `AUTHENTICATE`, never
    `LOGIN`; sending the bearer token to LOGIN fails, or worse, gets logged
    by a server as a password attempt.
31. **Yahoo's callback is not Google's.** Yahoo needs its own registered
    redirect (`/v1/deliverability/seeds/yahoo/callback`); a state minted for
    one provider or for a mailbox connect is refused there.

### Warm-up for newly connected mailboxes (step 2, first part)
The cap a person sets becomes the *ceiling*; while a mailbox is warming, the
cap in force today follows a ramp from the day it was connected (2/day the
first week, then 4, 7, 10, 15, 20, 25, then +5 a week) until it reaches the
ceiling. Computed from `warmup_started_at` on every read, so no job has to run
for it to advance.
32. **A new mailbox sends its full cap on day one.** Rotation hands the next
    send to the least-used mailbox, so a fresh one at the default 40 fills
    first -- 40 cold emails from a domain with no history. A newly created
    connection starts warming automatically.
33. **A reconnect restarts the ramp.** Re-consenting a seasoned mailbox (token
    refresh, scope change) must not throttle it back to 2/day; only a newly
    created row starts warming.
34. **One reader of the cap forgets the ramp.** Picking a mailbox, its
    remaining room, when it next frees up, the pool's capacity and a seed
    test's room all go through `effective_cap`; any path that reads
    `daily_send_cap` directly would send at the ceiling.
35. **Raising the cap skips the ramp.** The cap is the ceiling; the ramp still
    applies. Lowering it below today's ramp wins (min of the two), and 0
    still means "send nothing".
36. **It never visibly ends.** Once the ramp reaches the ceiling the mailbox
    reports warm-up complete and the cap in force is simply the cap.

### Signals and checks
17. **Young cohorts read as bad mailboxes.** Reply rate counts people first
    emailed at least 3 days ago; weekly series are by first-send week.
18. **A blocklist refusing our resolver reads as "listed".** Spamhaus answers
    127.255.255.x to public/cloud resolvers; that is `unknown`, not `listed`.
19. **DNS failures read as misconfiguration.** A timeout is `unknown`; only a
    definite NXDOMAIN/NoAnswer is "missing".
20. **Consumer domains get domain checks.** gmail.com's SPF/DMARC is Google's;
    those mailboxes are grouped but not domain-checked.
21. **Health label claims more than the data says.** Fewer than 3 seed results
    and fewer than 30 people is `no_data`, not green. A rate is only a signal
    over enough volume: bounce rate needs 50 sends in the window before it can
    mark a mailbox at risk (found in the browser: a two-day-old Gmail sender
    read "at risk" off one bounce in twenty).
