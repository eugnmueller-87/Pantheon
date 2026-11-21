"""
cTrader instrument universe, ESMA leverage caps and volume conversion.

Pure functions only (no Twisted, no network) so sizing can be unit-tested.

Universe
--------
The cTrader path trades FX majors + index CFDs, NOT cash stocks (IC Markets EU
does not offer stock CFDs on cTrader). The default logical names below are the
typical IC Markets / cTrader names, but brokers differ (US30 / NAS100 / GER40 /
SPX500 ...). Each logical name carries a list of broker aliases; the adapter
resolves the FIRST alias present in the account's ProtoOASymbolsListRes.
CONFIRM the names against the demo/live symbol list during the smoke test
(scripts/ctrader_smoke_test.py prints what resolved) and override via
settings.json → "ctrader_instruments" if needed.

Sizing
------
cTrader volume is in 0.01 of a unit: protocol 1000 == 10.00 units.

DEFAULT (ctrader.sizing_mode = "risk") — risk-based, see compute_risk_volume():

    risk_amount   = equity * risk_pct                       (1% default)
    loss_per_unit = |entry - stop| * quote_to_account       (account ccy lost per 1.00 unit)
    units         = risk_amount / loss_per_unit  → floored to the venue step grid
    below min lot → use min lot only if its loss <= risk_amount * max_min_lot_risk_overshoot
    notional      > equity * ESMA cap → shrink to the cap (skip if even the min lot exceeds it)

The resulting NOTIONAL can exceed equity (e.g. 1% risk with a 0.25% stop ≈ 4×
equity notional) — that is the point of risk sizing, and the ESMA cap still
bounds it. What is bounded at 1% is the LOSS at the stop, which is what the
Argus 8% drawdown kill is about.

LEGACY (ctrader.sizing_mode = "notional") — compute_volume(): UNLEVERED notional:

    notional_acct = default_account_equity * position_size_pct * sizing_leverage
    units         = notional_acct / (price * quote_to_account_rate)

with sizing_leverage = 1.0 by default (and never above the ESMA retail cap for
the asset class). The Argus 8% drawdown kill assumes this unlevered sizing —
do NOT deploy 1:30 notional.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from core.broker import BrokerConfigError, BrokerRejectedError

# cTrader protocol scaling
VOLUME_PER_UNIT = 100          # protocol volume 100 == 1.00 unit
PRICE_SCALE     = 100_000      # spot bid/ask and relative SL/TP are in 1/100000 of price

# IC Markets EU retail (ESMA) maximum leverage, from their product pages.
ESMA_LEVERAGE_CAPS: dict[str, float] = {
    "fx":        30.0,   # major FX pairs
    "index":     20.0,   # major index CFDs
    "commodity": 10.0,
    "stock":      5.0,
    "crypto":     2.0,
}

# Default universe. Typical IC Markets / cTrader names — MUST be confirmed
# against the live/demo symbol list in the smoke test (aliases tried in order).
DEFAULT_INSTRUMENTS: dict[str, dict[str, Any]] = {
    "EURUSD": {"asset_class": "fx",    "quote_ccy": "USD", "aliases": ["EURUSD"]},
    "GBPUSD": {"asset_class": "fx",    "quote_ccy": "USD", "aliases": ["GBPUSD"]},
    "USDJPY": {"asset_class": "fx",    "quote_ccy": "JPY", "aliases": ["USDJPY"]},
    "US500":  {"asset_class": "index", "quote_ccy": "USD", "aliases": ["US500", "SPX500", "US 500", "USA500"]},
    "USTEC":  {"asset_class": "index", "quote_ccy": "USD", "aliases": ["USTEC", "NAS100", "US100", "USTECH"]},
    "DE40":   {"asset_class": "index", "quote_ccy": "EUR", "aliases": ["DE40", "GER40", "DAX40", "GER30", "DE30"]},
}

# Pipeline tickers that are just another spelling of a universe instrument
# (yfinance-style symbols the research side already uses).
DEFAULT_TICKER_ALIASES: dict[str, str] = {
    "EURUSD=X": "EURUSD", "EUR/USD": "EURUSD",
    "GBPUSD=X": "GBPUSD", "GBP/USD": "GBPUSD",
    "USDJPY=X": "USDJPY", "JPY=X": "USDJPY", "USD/JPY": "USDJPY",
    "^GSPC": "US500", "SPX": "US500",
    "^NDX": "USTEC", "NDX": "USTEC",
    "^GDAXI": "DE40", "DAX": "DE40",
}


@dataclass
class Instrument:
    logical:     str
    asset_class: str
    quote_ccy:   str
    aliases:     list[str] = field(default_factory=list)
    cap_override: Optional[float] = None   # settings may LOWER (never raise) the ESMA cap

    @property
    def leverage_cap(self) -> float:
        cap = leverage_cap(self.asset_class)
        return min(cap, self.cap_override) if self.cap_override else cap


@dataclass
class SymbolSpec:
    """Venue details for one resolved symbol (from ProtoOASymbolByIdRes).

    min/step/max volume are PROTOCOL volumes (0.01 units)."""
    symbol_id:   int
    broker_name: str
    digits:      int
    min_volume:  int
    step_volume: int
    max_volume:  Optional[int] = None
    lot_size:    Optional[int] = None


def leverage_cap(asset_class: str) -> float:
    try:
        return ESMA_LEVERAGE_CAPS[asset_class]
    except KeyError as exc:
        raise BrokerConfigError(f"Unknown asset_class {asset_class!r} — no ESMA leverage cap") from exc


def load_instruments(
    cfg: Optional[dict[str, dict[str, Any]]] = None,
    leverage_caps: Optional[dict[str, float]] = None,
) -> dict[str, Instrument]:
    """Build the universe from settings ("ctrader.instruments") or the defaults.

    leverage_caps: optional per-asset-class caps from settings; they can only
    tighten the ESMA maximum (min() is applied), never loosen it.
    """
    raw = cfg or DEFAULT_INSTRUMENTS
    caps = {k: float(v) for k, v in (leverage_caps or {}).items() if v}
    out: dict[str, Instrument] = {}
    for logical, spec in raw.items():
        name = logical.upper()
        asset_class = spec.get("asset_class", "fx")
        leverage_cap(asset_class)   # validate early
        aliases = [a.upper() for a in spec.get("aliases") or [name]]
        if name not in aliases:
            aliases.insert(0, name)
        out[name] = Instrument(
            logical=name,
            asset_class=asset_class,
            quote_ccy=str(spec.get("quote_ccy", "USD")).upper(),
            aliases=aliases,
            cap_override=caps.get(asset_class),
        )
    return out


def resolve_logical(
    ticker: str,
    instruments: dict[str, Instrument],
    ticker_aliases: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Map a pipeline ticker onto a universe instrument name (or None)."""
    if not ticker:
        return None
    t = ticker.strip().upper()
    if t in instruments:
        return t
    aliases = {k.upper(): v.upper() for k, v in (ticker_aliases or DEFAULT_TICKER_ALIASES).items()}
    mapped = aliases.get(t)
    if mapped and mapped in instruments:
        return mapped
    for inst in instruments.values():
        if t in inst.aliases:
            return inst.logical
    return None


def match_broker_symbols(
    instruments: dict[str, Instrument],
    broker_symbols: dict[str, int],
) -> dict[str, tuple[str, int]]:
    """logical -> (broker_name, symbolId) using the first alias the broker lists.

    broker_symbols: UPPER-CASE broker symbol name -> symbolId.
    Unresolved instruments are simply absent from the result.
    """
    out: dict[str, tuple[str, int]] = {}
    for logical, inst in instruments.items():
        for alias in inst.aliases:
            sid = broker_symbols.get(alias.upper())
            if sid is not None:
                out[logical] = (alias.upper(), int(sid))
                break
    return out


# ---------------------------------------------------------------------------
# Volume conversion
# ---------------------------------------------------------------------------

def units_to_volume(units: float) -> int:
    """Instrument units → protocol volume (floor; never rounds a size UP)."""
    return int(math.floor(units * VOLUME_PER_UNIT + 1e-9))


def volume_to_units(volume: int) -> float:
    return volume / VOLUME_PER_UNIT


def round_volume(volume: int, spec: SymbolSpec) -> int:
    """Floor a protocol volume onto the symbol's step grid (anchored at min)."""
    step = max(1, int(spec.step_volume or 1))
    if volume < spec.min_volume:
        return 0
    steps = (volume - spec.min_volume) // step
    vol = spec.min_volume + steps * step
    if spec.max_volume:
        vol = min(vol, spec.max_volume - (spec.max_volume - spec.min_volume) % step)
    return int(vol)


def effective_leverage(sizing_leverage: float, inst: Instrument) -> float:
    """Leverage actually applied to sizing: >= 1 (unlevered) and <= ESMA cap."""
    return max(1.0, min(float(sizing_leverage), inst.leverage_cap))


def compute_volume(
    *,
    notional_acct: float,
    price: float,
    quote_to_account: float,
    spec: SymbolSpec,
    inst: Instrument,
    equity: float,
    max_min_lot_overshoot: float = 1.5,
) -> int:
    """Turn an (already leverage-adjusted) account-currency notional into a
    valid protocol volume.

    - floors to the venue step grid
    - if the result is below the venue minimum, bumps to the minimum ONLY when
      the minimum's notional is <= max_min_lot_overshoot × intended; otherwise
      raises BrokerRejectedError (we never silently deploy 12× the intent)
    - hard-guards that the final notional never exceeds equity × ESMA cap
    """
    if price <= 0 or quote_to_account <= 0:
        raise BrokerRejectedError(f"Invalid price/conversion for {inst.logical}: {price} / {quote_to_account}")
    if notional_acct <= 0:
        raise BrokerRejectedError(f"Invalid notional for {inst.logical}: {notional_acct}")

    unit_value_acct = price * quote_to_account          # account-ccy value of 1 unit
    units  = notional_acct / unit_value_acct
    volume = round_volume(units_to_volume(units), spec)

    if volume == 0:
        min_notional = volume_to_units(spec.min_volume) * unit_value_acct
        if min_notional <= notional_acct * max_min_lot_overshoot:
            volume = spec.min_volume
        else:
            raise BrokerRejectedError(
                f"{inst.logical}: intended notional {notional_acct:.2f} is below the venue minimum "
                f"({volume_to_units(spec.min_volume):g} units ≈ {min_notional:.2f} account ccy, "
                f"> {max_min_lot_overshoot:g}× intent) — order not placed"
            )

    final_notional = volume_to_units(volume) * unit_value_acct
    legal_max = equity * inst.leverage_cap
    if final_notional > legal_max:
        raise BrokerRejectedError(
            f"{inst.logical}: notional {final_notional:.2f} exceeds ESMA cap "
            f"1:{inst.leverage_cap:g} on equity {equity:.2f}"
        )
    return volume


@dataclass
class RiskSizing:
    """Outcome of compute_risk_volume (all money values in account currency)."""
    volume:             int      # protocol volume (0.01 units)
    units:              float
    risk_budget:        float    # equity × risk_pct
    risk_at_stop:       float    # loss if the stop is hit at the final volume
    notional:           float    # final volume × price × quote_to_account
    loss_per_unit:      float    # account-ccy loss per 1.00 unit at the stop
    equity:             float    # equity the trade was sized on
    used_min_lot:       bool = False
    capped_by_leverage: bool = False

    @property
    def notional_fraction(self) -> float:
        """Notional as a fraction of equity (what ZEUS's budget logic reasons about)."""
        return self.notional / self.equity if self.equity else 0.0


def compute_risk_volume(
    *,
    equity: float,
    risk_pct: float,
    entry: float,
    stop: float,
    quote_to_account: Optional[float],
    spec: SymbolSpec,
    inst: Instrument,
    max_min_lot_risk_overshoot: float = 1.5,
) -> RiskSizing:
    """Risk-based cTrader sizing. Pure — no network.

    Rules (one consistent policy, tested in tests/test_risk_sizing.py):
      1. units = equity × risk_pct / (|entry − stop| × quote_to_account), floored to step.
      2. Below the venue minimum: use the minimum ONLY if the min lot's loss at
         the stop is <= risk budget × max_min_lot_risk_overshoot; else reject.
      3. ESMA: if notional > equity × leverage cap, SHRINK to the largest valid
         volume under the cap (risk goes down, never up). If even the minimum
         lot exceeds the cap, reject.
      4. Missing / invalid conversion rate, price or stop → reject (fail closed).

    Raises BrokerRejectedError with a human-readable reason on every reject.
    """
    name = inst.logical
    if quote_to_account is None or not quote_to_account or quote_to_account <= 0:
        raise BrokerRejectedError(
            f"{name}: no {inst.quote_ccy}→account conversion rate — refusing to size (fail closed)"
        )
    if entry <= 0 or stop <= 0:
        raise BrokerRejectedError(f"{name}: invalid entry/stop {entry} / {stop}")
    if equity <= 0 or risk_pct <= 0:
        raise BrokerRejectedError(f"{name}: invalid equity/risk_pct {equity} / {risk_pct}")
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        raise BrokerRejectedError(f"{name}: stop equals entry — cannot risk-size")

    loss_per_unit = stop_distance * quote_to_account
    unit_value    = entry * quote_to_account
    risk_budget   = equity * risk_pct
    # (1 + 1e-9): absorb float noise like 1.08 − 1.077 = 0.0030000000000001 so an
    # exact-grid size is not floored one step down. Far below any venue step.
    raw_units     = risk_budget / loss_per_unit * (1 + 1e-9)
    volume        = round_volume(units_to_volume(raw_units), spec)
    used_min_lot  = False

    if volume == 0:
        min_units     = volume_to_units(spec.min_volume)
        min_lot_risk  = min_units * loss_per_unit
        allowed       = risk_budget * max_min_lot_risk_overshoot
        if min_lot_risk <= allowed:
            volume, used_min_lot = spec.min_volume, True
        else:
            raise BrokerRejectedError(
                f"{name}: risk-sized {raw_units:g} units is below the venue minimum "
                f"({min_units:g} units). Intended risk {risk_budget:.2f}, min-lot risk "
                f"{min_lot_risk:.2f} > tolerance {max_min_lot_risk_overshoot:g}× "
                f"(= {allowed:.2f}) — order not placed"
            )

    capped    = False
    legal_max = equity * inst.leverage_cap
    if volume_to_units(volume) * unit_value > legal_max:
        cap_volume = round_volume(units_to_volume(legal_max / unit_value), spec)
        if cap_volume == 0:
            raise BrokerRejectedError(
                f"{name}: even the minimum lot ({volume_to_units(spec.min_volume):g} units ≈ "
                f"{volume_to_units(spec.min_volume) * unit_value:.2f}) exceeds the ESMA cap "
                f"1:{inst.leverage_cap:g} on equity {equity:.2f} — order not placed"
            )
        volume, capped = cap_volume, True

    units = volume_to_units(volume)
    return RiskSizing(
        volume=volume, units=units, risk_budget=risk_budget,
        risk_at_stop=units * loss_per_unit, notional=units * unit_value,
        loss_per_unit=loss_per_unit, equity=equity, used_min_lot=used_min_lot,
        capped_by_leverage=capped,
    )


def estimate_risk_notional_fraction(risk_pct: float, entry: float, stop_distance: float) -> float:
    """Notional / equity implied by risk sizing, BEFORE venue rounding.

    notional/equity = risk_pct × entry / stop_distance (the FX conversion
    cancels). Used by ZEUS to log/check what a risk-sized trade deploys without
    a live venue connection.
    """
    if entry <= 0 or stop_distance <= 0:
        return 0.0
    return risk_pct * entry / stop_distance


def relative_distance(entry: float, level: float, digits: int) -> int:
    """|entry - level| in 1/100000 price units, snapped to the symbol's tick."""
    tick = 10 ** max(0, 5 - int(digits))
    raw = abs(entry - level) * PRICE_SCALE
    return max(tick, int(round(raw / tick)) * tick)
