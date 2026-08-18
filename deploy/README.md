# Deploying Foray/Juicebox to a VM

One always-on Linux VM running `docker-compose.prod.yml`. Caddy is the only
publicly listening service (80/443) and serves `https://$APP_DOMAIN` with
automatic Let's Encrypt TLS. Postgres, the API, and the web container publish
no host ports; the ingest webhook binds to localhost only.

```
internet ──▶ caddy :443 ──▶ web (nginx: SPA + proxy) ──▶ api :8000 ──▶ db (pgvector)
                                                          ▲
                                                 worker ──┘   (durable job queue)
```

Written for UpCloud (30-day trial) but nothing is provider-specific — any
Ubuntu 24.04 VM with 2 vCPU / 4 GB RAM works (Hetzner CPX21 is the cheap
fallback). Rebuilding on another provider is this runbook + a `pg_dump`
restore, ~30 minutes.

## 1. Provision

1. Create the server: Ubuntu 24.04, 2 vCPU / 4 GB, add your SSH public key.
2. Provider firewall: allow inbound **TCP 22, 80, 443** only.
3. Optionally enable the provider's scheduled backups.
4. **UpCloud trial note:** trial resources are deleted unless you make a
   deposit before the 30 days end — set a calendar reminder.

## 2. DNS

Add an `A` record for `app.goforay.io` → the VM's public IPv4. Do this first:
Caddy can only obtain the certificate once the name resolves (it retries
harmlessly until then).

## 3. Base system

```bash
apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh   # includes the compose plugin
```

Harden SSH — in `/etc/ssh/sshd_config` set:

```
PasswordAuthentication no
PermitRootLogin prohibit-password
```

then `systemctl restart ssh`. Optional but cheap: `apt install -y fail2ban
unattended-upgrades`.

## 4. Configure the app

```bash
git clone https://github.com/panchusathya/JuiceBox.git /opt/juicebox
cd /opt/juicebox
cp .env.production.example .env
chmod 600 .env
```

Edit `.env`:

- `SECRET_KEY` — generate locally:
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`
- `POSTGRES_PASSWORD` — any long random string.
- `SMTP_*`, `OUTREACH_FROM_EMAIL` — your sending mailbox credentials.
- `APP_DOMAIN`, `ACME_EMAIL` — prefilled; adjust if the hostname differs.

Compose refuses to start (loud `${VAR:?}` errors) if a required value is
missing — `docker compose -f docker-compose.prod.yml config` is a dry-run
check.

## 5. Start

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f api   # watch for a clean migrate + startup
```

Within a minute of DNS resolving, `https://app.goforay.io` serves the app.

## 6. First data

```bash
# demo org + candidates (prints login credentials) — dev/demo only:
docker compose -f docker-compose.prod.yml exec api python -m juicebox.cli seed

# real data:
docker compose -f docker-compose.prod.yml exec api python -m juicebox.cli import /path/in/container.jsonl --org <org-id>
```

The ingest webhook listens on the VM's localhost:8787; reach it from your
machine with `ssh -L 8787:127.0.0.1:8787 <vm>`.

## 7. Backups

`/etc/cron.d/juicebox-backup`:

```
15 3 * * * root docker compose -f /opt/juicebox/docker-compose.prod.yml exec -T db pg_dump -U juicebox juicebox | gzip > /var/backups/juicebox/$(date +\%F).sql.gz && find /var/backups/juicebox -name '*.sql.gz' -mtime +14 -delete
```

(`mkdir -p /var/backups/juicebox` first.) Restore:

```bash
gunzip -c /var/backups/juicebox/<date>.sql.gz | docker compose -f docker-compose.prod.yml exec -T db psql -U juicebox juicebox
```

## 8. Updating

```bash
cd /opt/juicebox
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

Migrations run inside the api container before uvicorn starts; the worker
waits for the api healthcheck. Expect a few seconds of downtime.

## Things to know

- **The login page is public.** Use strong passwords for every org user.
  Request rate limiting hasn't landed yet — treat it as a fast-follow.
- **Email deliverability** (SPF/DKIM/DMARC on the sending domain, warmup,
  volume) is configured at your mail provider, not in this repo. Gmail/Yahoo
  bulk-sender rules make DKIM alignment non-optional.
- **Tracking links** in outbound email point at `https://$APP_DOMAIN/t/...`;
  they are HMAC-signed with `SECRET_KEY`. Rotating `SECRET_KEY` breaks
  click-tracking links in already-sent messages and logs everyone out.
- **Postgres is the only stateful service** — the `pgdata` volume plus the
  nightly dump is the whole backup story. `caddy_data` only caches
  certificates and re-issues if lost.
