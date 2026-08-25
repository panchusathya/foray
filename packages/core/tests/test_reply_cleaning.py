"""Inbound replies arrive as whole documents; the CRM should store the reply.

Taken from a real thread: a two-sentence "not right now" was stored as 1.4kB
of divs with our own outreach quoted underneath, and `classify_reply` was
reading that blob -- so the intent came partly from our pitch, attributed to
the candidate.
"""

from __future__ import annotations

from juicebox.integrations.replies import clean_inbound_body, strip_quoted

GMAIL_REPLY = (
    '<div><div dir="auto">Hi Sarah&nbsp;</div>'
    '<div dir="auto">Thanks for reaching out. I recently took a new position '
    "at Schwab, so the timing is not great.</div>"
    '<div dir="auto"><br></div><div dir="auto">Best&nbsp;<br>'
    '<div dir="ltr" class="gmail_signature"><div>James Allan Hall</div>'
    '<div>Phone: (317) 501-6460</div></div></div></div>'
    '<div><br><div class="gmail_quote">'
    '<div dir="ltr" class="gmail_attr">On Fri, Aug 21, 2026 at 9:00 AM '
    '&lt;<a href="mailto:sarah@goforay.io">sarah@goforay.io</a>&gt; wrote:<br></div>'
    "<blockquote><p>Hi James,</p>"
    "<p>This is for an Accel-backed startup building in applied ML.</p>"
    "<p>We liked your background, so let me know if there is any interest!</p>"
    "</blockquote></div></div>"
)


class TestCleanInboundBody:
    def test_the_reply_survives_and_the_markup_does_not(self):
        out = clean_inbound_body(GMAIL_REPLY, is_html=True)
        assert "Thanks for reaching out" in out
        assert "Schwab" in out
        assert "<div" not in out and "&nbsp;" not in out
        assert chr(0x00A0) not in out, "a literal nbsp renders as a stray box"

    def test_our_own_pitch_does_not_come_back_as_theirs(self):
        """The bug with teeth.

        `classify_reply` runs on this text. With the quoted thread left in, the
        classifier sees "let me know if there is any interest" -- our sentence
        -- and weighs it as though the candidate wrote it.
        """
        out = clean_inbound_body(GMAIL_REPLY, is_html=True)
        assert "Accel-backed" not in out
        assert "let me know if there is any interest" not in out
        assert "On Fri, Aug 21" not in out

    def test_it_is_dramatically_shorter(self):
        out = clean_inbound_body(GMAIL_REPLY, is_html=True)
        assert len(out) < len(GMAIL_REPLY) / 3

    def test_plain_text_replies_are_left_alone(self):
        assert clean_inbound_body("No thanks, happy where I am.") == (
            "No thanks, happy where I am."
        )

    def test_html_is_detected_even_when_the_flag_is_wrong(self):
        """Providers disagree about `body_is_html`; the content decides."""
        out = clean_inbound_body("<div>Not right now</div>", is_html=False)
        assert out == "Not right now"

    def test_a_reply_that_is_only_a_quote_is_not_emptied(self):
        """Better an ugly timeline entry than an empty one.

        Someone replying with nothing but the quoted thread has still told us
        something -- and blanking the row loses the fact that they wrote back.
        """
        quoted = "On Fri, Aug 21, 2026 at 9:00 AM sarah@goforay.io wrote:\nthe original"
        assert clean_inbound_body(quoted).strip()

    def test_empty_stays_empty(self):
        assert clean_inbound_body("") == ""
        assert clean_inbound_body("   ") == ""


class TestStripQuoted:
    def test_it_cuts_at_the_attribution_line(self):
        text = "Sure, let's talk.\n\nOn Mon, Jan 1, 2026 at 9:00 AM bob@x.com wrote:\n> hi"
        assert strip_quoted(text) == "Sure, let's talk."

    def test_it_cuts_at_an_outlook_header_block(self):
        text = "Not interested.\n\nFrom: Sarah <sarah@goforay.io>\nSent: Monday\n"
        assert strip_quoted(text) == "Not interested."

    def test_a_mention_mid_sentence_does_not_truncate(self):
        """Anchored to a line start, or "I saw the note from: Dan" eats a reply."""
        text = "I saw the note from: Dan and I am interested."
        assert strip_quoted(text) == text

    def test_a_reply_with_no_quote_is_untouched(self):
        assert strip_quoted("Just this.") == "Just this."
