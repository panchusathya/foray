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
