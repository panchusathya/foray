"""Test fixtures: an isolated database and an authenticated API client.

SQLite by default, so the suite needs no infrastructure. Set
``JUICEBOX_TEST_DATABASE_URL`` to run the identical suite against a real
PostgreSQL — the production database deserves the same coverage:

    JUICEBOX_TEST_DATABASE_URL=postgresql+psycopg://juicebox:juicebox@localhost/juicebox_test \\
        pytest

SQLite tests copy a session-built empty schema instead of running
``create_all`` per test, and hash passwords at ``TEST_PBKDF2_ROUNDS`` so
signup is not the suite's bottleneck. ``verify_password`` reads the cost
out of the stored hash, so a leftover 240k hash still verifies.
"""

from __future__ import annotations

import os
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

TEST_DATABASE_URL = os.environ.get("JUICEBOX_TEST_DATABASE_URL", "")
# Cheap enough that hundreds of signups are noise, high enough that we still
# exercise PBKDF2 rather than a plaintext shortcut.
TEST_PBKDF2_ROUNDS = 1_000


def pytest_configure(config):
    """Fail clearly when the Postgres URL is set but its driver is missing.

    Otherwise every test errors on connect and the real cause is buried.
    """
    if not TEST_DATABASE_URL:
        return
    from sqlalchemy.engine import make_url

    try:
        # get_dialect() alone does not import the driver; import_dbapi() does.
        make_url(TEST_DATABASE_URL).get_dialect().import_dbapi()
    except Exception as exc:  # reported verbatim below
        raise pytest.UsageError(
            f"JUICEBOX_TEST_DATABASE_URL={TEST_DATABASE_URL!r} is unusable: {exc}\n"
            'Install the driver with: pip install -e "packages/core[postgres]"'
        ) from exc


@pytest.fixture(scope="session")
def sqlite_schema_template(tmp_path_factory):
    """Empty SQLite file with the full schema, reused by every test via copy.

    ``create_all`` on a fresh file is tens of milliseconds; ``shutil.copyfile``
    of this template is sub-millisecond. Postgres tests skip this and still
    drop/create per test against the shared database.
    """
    if TEST_DATABASE_URL:
        yield None
        return

    path = tmp_path_factory.mktemp("schema") / "empty.db"
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite:///{path.as_posix()}"
    try:
        from juicebox import db as db_module
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        db_module.reset_engine()
        db_module.create_all()
        db_module.reset_engine()
        # create_all enables WAL; a copy of the main file alone would miss
        # uncheckpointed pages. Truncate the WAL and switch to DELETE so the
        # template is a single self-contained file.
        conn = sqlite3.connect(path.as_posix())
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.commit()
        finally:
            conn.close()
        reset_settings_cache()
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
    yield path


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch, sqlite_schema_template):
    """Every test gets its own database, no model calls and a memory mailbox."""
    if TEST_DATABASE_URL:
        monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    else:
        db_path = tmp_path / "test.db"
        shutil.copyfile(sqlite_schema_template, db_path)
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("JUICEBOX_LLM_ENABLED", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("JUICEBOX_PBKDF2_ROUNDS", str(TEST_PBKDF2_ROUNDS))
    # Schema is already in the copied file (SQLite) or created below (Postgres).
    monkeypatch.setenv("JUICEBOX_AUTO_CREATE_SCHEMA", "false")
    # Both provider keys must be blank: the repo-root .env carries a real
    # OPENROUTER_API_KEY for Foray, and a test that enables the LLM would
    # otherwise reach the network.
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("JUICEBOX_LLM_PROVIDER", "none")
    monkeypatch.setenv("EMAIL_PROVIDER", "memory")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hashing")
    monkeypatch.setenv("EXA_API_KEY", "")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    # Retry logic is asserted by call counts, so don't actually sleep for it.
    monkeypatch.setenv("LLM_RETRY_INITIAL_DELAY", "0")

    from juicebox import db as db_module
    from juicebox.config import reset_settings_cache
    from juicebox.integrations.foray import reset_foray, set_foray
    from juicebox.integrations.providers import reset_providers
    from juicebox.jobmatch.exa import reset_exa
    from juicebox.llm.client import reset_llm, set_llm
    from juicebox.llm.embeddings import reset_embedder
    from juicebox.messaging.channels import reset_channels
    from juicebox.outreach.personalize import reset_fragment_cache
    from juicebox.outreach.sender import reset_sender_factories, set_sender
    from juicebox.search.engine import reset_engine
    from juicebox.search.index import invalidate

    reset_settings_cache()
    db_module.reset_engine()
    reset_llm()
    reset_embedder()
    reset_exa()
    reset_engine()
    invalidate()
    reset_fragment_cache()
    set_sender(None)
    reset_sender_factories()
    reset_providers()
    reset_channels()
    reset_foray()

    if TEST_DATABASE_URL:
        # One shared database, so wipe it between tests instead of relying on a
        # fresh file per test the way the SQLite path does.
        db_module.drop_all()
        db_module.create_all()
    yield

    db_module.reset_engine()
    reset_settings_cache()
    # A test that injected a fake client must not leak it into the next one.
    set_llm(None)
    reset_llm()
    reset_embedder()
    reset_exa()
    reset_engine()
    invalidate()
    # Written fragments are cached in-process; a leak would make one test's
    # model output appear in another's preview.
    reset_fragment_cache()
    set_sender(None)
    # A test that swapped a mail provider or sender must not leak it forward.
    reset_sender_factories()
    reset_providers()
    # A test that injected a FakeChannel must not leak it into the next one.
    reset_channels()
    # Likewise a stubbed Foray client: leaking one would let a later test
    # think enrichment is configured when the deployment has no token.
    set_foray(None)
    reset_foray()


@pytest.fixture
def session():
    from juicebox.db import get_session_factory

    db = get_session_factory()()
    try:
        yield db
        db.commit()
    finally:
        db.close()


@pytest.fixture
def mailbox():
    from juicebox.outreach.sender import MemorySender, set_sender

    sender = MemorySender()
    set_sender(sender)
    return sender


@pytest.fixture
def client():
    from juicebox.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def account(client):
    """A signed-up org with an auth header ready to use."""
    response = client.post(
        "/v1/auth/signup",
        json={
            "org_name": "Acme Talent",
            "email": "recruiter@acme-talent.com",
            "password": "supersecret1",
            "name": "Ada",
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    return {
        "token": payload["access_token"],
        "headers": {"Authorization": f"Bearer {payload['access_token']}"},
        "org_id": payload["user"]["org_id"],
        "user_id": payload["user"]["id"],
    }


@pytest.fixture
def sample_candidates() -> list[dict]:
    return [
        {
            "source": "test",
            "source_id": "ml-1",
            "full_name": "Ada Okonkwo",
            "location_city": "San Francisco",
            "location_region": "CA",
            "location_country": "United States",
            "emails": ["ada@example.com"],
            "skills": ["Python", "PyTorch", "LLM", "Kubernetes"],
            "experiences": [
                {
                    "company": "Modal",
                    "company_industry": "ai",
                    "company_stage": "series a",
                    "company_size": "11-50",
                    "title": "Senior Machine Learning Engineer",
                    "start_date": "2019-01",
                    "is_current": True,
                    "description": "Owned the production LLM inference platform.",
                },
                {
                    "company": "Stripe",
                    "company_industry": "fintech",
                    "title": "Machine Learning Engineer",
                    "start_date": "2016-01",
                    "end_date": "2018-12",
                },
            ],
            "educations": [
                {"school": "Stanford University", "degree": "MS", "field_of_study": "Computer Science"}
            ],
        },
        {
            "source": "test",
            "source_id": "fe-1",
            "full_name": "Bo Lindqvist",
            "location_city": "Berlin",
            "location_country": "Germany",
            "emails": ["bo@example.com"],
            "open_to_remote": True,
            "skills": ["TypeScript", "React", "Design Systems"],
            "experiences": [
                {
                    "company": "Linear",
                    "company_industry": "saas",
                    "company_stage": "series b",
                    "title": "Senior Frontend Engineer",
                    "start_date": "2020-06",
                    "is_current": True,
                    "description": "Owned the component library.",
                }
            ],
        },
        {
            "source": "test",
            "source_id": "be-1",
            "full_name": "Cleo Rahman",
            "location_city": "London",
            "location_country": "United Kingdom",
            "emails": ["cleo@example.com"],
            "skills": ["Go", "PostgreSQL", "Kafka", "Distributed Systems"],
            "experiences": [
                {
                    "company": "Ramp",
                    "company_industry": "fintech",
                    "company_stage": "series d",
                    "title": "Staff Backend Engineer",
                    "start_date": "2014-03",
                    "is_current": True,
                    "description": "Rebuilt the ledger service.",
                }
            ],
        },
    ]


@pytest.fixture
def loaded_org(client, account, sample_candidates):
    response = client.post(
        "/v1/candidates/bulk",
        json={"candidates": sample_candidates},
        headers=account["headers"],
    )
    assert response.status_code == 200, response.text
    return account


@pytest.fixture
def outreach_fixture(session, account, sample_candidates):
    from juicebox.models import Candidate, Project, Sequence, SequenceStep
    from juicebox.repository import upsert_candidates

    upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
    project = Project(
        org_id=account["org_id"],
        name="Founding ML Engineer",
        role_title="Senior Machine Learning Engineer",
        location="San Francisco",
    )
    session.add(project)
    session.flush()

    sequence = Sequence(
        org_id=account["org_id"],
        project_id=project.id,
        name="Three touch",
        from_email="talent@acme-talent.com",
        from_name="Acme Talent",
        personalize=False,
    )
    session.add(sequence)
    session.flush()
    session.add_all(
        [
            SequenceStep(
                sequence_id=sequence.id,
                step_order=0,
                delay_days=0,
                subject_template="{{ role_title }} at {{ project_name }}",
                body_template="Hi {{ first_name }},\n\n{{ opening_line }}\n\nBest,\nAcme",
            ),
            SequenceStep(
                sequence_id=sequence.id,
                step_order=1,
                delay_days=3,
                subject_template="Re: {{ role_title }}",
                body_template="Following up, {{ first_name }}.",
            ),
        ]
    )
    session.commit()
    candidates = session.query(Candidate).order_by(Candidate.full_name).all()
    return {"sequence": sequence, "project": project, "candidates": candidates}
