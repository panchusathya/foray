# Working in `packages/web/`

The recruiter SPA: React + Vite, plain JS/JSX, no TypeScript. Read the root
`AGENTS.md` first for repo-wide conventions.

`npm run dev` on :5173 proxies `/v1` and `/health` to the API on :8000 and
`/foray/*` to the Foray API on :8100, so the API must be running. `npm test` is
`node --test` over `test/*.test.js` and is **not** part of `make test`; `npm run
test:e2e` is Playwright.

## Phone numbers in the UI (`packages/web/src/lib/phone.js`)

- **One formatter, because E.164 reads badly and guessing reads wrong.** The API
  stores what `intake.phones.lookup_key` produced, which is correct and
  unambiguous and makes `+14155550123` a string a person has to decode before
  they can dial it. Only North American numbers get the grouped form: a country
  code is one to three digits and the national grouping differs by country, so a
  general "insert spaces every three" reformats a real London number into one
  that looks wrong to anybody who knows it. For everything else the stored
  E.164 *is* the presentation -- unfamiliar, but never misleading.
- **`telHref` always dials the raw digits.** The pretty form is for reading.
- **`CandidateSummary.phones` is empty on the search path** and that is honest,
  not a gap: search hits are built from the in-memory `CandidateDoc`, which
  carries addresses and not numbers, and no search surface shows a phone. It is
  why the field has a default.
- **`PhoneEditor`'s warning is about lookups, not deliverability.** A number
  stored without a country code is real data a recruiter can still ring; what it
  cannot do is match an inbound call, because `lookup_key` refuses to guess a
  country. Saying so beats rejecting it. Nothing else about the editor is new --
  `crm.patch_candidate` has always had `phones` in `EDITABLE_FIELDS`, in
  `UNION_FIELDS`, and syncing the projection; only the UI was missing.

## CSV candidate import (`packages/web/src/lib/csvImport.js`)

Import a Juicebox CSV export and enroll the rows in an existing sequence. There
is deliberately **no new backend**: the page ends in the same
`POST /v1/candidates/bulk` then `POST /v1/sequences/{id}/enroll` that Sourcing
calls, so a CSV-sourced enrolment and a search-sourced one are the same row
afterwards. Non-obvious points:

- **Parsing is client-side on purpose.** The recruiter sees every row, with the
  address we would actually mail, before anything is written — which is what
  makes "take however many I want" possible. It also means no upload endpoint
  and no half-imported file to clean up.
- **Personal address before work, always.** `emails[0]` is what
  `sequencer.send_step` mails, so a work address in front of a personal one puts
  a recruiting pitch in somebody's work inbox. Same rule `forayImport.js`
  records, same reason.
- **Undeliverable addresses are dropped at import, not left to the reach
  floor.** `FORAY_MIN_REACH_SCORE` defaults to `0`, and `_sendable_address`
  short-circuits on `floor <= 0` and returns `emails[0]` unconditionally — so on
  a default install a known-bad address *would* be mailed. The floor is the
  second line of defence, not the first.
- **`email_reach` is no longer Foray-only.** Juicebox's
  Deliverable/Risky/Undeliverable verdict maps onto the same reach scores, and
  writing those under `raw.foray` to make them visible would be a lie about
  provenance. `sequencer._email_reach_scores` therefore spans every namespace in
  `raw`, merging `foray` last so its measured scores still win a conflict.
- **`source_id` is the LinkedIn slug** (`li:ada-lovelace`), falling back to the
  Juicebox profile URL, then an address, then a name slug. That is what makes
  re-uploading two overlapping exports update rather than duplicate — the same
  free idempotency the Foray importer gets from a GitHub login.
- **An `.xlsx` makes one round trip and comes back as CSV.** A workbook is a zip
  of XML the browser cannot read, so `POST /v1/imports/workbook` converts the
  first sheet with `openpyxl` and returns delimited text; the client-side mapper,
  the review table and their tests then carry on unchanged. That route persists
  nothing -- it is a format conversion, so no `CommittingRoute` and no audit row
  -- and keeping the conversion there rather than the whole import is what
  preserves "nothing is written until the recruiter presses the button". It
  reports the other sheet names, because data on tab two would otherwise look
  like an empty file.
- **An export can have no email columns at all.** Which columns a Juicebox
  export carries depends on what the user ticked, so every column is optional
  and looked up by name; the page says so rather than importing profiles nobody
  can be mailed. Real fill rates are low — 15 usable addresses in 79 rows is a
  normal file.
- `years_experience` is **omitted** when the column is blank rather than sent as
  `0`: `normalize_candidate` only computes it from experiences when it arrives
  as `None`.
