"""
core/types.py — Single source of truth for all shared data contracts.

Rules:
  - Every inter-agent data structure lives here and ONLY here.
  - Agents import from core.types, never from each other.
  - ZEUS imports from core.types + individual agents (for method calls only).
  - Adding a field here is the only way to change the pipeline contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class SignalCategory(Enum):
    SUPPLIER_DISRUPTION = "supplier_disruption"
    POSITIVE_NEWS       = "positive_news"
    EARNINGS_SURPRISE   = "earnings_surprise"
    REGULATORY_ACTION   = "regulatory_action"
    MACRO_SHIFT         = "macro_shift"
    NEUTRAL             = "neutral"


class Severity(Enum):
    LOW      = 1
    MEDIUM   = 2
    HIGH     = 3
    CRITICAL = 4


class MarketRegime(str, Enum):
    BULL     = "bull"
    BEAR     = "bear"
    SIDEWAYS = "sideways"
    UNKNOWN  = "unknown"


class PipelineStatus(Enum):
    RUNNING  = "running"
    HALTED   = "halted"
    PAUSED   = "paused"
    SHUTDOWN = "shutdown"


class AgentHealth(Enum):
    HEALTHY   = "healthy"
    DEGRADED  = "degraded"   # errors but still running
    FAILED    = "failed"     # not responding


# ---------------------------------------------------------------------------
# Stage 1 output — Icarus
# ---------------------------------------------------------------------------

@dataclass
class RawSignal:
    signal_id:       str
    source_url:      str
    headline:        str
    summary:         str
    published_at:    datetime
    category:        SignalCategory
    severity:        Severity
    affected_tickers: list[str]        = field(default_factory=list)
    raw_text:        str               = ""
    supplier:        str               = ""
    hermes_signal_type: str            = ""


# ---------------------------------------------------------------------------
# Stage 2 output — Hades
# ---------------------------------------------------------------------------

@dataclass
class FilteredSignal:
    original:         RawSignal
    compliance_score: float              # 0.0 (risky) → 1.0 (clean)
    esg_flag:         bool               = False
    ofac_flag:        bool               = False
    downgraded:       bool               = False
    notes:            list[str]          = field(default_factory=list)

    # Convenience pass-throughs so downstream agents don't touch .original
    @property
    def signal_id(self) -> str:           return self.original.signal_id
    @property
    def affected_tickers(self) -> list[str]: return self.original.affected_tickers
    @property
    def category(self) -> SignalCategory: return self.original.category
    @property
    def severity(self) -> Severity:       return self.original.severity
    @property
    def headline(self) -> str:            return self.original.headline
    @property
    def supplier(self) -> str:            return self.original.supplier
    @property
    def raw_text(self) -> str:            return self.original.raw_text
    @property
    def published_at(self) -> datetime:   return self.original.published_at


# ---------------------------------------------------------------------------
# Stage 3 output — Trend
# ---------------------------------------------------------------------------

@dataclass
class MacroContext:
    fetched_at:       datetime
    regime:           MarketRegime
    vix:              float
    sp500_1m_return:  float
    sector_momentum:  dict[str, float]   = field(default_factory=dict)
    suppress:         bool               = False
    suppress_reason:  Optional[str]      = None

    @property
    def is_high_volatility(self) -> bool: return self.vix > 25.0
    @property
    def is_bear(self) -> bool:            return self.regime == MarketRegime.BEAR
    @property
    def is_bull(self) -> bool:            return self.regime == MarketRegime.BULL


# ---------------------------------------------------------------------------
# Stage 4 output — Pattern
# ---------------------------------------------------------------------------

@dataclass
class SizedSignal:
    original:           FilteredSignal
    macro:              MacroContext
    confidence:         float            # 0.0 → 1.0
    position_size_pct:  float            # % of portfolio to allocate
    skip:               bool             = False
    skip_reason:        Optional[str]    = None

    @property
    def signal_id(self) -> str:            return self.original.signal_id
    @property
    def affected_tickers(self) -> list[str]: return self.original.affected_tickers
    @property
    def category(self) -> SignalCategory:  return self.original.category
    @property
    def severity(self) -> Severity:        return self.original.severity
    @property
    def supplier(self) -> str:             return self.original.supplier
    @property
    def headline(self) -> str:             return self.original.headline


# ---------------------------------------------------------------------------
# Stage 5 output — Execution
# ---------------------------------------------------------------------------

@dataclass
class TradeResult:
    order_id:          str
    symbol:            str
    side:              str               # "BUY" | "SELL"
    fill_price:        Optional[float]
    qty:               float
    stop_loss_price:   Optional[float]   = None
    take_profit_price: Optional[float]   = None
    pnl_pct:           Optional[float]   = None  # populated later by Monitor
    status:            str               = "submitted"


# ---------------------------------------------------------------------------
# ZEUS — Full decision trace written to KB for every pipeline run
# ---------------------------------------------------------------------------

@dataclass
class DecisionTrace:
    """
    ZEUS writes one of these for every signal processed.
    Stored in the knowledge base — used to teach agents over time.
    """
    trace_id:          str
    signal_id:         str
    timestamp:         datetime
    headline:          str
    supplier:          str
    category:          str
    severity:          str

    # Stage outcomes
    hades_passed:      bool
    hades_notes:       list[str]         = field(default_factory=list)
    trend_suppressed:  bool              = False
    trend_regime:      str               = ""
    trend_vix:         float             = 0.0
    pattern_confidence: float            = 0.0
    pattern_size_pct:  float             = 0.0

    # ZEUS reasoning (LLM-generated)
    zeus_reasoning:    str               = ""
    zeus_approved:     bool              = False
    zeus_override:     bool              = False  # True if ZEUS overrode Pattern sizing
    zeus_override_reason: str            = ""

    # Outcome
    trade_placed:      bool              = False
    symbol:            str               = ""
    side:              str               = ""
    fill_price:        Optional[float]   = None
    pnl_pct:           Optional[float]   = None  # filled in by Monitor later
    killed_at_stage:   Optional[str]     = None
    kill_reason:       Optional[str]     = None


# ---------------------------------------------------------------------------
# Agent health report — used by Watchdog
# ---------------------------------------------------------------------------

@dataclass
class HealthReport:
    agent_name: str
    status:     AgentHealth
    message:    str              = ""
    checked_at: datetime         = field(default_factory=lambda: datetime.now(timezone.utc))
    error_count: int             = 0
