from __future__ import annotations

from foray.embed.blob import build_linkedin_blob


def _linkedin_blob(**kw):
    base = dict(
        record={
            "headline": "Staff engineer",
            "experience": [{"title": "Staff Engineer", "company": "Stripe",
                            "start_date": "2019", "end_date": "2022"}],
        },
        name="Ada",
        login="li:ada",
        title=None,
        employer="Stripe",
    )
    base.update(kw)
    return build_linkedin_blob(**base)


def test_linkedin_blob_role_span_separator_is_pinned():
    """This string IS the embedded document text.

    embed/run.py re-embeds any row whose stored blob no longer matches, so
    changing this separator silently re-embeds every LinkedIn candidate with a
    dated role. Pinned so a lint autofix or a stray edit cannot do that quietly.
    """
    assert "(2019 \u2013 2022)" in _linkedin_blob()
