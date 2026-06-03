"""
Agent 0.5 — Atlas: Self-Sourced Universe Screener
Titan who carries the world — Atlas scans the entire market so Zeus never runs dry.

Imports ONLY from core.types — never from other agents.

Gated behind settings 'universe_screener_enabled' (default False).
When enabled, Atlas pulls a candidate universe via yfinance, screens by configurable
bands, and emits RawSignals for momentum/volume triggers.

Settings keys (all in config/settings._DEFAULTS):
  universe_screener_enabled:        bool  = False
  universe_market_cap_min:          float = 5e9      # $5B minimum market cap
  universe_market_cap_max:          float = 2e12     # $2T maximum (exclude mega-caps above this)
  universe_min_beta:                float = 0.5      # exclude sleepy low-beta stocks
  universe_min_avg_dollar_volume:   float = 5e6      # $5M/day minimum dollar volume
  universe_min_price:               float = 10.0     # exclude penny stocks
  universe_momentum_window_days:    int   = 20       # lookback for momentum trigger (trading days)
  universe_momentum_threshold:      float = 0.05     # 5% return over window to qualify
  universe_volume_spike_mult:       float = 1.5      # current volume must be > N× 20-day avg
  universe_max_candidates:          int   = 50       # cap before screening to limit yfinance calls
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from core.types import AgentHealth, RawSignal, Severity, SignalCategory

logger = logging.getLogger("atlas")

# Seed universe — broad S&P 500 sample covering large/mid cap across sectors.
# Atlas screens this list down further via the configurable bands.
_SEED_UNIVERSE = [
    # Technology
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "INTC",
    "TSM", "AVGO", "QCOM", "ORCL", "CRM", "ADBE", "NOW", "SNOW", "PLTR",
    # Financials
    "JPM", "GS", "MS", "BAC", "WFC", "BLK", "AXP", "V", "MA", "PYPL",
    # Healthcare
    "JNJ", "UNH", "PFE", "ABBV", "MRK", "LLY", "ABT", "BMY", "AMGN",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG",
    # Consumer
    "COST", "WMT", "HD", "NKE", "MCD", "SBUX", "TGT",
    # Industrials / Defense
    "BA", "CAT", "GE", "RTX", "LMT",
    # ETFs (broad market momentum proxy)
    "SPY", "QQQ", "IWM", "XLK", "XLF",
]


class AtlasAgent:
    """
    Self-sourced universe screener.

    Call `scan()` to get a list of RawSignals for tickers passing all screening
    criteria. Returns an empty list if the screener is disabled in settings.
    """

    def __init__(self):
        self._last_run: Optional[datetime] = None
        logger.info("[ATLAS] Initialised — universe screener (dark, gated).")

    def health(self) -> AgentHealth:
        return AgentHealth.HEALTHY

    def scan(self) -> list[RawSignal]:
        """
        Run a full screening pass.
        Returns RawSignals for qualifying tickers, or [] if disabled.
        """
        from config.settings import load_settings
        s = load_settings()

        if not s.get("universe_screener_enabled", False):
            logger.debug("[ATLAS] Screener disabled — skipping scan.")
            return []

        cfg = _AtlasConfig.from_settings(s)
        candidates = _SEED_UNIVERSE[: cfg.max_candidates]
        signals: list[RawSignal] = []

        for ticker in candidates:
            try:
                sig = self._evaluate_ticker(ticker, cfg)
                if sig is not None:
                    signals.append(sig)
            except Exception as exc:
                logger.debug("[ATLAS] %s evaluation error: %s", ticker, exc)

        self._last_run = datetime.now(timezone.utc)
        logger.info(
            "[ATLAS] Scan complete — %d/%d candidates qualified.",
            len(signals), len(candidates),
        )
        return signals

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evaluate_ticker(self, ticker: str, cfg: "_AtlasConfig") -> Optional[RawSignal]:
        """
        Fetch yfinance data for one ticker and apply all screening filters.
        Returns a RawSignal if the ticker qualifies, None otherwise.
        """
        import yfinance as yf

        info = yf.Ticker(ticker).info
        if not info:
            return None

        # --- Band 1: price floor ---
        price = info.get("currentPrice") or info.get("regularMarketPrice") or 0.0
        if price < cfg.min_price:
            return None

        # --- Band 2: market cap ---
        mktcap = info.get("marketCap") or 0.0
        if mktcap < cfg.market_cap_min or mktcap > cfg.market_cap_max:
            return None

        # --- Band 3: beta ---
        beta = info.get("beta") or 0.0
        if beta < cfg.min_beta:
            return None

        # --- Band 4: dollar volume ---
        avg_volume = info.get("averageVolume") or info.get("averageDailyVolume10Day") or 0
        dollar_volume = avg_volume * price
        if dollar_volume < cfg.min_avg_dollar_volume:
            return None

        # --- Band 5: momentum + volume spike (requires price history) ---
        hist = yf.Ticker(ticker).history(period=f"{cfg.momentum_window_days + 5}d")
        if hist is None or len(hist) < cfg.momentum_window_days:
            return None

        closes = hist["Close"]
        volumes = hist["Volume"]

        period_return = (closes.iloc[-1] - closes.iloc[-cfg.momentum_window_days]) / closes.iloc[-cfg.momentum_window_days]
        if period_return < cfg.momentum_threshold:
            return None

        avg_vol = volumes.iloc[-cfg.momentum_window_days:].mean()
        current_vol = volumes.iloc[-1]
        if avg_vol > 0 and (current_vol / avg_vol) < cfg.volume_spike_mult:
            return None

        # All bands passed — emit a RawSignal
        return self._make_signal(ticker, price, period_return, current_vol / avg_vol if avg_vol > 0 else 1.0)

    def _make_signal(
        self,
        ticker: str,
        price: float,
        momentum: float,
        volume_mult: float,
    ) -> RawSignal:
        return RawSignal(
            signal_id=str(uuid.uuid4()),
            source_url="atlas://universe_screener",
            headline=(
                f"{ticker} screener trigger: +{momentum*100:.1f}% momentum, "
                f"{volume_mult:.1f}× volume spike"
            ),
            summary=(
                f"Atlas universe screener: {ticker} passed all bands. "
                f"Price=${price:.2f}, {momentum*100:.1f}% {_ATR_WINDOW_LABEL}-day return, "
                f"{volume_mult:.1f}× avg volume."
            ),
            published_at=datetime.now(timezone.utc),
            category=SignalCategory.POSITIVE_NEWS,
            severity=Severity.MEDIUM,
            affected_tickers=[ticker],
            supplier=ticker,
            hermes_signal_type="momentum_screener",
        )


# Global label used in signal text — not a magic number, just cosmetic.
_ATR_WINDOW_LABEL = "20"


class _AtlasConfig:
    __slots__ = (
        "market_cap_min", "market_cap_max", "min_beta",
        "min_avg_dollar_volume", "min_price",
        "momentum_window_days", "momentum_threshold",
        "volume_spike_mult", "max_candidates",
    )

    def __init__(
        self,
        market_cap_min: float,
        market_cap_max: float,
        min_beta: float,
        min_avg_dollar_volume: float,
        min_price: float,
        momentum_window_days: int,
        momentum_threshold: float,
        volume_spike_mult: float,
        max_candidates: int,
    ):
        self.market_cap_min        = market_cap_min
        self.market_cap_max        = market_cap_max
        self.min_beta              = min_beta
        self.min_avg_dollar_volume = min_avg_dollar_volume
        self.min_price             = min_price
        self.momentum_window_days  = momentum_window_days
        self.momentum_threshold    = momentum_threshold
        self.volume_spike_mult     = volume_spike_mult
        self.max_candidates        = max_candidates

    @classmethod
    def from_settings(cls, s: dict) -> "_AtlasConfig":
        return cls(
            market_cap_min        = float(s.get("universe_market_cap_min", 5e9)),
            market_cap_max        = float(s.get("universe_market_cap_max", 2e12)),
            min_beta              = float(s.get("universe_min_beta", 0.5)),
            min_avg_dollar_volume = float(s.get("universe_min_avg_dollar_volume", 5e6)),
            min_price             = float(s.get("universe_min_price", 10.0)),
            momentum_window_days  = int(s.get("universe_momentum_window_days", 20)),
            momentum_threshold    = float(s.get("universe_momentum_threshold", 0.05)),
            volume_spike_mult     = float(s.get("universe_volume_spike_mult", 1.5)),
            max_candidates        = int(s.get("universe_max_candidates", 50)),
        )
