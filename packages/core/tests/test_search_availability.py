"""Whether somebody is on the market, and what search does with that.

The rule that shapes these: an absent preference record means nobody has
asked, not that the person said no. Most of any real database is in that
state, so anything that treats silence as a negative quietly removes almost
everyone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from juicebox.models import CandidatePreference, new_id
from juicebox.repository import upsert_candidates
from juicebox.search.availability import (
    FRESH_DAYS,
    STALE_DAYS,
    from_preference,
)
from juicebox.search.filters import apply_filters
from juicebox.search.index import get_index
from juicebox.search.parser import ParsedQuery, heuristic_parse
from juicebox.search.scoring import assess


def _pref(**kwargs) -> CandidatePreference:
    defaults = {
        "id": new_id("cpref"),
        "org_id": "org_x",
        "candidate_id": "cand_x",
        "employment_status": "",
        "timeline": "",
        "spec": {},
        "active_processes": [],
        "offers_in_hand": [],
        "captured_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    return CandidatePreference(**defaults)


class TestReading:
    def test_no_record_is_unknown_not_a_no(self):
        availability = from_preference(None)
        assert availability.state == "unknown"
        assert not availability.known

    def test_a_record_with_signal_but_no_status_is_passive(self):
        # Somebody sat through an intake call about moving; that is not nothing.
        assert from_preference(_pref(timeline="1_3_months")).state == "passive"

    def test_an_empty_record_is_still_unknown(self):
        assert from_preference(_pref()).state == "unknown"

    def test_placed_overrides_what_was_said_on_the_call(self):
        availability = from_preference(
            _pref(employment_status="employed_searching"), crm_status="placed"
        )
        assert availability.state == "not_looking"

    def test_do_not_contact_overrides_with_no_record_at_all(self):
        assert from_preference(None, crm_status="do_not_contact").state == "not_looking"

    def test_a_year_old_yes_decays_to_passive(self):
        old = datetime.now(UTC) - timedelta(days=STALE_DAYS + 30)
        availability = from_preference(
            _pref(employment_status="employed_searching", captured_at=old)
        )
        assert availability.state == "passive"
        assert availability.stale


class TestScore:
    def test_active_beats_open_beats_not_looking(self):
        active = from_preference(_pref(employment_status="employed_searching")).score()
        open_to = from_preference(_pref(employment_status="employed_open")).score()
        no = from_preference(_pref(employment_status="employed_not_looking")).score()
        assert active > open_to > no

    def test_urgency_separates_two_active_people(self):
        soon = from_preference(
            _pref(employment_status="employed_searching", timeline="immediate")
        ).score()
        later = from_preference(
            _pref(employment_status="employed_searching", timeline="exploratory")
        ).score()
        assert soon > later

    def test_a_stale_yes_scores_below_a_fresh_one(self):
        fresh = from_preference(_pref(employment_status="employed_searching")).score()
        stale = from_preference(
            _pref(
                employment_status="employed_searching",
                captured_at=datetime.now(UTC) - timedelta(days=FRESH_DAYS + 120),
            )
        ).score()
        assert fresh > stale > 0


class TestScoringIntegration:
    def _docs(self, session, account, sample_candidates):
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        session.commit()
        return get_index(session, account["org_id"]).docs

    def test_never_asked_adds_no_criterion(self, session, account, sample_candidates):
        docs = self._docs(session, account, sample_candidates)
        parsed = heuristic_parse("senior machine learning engineer")
        criteria = assess(docs[0], parsed).criteria
        assert not [c for c in criteria if c.kind == "availability"]

    def test_never_asked_does_not_move_the_score(self, session, account, sample_candidates):
        """The reason it is omitted rather than scored neutral.

        A neutral criterion carrying real weight pulls every strong match down
        and every weak one up. With most of a corpus unasked, that compression
        would be the normal case rather than the exception.
        """
        docs = self._docs(session, account, sample_candidates)
        ada = next(d for d in docs if d.full_name == "Ada Okonkwo")
        parsed = heuristic_parse(
            "senior machine learning engineer in San Francisco with pytorch"
        )
        assert assess(ada, parsed).score > 0.8

    def test_a_stated_yes_becomes_visible_evidence(
        self, session, account, sample_candidates
    ):
        docs = self._docs(session, account, sample_candidates)
        ada = next(d for d in docs if d.full_name == "Ada Okonkwo")
        ada.availability = from_preference(
            _pref(employment_status="employed_searching", timeline="immediate")
        )
        criteria = assess(ada, heuristic_parse("machine learning engineer")).criteria
        hit = next(c for c in criteria if c.kind == "availability")
        assert hit.status == "strong"
        assert "Actively looking" in hit.evidence


class TestFilter:
    def _docs(self, session, account, sample_candidates):
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        session.commit()
        return get_index(session, account["org_id"]).docs

    def test_off_by_default_keeps_everyone(self, session, account, sample_candidates):
        docs = self._docs(session, account, sample_candidates)
        assert apply_filters(docs, ParsedQuery()).eligible_count == len(docs)

    def test_on_removes_the_never_asked(self, session, account, sample_candidates):
        docs = self._docs(session, account, sample_candidates)
        outcome = apply_filters(docs, ParsedQuery(), only_available=True)
        assert outcome.eligible_count == 0
        assert outcome.excluded["not_on_market"] == len(docs)

    def test_on_still_drops_the_people_who_said_no(
        self, session, account, sample_candidates
    ):
        docs = self._docs(session, account, sample_candidates)
        docs[0].availability = from_preference(_pref(employment_status="employed_not_looking"))
        assert apply_filters(docs, ParsedQuery(), only_available=True).eligible_count == 0


class TestIndexWiring:
    def test_a_new_preference_invalidates_the_cached_index(
        self, session, account, sample_candidates
    ):
        """Otherwise an intake call never reaches search.

        The snapshot key was built from candidate rows alone, and recording a
        preference does not touch one.
        """
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        session.commit()
        index = get_index(session, account["org_id"])
        candidate_id = index.docs[0].id
        assert index.docs[0].availability.state == "unknown"

        session.add(
            _pref(
                org_id=account["org_id"],
                candidate_id=candidate_id,
                employment_status="employed_searching",
            )
        )
        session.commit()

        refreshed = get_index(session, account["org_id"])
        doc = next(d for d in refreshed.docs if d.id == candidate_id)
        assert doc.availability.state == "active"
