"""
IB Gateway connection smoke test — proves the port/host wiring against a real gateway.

This script is intentionally separate from the unit tests (which mock the IB handle).
Run it before market open or after a deploy to confirm connectivity end-to-end.

Usage
-----
Inside the production stack (Hetzner):
    docker exec pantheon_zeus python scripts/smoke_ib_connection.py

Locally against a tunneled gateway (e.g. SSH port-forward 4002→server):
    IB_HOST=127.0.0.1 IB_PORT=4002 python scripts/smoke_ib_connection.py

Exit codes
----------
0 — connected, confirmed isConnected(), disconnected cleanly.
1 — connection failed; diagnostic printed to stderr.

Notes
-----
- Uses clientId=9 — a dedicated smoke-test ID that cannot collide with
  Ares (clientId=1) or Argus (clientId=2).
- Places NO order, requests NO market data.
- Safe to run against a live or paper gateway; it is read-only.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    host = os.getenv("IB_HOST", "ibgateway")
    port = int(os.getenv("IB_PORT", "4002"))
    client_id = 9

    print(f"Connecting to IB Gateway at {host}:{port} (clientId={client_id}) ...")

    import asyncio
    try:
        old_loop = asyncio.get_event_loop()
        if not old_loop.is_running():
            old_loop.close()
    except Exception:
        pass
    asyncio.set_event_loop(asyncio.new_event_loop())

    try:
        from ib_async import IB
    except ImportError as exc:
        print(f"ERROR: ib_async not installed — {exc}", file=sys.stderr)
        return 1

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id)
    except Exception as exc:
        print(f"ERROR: Connection failed — host={host} port={port} — {exc}", file=sys.stderr)
        return 1

    if not ib.isConnected():
        print(f"ERROR: connect() returned but isConnected() is False", file=sys.stderr)
        try:
            ib.disconnect()
        except Exception:
            pass
        return 1

    server_version = getattr(ib.client, "serverVersion", lambda: "unknown")
    if callable(server_version):
        server_version = server_version()

    managed_accounts: list[str] = []
    try:
        managed_accounts = list(ib.managedAccounts())
    except Exception:
        pass

    print(f"OK — connected to {host}:{port}")
    print(f"  server version : {server_version}")
    print(f"  managed accounts: {managed_accounts or '(none returned)'}")

    try:
        ib.disconnect()
        print("Disconnected cleanly.")
    except Exception as exc:
        print(f"WARNING: disconnect raised {exc} (connection was confirmed)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
