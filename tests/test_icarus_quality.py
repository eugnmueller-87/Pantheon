"""
Icarus quality filter is TIME-WINDOWED (daily buckets, 14-day TTL) with a
>=3-bucket requirement, so a bad cold-start day/week can't permanently mute a
pattern. Includes a per-pattern reset.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agents.icarus import _SignalQualityFilter


class FakeRedis:
    """In-memory stand-in for the Upstash REST client (counter subset)."""
    def __init__(self):
        self.store: dict[str, int] = {}

    def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def get(self, key):
        v = self.store.get(key)
        return str(v) if v is not None else None

    def expire(self, key, seconds):
        return 1

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
        return n


class _Sig:
    def __init__(self, hermes_signal_type="EARNINGS", headline="Acme beats earnings"):
        self.hermes_signal_type = hermes_signal_type
        self.headline = headline


def _set_bucket(redis, pattern, day, seen, approved):
    redis.store[f"icarus:quality:{pattern}:seen:{day}"] = seen
    redis.store[f"icarus:quality:{pattern}:approved:{day}"] = approved


def _days(n):
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]


def test_suppresses_when_window_rejection_high_over_3_days():
    redis = FakeRedis()
    qf = _SignalQualityFilter(redis)
    sig = _Sig()
    from agents.icarus import _quality_pattern
    pat = _quality_pattern(sig)
    # 3 days, 12 seen total, all rejected (0 approved) → >=85% reject, >=3 buckets.
    for d in _days(3):
        _set_bucket(redis, pat, d, seen=4, approved=0)
    assert qf.should_suppress(sig) is True


def test_one_bad_day_does_not_suppress():
    redis = FakeRedis()
    qf = _SignalQualityFilter(redis)
    sig = _Sig()
    from agents.icarus import _quality_pattern
    pat = _quality_pattern(sig)
    # All 12 rejections on a SINGLE day → only 1 bucket → below _QUALITY_MIN_BUCKETS.
    _set_bucket(redis, pat, _days(1)[0], seen=12, approved=0)
    assert qf.should_suppress(sig) is False


def test_below_min_samples_does_not_suppress():
    redis = FakeRedis()
    qf = _SignalQualityFilter(redis)
    sig = _Sig()
    from agents.icarus import _quality_pattern
    pat = _quality_pattern(sig)
    # 3 days but only 6 samples total (< 10).
    for d in _days(3):
        _set_bucket(redis, pat, d, seen=2, approved=0)
    assert qf.should_suppress(sig) is False


def test_old_buckets_outside_window_ignored():
    redis = FakeRedis()
    qf = _SignalQualityFilter(redis)
    sig = _Sig()
    from agents.icarus import _quality_pattern
    pat = _quality_pattern(sig)
    # Rejections only on a day 30 days ago → outside the 14-day window.
    old = (datetime.now(timezone.utc).date() - timedelta(days=30)).strftime("%Y%m%d")
    _set_bucket(redis, pat, old, seen=20, approved=0)
    assert qf.should_suppress(sig) is False


def test_record_seen_writes_today_bucket():
    redis = FakeRedis()
    qf = _SignalQualityFilter(redis)
    sig = _Sig()
    from agents.icarus import _quality_pattern
    pat = _quality_pattern(sig)
    qf.record_seen(sig)
    today = _days(1)[0]
    assert redis.store.get(f"icarus:quality:{pat}:seen:{today}") == 1


def test_reset_clears_all_buckets():
    redis = FakeRedis()
    qf = _SignalQualityFilter(redis)
    sig = _Sig()
    from agents.icarus import _quality_pattern
    pat = _quality_pattern(sig)
    for d in _days(3):
        _set_bucket(redis, pat, d, seen=4, approved=0)
    assert qf.should_suppress(sig) is True
    deleted = qf.reset(pat)
    assert deleted >= 6                       # 3 days × (seen+approved)
    assert qf.should_suppress(sig) is False   # state cleared
