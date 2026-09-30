"""Spam-risk score: what trips it, what does not, and the editor endpoint."""

from __future__ import annotations

from juicebox.outreach.spamcheck import (
    personalization_depth,
    score_email,
    score_template,
)

# Close to a real Foray first touch: pay, a deadline, name-only variables.
REAL_SUBJECT = "{{ first_name | there }}, engineering role at Spark-backed AI startup in SF"
REAL_BODY = (
    "<p>Hi {{ first_name | there }},</p><p>I'm {{ sender_first_name }} with Foray, a "
    "recruiting team in San Francisco.</p><p>We're filling a role at a company building AI "
    "agents that run SAP workflows for large enterprises. Spark Capital backing, and revenue "
    "is up ~35x since February.</p><p>Role: hands-on engineer owning platform deployments. "
    "In person in SF, sponsorship available, up to 375k plus equity.</p><p>We're sending them "
    "a shortlist by end of week. If interested, please share your resume.</p><p>Best,</p>"
    "<p>{{ sender_first_name }} | Foray, goforay.io</p>"
)


def _codes(report):
    return {finding.code for finding in report.findings}


class TestOrdinaryRecruitingCopy:
    def test_scores_low_on_the_copy_and_names_the_pixel_separately(self):
        report = score_template(REAL_SUBJECT, REAL_BODY)

        assert report.content_score < 15
        assert report.delivery_score == 10
        assert "delivery_pixel" in _codes(report)
        # Acronyms are not shouting.
        assert "subject_caps" not in _codes(report)
        assert "body_caps" not in _codes(report)

    def test_feel_free_is_not_the_word_free(self):
        report = score_email(subject="Quick question", body="Feel free to reply with questions.")
        assert "body_triggers" not in _codes(report)


class TestSpammyCopy:
    def test_every_classic_signal_is_named(self):
        report = score_template(
            "FREE money!!",
            "<p>Dear candidate, AMAZING OPPORTUNITY. Click here to apply now, "
            "guaranteed. Limited time!!!</p>"
            '<p><a href="https://bit.ly/x">https://goforay.io/jobs</a> '
            '<a href="https://a.com">a</a> <a href="https://b.com">b</a></p>',
        )

        assert report.level == "high"
        assert {
            "subject_exclaim",
            "subject_caps",
            "subject_triggers",
            "body_triggers",
            "body_shortener",
            "body_link_mismatch",
            "body_links",
            "body_identical",
            "delivery_click_tracking",
        } <= _codes(report)
        triggers = next(f for f in report.findings if f.code == "body_triggers")
        assert "click here" in triggers.matches

    def test_a_first_email_pretending_to_be_a_reply(self):
        report = score_email(subject="Re: your application", body="Hello there, a note.")
        assert "subject_fake_reply" in _codes(report)

    def test_a_follow_up_reply_subject_is_not_judged(self):
        report = score_email(subject="Re: role", body="Just bumping this.", is_follow_up=True)
        assert not {f.code for f in report.findings if f.area == "subject"}


class TestPersonalization:
    def test_depth(self):
        assert personalization_depth("Hello everyone") == "none"
        assert personalization_depth("Hi {{ first_name }}, {{ sender_first_name }}") == "surface"
        assert personalization_depth("Hi {{ first_name }}, {{ opening_line }}") == "deep"
        assert personalization_depth("{{ai: say why this fits}}") == "deep"

    def test_only_a_name_is_flagged_softly(self):
        report = score_template("Hi", "<p>Hi {{ first_name }}, a role for you.</p>")
        assert "body_surface" in _codes(report)
        assert "body_identical" not in _codes(report)


class TestTheEndpoint:
    def test_scores_unsaved_steps_in_order(self, client, account):
        response = client.post(
            "/v1/sequences/spam-check",
            json={
                "steps": [
                    {"subject_template": REAL_SUBJECT, "body_template": REAL_BODY},
                    {"subject_template": "", "body_template": "FREE!!! Click here!!!"},
                ]
            },
            headers=account["headers"],
        )

        assert response.status_code == 200
        first, second = response.json()["results"]
        assert first["level"] in {"low", "medium"}
        # A follow-up's blank subject is the thread's, not a missing one.
        assert "subject_empty" not in {f["code"] for f in second["findings"]}
        assert second["score"] > first["score"]

    def test_needs_a_login(self, client):
        response = client.post("/v1/sequences/spam-check", json={"steps": []})
        assert response.status_code == 401
