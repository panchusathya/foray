# Working in `applications/`

Read the root `AGENTS.md` first for repo-wide conventions. Design record:
`docs/CANDIDATE_AGENT_PLAN.md` — §4 for the three budgets, §5 for what "meets
our bar" means, §8 for the submission tiers and the Tier-3 reversal. Turning any
of it on is `docs/AUTO_APPLY_SETUP.md`.

## Submitting applications (`packages/core/juicebox/applications`)

- **`request_submission` is the one door in.** Consent and the submission budget
  are checked there, before a job row exists, the same way `create_application`
  owns per-posting uniqueness. A surface that forgets a check must not be able
  to bypass one. `auto_submit` is the forgiving wrapper for side-effect surfaces
  (a portal approval, a thread "yes") — it reports a reason instead of raising,
  because "no adapter for this employer" is the normal answer.
- **Adding a vendor means implementing `Submitter` in `submit/base.py` and
  registering it in `submit/registry.py`.** Nothing else moves. An adapter gets a
  `SubmissionContext` with the bytes already read and returns a
  `SubmissionResult`; it never takes a `Session`, because an adapter one refactor
  away from moving the pursuit itself is a second status machine.
- **Only `applications/state.transition` marks anything submitted.** An adapter
  returns a verdict; the machine decides what it means. The consent gate guards
  `submitting` as well as `submitted`, so we never discover at the end of a
  browser run that we were never allowed to record it.
- **`needs_human` is a terminus, not a retry.** A required field we cannot fill
  from the resume, the preference row or the form answers stops the run. Nothing
  in this package invents an answer — `docgen/answers.py` leaves `why_company`
  blank on purpose, and a browser adapter leaves a blank field blank.
- **Every attempt gets a row, including the ones that failed.** A retried job
  must not look like a submission that never happened, and the partial unique
  index on a succeeded row is the double-submit guard that survives two workers
  racing.
- **`ats/detect.py` resolves from the URL alone on the ingest path** and is
  allowed one network hop only when somebody is actually applying. A URL-only
  miss leaves `ats_resolved_at` NULL on purpose — that is what lets the later
  follow happen. Recognising a vendor is not the same as having an adapter for
  it; `registry.get_submitter` returning `None` means Tier 0, not an error.
- **Alias mail never reaches the recruiter-reply path.** `integrations/sync.py`
  routes anything addressed to `anna+<token>@goforay.io` into
  `applications/inbox.py` and returns. Intent classification, reply drafting and
  enrollment stops are built for a candidate answering a recruiter, and would
  read an automated ATS acknowledgement as somebody changing their mind.
- **The alias token is derived, not stored** (`security.alias_token`). Inbound
  mail arrives before any lookup, and `candidate_for` re-verifies the token
  against the row's own candidate id rather than trusting an address it was
  handed.
