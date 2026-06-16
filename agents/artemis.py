"""
Agent 3 — Artemis: Macro Context & Regime Detection
Goddess of the hunt — tracks macro conditions, spots the right moment.
VIX, market regime, sector ETF momentum.
Imports only from core.types — never from other agents.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import yfinance as yf

from core.agent_knowledge import AgentKnowledgeBase
from core.types import (
    AgentHealth,
    FilteredSignal,
    MacroContext,
    MacroFetchError,
    MarketRegime,
)

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
        self._last_macro_alert: Optional[datetime] = None  # rate-limit fetch alerts
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
        try:
            vix = self._fetch_vix()
        except MacroFetchError as exc:
            # Fail CLOSED: never operate on a fabricated benign VIX. Return a
            # suppressed UNKNOWN context with the -1.0 sentinel so downstream
            # (ZEUS) renders "VIX UNAVAILABLE" and treats macro as not-benign.
            logger.error("[ARTEMIS] macro data unavailable (%s) — suppressing", exc)
            self._alert_macro_unavailable(str(exc))
            return MacroContext(
                fetched_at      = datetime.now(timezone.utc),
                regime          = MarketRegime.UNKNOWN,
                vix             = -1.0,
                sp500_1m_return = 0.0,
                sector_momentum = {},
                suppress        = True,
                suppress_reason = "macro data unavailable",
            )
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
        """Fetch live VIX. Raises MacroFetchError on failure or empty data —
        callers fail closed rather than substitute a benign default."""
        try:
            hist = yf.Ticker("^VIX").history(period="1d")
        except Exception as exc:
            raise MacroFetchError(f"VIX fetch failed: {exc}") from exc
        if hist.empty:
            raise MacroFetchError("VIX fetch returned no data")
        return float(hist["Close"].iloc[-1])

    def _alert_macro_unavailable(self, detail: str) -> None:
        """Emit at most one Telegram alert per hour on persistent macro-fetch
        failure (best-effort; no-op if Telegram env vars aren't set)."""
        now = datetime.now(timezone.utc)
        if self._last_macro_alert and (now - self._last_macro_alert).total_seconds() < 3600:
            return
        self._last_macro_alert = now
        msg = f"⚠️ ARTEMIS: macro data unavailable — {detail}. Operating suppressed."
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat  = os.getenv("TELEGRAM_CHAT_ID")
        if not (token and chat):
            logger.info("[ARTEMIS] Alert (no Telegram): %s", msg)
            return
        try:
            import requests
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": msg}, timeout=5,
            )
        except Exception as exc:
            logger.warning("[ARTEMIS] Telegram alert failed: %s", exc)

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
                    base = float(hist["Close"].iloc[0])
                    if base != 0:
                        result[name] = round(
                            (float(hist["Close"].iloc[-1]) - base) / base, 4
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
        import dataclasses

        from core.types import SignalCategory
        out = dataclasses.replace(ctx)
        if signal.category == SignalCategory.POSITIVE_NEWS and out.is_bear and out.is_high_volatility:
            out.suppress        = True
            out.suppress_reason = f"Bear regime + VIX={out.vix:.1f}: suppressing positive signal"
        elif out.vix >= _VIX_EXTREME:
            out.suppress        = True
            out.suppress_reason = f"Extreme VIX={out.vix:.1f}: all signals suppressed"
        return out
