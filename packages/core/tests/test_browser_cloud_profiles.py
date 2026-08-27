"""Cloud profiles are per candidate per origin. Never reused across either."""

from __future__ import annotations

import pytest

from juicebox.applications.submit.browser.cloud import FakeCloudClient, ProfileQuota, set_cloud_client
from juicebox.applications.submit.browser.profiles import cloud_profile_for
from juicebox.config import reset_settings_cache
from juicebox.models import BrowserCloudProfile, Candidate


@pytest.fixture
def fake_cloud(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_API_KEY", "bu_test")
    reset_settings_cache()
    fake = FakeCloudClient()
    set_cloud_client(fake)
    yield fake
    set_cloud_client(None)
    reset_settings_cache()


def _candidate(session, org_id, suffix):
    row = Candidate(
        org_id=org_id,
        source="manual",
        source_id=f"prof-{suffix}",
        full_name=suffix,
        emails=[f"{suffix}@example.com"],
    )
    session.add(row)
    session.flush()
    return row


class TestCloudProfileAllocator:
    def test_two_candidates_two_origins_four_ids(self, session, account, fake_cloud):
        org = account["org_id"]
        ada = _candidate(session, org, "ada")
        priya = _candidate(session, org, "priya")
        gh = "https://boards.greenhouse.io"
        lever = "https://jobs.lever.co"
        ids = []
        for person in (ada, priya):
            for origin in (gh, lever):
                state = cloud_profile_for(
                    session, org_id=org, candidate_id=person.id, origin=origin
                )
                assert state is not None
                ids.append(state.cloud_profile_id)
        assert len(ids) == 4
        assert len(set(ids)) == 4
        from sqlalchemy import select

        rows = session.scalars(select(BrowserCloudProfile)).all()
        assert len(rows) == 4
        user_ids = {row.user_id for row in rows}
        assert f"{ada.id}:{gh}" in user_ids
        assert f"{ada.id}:{lever}" in user_ids
        assert f"{priya.id}:{gh}" in user_ids

    def test_same_candidate_origin_reuses(self, session, account, fake_cloud):
        org = account["org_id"]
        ada = _candidate(session, org, "ada2")
        origin = "https://boards.greenhouse.io"
        a = cloud_profile_for(session, org_id=org, candidate_id=ada.id, origin=origin)
        b = cloud_profile_for(session, org_id=org, candidate_id=ada.id, origin=origin)
        assert a.cloud_profile_id == b.cloud_profile_id

    def test_quota_is_not_a_silent_share(self, session, account, fake_cloud):
        org = account["org_id"]
        ada = _candidate(session, org, "ada3")
        fake_cloud.fail_profile = ProfileQuota("plan cap")
        with pytest.raises(ProfileQuota):
            cloud_profile_for(
                session,
                org_id=org,
                candidate_id=ada.id,
                origin="https://boards.greenhouse.io",
            )
