# Browser regression suite

Drives a real stack through Chromium: login, project dedupe, enrolment, the
scheduling preview. Specs sign up their own org over the API and assert through
the UI, so they never depend on seeded data or on each other.

## Run it

Against the CI-shaped staging stack:

```bash
docker compose -f docker-compose.staging.yml --env-file deploy/ci.env up -d --build --wait
```

then

```bash
npm run test:e2e
```

Against a local dev pair on a throwaway SQLite file, so the suite never touches
`data/juicebox.db` (`.env.e2e` holds just `DATABASE_URL` and `EMAIL_PROVIDER=console`):

```bash
python -m uvicorn juicebox.main:app --app-dir packages/core --port 8001 --env-file .env.e2e
```

```bash
JUICEBOX_API_URL=http://localhost:8001 npm run dev --prefix packages/web -- --port 5175 --strictPort
```

```bash
E2E_BASE_URL=http://localhost:5175 npm run test:e2e
```

(`.claude/launch.json` is untracked; entries named `api-8001` and `web-5175` running
those two commands let the desktop app's preview start the pair.)

First run only:

```bash
npx playwright install chromium
```

## House rules

- **Never point `E2E_BASE_URL` at production.** Specs create orgs, projects and
  enrolments. The staging stack hard-wires `EMAIL_PROVIDER=console`; production
  sends through a real Outlook mailbox.
- **Workers stay at 1.** One shared database — parallel specs racing on the same
  rows produce failures that say nothing about the code.
- **Signup emails end in `@e2e.example.com`.** Pydantic's `EmailStr` rejects the
  special-use TLDs (`.invalid`, `.test`, `.example`, `.localhost`).
- **Prefer `data-testid` over text** for anything that also appears elsewhere on
  the page (a candidate name shows up in both the preview dropdown and the
  enrolment list).
- Sourcing specs skip themselves unless `/foray/health` answers, because Foray
  is a compose profile that CI does not start.

## What each file covers

| File | Guards |
|---|---|
| `auth.spec.js` | login, bad password, logout |
| `pipeline.spec.js` | duplicate adds collapse, one candidate across projects, the board stays a tracker, outreach-driven stages need an explicit override |
| `sequences.spec.js` | the enrol picker, already-enrolled exclusion, enrol idempotency, the no-email warning, the paused banner, step editing blocked mid-flight, unsaved edits surviving a pause |
| `sourcing.spec.js` | contactable filter, and that a guessed address never reaches the UI |
| `contacts-grid.spec.js` | the CRM grid renders and sorts, the preset views select the right population (going cold, sourced pool vs CRM), view state survives reload and the back button, and the name cell carries the address without widening the table |
| `contacts-bulk.spec.js` | the bulk bar appears only with a selection, select-page and clear, add-to-project reporting an outcome that outlives the cleared selection, the no-email enrol warning, and that a select-all-matching selection cannot be exported |
| `contact-record.spec.js` | the person record: Emails is the default tab and present on first paint, notes written/edited/deleted and surviving a reload, inline field editing, and that do-not-contact writes a real suppression |
| `contacts-add.spec.js` | adding a contact by hand: creation lands on the new record, an optional first note is saved, a duplicate address offers the existing record instead of an error, save-and-add-another, and two same-named people with no email stay two people |
| `discovered.spec.js` | the Discovered tray is a view of Contacts rather than its own nav entry, its empty state points at Settings, and the review filters default to new / no cc-only (promote and dismiss are covered in test_discovery.py, since staging needs a mailbox sweep) |
