"""
Broker abstraction — the seam between Ares/Argus and a concrete venue.

Ares (execution) and Argus (portfolio/kill switch) talk to a BrokerAdapter
instead of a vendor SDK, so the venue can be swapped (IBKR ↔ cTrader) without
touching the pipeline. Implementations live in core/brokers/:

  - IbkrAdapter      — Interactive Brokers via ib_async (connect-per-call, as before)
  - CTraderAdapter   — cTrader Open API (persistent Twisted session, IC Markets EU)
  - MockBrokerAdapter — in-memory, for tests

Risk rule shared by every adapter: position size is computed from the
configured `default_account_equity`, NEVER from the broker's raw account
balance (paper/demo accounts start with 100k+ and would massively over-size).

Imports only stdlib — this module must stay dependency-free so it can be
imported in tests without any broker SDK installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Errors — Ares maps these onto its queue / reject / error decision
# ---------------------------------------------------------------------------

class BrokerError(RuntimeError):
    """Generic broker failure (not retried, not terminal-classified)."""


class BrokerConnectivityError(BrokerError):
    """Venue unreachable / session not authorized. Safe to queue and retry.

    The message always contains "not connected" so legacy string-based
    classifiers (agents/ares._is_connectivity_error) also recognise it.
    """

    def __init__(self, message: str):
        if "not connected" not in message.lower():
            message = f"{message} (not connected)"
        super().__init__(message)


class BrokerRejectedError(BrokerError):
    """Venue rejected the request (bad symbol, volume, funds...). Never retried."""


class BrokerConfigError(BrokerError):
    """Broker is mis-configured (e.g. missing credentials). Fail closed at startup."""


# ---------------------------------------------------------------------------
# Shared result types
# ---------------------------------------------------------------------------

@dataclass
class BracketResult:
    """Broker-level result of a bracket placement. Ares maps it to TradeResult."""
    order_id:          str
    symbol:            str
    side:              str              # "BUY" | "SELL"
    qty:               float            # instrument units (shares for IBKR, units for cTrader)
    entry_price:       float
    stop_loss_price:   float
    take_profit_price: Optional[float]  # None → no take-profit was placed
    status:            str              = "submitted"
    raw:               dict[str, Any]   = field(default_factory=dict)


@dataclass
class BrokerPosition:
    """One open position. Field names mirror argus.PositionSnapshot."""
    symbol:         str
    side:           str                 # "LONG" | "SHORT"
    qty:            float               # absolute units
    avg_cost:       float
    current_price:  float
    unrealized_pnl: float
    # Optional venue details (cTrader fills them; IBKR leaves the defaults).
    position_id:    Optional[str]   = None
    stop_loss:      Optional[float] = None
    take_profit:    Optional[float] = None


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

@runtime_checkable
class BrokerAdapter(Protocol):
    """What Ares and Argus need from a venue. All methods are synchronous."""

    name: str                 # "ibkr" | "ctrader" | "mock"
    supports_short: bool      # False → Ares stays long-only on this venue

    def connect(self) -> Any:
        """Ensure a usable session; returns an opaque handle. Raises
        BrokerConnectivityError (or a connectivity-flavoured RuntimeError)."""

    def disconnect(self) -> None: ...

    def health(self) -> bool:
        """Cheap liveness probe — True when the venue is reachable."""

    def resolve_symbol(self, ticker: str) -> Optional[str]:
        """Map a pipeline ticker onto this venue's tradable universe, or None."""

    def price_decimals(self, symbol: str) -> int:
        """Decimals to round stop/target prices to."""

    def get_quote(self, symbol: str, exchange: Optional[str] = None) -> float:
        """Current mid (or last/close) price. Raises BrokerError when unpriced."""

    def size_order(self, symbol: str, notional: float, price: float) -> float:
        """Convert an (unlevered) notional in account currency into a valid
        venue quantity in instrument units. Raises BrokerRejectedError if no
        valid size exists within the risk tolerance."""

    def place_bracket(
        self,
        symbol: str,
        side: str,
        qty: float,
        entry: float,
        stop_loss: float,
        take_profit: Optional[float],      # None only on cTrader with trailing + no fixed target
        exchange: Optional[str] = None,
    ) -> BracketResult: ...

    def cancel_all_pending(self) -> int:
        """Cancel every working (unfilled) order. Returns the count cancelled."""

    def positions(self) -> list[BrokerPosition]: ...

    def closing_fills(self) -> dict[str, list[dict]]:
        """symbol -> [{price, qty, time}] of closing executions, oldest-first
        (format consumed by core.shadow_learning.OutcomeResolver)."""

    # Optional trade-management hooks (NOT part of the runtime-checked protocol,
    # so IbkrAdapter needs none of them). Ares feature-detects them with getattr
    # and only uses them on the cTrader path when trailing / pyramiding is on:
    #
    #   modify_position_sltp(position_id, stop_loss, take_profit=None, symbol=None) -> None
    #       Amend the working SL (and TP; None = no TP) of an open position.
    #   leverage_cap_for(symbol) -> float
    #       ESMA leverage cap of the symbol (pyramid combined-notional check).
    #   set_risk_ceiling(pct) -> None
    #       Raise/lower the max risk fraction size_order_risk() accepts (risk ramp).
