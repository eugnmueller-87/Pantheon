"""
Agent 3 — Artemis: Macro Context & Regime Detection
Goddess of the hunt — tracks macro conditions, spots the right moment.
VIX, market regime, sector ETF momentum.
Imports only from core.types — never from other agents.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import yfinance as yf

from core.types import AgentHealth, FilteredSignal, MacroContext, MarketRegime
from core.agent_knowledge import AgentKnowledgeBase

logger = logging.getLogger("artemis")

_VIX_HIGH    = 25.0
_VIX_EXTREME = 35.0
_BULL_THRESH =  0.02
_BEAR_THRESH = -0.03


class ArtemisAgent:
    def __init__(self, cache_ttl_seconds: int = 900):
        self._cache_ttl = cache_ttl_seconds
        self._cached:    Optional[MacroContext] = None
        self._cache_time: Optional[datetime]   = None
        self.kb = AgentKnowledgeBase("artemis")

    def health(self) -> AgentHealth:
        try:
            yf.Ticker("^VIX").history(period="1d")
            return AgentHealth.HEALTHY
        except Exception:
            return AgentHealth.DEGRADED

    def analyze(self, signal: FilteredSignal) -> MacroContext:
        ctx = self._get_context()
        return self._apply_suppression(ctx, signal)

    def _get_context(self) -> MacroContext:
        now = datetime.now(timezone.utc)
        if self._cached and self._cache_time:
            if (now - self._cache_time).total_seconds() < self._cache_ttl:
                return self._cached
        ctx = self._fetch_macro()
        self._cached    = ctx
        self._cache_time = now
        return ctx

    def _fetch_macro(self) -> MacroContext:
        vix          = self._fetch_vix()
        sp500_return = self._fetch_sp500_return()
        regime       = self._classify_regime(sp500_return, vix)
        sectors      = self._fetch_sector_momentum()
        logger.info("[ARTEMIS] regime=%s VIX=%.2f SP500_1m=%.2f%%", regime, vix, sp500_return * 100)
        return MacroContext(
            fetched_at      = datetime.now(timezone.utc),
            regime          = regime,
            vix             = vix,
            sp500_1m_return = sp500_return,
            sector_momentum = sectors,
        )

    def _fetch_vix(self) -> float:
        try:
            hist = yf.Ticker("^VIX").history(period="1d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception as exc:
            logger.warning("[ARTEMIS] VIX fetch failed: %s", exc)
        return 20.0

    def _fetch_sp500_return(self) -> float:
        try:
            hist = yf.Ticker("SPY").history(period="1mo")
            if len(hist) >= 2:
                return (float(hist["Close"].iloc[-1]) - float(hist["Close"].iloc[0])) / float(hist["Close"].iloc[0])
        except Exception as exc:
            logger.warning("[ARTEMIS] SPY fetch failed: %s", exc)
        return 0.0

    def _fetch_sector_momentum(self) -> dict[str, float]:
        etfs = {"tech": "XLK", "energy": "XLE", "financials": "XLF",
                "healthcare": "XLV", "industrials": "XLI", "materials": "XLB"}
        result: dict[str, float] = {}
        for name, ticker in etfs.items():
            try:
                hist = yf.Ticker(ticker).history(period="1mo")
                if len(hist) >= 2:
                    result[name] = round(
                        (float(hist["Close"].iloc[-1]) - float(hist["Close"].iloc[0])) / float(hist["Close"].iloc[0]), 4
                    )
            except Exception:
                result[name] = 0.0
        return result

    @staticmethod
    def _classify_regime(sp500_return: float, vix: float) -> MarketRegime:
        if vix >= _VIX_EXTREME:        return MarketRegime.BEAR
        if sp500_return >= _BULL_THRESH: return MarketRegime.BULL
        if sp500_return <= _BEAR_THRESH: return MarketRegime.BEAR
        return MarketRegime.SIDEWAYS

    def _apply_suppression(self, ctx: MacroContext, signal: FilteredSignal) -> MacroContext:
        from core.types import SignalCategory
        import dataclasses
        out = dataclasses.replace(ctx)
        if signal.category == SignalCategory.POSITIVE_NEWS and out.is_bear and out.is_high_volatility:
            out.suppress        = True
            out.suppress_reason = f"Bear regime + VIX={out.vix:.1f}: suppressing positive signal"
        elif out.vix >= _VIX_EXTREME:
            out.suppress        = True
            out.suppress_reason = f"Extreme VIX={out.vix:.1f}: all signals suppressed"
        return out
