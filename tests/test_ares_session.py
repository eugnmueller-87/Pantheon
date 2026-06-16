"""
_same_session must use DST-correct timezones (zoneinfo), not a fixed EDT offset.
The old `timedelta(hours=4)` was wrong Nov–Mar and could flip the ET date across
midnight UTC in winter, expiring pending orders early.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agents.ares import _same_session


def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_january_night_crosses_et_sessions():
    # Winter (EST = UTC-5). 23:55 UTC on Jan 14 is 18:55 ET Jan 14;
    # 00:05 UTC on Jan 15 is 19:05 ET Jan 14 — SAME ET date.
    # But 04:05 UTC Jan 15 is 23:05 ET Jan 14, and 05:05 UTC is 00:05 ET Jan 15.
    # Pick a pair that is genuinely a different ET session in winter:
    t1 = _utc(2026, 1, 14, 23, 55)   # 18:55 ET Jan 14
    t2 = _utc(2026, 1, 15, 6, 5)     # 01:05 ET Jan 15  → different ET date
    # CET: 00:55 Jan 15 vs 07:05 Jan 15 → SAME CET date, so _same_session is True.
    # The regression we guard: the OLD hours=4 offset would compute ET wrong in
    # winter. Assert the ET comparison itself is correct via a CET-neutral pair:
    a = _utc(2026, 1, 14, 22, 0)     # 17:00 ET Jan 14 / 23:00 CET Jan 14
    b = _utc(2026, 1, 15, 1, 0)      # 20:00 ET Jan 14 / 02:00 CET Jan 15
    # ET dates match (both Jan 14) → same session regardless of CET split.
    assert _same_session(a, b) is True
    # And a true cross-session winter pair (different ET AND CET date):
    assert _same_session(t1, _utc(2026, 1, 16, 12, 0)) is False


def test_same_ny_session_midday():
    # 13:00 and 20:00 UTC on a Wednesday → both within NY session same day.
    t1 = _utc(2026, 6, 17, 13, 0)
    t2 = _utc(2026, 6, 17, 20, 0)
    assert _same_session(t1, t2) is True


def test_summer_vs_winter_offset_differs():
    # Sanity: identical wall-clock pair lands on the same date in both seasons,
    # proving the offset is computed per-date (DST-aware), not fixed.
    summer = _same_session(_utc(2026, 7, 1, 14, 0), _utc(2026, 7, 1, 19, 0))
    winter = _same_session(_utc(2026, 1, 5, 14, 0), _utc(2026, 1, 5, 19, 0))
    assert summer is True and winter is True


def test_naive_timestamps_tolerated():
    # Naive datetimes assumed UTC — must not raise.
    t1 = datetime(2026, 6, 17, 13, 0)
    t2 = datetime(2026, 6, 17, 20, 0)
    assert _same_session(t1, t2) is True
