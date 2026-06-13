"""
Quality gate — ZEUS bull/bear adversarial debate sub-step.

Verifies the debate logic in isolation, without booting a full
ZeusOrchestrator (no watchdog, no 8 agents, no network):

  - Bull is called first, Bear second, and Bear's prompt contains the bull's case
  - Debate output is folded into the auditable reasoning text
  - use_debate=False short-circuits to single-shot (no LLM calls)
  - A failing debate call degrades gracefully — ZEUS proceeds without it
  - Token usage is recorded per side, tagged {ticker}:bull / {ticker}:bear

These run with a mocked Claude client. No ANTHROPIC_API_KEY needed.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from agents.zeus import ZeusConfig, ZeusOrchestrator
from core.types import (
    FilteredSignal,
    MacroContext,
    MarketRegime,
    RawSignal,
    Severity,
    SignalCategory,
    SizedSignal,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

def _sized(ticker: str = "AAPL") -> SizedSignal:
    raw = RawSignal(
        signal_id=str(uuid.uuid4()),
        source_url="https://mock",
        headline="ACME beats earnings by 20%",
        summary="Strong quarter",
        published_at=datetime.now(timezone.utc),
        category=SignalCategory.EARNINGS_SURPRISE,
        severity=Severity.HIGH,
        affected_tickers=[ticker],
        supplier="ACME Corp",
    )
    filtered = FilteredSignal(original=raw, compliance_score=0.9, notes=["clean"])
    macro = MacroContext(
        fetched_at=datetime.now(timezone.utc),
        regime=MarketRegime.BULL,
        vix=18.0,
        sp500_1m_return=0.03,
    )
    return SizedSignal(
        original=filtered, macro=macro,
        confidence=0.72, position_size_pct=0.025,
    )


def _zeus(config: ZeusConfig, claude: MagicMock) -> ZeusOrchestrator:
    """
    Build a ZeusOrchestrator surface without running __init__ (which would
    start the watchdog and construct all 8 agents). We only need the
    attributes the debate methods touch.
    """
    z = ZeusOrchestrator.__new__(ZeusOrchestrator)
    z.config = config
    z._claude = claude
    # _record_token_usage writes to Supabase; stub it so the debate path is pure.
    z._record_token_usage = MagicMock()
    return z


def _claude_returning(*texts: str) -> MagicMock:
    """A Claude mock whose successive .messages.create() calls return given texts."""
    client = MagicMock()
    responses = []
    for t in texts:
        resp = MagicMock()
        resp.content = [MagicMock(text=t)]
        resp.usage = MagicMock(input_tokens=100, output_tokens=50)
        responses.append(resp)
    client.messages.create.side_effect = responses
    return client


_MACRO = MacroContext(
    fetched_at=datetime.now(timezone.utc),
    regime=MarketRegime.BULL, vix=18.0, sp500_1m_return=0.03,
)


# ── Debate runs bull then bear, bear sees bull ────────────────────────────────

class TestDebateMechanics:
    def test_bull_called_first_bear_second(self):
        claude = _claude_returning("BULL ARGUMENT TEXT", "BEAR ARGUMENT TEXT")
        z = _zeus(ZeusConfig(use_debate=True), claude)

        bull, bear = z._run_debate(_sized(), _MACRO, kb_context="some KB")

        assert bull == "BULL ARGUMENT TEXT"
        assert bear == "BEAR ARGUMENT TEXT"
        assert claude.messages.create.call_count == 2

    def test_bear_prompt_contains_bull_case(self):
        claude = _claude_returning("THE-BULL-CASE-MARKER", "bear text")
        z = _zeus(ZeusConfig(use_debate=True), claude)

        z._run_debate(_sized(), _MACRO, kb_context="kb")

        # Second call (bear) must include the bull's argument so it's adversarial.
        bear_prompt = claude.messages.create.call_args_list[1].kwargs["messages"][0]["content"]
        assert "THE-BULL-CASE-MARKER" in bear_prompt
        assert "BEAR" in bear_prompt

    def test_token_usage_tagged_per_side(self):
        claude = _claude_returning("bull", "bear")
        z = _zeus(ZeusConfig(use_debate=True), claude)

        z._run_debate(_sized(ticker="NVDA"), _MACRO, kb_context="kb")

        tagged = [c.args[1] for c in z._record_token_usage.call_args_list]
        assert "NVDA:bull" in tagged
        assert "NVDA:bear" in tagged


# ── Master switch ─────────────────────────────────────────────────────────────

class TestDebateDisabled:
    def test_use_debate_false_makes_no_calls(self):
        claude = _claude_returning("should-not-be-used")
        z = _zeus(ZeusConfig(use_debate=False), claude)

        bull, bear = z._run_debate(_sized(), _MACRO, kb_context="kb")

        assert bull == ""
        assert bear == ""
        assert claude.messages.create.call_count == 0


# ── Graceful degradation ──────────────────────────────────────────────────────

class TestDebateFailure:
    def test_bull_failure_returns_empty(self):
        claude = MagicMock()
        claude.messages.create.side_effect = RuntimeError("API down")
        z = _zeus(ZeusConfig(use_debate=True), claude)

        bull, bear = z._run_debate(_sized(), _MACRO, kb_context="kb")

        # Bull failed → debate yields nothing, ZEUS proceeds single-shot.
        assert bull == ""
        assert bear == ""

    def test_bear_failure_keeps_bull(self):
        client = MagicMock()
        ok = MagicMock()
        ok.content = [MagicMock(text="BULL ONLY")]
        ok.usage = MagicMock(input_tokens=100, output_tokens=50)
        client.messages.create.side_effect = [ok, RuntimeError("bear failed")]
        z = _zeus(ZeusConfig(use_debate=True), client)

        bull, bear = z._run_debate(_sized(), _MACRO, kb_context="kb")

        assert bull == "BULL ONLY"
        assert bear == ""


# ── Director prompt rendering ─────────────────────────────────────────────────

class TestDirectorPromptIntegration:
    def test_debate_section_rendered_when_present(self):
        claude = _claude_returning()  # not used here
        z = _zeus(ZeusConfig(use_debate=True), claude)
        # _build_director_prompt reads argus/seniority state — stub the minimum.
        z.argus = MagicMock()
        z.argus.open_position_count.return_value = 0
        z.argus.portfolio_state.return_value = MagicMock(
            current_drawdown_pct=0.0, total_equity=4000.0,
        )
        z._seniority_report = None

        prompt = z._build_director_prompt(
            _sized(), _MACRO, kb_context="kb",
            bull_case="BULLMARKER", bear_case="BEARMARKER",
        )

        assert "ANALYST DEBATE" in prompt
        assert "BULLMARKER" in prompt
        assert "BEARMARKER" in prompt
        assert "DEBATE RESOLUTION" in prompt  # the 6th governance question

    def test_no_debate_section_when_absent(self):
        claude = _claude_returning()
        z = _zeus(ZeusConfig(use_debate=True), claude)
        z.argus = MagicMock()
        z.argus.open_position_count.return_value = 0
        z.argus.portfolio_state.return_value = MagicMock(
            current_drawdown_pct=0.0, total_equity=4000.0,
        )
        z._seniority_report = None

        prompt = z._build_director_prompt(
            _sized(), _MACRO, kb_context="kb",
            bull_case="", bear_case="",
        )

        assert "ANALYST DEBATE" not in prompt
