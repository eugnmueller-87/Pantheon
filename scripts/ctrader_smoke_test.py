"""
cTrader Open API smoke test — proves credentials, auth and symbol mapping
against a real (DEMO by default) cTrader account, then places and cancels one
tiny pending order.

Usage
-----
    # credentials from env / .env (never pass them on the command line)
    python scripts/ctrader_smoke_test.py
    CTRADER_SMOKE_SYMBOL=DE40 python scripts/ctrader_smoke_test.py
    python scripts/ctrader_smoke_test.py --no-order        # auth + symbols only

What it does
------------
1. Reads CTRADER_CLIENT_ID / _CLIENT_SECRET / _ACCESS_TOKEN / _REFRESH_TOKEN /
   _ACCOUNT_ID / _ENV from the environment. Refuses to run if any is missing.
2. Refuses CTRADER_ENV=live unless CTRADER_SMOKE_ALLOW_LIVE=yes.
3. Connects to demo|live.ctraderapi.com:5035, app-auth → account-auth.
4. Prints which of our logical instruments (EURUSD, ..., DE40) resolved to
   which broker symbol, with min volume / step — confirm the aliases here.
5. Places ONE pending BUY LIMIT at the venue MINIMUM volume, ~20% below the
   market (cannot fill), then cancels it and verifies it is gone.
   No order is placed if auth, symbol lookup or pricing fails.

Exit codes
----------
0 success · 1 runtime failure · 2 missing/invalid config · 3 live refused
Diagnostics go to stderr; secrets are never printed.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


SETUP_HELP = """\
cTrader smoke test needs these environment variables (see .env.example):
  CTRADER_CLIENT_ID       app client id    (https://openapi.ctrader.com → Applications; app must be approved)
  CTRADER_CLIENT_SECRET   app client secret
  CTRADER_ACCESS_TOKEN    OAuth token, scope=trading (OAuth flow or the app's Playground)
  CTRADER_REFRESH_TOKEN   matching refresh token
  CTRADER_ACCOUNT_ID      ctidTraderAccountId of the DEMO account (not the login number)
  CTRADER_ENV             demo (default) | live
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="cTrader Open API smoke test (demo by default)")
    parser.add_argument("--no-order", action="store_true", help="only authenticate and list symbols")
    parser.add_argument("--symbol", default=os.getenv("CTRADER_SMOKE_SYMBOL", "EURUSD"),
                        help="logical instrument for the test order (default EURUSD)")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from core.broker import BrokerConfigError, BrokerError
    from core.brokers.ctrader import CTraderAdapter, CTraderCredentials
    from core.brokers.ctrader_instruments import volume_to_units

    try:
        creds = CTraderCredentials.from_env()
    except BrokerConfigError as exc:
        _err(f"ERROR: {exc}\n\n{SETUP_HELP}")
        return 2

    if creds.env == "live" and os.getenv("CTRADER_SMOKE_ALLOW_LIVE", "").strip().lower() != "yes":
        _err("REFUSED: CTRADER_ENV=live. The smoke test is demo-only by default.\n"
             "Set CTRADER_SMOKE_ALLOW_LIVE=yes to run it against a LIVE account (it places a real order).")
        return 3

    try:
        from config.settings import load_settings
        cfg = load_settings().get("ctrader", {}) or {}
    except Exception:
        cfg = {}

    adapter = CTraderAdapter(
        creds,
        account_equity=1_000.0,   # only used for the ESMA legal-cap guard; this script sizes at venue minimum
        instruments=cfg.get("instruments"),
        leverage_caps=cfg.get("leverage_caps"),
        ticker_aliases=cfg.get("ticker_aliases"),
        account_currency=cfg.get("account_currency", "EUR"),
        connect_timeout=20.0,
        quote_timeout=10.0,
    )
    print(f"Connecting to cTrader {creds.env.upper()} {creds.host}:5035 ...")

    try:
        adapter.connect()
    except BrokerError as exc:
        _err(f"ERROR: authentication/connection failed — {exc}\nNo order placed.")
        adapter.disconnect()
        return 1

    print(f"Authenticated. {len(adapter.broker_symbol_names())} symbols on the account.\n")
    print(f"{'logical':8} {'broker':10} {'id':>8} {'digits':>6} {'min units':>10} {'step units':>10}")
    resolved = adapter.symbol_map
    for logical in adapter.instruments:
        if logical in resolved:
            name, sid = resolved[logical]
            spec = adapter._spec(sid)
            print(f"{logical:8} {name:10} {sid:>8} {spec.digits:>6} "
                  f"{volume_to_units(spec.min_volume):>10g} {volume_to_units(spec.step_volume):>10g}")
        else:
            print(f"{logical:8} {'NOT FOUND':10}   — add the broker's name to ctrader.instruments aliases")
    hints = [n for n in adapter.broker_symbol_names()
             if any(k in n for k in ("US", "GER", "DE", "NAS", "SPX", "DAX"))
             and len(n) <= 8][:25]
    if hints:
        print("\nIndex-like broker names (for alias tuning): " + ", ".join(hints))

    if not resolved:
        _err("ERROR: none of the configured instruments resolved — no order placed.")
        adapter.disconnect()
        return 1
    if args.no_order:
        adapter.disconnect()
        print("\nOK (--no-order): auth + symbol lookup succeeded.")
        return 0

    target = adapter.resolve_symbol(args.symbol)
    if target is None or target not in resolved:
        _err(f"ERROR: test symbol {args.symbol!r} did not resolve — no order placed.")
        adapter.disconnect()
        return 1

    try:
        mid = adapter.get_quote(target)
    except BrokerError as exc:
        _err(f"ERROR: could not price {target} ({exc}) — market closed? No order placed.")
        adapter.disconnect()
        return 1

    spec  = adapter._spec(resolved[target][1])
    units = volume_to_units(spec.min_volume)
    limit = round(mid * 0.80, spec.digits)          # far below market → stays pending
    sl    = round(limit * 0.98, spec.digits)
    tp    = round(limit * 1.04, spec.digits)
    print(f"\n{target} mid={mid}. Placing pending BUY LIMIT {units:g} units (venue minimum) @ {limit} "
          f"SL={sl} TP={tp} ...")

    try:
        res = adapter.place_bracket(target, "BUY", units, limit, sl, tp, order_type="LIMIT")
    except BrokerError as exc:
        _err(f"ERROR: order placement rejected — {exc}")
        adapter.disconnect()
        return 1
    print(f"Placed order id={res.order_id}. Cancelling ...")

    try:
        adapter.cancel_order(int(res.order_id))
        still_open = [o.orderId for o in getattr(adapter.reconcile(), "order", [])
                      if str(o.orderId) == res.order_id]
    except BrokerError as exc:
        _err(f"ERROR: cancel failed for order {res.order_id} — CANCEL IT MANUALLY in cTrader. ({exc})")
        adapter.disconnect()
        return 1
    if still_open:
        _err(f"ERROR: order {res.order_id} still open after cancel — CANCEL IT MANUALLY in cTrader.")
        adapter.disconnect()
        return 1

    adapter.disconnect()
    print("OK: authenticated, symbols resolved, tiny pending order placed and cancelled.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
