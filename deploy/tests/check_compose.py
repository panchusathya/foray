#!/usr/bin/env python3
"""Guardrails for the prod and staging compose files.

Run from the repo root:

    python deploy/tests/check_compose.py

Stdlib only — CI runs this before `docker compose config` so a missing
interpolation still fails the job for the right reason.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROD = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
STAGING = (ROOT / "docker-compose.staging.yml").read_text(encoding="utf-8")
CI_ENV = (ROOT / "deploy" / "ci.env").read_text(encoding="utf-8")


class Failed(Exception):
    pass


def _must(cond: bool, msg: str) -> None:
    if not cond:
        raise Failed(msg)


def check_staging_isolation() -> None:
    _must('name: juicebox-staging' in STAGING, "staging compose project must be juicebox-staging")
    _must("pgdata_staging" in STAGING, "staging must use a distinct volume, not pgdata")
    _must("juicebox_staging" in STAGING, "staging Juicebox db must be juicebox_staging")
    _must("foray_staging" in STAGING, "staging Foray db must be foray_staging")
    _must("5432/juicebox_staging" in STAGING, "staging DATABASE_URL must target juicebox_staging")
    _must("5432/foray_staging" in STAGING, "staging FORAY_DATABASE_URL must target foray_staging")
    stripped = STAGING.replace("juicebox_staging", "").replace("foray_staging", "")
    _must("5432/juicebox" not in stripped, "staging DATABASE_URL must not target unsuffixed juicebox")
    _must("5432/foray" not in stripped, "staging FORAY_DATABASE_URL must not target unsuffixed foray")
    _must("EMAIL_PROVIDER: console" in STAGING, "staging must hard-wire EMAIL_PROVIDER=console")
    _must(
        "APPLY_SUBMITTER: console" in STAGING,
        "staging must hard-wire APPLY_SUBMITTER=console",
    )
    _must(
        "${APPLY_SUBMITTER" not in STAGING,
        "staging must not interpolate APPLY_SUBMITTER -- it would inherit prod's",
    )
    _must("JUICEBOX_ENV: staging" in STAGING, "staging must set JUICEBOX_ENV=staging")
    _must("${STAGING_SECRET_KEY:?" in STAGING, "staging SECRET_KEY must come from ${STAGING_SECRET_KEY:?}")
    _must("${SMTP" not in STAGING, "staging must not interpolate SMTP credentials")
    _must("SMTP_HOST" not in STAGING, "staging must not interpolate SMTP credentials")
    _must(
        not any("80:80" in line and "18080" not in line for line in STAGING.splitlines()),
        "staging must not publish port 80",
    )
    _must("443:443" not in STAGING, "staging must not publish port 443")
    _must("127.0.0.1:18080:80" in STAGING, "staging web must bind 127.0.0.1:18080 only")
    _must("juicebox-staging-foray" in STAGING, "staging Foray image must not be juicebox-foray")
    _must("image: juicebox-foray" not in STAGING, "staging must not retag the prod foray image")
    _must('profiles: ["foray"]' in STAGING, "foray service must be opt-in (RAM)")
    _must("max-size" in STAGING, "staging must cap container logs")
    _must("caddy:" not in STAGING, "staging must not run caddy")


def check_prod_still_prod() -> None:
    _must('name: juicebox' in PROD, "prod compose project must stay juicebox")
    _must("5432/juicebox" in PROD, "prod must still target the juicebox database")
    _must("5432/foray" in PROD, "prod must still target the foray database")
    _must("juicebox_staging" not in PROD, "prod compose must not mention juicebox_staging")
    _must("foray_staging" not in PROD, "prod compose must not mention foray_staging")
    _must("image: juicebox-foray" in PROD, "prod foray-jobs must keep using juicebox-foray")
    _must("80:80" in PROD and "443:443" in PROD, "prod caddy must still publish 80/443")


def check_ci_env_is_inert() -> None:
    _must("example.invalid" in CI_ENV, "deploy/ci.env must use a non-routable example domain")
    _must("STAGING_SECRET_KEY=" in CI_ENV, "deploy/ci.env must set STAGING_SECRET_KEY")
    _must("do-not-use" in CI_ENV or "not-a-real" in CI_ENV, "deploy/ci.env secrets must look fake")


def main() -> int:
    checks = (
        check_staging_isolation,
        check_prod_still_prod,
        check_ci_env_is_inert,
    )
    failed = 0
    for check in checks:
        try:
            check()
            print(f"ok  {check.__name__}")
        except Failed as exc:
            failed += 1
            print(f"FAIL  {check.__name__}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
