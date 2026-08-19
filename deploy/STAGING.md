# Staging

A rehearsal environment on the same VM as production: production-shaped
(Postgres 16 + pgvector, the same Dockerfiles) but a **separate compose
project, separate Postgres, separate database names, localhost only**. It is
how a migration or Foray pipeline change gets tried against a restored dump
before that change is merged to `main`.

This is not a second VM and not a public `staging.goforay.io`. There is still
no automatic promotion — merging to `main` and `git pull && compose up` on
prod is the deploy. Staging is the missing middle of `dev → CI → staging → prod`.

Cloud agents cannot execute anything in this file against the live VM. They
land the code and the runbook; an operator (or the on-VM agent) runs the
commands during a safe window.

## What CI already gates (no VM)

On every PR and push to `main`:

| Job | What it catches |
|---|---|
| `python (sqlite)` / `python (postgres)` | Juicebox tests, alembic drift, pgvector retrieval |
| `foray (postgres)` | Foray unit tests **and** DB tests against pgvector; `migrate.sh` twice (idempotence); synthetic seed → derive → profile-email dry-run |
| `compose files` | `check_compose.py` isolation guards + `docker compose config` for prod and staging |
| `staging smoke` | `docker compose -f docker-compose.staging.yml up --wait` (db + api + web) and `GET /health` |
| `docker images build` | All four production Dockerfiles still build |

CI does **not** restore a production dump, run Foray's embedding model, or
exercise GitHub/Apollo. Those are the VM steps below.

## Layout

```
feature branch ──▶ PR ──▶ CI (above) ──▶ operator deploys SHA to
                                         /opt/juicebox-staging
                                         restores dump, migrates, smokes
                                    ──▶ merge to main
                                    ──▶ operator deploys /opt/juicebox (prod)
```

Two git checkouts on the VM. **Do not** `git checkout` a feature branch in
`/opt/juicebox` — the next prod `compose up --build` would then ship unmerged
code, and a rebuild of `juicebox-foray` would recreate the prod Foray
container (killing an in-flight `foray.enrich` contacts chain).

```
/opt/juicebox           main          docker-compose.prod.yml     project juicebox
/opt/juicebox-staging   feature SHA   docker-compose.staging.yml  project juicebox-staging
```

The staging image tags are `juicebox-staging-api` / `juicebox-staging-web` /
`juicebox-staging-foray`. Building them cannot retag prod's `juicebox-foray`.

Default staging services: `db`, `api`, `web` on `127.0.0.1:18080`. Foray is
`--profile foray` because a second copy of the baked embedding model is the
RAM risk on a 4 GB box. Worker and `foray-jobs` are also profiles. Email is
hard-wired to `console`. There is no Caddy and no port 80/443.

## One-time VM setup

```bash
git clone https://github.com/panchusathya/JuiceBox.git /opt/juicebox-staging
cd /opt/juicebox-staging
cp .env.staging.example .env.staging
chmod 600 .env.staging
# Set STAGING_SECRET_KEY to a NEW value, not prod SECRET_KEY:
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

`STAGING_SECRET_KEY` is `${:?}`-required. Reusing prod `SECRET_KEY` would let
a staging leak mint valid production sessions.

## Safe window

Prod nightly cron fires at **23:00 UTC**. The contacts chain is
`pgrep -f foray.enrich` inside the prod Foray container. `up.sh` refuses
22:00–00:30 UTC, and refuses `--foray` while that process is running, unless
you pass `--force`.

```bash
pgrep -f foray.enrich || echo "enrich idle"
date -u
```

A staging `up`/`down` does not recreate the prod Foray container. What *does*
is `docker compose -f docker-compose.prod.yml up -d --build` from `/opt/juicebox`.

## Rehearse a change

From a laptop, after CI is green on the PR:

```bash
ssh <vm> 'cd /opt/juicebox-staging && git fetch origin && git checkout <sha>'
ssh <vm> /opt/juicebox-staging/deploy/staging/up.sh          # db + api + web
# Optional, only when rehearsing Foray, and not during enrich/nightly:
ssh <vm> /opt/juicebox-staging/deploy/staging/up.sh --foray

ssh <vm> /opt/juicebox-staging/deploy/staging/refresh-data.sh --migrate
```

`refresh-data.sh` pg_dumps prod `juicebox` and `foray` into
`juicebox_staging` / `foray_staging`. It aborts if those names do not end in
`_staging`, or if the staging db container is the same as prod.

Tunnel and poke:

```bash
ssh -L 18080:127.0.0.1:18080 <vm>
curl -sf http://127.0.0.1:18080/health
# Foray (only if --foray was used):
curl -sf http://127.0.0.1:18080/foray/health
```

Log in with a prod user from the restored dump (sessions were signed with
prod `SECRET_KEY` and will not carry over — that's intended). Confirm the
migration did what you wanted: a new column is there, a query still returns,
Foray search still ranks. Then:

```bash
ssh <vm> /opt/juicebox-staging/deploy/staging/down.sh          # keep the volume
# after you're done with the dump, reclaim disk:
ssh <vm> /opt/juicebox-staging/deploy/staging/down.sh --purge
```

`--purge` drops `pgdata_staging` and the `juicebox-staging-*` images. The
Foray image is ~3 GB; leaving a second copy around is how the box fills up.
Prod deploy already wants `docker image prune -f`.

## Promote

Staging going green is not a merge. Merge the PR to `main`, then on prod:

```bash
pgrep -f foray.enrich && echo "WAIT — contacts chain is running" && exit 1
date -u   # not 23:00 UTC
cd /opt/juicebox
git pull
docker compose -f docker-compose.prod.yml up -d --build
docker image prune -f
```

## What staging will not catch

- Nightly wall-clock, GitHub 502 storms, or OpenRouter spend against the live
  pool. `--profile jobs` can run `foray.refresh.nightly` against the restored
  dump, but the default budgets in `docker-compose.staging.yml` are tiny
  (`FORAY_NIGHTLY_SHARDS=2`, spend cap $1) so a rehearsal cannot impersonate
  unattended prod. Raise them explicitly if you mean to.
- Using prod `GITHUB_TOKEN` / `OPENROUTER_API_KEY` / `APOLLO_API_KEY` in
  `.env.staging` spends the **same** quotas as prod. Leave them unset unless
  the rehearsal needs them.
- Search latency at 100k (`scripts/bench.py`) still wants the prod database.

## Local (no VM)

If you have Docker on a development machine:

```bash
cp .env.staging.example .env.staging
# set STAGING_SECRET_KEY
docker compose -f docker-compose.staging.yml --env-file .env.staging up -d --build
curl -sf http://127.0.0.1:18080/health
```

There is no prod dump to restore locally; the api container runs `juicebox
migrate` on an empty `juicebox_staging`. Foray still needs `--profile foray`.
This is the same shape CI's `staging smoke` job boots.
