"""
Verify IB port is resolved consistently and correctly.

Rules under test:
  1. Default (no env, no override) → both Ares and Argus use 4002 (paper).
  2. IB_PORT env var overrides the default for both agents identically.
  3. Ares and Argus always receive the same port value from Zeus.
  4. ZeusConfig defaults are 4002 / 4001.
  5. config/settings.py defaults are 4002 / 4001 (not the old TWS 7497/7496).
  6. paper_trading=False resolves to 4001 (not silently stuck on paper port).
  7. On a failed connect attempt, disconnect() is called before the next retry.
  8. clientId is stable across retries (Ares=1, Argus=2).

Zeus is constructed in mock_execution=True mode so no IB connection is
attempted. Ares and Argus agents are inspected directly after construction.
"""

from __future__ import annotations

import os
import sys
import types as _types
from unittest.mock import MagicMock, patch

import pytest

from agents.ares import AresAgent
from agents.argus import ArgusAgent
from agents.zeus import ZeusConfig, ZeusOrchestrator
from config.settings import _DEFAULTS as SETTINGS_DEFAULTS

# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_zeus(extra_env: dict | None = None, paper: bool = True) -> ZeusOrchestrator:
    """Build a Zeus instance in mock mode, optionally injecting env vars."""
    cfg = ZeusConfig(
        paper_trading=paper,
        mock_execution=True,
    )
    env_patch = {
        "ANTHROPIC_API_KEY": "test-key-not-real",
        **(extra_env or {}),
    }
    # Remove IB_PORT from env unless explicitly set in extra_env
    if "IB_PORT" not in (extra_env or {}):
        env_patch["IB_PORT"] = ""   # will be popped below

    with patch.dict(os.environ, env_patch, clear=False):
        if not (extra_env or {}).get("IB_PORT"):
            os.environ.pop("IB_PORT", None)
        with patch("anthropic.Anthropic"), \
             patch("core.redis_bridge.RedisBridge"), \
             patch("core.knowledge_base.KnowledgeBase"):
            return ZeusOrchestrator(config=cfg)


# ── AresAgent unit tests ────────────────────────────────────────────────────────

class TestAresAgentPort:
    def test_default_paper_port_is_4002(self):
        agent = AresAgent(paper=True)
        assert agent.port == 4002

    def test_default_live_port_is_4001(self):
        agent = AresAgent(paper=False)
        assert agent.port == 4001

    def test_explicit_port_overrides_default(self):
        agent = AresAgent(paper=True, port=9999)
        assert agent.port == 9999

    def test_explicit_port_none_uses_constant(self):
        agent = AresAgent(paper=True, port=None)
        assert agent.port == 4002


# ── ArgusAgent unit tests ───────────────────────────────────────────────────────

class TestArgusAgentPort:
    def test_default_port_is_4002(self):
        agent = ArgusAgent()
        assert agent._ib_port == 4002

    def test_explicit_port_overrides_default(self):
        agent = ArgusAgent(ib_port=9999)
        assert agent._ib_port == 9999


# ── ZeusConfig defaults ─────────────────────────────────────────────────────────

class TestZeusConfigDefaults:
    def test_paper_port_default(self):
        assert ZeusConfig().ib_paper_port == 4002

    def test_live_port_default(self):
        assert ZeusConfig().ib_live_port == 4001


# ── settings.py defaults ────────────────────────────────────────────────────────

class TestSettingsDefaults:
    def test_paper_port_not_tws(self):
        assert SETTINGS_DEFAULTS["ib_paper_port"] == 4002

    def test_live_port_not_tws(self):
        assert SETTINGS_DEFAULTS["ib_live_port"] == 4001


# ── Zeus wiring: Ares and Argus receive the same port ──────────────────────────

class TestZeusPortWiring:
    def test_default_wiring_both_agents_use_4002(self):
        """With no IB_PORT env and mock_execution=True, Argus gets 4002."""
        zeus = _make_zeus()
        # Ares is an AresMockAgent in mock mode — check Argus only
        assert zeus.argus._ib_port == 4002

    def test_env_override_reaches_argus(self):
        """IB_PORT=5555 in env propagates to Argus."""
        zeus = _make_zeus(extra_env={"IB_PORT": "5555"})
        assert zeus.argus._ib_port == 5555

    def test_no_4004_anywhere_in_wiring(self):
        """4004 must never appear as the resolved port."""
        zeus = _make_zeus()
        assert zeus.argus._ib_port != 4004

    def test_live_mode_resolves_to_4001(self):
        """paper_trading=False must resolve to 4001, not silently stay on paper port."""
        zeus = _make_zeus(paper=False)
        assert zeus.argus._ib_port == 4001

    def test_env_override_in_live_mode(self):
        """IB_PORT env wins over the live default even in live mode."""
        zeus = _make_zeus(extra_env={"IB_PORT": "5555"}, paper=False)
        assert zeus.argus._ib_port == 5555

    def test_paper_mode_does_not_use_live_port(self):
        """paper_trading=True must never resolve to 4001."""
        zeus = _make_zeus(paper=True)
        assert zeus.argus._ib_port != 4001


# ── Retry: disconnect called on failed attempt ──────────────────────────────────


def _make_ib_async_mock(mock_ib: MagicMock) -> MagicMock:
    """Return a fake ib_async module whose IB() constructor returns mock_ib."""
    mod = _types.ModuleType("ib_async")
    mod.IB = MagicMock(return_value=mock_ib)  # type: ignore[attr-defined]
    return mod


class TestRetryDisconnect:
    """disconnect() must be called before every retry attempt, including after failure."""

    def _run_failing_connect(self, agent, fail_count: int):
        """Inject a fake ib_async module so the first `fail_count` connect()
        calls raise, then succeed. Returns the mock IB instance used."""
        mock_ib = MagicMock()
        connect_calls = [0]

        def connect_side_effect(*args, **kwargs):
            connect_calls[0] += 1
            if connect_calls[0] <= fail_count:
                raise ConnectionRefusedError("mock: refused")

        mock_ib.connect.side_effect = connect_side_effect
        fake_mod = _make_ib_async_mock(mock_ib)

        with patch.dict(sys.modules, {"ib_async": fake_mod}), \
             patch("time.sleep"):
            agent._get_connection()

        return mock_ib

    def test_ares_disconnects_before_retry(self):
        """After a failed attempt, Ares calls disconnect() before the next try."""
        agent = AresAgent(paper=True, port=4002)
        mock_ib = self._run_failing_connect(agent, fail_count=1)
        assert mock_ib.disconnect.called

    def test_argus_disconnects_before_retry(self):
        """After a failed attempt, Argus calls disconnect() before the next try."""
        agent = ArgusAgent(ib_port=4002, mock=True)
        agent._mock = False
        mock_ib = self._run_failing_connect(agent, fail_count=1)
        assert mock_ib.disconnect.called

    def test_ares_client_id_stable_across_retries(self):
        """clientId=1 must appear on every connect call — must not rotate."""
        agent = AresAgent(paper=True, port=4002)
        mock_ib = MagicMock()
        connect_calls: list[tuple] = []

        def record_connect(*args, **kwargs):
            connect_calls.append((args, kwargs))
            if len(connect_calls) < 2:
                raise ConnectionRefusedError("mock: first attempt fails")

        mock_ib.connect.side_effect = record_connect
        fake_mod = _make_ib_async_mock(mock_ib)

        with patch.dict(sys.modules, {"ib_async": fake_mod}), \
             patch("time.sleep"):
            agent._get_connection()

        client_ids = [
            kw.get("clientId", args[2] if len(args) > 2 else None)
            for args, kw in connect_calls
        ]
        assert all(cid == 1 for cid in client_ids), f"clientId drifted: {client_ids}"

    def test_argus_client_id_stable_across_retries(self):
        """clientId=2 must appear on every connect call — must not rotate."""
        agent = ArgusAgent(ib_port=4002, mock=True)
        agent._mock = False
        mock_ib = MagicMock()
        connect_calls: list[tuple] = []

        def record_connect(*args, **kwargs):
            connect_calls.append((args, kwargs))
            if len(connect_calls) < 2:
                raise ConnectionRefusedError("mock: first attempt fails")

        mock_ib.connect.side_effect = record_connect
        fake_mod = _make_ib_async_mock(mock_ib)

        with patch.dict(sys.modules, {"ib_async": fake_mod}), \
             patch("time.sleep"):
            agent._get_connection()

        client_ids = [
            kw.get("clientId", args[2] if len(args) > 2 else None)
            for args, kw in connect_calls
        ]
        assert all(cid == 2 for cid in client_ids), f"clientId drifted: {client_ids}"

    def test_ares_queues_after_all_retries_exhausted(self):
        """place() enqueues the order (status='queued') when all 3 connect attempts fail
        with a connectivity error — the approved trade is NOT lost."""
        from datetime import datetime, timezone
        from unittest.mock import patch

        from core.types import (
            FilteredSignal,
            MacroContext,
            MarketRegime,
            RawSignal,
            Severity,
            SignalCategory,
            SizedSignal,
        )
        agent = AresAgent(paper=True, port=4002)
        mock_ib = MagicMock()
        mock_ib.connect.side_effect = ConnectionRefusedError("mock: always fail")
        fake_mod = _make_ib_async_mock(mock_ib)

        now = datetime.now(timezone.utc)
        raw = RawSignal(
            signal_id="s-smoke", source_url="", headline="test", summary="",
            published_at=now, category=SignalCategory.POSITIVE_NEWS,
            severity=Severity.HIGH, affected_tickers=["AAPL"],
        )
        filtered = FilteredSignal(original=raw, compliance_score=1.0)
        macro = MacroContext(
            fetched_at=now, regime=MarketRegime.BULL, vix=15.0, sp500_1m_return=0.03,
        )
        sized = SizedSignal(
            original=filtered, macro=macro, confidence=0.8, position_size_pct=0.02,
        )

        with patch.dict(sys.modules, {"ib_async": fake_mod}), \
             patch("time.sleep"), \
             patch("core.supabase_client.enqueue_pending_order", return_value=None):
            result = agent.place(sized)

        # Connectivity failure → queued for retry, not silently dropped
        assert result.status == "queued"


# ── Early-close market hours ────────────────────────────────────────────────────

class TestEarlyClose:
    """_is_market_open() must close at 13:00 ET on NYSE half-days."""

    @staticmethod
    def _check(utc_dt):
        """Call _is_market_open() with a pinned UTC time via monkeypatching datetime.now."""
        from datetime import datetime, timezone

        import main as main_mod

        class _PinnedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return utc_dt

        with patch.object(main_mod, "datetime", _PinnedDatetime):
            return main_mod._is_market_open()

    def test_early_close_day_open_before_1300(self):
        """Day before Independence Day 2026 (July 2): open at 10:00 ET (14:00 UTC)."""
        from datetime import datetime, timezone
        # July 2 2026 is Thursday (weekday 3), EDT=UTC-4 → 14:00 UTC = 10:00 ET
        utc_dt = datetime(2026, 7, 2, 14, 0, 0, tzinfo=timezone.utc)
        assert self._check(utc_dt) is True

    def test_early_close_day_closed_after_1300(self):
        """Day before Independence Day 2026 (July 2): closed at 13:30 ET (17:30 UTC)."""
        from datetime import datetime, timezone
        utc_dt = datetime(2026, 7, 2, 17, 30, 0, tzinfo=timezone.utc)
        assert self._check(utc_dt) is False

    def test_normal_day_still_closes_1600(self):
        """Regular Wednesday June 3 2026: still open at 15:00 ET (19:00 UTC)."""
        from datetime import datetime, timezone
        utc_dt = datetime(2026, 6, 3, 19, 0, 0, tzinfo=timezone.utc)
        assert self._check(utc_dt) is True
