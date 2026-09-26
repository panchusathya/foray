from __future__ import annotations

from datetime import UTC, datetime

from foray.ingest.activity import (
    URN_TIMESTAMP_SHIFT,
    decode_activity_ts,
    latest_activity,
)

# The id verified against the real 2 GB export: this activity item's CDN image
# URL carries the millisecond stamp 1785442278425, and the URN decodes to the
# same instant. It is the anchor for the whole bit-shift claim.
VERIFIED_ID = "7488687697968975873"


def test_verified_urn_decodes_to_the_measured_date():
    dt = decode_activity_ts(VERIFIED_ID)
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.date() == datetime(2026, 7, 30, tzinfo=UTC).date()
    # Accepted as an int as well as a string — the export ships strings, but a
    # JSON revision shipping numbers must not silently stop decoding.
    assert decode_activity_ts(int(VERIFIED_ID)) == dt


def test_pre_2015_shifted_epoch_is_rejected():
    """The plausibility window is the guard on an UNDOCUMENTED format: if
    LinkedIn changes the encoding, ids decode to absurd dates and must read as
    'unknown', never as a fabricated timestamp."""
    ms_2010 = int(datetime(2010, 6, 1, tzinfo=UTC).timestamp() * 1000)
    urn_2010 = ms_2010 << URN_TIMESTAMP_SHIFT
    assert decode_activity_ts(urn_2010) is None
    # And the far future, the other failure shape.
    ms_far = int(datetime(2099, 1, 1, tzinfo=UTC).timestamp() * 1000)
    assert decode_activity_ts(ms_far << URN_TIMESTAMP_SHIFT) is None
    # Just inside the window still decodes, so the guard is a window, not a ban.
    ms_ok = int(datetime(2016, 6, 1, tzinfo=UTC).timestamp() * 1000)
    assert decode_activity_ts(ms_ok << URN_TIMESTAMP_SHIFT) is not None


def _urn(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1000) << URN_TIMESTAMP_SHIFT)


def test_latest_activity_takes_the_max_across_mixed_ids():
    older = datetime(2019, 3, 4, tzinfo=UTC)
    newer = datetime(2024, 11, 20, tzinfo=UTC)
    record = {
        "activity": [
            {"id": _urn(older)},
            {"id": "not-an-id"},
            {"id": None},
            {"id": _urn(newer)},
            {"id": 42},  # decodes to 1970, out of window
            "junk-not-a-dict",
        ]
    }
    result = latest_activity(record)
    assert result is not None
    dt, kind = result
    assert kind == "linkedin_activity"
    assert dt.date() == newer.date()


def test_latest_activity_falls_back_to_posts_created_at():
    """posts[].created_at is JavaScript Date.prototype.toString(), not ISO --
    the only format the export uses (625/625 distinct values in the sample)."""
    record = {
        "activity": [{"id": "garbage"}],
        "posts": [
            {"created_at": "Sat Nov 26 2016 00:00:00 GMT+0000 (Coordinated Universal Time)"},
            {"created_at": "Wed Apr 05 2021 13:45:09 GMT+0000 (Coordinated Universal Time)"},
            {"created_at": "not a date"},
        ],
    }
    result = latest_activity(record)
    assert result is not None
    dt, kind = result
    assert kind == "linkedin_post"
    assert (dt.year, dt.month, dt.day) == (2021, 4, 5)
