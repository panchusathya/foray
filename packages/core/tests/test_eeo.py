"""Prefer not to answer is a refusal, not a protected-attribute assertion."""

from juicebox.applications.eeo import decline_choice, is_decline


class TestDeclineChoice:
    def test_common_wordings(self):
        options = [
            "Male",
            "Female",
            "Non-binary",
            "Prefer not to answer",
        ]
        assert decline_choice(options) == "Prefer not to answer"
        assert is_decline("Prefer not to answer")
        assert is_decline("Decline to self-identify")
        assert is_decline("I do not wish to disclose")
        assert is_decline("Choose not to identify")
        assert not is_decline("Male")
        assert not is_decline("")

    def test_empty_when_the_form_has_no_decline(self):
        assert decline_choice(["Male", "Female"]) == ""
        assert decline_choice([]) == ""
        assert decline_choice(None) == ""
