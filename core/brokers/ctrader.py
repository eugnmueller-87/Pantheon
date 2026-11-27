"""
CTraderAdapter — cTrader Open API (Spotware OpenApiPy, protobuf over SSL/TCP 5035).

Twisted isolation strategy
--------------------------
The official SDK (`ctrader-open-api`) is Twisted-based and TCP-only. Zeus runs a
threaded http.server plus ib_async/asyncio, so the Twisted reactor must never
run on — or block — those threads. We isolate it like this:

  1. ONE reactor per process runs in a dedicated daemon thread
     ("ctrader-reactor"), started lazily with installSignalHandlers=False.
  2. TwistedTransport is a small synchronous facade: every SDK call is
     marshalled into the reactor with reactor.callFromThread(); the resulting
     Deferred is bridged to a concurrent.futures.Future, and the caller blocks
     on future.result(timeout). No Deferred ever leaks out of this module.
  3. SDK callbacks (connected / disconnected / message) are handed off to a
     single-worker ThreadPoolExecutor, so adapter code never runs on the
     reactor thread. That lets the reconnect hook make blocking requests
     (re-auth, reconcile, resubscribe) without deadlocking the reactor.
  4. request() refuses to run on the reactor thread (deadlock guard).

TLS: the SDK's Client builds an unverified "ssl:host:port" endpoint. We build
the same Client over a hostname-verifying TLS endpoint instead
(optionsForClientTLS + platform trust, service_identity), so the access token
is never sent to an unauthenticated peer.

Session model (persistent, NOT connect-per-order)
-------------------------------------------------
  connect → ProtoOAApplicationAuthReq → ProtoOAGetAccountListByAccessTokenReq
  (validates the account belongs to the token) → ProtoOAAccountAuthReq →
  ProtoOASymbolsListReq/ProtoOASymbolByIdReq (symbol map) → ProtoOAReconcileReq.
  A ProtoHeartbeatEvent is sent every `heartbeat_interval` (default 9s < the
  10s requirement; the SDK's own 20s idle heartbeat is too slow). On
  disconnect the SDK's ClientService reconnects with backoff, and our
  connected-hook re-runs the full auth + reconcile + spot resubscription.

Tokens
------
Credentials come ONLY from env (see CTraderCredentials.from_env). The access
token lasts ~30 days. If CTRADER_TOKEN_EXPIRES_AT is set we refresh ahead of
expiry; on CH_ACCESS_TOKEN_INVALID / OA_AUTH_TOKEN_EXPIRED we refresh and retry
once. Refreshed tokens live IN MEMORY only (this repo has no secrets store):
we log a warning that the operator must update CTRADER_ACCESS_TOKEN /
CTRADER_REFRESH_TOKEN. Token values are never logged.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from core.broker import (
    BracketResult,
    BrokerConfigError,
    BrokerConnectivityError,
    BrokerError,
    BrokerPosition,
    BrokerRejectedError,
)
from core.brokers.ctrader_instruments import (
    DEFAULT_TICKER_ALIASES,
    PRICE_SCALE,
    Instrument,
    RiskSizing,
    SymbolSpec,
    compute_risk_volume,
    compute_volume,
    effective_leverage,
    load_instruments,
    match_broker_symbols,
    relative_distance,
    resolve_logical,
    round_volume,
    units_to_volume,
    volume_to_units,
)

logger = logging.getLogger("broker.ctrader")

CTRADER_HOSTS = {
    "demo": "demo.ctraderapi.com",
    "live": "live.ctraderapi.com",
}
CTRADER_PORT = 5035
TOKEN_URL    = "https://openapi.ctrader.com/apps/token"

REQUIRED_ENV = (
    "CTRADER_CLIENT_ID",
    "CTRADER_CLIENT_SECRET",
    "CTRADER_ACCESS_TOKEN",
    "CTRADER_REFRESH_TOKEN",
    "CTRADER_ACCOUNT_ID",
)

# Error codes that mean "session/venue problem" → queue + retry, not a reject.
_CONNECTIVITY_CODES = {
    "CH_SERVER_NOT_REACHABLE", "SERVER_IS_UNDER_MAINTENANCE", "REQUEST_FREQUENCY_EXCEEDED",
    "TIMEOUT_ERROR", "CANT_ROUTE_REQUEST", "CH_CLIENT_NOT_AUTHENTICATED",
    "ACCOUNT_NOT_AUTHORIZED", "CONNECTIONS_LIMIT_EXCEEDED",
}
_TOKEN_CODES = {"CH_ACCESS_TOKEN_INVALID", "OA_AUTH_TOKEN_EXPIRED"}
_ALREADY_OK  = {"ALREADY_LOGGED_IN", "CH_CLIENT_ALREADY_AUTHENTICATED", "ALREADY_SUBSCRIBED"}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

@dataclass
class CTraderCredentials:
    client_id:     str
    client_secret: str = field(repr=False)
    access_token:  str = field(repr=False)
    refresh_token: str = field(repr=False)
    account_id:    int = 0
    env:           str = "demo"
    token_expires_at: Optional[datetime] = None

    @property
    def host(self) -> str:
        return CTRADER_HOSTS[self.env]

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "CTraderCredentials":
        """Read CTRADER_* from the environment. Raises BrokerConfigError naming
        every missing variable (never echoing any value)."""
        e = os.environ if environ is None else environ
        missing = [k for k in REQUIRED_ENV if not (e.get(k) or "").strip()]
        if missing:
            raise BrokerConfigError(
                "cTrader credentials missing: " + ", ".join(missing) +
                ". Set them in .env (see .env.example → cTrader section). "
                "Register an app at https://openapi.ctrader.com and obtain tokens with scope=trading."
            )
        env = (e.get("CTRADER_ENV") or "demo").strip().lower()
        if env not in CTRADER_HOSTS:
            raise BrokerConfigError(f"CTRADER_ENV must be 'demo' or 'live', got {env!r}")
        try:
            account_id = int(e["CTRADER_ACCOUNT_ID"].strip())
        except ValueError as exc:
            raise BrokerConfigError("CTRADER_ACCOUNT_ID must be the numeric ctidTraderAccountId") from exc
        return cls(
            client_id=e["CTRADER_CLIENT_ID"].strip(),
            client_secret=e["CTRADER_CLIENT_SECRET"].strip(),
            access_token=e["CTRADER_ACCESS_TOKEN"].strip(),
            refresh_token=e["CTRADER_REFRESH_TOKEN"].strip(),
            account_id=account_id,
            env=env,
            token_expires_at=_parse_expiry(e.get("CTRADER_TOKEN_EXPIRES_AT")),
        )


def _parse_expiry(raw: Optional[str]) -> Optional[datetime]:
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning("[CTRADER] CTRADER_TOKEN_EXPIRES_AT is not a unix timestamp or ISO date — ignored")
        return None


# ---------------------------------------------------------------------------
# Protobuf helpers (lazy import — the SDK is optional until BROKER=ctrader)
# ---------------------------------------------------------------------------

def _pb():
    try:
        from ctrader_open_api.messages import OpenApiCommonMessages_pb2 as common
        from ctrader_open_api.messages import OpenApiMessages_pb2 as msgs
        from ctrader_open_api.messages import OpenApiModelMessages_pb2 as model
    except ImportError as exc:  # pragma: no cover - exercised only without the SDK
        raise BrokerConfigError(
            "ctrader-open-api is not installed — pip install -r requirements.txt"
        ) from exc
    return common, msgs, model


def _payload_name(msg: Any) -> str:
    return type(msg).__name__


def _is_cancellation(exc: BaseException) -> bool:
    """True if `exc` is, or wraps via __cause__/__context__, a cancelled
    Future or Deferred. That is what TwistedTransport.stop() and Twisted's
    ClientService.stopService() produce for requests still in flight."""
    kinds: tuple[type, ...] = (concurrent.futures.CancelledError,)
    try:
        from twisted.internet.defer import CancelledError as DeferredCancelled
        kinds += (DeferredCancelled,)
    except ImportError:  # pragma: no cover - exercised only without the SDK
        pass
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, kinds):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False


def _settle(fut: concurrent.futures.Future, *, result: Any = None, error: Optional[BaseException] = None) -> None:
    """Resolve `fut` unless stop() already cancelled it (no InvalidStateError
    may escape into a Deferred chain, Twisted would print it as unhandled)."""
    if fut.done():
        return
    try:
        if error is None:
            fut.set_result(result)
        else:
            fut.set_exception(error)
    except concurrent.futures.InvalidStateError:
        pass


# ---------------------------------------------------------------------------
# Twisted transport — the ONLY code that touches the reactor
# ---------------------------------------------------------------------------

_REACTOR_LOCK = threading.Lock()
_REACTOR_THREAD: Optional[threading.Thread] = None


def _ensure_reactor_thread():
    """Start the process-wide Twisted reactor in a daemon thread (once)."""
    global _REACTOR_THREAD
    from twisted.internet import reactor

    with _REACTOR_LOCK:
        if _REACTOR_THREAD is None or not _REACTOR_THREAD.is_alive():
            _REACTOR_THREAD = threading.Thread(
                target=reactor.run, kwargs={"installSignalHandlers": False},
                name="ctrader-reactor", daemon=True,
            )
            _REACTOR_THREAD.start()
    return reactor, _REACTOR_THREAD


class HeartbeatLoop:
    """Calls `send` every `interval` seconds on a Twisted clock.

    Separate from the transport so it can be unit-tested with
    twisted.internet.task.Clock (no network, no real reactor)."""

    def __init__(self, send: Callable[[], None], interval: float, clock: Any):
        from twisted.internet import task

        self.interval = interval
        self._send    = send
        self._loop    = task.LoopingCall(self._tick)
        self._loop.clock = clock

    def _tick(self) -> None:
        try:
            self._send()
        except Exception as exc:   # never let a heartbeat failure kill the loop
            logger.debug("[CTRADER] heartbeat send failed: %s", exc)

    def start(self) -> None:
        if not self._loop.running:
            self._loop.start(self.interval, now=False)

    def stop(self) -> None:
        if self._loop.running:
            self._loop.stop()

    @property
    def running(self) -> bool:
        return self._loop.running


def _make_verified_client(reactor: Any, host: str, port: int, tls_options: Any = None):
    """ctrader_open_api.Client over a certificate-verifying TLS endpoint.

    Mirrors Client.__init__ (ctrader-open-api 0.9.x) but swaps the endpoint.
    """
    from ctrader_open_api import Client, TcpProtocol
    from ctrader_open_api.factory import Factory
    from twisted.application.internet import ClientService
    from twisted.internet import endpoints, ssl

    class _VerifiedClient(Client):
        def __init__(self):   # noqa: D401 — deliberately not calling Client.__init__
            self._runningReactor = reactor
            self.numberOfMessagesToSendPerSecond = 5
            options  = tls_options or ssl.optionsForClientTLS(host)
            endpoint = endpoints.wrapClientTLS(options, endpoints.HostnameEndpoint(reactor, host, port))
            factory  = Factory.forProtocol(TcpProtocol, client=self)
            ClientService.__init__(self, endpoint, factory)
            self._events = dict()
            self._responseDeferreds = dict()
            self.isConnected = False

    return _VerifiedClient()


class TwistedTransport:
    """Synchronous facade over ctrader_open_api.Client (see module docstring)."""

    def __init__(self, host: str, port: int = CTRADER_PORT, heartbeat_interval: float = 9.0,
                 tls_options: Any = None):
        self.host = host
        self.port = port
        self.heartbeat_interval = heartbeat_interval
        self._tls_options = tls_options   # tests inject a trust root; prod = platform CAs
        self._client = None
        self._reactor = None
        self._reactor_thread: Optional[threading.Thread] = None
        self._heartbeat: Optional[HeartbeatLoop] = None
        self._connected = threading.Event()
        # Set by stop(), cleared by start(). While set: request() refuses, and
        # SDK callbacks (connected → re-auth, disconnected, messages) are
        # dropped, so a stopped session never starts work after stop() returns.
        self._stopped = threading.Event()
        self._pending: set[concurrent.futures.Future] = set()   # request() futures in flight
        self._pending_lock = threading.Lock()
        self._cb_local = threading.local()                       # .active on the ctrader-cb worker
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctrader-cb")

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def wait_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    def _dispatch(self, fn: Callable[..., None], *args: Any) -> None:
        """Hand an SDK callback to the ctrader-cb worker (called on the reactor thread)."""
        if self._stopped.is_set():
            return

        def _run():
            if self._stopped.is_set():   # queued before stop(), would run after it
                return
            self._cb_local.active = True
            try:
                fn(*args)
            finally:
                self._cb_local.active = False

        try:
            self._executor.submit(_run)
        except RuntimeError:
            # "cannot schedule new futures after interpreter shutdown": an SDK
            # event raced process exit. Raising here would surface as a Twisted
            # "Unhandled Error" traceback on the reactor thread.
            logger.debug("[CTRADER] callback %s dropped: executor is shut down", getattr(fn, "__name__", fn))

    def start(self, on_connected, on_disconnected, on_message) -> None:
        self._stopped.clear()
        self._reactor, self._reactor_thread = _ensure_reactor_thread()

        def _start_in_reactor():
            from ctrader_open_api import Protobuf

            client = _make_verified_client(self._reactor, self.host, self.port, self._tls_options)

            def _conn(_client):
                if self._stopped.is_set():
                    return
                self._connected.set()
                self._dispatch(on_connected)

            def _disc(_client, reason):
                self._connected.clear()
                self._dispatch(on_disconnected, str(getattr(reason, "value", reason)))

            def _msg(_client, message):
                if message.payloadType == 51:   # ProtoHeartbeatEvent — SDK already answers
                    return
                try:
                    payload = Protobuf.extract(message)
                except Exception as exc:
                    logger.debug("[CTRADER] could not decode payloadType=%s: %s", message.payloadType, exc)
                    return
                self._dispatch(on_message, payload)

            client.setConnectedCallback(_conn)
            client.setDisconnectedCallback(_disc)
            client.setMessageReceivedCallback(_msg)
            self._client = client
            self._heartbeat = HeartbeatLoop(self._send_heartbeat_in_reactor, self.heartbeat_interval, self._reactor)
            self._heartbeat.start()
            client.startService()

        self._reactor.callFromThread(_start_in_reactor)

    def _send_heartbeat_in_reactor(self) -> None:
        if self._client is None or not self._connected.is_set():
            return
        d = self._client.whenConnected(failAfterFailures=1)
        d.addCallback(lambda protocol: protocol.heartbeat())
        d.addErrback(lambda f: logger.debug("[CTRADER] heartbeat skipped: %s", f.value))

    def request(self, message: Any, timeout: float) -> Any:
        """Send `message`, block until its response arrives, return the decoded payload."""
        if threading.current_thread() is self._reactor_thread:
            raise RuntimeError("[CTRADER] request() called on the reactor thread — would deadlock")
        name = _payload_name(message)
        if self._stopped.is_set():
            raise BrokerConnectivityError(f"cTrader {name} refused: transport stopped")
        if self._client is None or self._reactor is None:
            raise BrokerConnectivityError("cTrader transport not started")
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def _send():
            if fut.done():   # stop() cancelled it before it reached the reactor
                return
            try:
                d = self._client.send(message, responseTimeoutInSeconds=timeout)
            except Exception as exc:
                _settle(fut, error=exc)
                return
            d.addCallbacks(
                lambda m: _settle(fut, result=m),
                lambda f: _settle(fut, error=f.value),
            )

        with self._pending_lock:
            self._pending.add(fut)
        try:
            self._reactor.callFromThread(_send)
            raw = fut.result(timeout + 2)
        except concurrent.futures.CancelledError as exc:
            raise BrokerConnectivityError(f"cTrader {name} cancelled: transport stopped") from exc
        except concurrent.futures.TimeoutError as exc:
            raise BrokerConnectivityError(f"cTrader {name} timed out after {timeout}s") from exc
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerConnectivityError(f"cTrader {name} failed: {type(exc).__name__}: {exc}") from exc
        finally:
            with self._pending_lock:
                self._pending.discard(fut)
        from ctrader_open_api import Protobuf
        return Protobuf.extract(raw)

    def stop(self, drain_timeout: float = 5.0) -> None:
        """Stop the session and cancel what it still has running.

        Order matters: mark stopped (no new requests, late SDK callbacks are
        dropped), cancel in-flight request futures (a re-auth blocked in
        request() returns at once instead of after its timeout), stop the
        client service, then wait until the ctrader-cb worker has unwound, so
        nothing of this session still runs or logs after stop() returns.
        """
        self._stopped.set()
        self._connected.clear()
        with self._pending_lock:
            pending = list(self._pending)
        for fut in pending:
            fut.cancel()
        if self._reactor is None:
            return

        def _stop():
            from twisted.application.internet import ClientService

            if self._heartbeat is not None:
                self._heartbeat.stop()
            # Client.stopService() is a no-op while disconnected, which would
            # leave the reconnect loop running forever — stop the service directly.
            if self._client is not None and self._client.running:
                ClientService.stopService(self._client)

        self._reactor.callFromThread(_stop)
        self._drain_callbacks(drain_timeout)

    def _drain_callbacks(self, timeout: float) -> None:
        if getattr(self._cb_local, "active", False):
            return   # stop() called from a callback: waiting on ourselves would deadlock
        try:
            self._executor.submit(lambda: None).result(timeout)
        except concurrent.futures.TimeoutError:
            logger.warning("[CTRADER] A callback was still running %.0fs after stop()", timeout)
        except RuntimeError:
            pass   # executor already shut down (interpreter exit): nothing left to wait for


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

class CTraderAdapter:
    name = "ctrader"
    supports_short = True   # FX / index CFDs are two-way

    def __init__(
        self,
        credentials: CTraderCredentials,
        *,
        account_equity: float,
        instruments: Optional[dict[str, dict[str, Any]]] = None,
        leverage_caps: Optional[dict[str, float]] = None,
        ticker_aliases: Optional[dict[str, str]] = None,
        account_currency: str = "EUR",
        sizing_leverage: float = 1.0,
        max_min_lot_overshoot: float = 1.5,
        sizing_mode: str = "risk",
        risk_per_trade_pct: float = 0.01,
        max_min_lot_risk_overshoot: float = 1.5,
        entry_order_type: str = "MARKET",
        heartbeat_interval: float = 9.0,
        request_timeout: float = 10.0,
        connect_timeout: float = 15.0,
        quote_timeout: float = 5.0,
        quote_max_age: float = 300.0,
        token_refresh_margin: timedelta = timedelta(days=3),
        deal_lookback_days: int = 7,
        label: str = "pantheon",
        transport: Any = None,
        on_tokens_refreshed: Optional[Callable[[str, str], None]] = None,
    ):
        if account_equity is None or account_equity <= 0:
            raise BrokerConfigError("CTraderAdapter needs a positive account_equity (risk budget)")
        self.creds              = credentials
        self.account_equity     = float(account_equity)
        self.instruments: dict[str, Instrument] = load_instruments(instruments, leverage_caps)
        self.ticker_aliases     = ticker_aliases or DEFAULT_TICKER_ALIASES
        self.account_currency   = account_currency.upper()
        self.sizing_leverage    = float(sizing_leverage)
        self.max_min_lot_overshoot = float(max_min_lot_overshoot)
        # "risk" (default): Ares sizes from the stop via size_order_risk().
        # "notional" (legacy): Ares uses size_order() as before.
        self.sizing_mode        = str(sizing_mode or "risk").lower()
        if self.sizing_mode not in ("risk", "notional"):
            raise BrokerConfigError(f"ctrader.sizing_mode must be 'risk' or 'notional', got {sizing_mode!r}")
        self.risk_per_trade_pct = float(risk_per_trade_pct)
        if not 0 < self.risk_per_trade_pct <= 0.05:
            raise BrokerConfigError(
                f"ctrader.risk_per_trade_pct={risk_per_trade_pct} outside (0, 0.05] — refusing to start"
            )
        # Highest risk fraction size_order_risk() accepts. Equals
        # risk_per_trade_pct unless the (opt-in) risk ramp raises it via
        # set_risk_ceiling(); never above 5%.
        self.max_risk_per_trade_pct = self.risk_per_trade_pct
        self.max_min_lot_risk_overshoot = float(max_min_lot_risk_overshoot)
        self.entry_order_type   = entry_order_type.upper()
        self.request_timeout    = request_timeout
        self.connect_timeout    = connect_timeout
        self.quote_timeout      = quote_timeout
        self.quote_max_age      = quote_max_age
        self.token_refresh_margin = token_refresh_margin
        self.deal_lookback_days = deal_lookback_days
        self.label              = label
        self._on_tokens_refreshed = on_tokens_refreshed
        self.transport = transport or TwistedTransport(
            credentials.host, CTRADER_PORT, heartbeat_interval=heartbeat_interval,
        )

        self._started        = False
        self._state_lock     = threading.RLock()
        self._auth_lock      = threading.Lock()
        self._app_authed     = False
        self._account_authed = threading.Event()
        # Set by disconnect(), cleared by connect(). While set, no background
        # re-auth runs: a reconnect or server event that races the shutdown
        # must not authorize a session we are closing.
        self._closing        = threading.Event()
        self._broker_symbols: dict[str, int] = {}       # UPPER broker name -> symbolId
        self._resolved: dict[str, tuple[str, int]] = {} # logical -> (broker name, symbolId)
        self._specs: dict[int, SymbolSpec] = {}
        self._spots: dict[int, dict[str, Any]] = {}     # symbolId -> {bid, ask, ts}
        self._spot_cond      = threading.Condition()
        self._subscribed: set[int] = set()
        self.last_reconcile: Any = None
        self.auth_count      = 0   # number of completed full authorizations (observability/tests)

        logger.info(
            "[CTRADER] Adapter configured — env=%s host=%s:%d account=%s instruments=%s",
            credentials.env.upper(), credentials.host, CTRADER_PORT,
            _mask_account(credentials.account_id), ",".join(self.instruments),
        )

    @classmethod
    def from_env(cls, *, account_equity: float, environ: Optional[Mapping[str, str]] = None, **kwargs) -> "CTraderAdapter":
        return cls(CTraderCredentials.from_env(environ), account_equity=account_equity, **kwargs)

    # ── session lifecycle ───────────────────────────────────────────────────

    def connect(self) -> "CTraderAdapter":
        """Idempotent: no-op when the persistent session is already authorized."""
        if self._account_authed.is_set() and self.transport.is_connected:
            self._maybe_refresh_token()
            return self
        self._closing.clear()
        if not self._started:
            self.transport.start(self._on_connected, self._on_disconnected, self._on_message)
            self._started = True
        if not self.transport.wait_connected(self.connect_timeout):
            raise BrokerConnectivityError(
                f"Could not connect to cTrader {self.creds.host}:{CTRADER_PORT} within {self.connect_timeout:.0f}s"
            )
        self._authorize()
        return self

    def disconnect(self) -> None:
        """Close the session. The closing flag goes up FIRST, so a re-auth the
        transport already queued (reconnect, server disconnect event) does not
        fire afterwards; transport.stop() then cancels requests still in flight
        and returns once the callback worker has unwound."""
        self._closing.set()
        self._account_authed.clear()
        self._app_authed = False
        if self._started:
            self.transport.stop()
            self._started = False

    def health(self) -> bool:
        return self.transport.is_connected and self._account_authed.is_set()

    @property
    def authorized(self) -> bool:
        return self._account_authed.is_set()

    # ── transport callbacks (run on the ctrader-cb worker, never the reactor) ─

    def _on_connected(self) -> None:
        if self._closing.is_set():
            logger.debug("[CTRADER] Transport connected during disconnect(), not re-authorizing")
            return
        logger.info("[CTRADER] Transport connected — authorizing session")
        self._reauth("Re-authorization after connect")

    def _on_disconnected(self, reason: str) -> None:
        self._account_authed.clear()
        self._app_authed = False
        if self._closing.is_set():
            logger.debug("[CTRADER] Disconnected (%s) after disconnect()", reason)
            return
        logger.warning("[CTRADER] Disconnected (%s) — SDK will reconnect, session will re-authorize", reason)

    def _on_message(self, payload: Any) -> None:
        name = _payload_name(payload)
        if name == "ProtoOASpotEvent":
            self._record_spot(payload)
        elif name == "ProtoOAExecutionEvent":
            logger.debug("[CTRADER] execution event type=%s", payload.executionType)
        elif self._closing.is_set():
            logger.debug("[CTRADER] %s ignored: adapter is disconnecting", name)
        elif name in ("ProtoOAClientDisconnectEvent", "ProtoOAAccountDisconnectEvent"):
            logger.warning("[CTRADER] Server-side disconnect (%s) — will re-authorize", name)
            self._account_authed.clear()
            if name == "ProtoOAAccountDisconnectEvent":
                self._safe_reauth()
        elif name == "ProtoOAAccountsTokenInvalidatedEvent":
            logger.warning("[CTRADER] Access token invalidated by server — refreshing")
            self._account_authed.clear()
            try:
                self.refresh_access_token()
            except Exception as exc:
                logger.error("[CTRADER] Token refresh failed: %s — operator must issue new tokens", exc)
                return
            self._safe_reauth()

    def _safe_reauth(self) -> None:
        self._reauth("Re-authorization")

    def _reauth(self, what: str) -> None:
        """Background re-auth on the ctrader-cb worker. Never raises.

        If disconnect() overtakes it, its request ends in a CancelledError
        (concurrent.futures from transport.stop(), or Twisted's from
        stopService(), usually wrapped in BrokerConnectivityError) or is
        refused because the transport stopped. That is the shutdown working,
        so it is caught here and logged at debug, not as an error. Genuine
        failures while the session is meant to be up still log as errors.
        """
        if self._closing.is_set():
            logger.debug("[CTRADER] %s skipped: adapter is disconnecting", what)
            return
        try:
            self._authorize()
        except concurrent.futures.CancelledError as exc:
            logger.debug("[CTRADER] %s cancelled by disconnect: %s", what, exc)
        except Exception as exc:
            if self._closing.is_set() or _is_cancellation(exc):
                logger.debug("[CTRADER] %s cancelled by disconnect: %s", what, exc)
                return
            logger.error("[CTRADER] %s failed: %s", what, exc)

    # ── auth ────────────────────────────────────────────────────────────────

    def _authorize(self) -> None:
        _, msgs, _ = _pb()
        with self._auth_lock:
            if self._account_authed.is_set():
                return
            if self._closing.is_set():   # a re-auth that waited on the lock while disconnect() ran
                raise BrokerConnectivityError("cTrader session is disconnecting, not authorizing")
            # 1. application
            if not self._app_authed:
                self._call(msgs.ProtoOAApplicationAuthReq(
                    clientId=self.creds.client_id, clientSecret=self.creds.client_secret,
                ), ok_codes=_ALREADY_OK, auth_stage=True)
                self._app_authed = True
                logger.info("[CTRADER] Application authorized")

            self._maybe_refresh_token()

            # 2. validate the token can see our account (optional but catches misconfig early)
            try:
                res = self._call(msgs.ProtoOAGetAccountListByAccessTokenReq(
                    accessToken=self.creds.access_token,
                ), auth_stage=True)
                ids = {a.ctidTraderAccountId for a in getattr(res, "ctidTraderAccount", [])}
                if ids and self.creds.account_id not in ids:
                    raise BrokerConfigError(
                        f"CTRADER_ACCOUNT_ID {_mask_account(self.creds.account_id)} is not linked to this "
                        f"access token ({len(ids)} account(s) visible) — check the account id"
                    )
                if _has(res, "permissionScope") and int(res.permissionScope) == 0:   # SCOPE_VIEW
                    logger.warning("[CTRADER] Access token has VIEW scope only — trading will be rejected "
                                   "(re-issue the token with scope=trading)")
            except _TokenExpired:
                self.refresh_access_token()

            # 3. account (refresh + retry once on an expired/invalid token)
            try:
                self._account_auth()
            except _TokenExpired:
                self.refresh_access_token()
                self._account_auth()

            # 4. symbols, reconcile, spots
            if not self._resolved:
                self._load_symbols()
            self.last_reconcile = self._call(msgs.ProtoOAReconcileReq(ctidTraderAccountId=self.creds.account_id))
            self._resubscribe_spots()
            self._account_authed.set()
            self.auth_count += 1
            logger.info(
                "[CTRADER] Account authorized (%s) — %d position(s), %d pending order(s) reconciled",
                self.creds.env.upper(),
                len(getattr(self.last_reconcile, "position", [])),
                len(getattr(self.last_reconcile, "order", [])),
            )

    def _account_auth(self) -> None:
        _, msgs, _ = _pb()
        self._call(msgs.ProtoOAAccountAuthReq(
            ctidTraderAccountId=self.creds.account_id, accessToken=self.creds.access_token,
        ), ok_codes=_ALREADY_OK, auth_stage=True)

    def _maybe_refresh_token(self) -> None:
        exp = self.creds.token_expires_at
        if exp is None:
            return
        if datetime.now(timezone.utc) + self.token_refresh_margin >= exp:
            logger.warning("[CTRADER] Access token expires %s — refreshing proactively", exp.date().isoformat())
            try:
                self.refresh_access_token()
            except Exception as exc:
                logger.error("[CTRADER] Proactive token refresh failed: %s", exc)

    def refresh_access_token(self) -> None:
        """Refresh via ProtoOARefreshTokenReq (needs app auth) or the HTTP
        token endpoint as a fallback. New tokens are kept in memory only."""
        access = refresh = None
        expires_in: Optional[int] = None
        if self._app_authed and self.transport.is_connected:
            try:
                _, msgs, _ = _pb()
                res = self._call(msgs.ProtoOARefreshTokenReq(refreshToken=self.creds.refresh_token), auth_stage=True)
                access, refresh, expires_in = res.accessToken, res.refreshToken, int(res.expiresIn)
            except Exception as exc:
                logger.warning("[CTRADER] ProtoOARefreshTokenReq failed (%s) — trying HTTP token endpoint", exc)
        if not access:
            access, refresh, expires_in = self._refresh_via_http()
        self.creds.access_token  = access
        self.creds.refresh_token = refresh or self.creds.refresh_token
        if expires_in:
            self.creds.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        logger.warning(
            "[CTRADER] Access token refreshed IN MEMORY (valid until %s). The operator must update "
            "CTRADER_ACCESS_TOKEN / CTRADER_REFRESH_TOKEN (and CTRADER_TOKEN_EXPIRES_AT) in the "
            "environment before the next restart — the old refresh token is now invalid.",
            self.creds.token_expires_at.date().isoformat() if self.creds.token_expires_at else "unknown",
        )
        if self._on_tokens_refreshed:
            self._on_tokens_refreshed(self.creds.access_token, self.creds.refresh_token)

    def _refresh_via_http(self) -> tuple[str, str, Optional[int]]:
        import requests

        resp = requests.post(TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "refresh_token": self.creds.refresh_token,
            "client_id":     self.creds.client_id,
            "client_secret": self.creds.client_secret,
        }, headers={"Accept": "application/json"}, timeout=15)
        body = resp.json() if resp.content else {}
        access = body.get("accessToken") or body.get("access_token")
        if resp.status_code >= 400 or not access:
            # never include the body — it may echo credentials
            raise BrokerConfigError(
                f"cTrader token refresh failed (HTTP {resp.status_code}, "
                f"error={body.get('errorCode') or body.get('error') or 'unknown'})"
            )
        refresh = body.get("refreshToken") or body.get("refresh_token")
        expires = body.get("expiresIn") or body.get("expires_in")
        return access, refresh, int(expires) if expires else None

    # ── request plumbing ────────────────────────────────────────────────────

    def _call(self, message: Any, *, ok_codes: frozenset | set = frozenset(), auth_stage: bool = False) -> Any:
        res = self.transport.request(message, self.request_timeout)
        name = _payload_name(res)
        if name in ("ProtoOAErrorRes", "ProtoErrorRes", "ProtoOAOrderErrorEvent"):
            code = str(getattr(res, "errorCode", "") or "UNKNOWN")
            desc = str(getattr(res, "description", "") or "")
            if code in ok_codes:
                return res
            text = f"cTrader {_payload_name(message)} → {code}: {desc}".strip()
            if code in _TOKEN_CODES:
                raise _TokenExpired(text)
            if code in _CONNECTIVITY_CODES:
                if code in ("ACCOUNT_NOT_AUTHORIZED", "CH_CLIENT_NOT_AUTHENTICATED"):
                    self._account_authed.clear()
                raise BrokerConnectivityError(text)
            if auth_stage:
                raise BrokerConfigError(text)
            raise BrokerRejectedError(text)
        return res

    def _require_session(self) -> None:
        if not self.health():
            self.connect()

    # ── symbols ─────────────────────────────────────────────────────────────

    def _load_symbols(self) -> None:
        _, msgs, _ = _pb()
        res = self._call(msgs.ProtoOASymbolsListReq(ctidTraderAccountId=self.creds.account_id))
        self._broker_symbols = {
            s.symbolName.upper(): int(s.symbolId) for s in res.symbol if getattr(s, "symbolName", "")
        }
        self._resolved = match_broker_symbols(self.instruments, self._broker_symbols)
        missing = [k for k in self.instruments if k not in self._resolved]
        if missing:
            logger.warning("[CTRADER] Universe instruments NOT found on this account: %s "
                           "(add the broker's name to ctrader_instruments aliases)", ",".join(missing))
        ids = sorted({sid for _, sid in self._resolved.values()} | set(self._conversion_symbol_ids()))
        if ids:
            self._load_specs(ids)
        logger.info("[CTRADER] Symbol map: %s",
                    ", ".join(f"{k}->{v[0]}#{v[1]}" for k, v in self._resolved.items()) or "(empty)")

    def _conversion_symbol_ids(self) -> list[int]:
        out = []
        for inst in self.instruments.values():
            pair = self._conversion_pair(inst.quote_ccy)
            if pair:
                out.append(pair[1])
        return out

    def _load_specs(self, ids: list[int]) -> None:
        _, msgs, _ = _pb()
        res = self._call(msgs.ProtoOASymbolByIdReq(ctidTraderAccountId=self.creds.account_id, symbolId=ids))
        names = {sid: name for name, sid in self._broker_symbols.items()}
        for s in res.symbol:
            sid = int(s.symbolId)
            self._specs[sid] = SymbolSpec(
                symbol_id=sid,
                broker_name=names.get(sid, str(sid)),
                digits=int(s.digits),
                min_volume=int(s.minVolume),
                step_volume=int(s.stepVolume) or 1,
                max_volume=int(s.maxVolume) if s.maxVolume else None,
                lot_size=int(s.lotSize) if s.lotSize else None,
            )

    @property
    def symbol_map(self) -> dict[str, tuple[str, int]]:
        return dict(self._resolved)

    def broker_symbol_names(self) -> list[str]:
        return sorted(self._broker_symbols)

    def resolve_symbol(self, ticker: str) -> Optional[str]:
        """Pipeline ticker → logical universe name. Unknown tickers (e.g. stock
        symbols) return None — this venue has no stock CFDs on cTrader."""
        return resolve_logical(ticker, self.instruments, self.ticker_aliases)

    def _symbol_id(self, symbol: str) -> int:
        logical = self.resolve_symbol(symbol) or symbol.upper()
        if logical in self._resolved:
            return self._resolved[logical][1]
        if symbol.upper() in self._broker_symbols:
            return self._broker_symbols[symbol.upper()]
        raise BrokerRejectedError(f"Symbol {symbol!r} is not available on this cTrader account")

    def _spec(self, sid: int) -> SymbolSpec:
        if sid not in self._specs:
            self._load_specs([sid])
        if sid not in self._specs:
            raise BrokerRejectedError(f"No symbol details for symbolId {sid}")
        return self._specs[sid]

    def _instrument(self, symbol: str) -> Instrument:
        logical = self.resolve_symbol(symbol)
        if logical is None:
            raise BrokerRejectedError(f"{symbol!r} is not in the configured cTrader universe")
        return self.instruments[logical]

    def price_decimals(self, symbol: str) -> int:
        try:
            return self._spec(self._symbol_id(symbol)).digits
        except BrokerError:
            return 5

    def _conversion_pair(self, quote_ccy: str) -> Optional[tuple[bool, int]]:
        """(inverted?, symbolId) of a pair converting quote_ccy → account ccy."""
        acct = self.account_currency
        if quote_ccy == acct:
            return None
        direct = self._broker_symbols.get(f"{quote_ccy}{acct}")    # e.g. USDEUR (rare)
        if direct is not None:
            return (False, direct)
        inverse = self._broker_symbols.get(f"{acct}{quote_ccy}")   # e.g. EURUSD
        if inverse is not None:
            return (True, inverse)
        return None

    def quote_to_account_rate(self, quote_ccy: str) -> float:
        if quote_ccy.upper() == self.account_currency:
            return 1.0
        pair = self._conversion_pair(quote_ccy.upper())
        if pair is None:
            raise BrokerRejectedError(
                f"No {quote_ccy}/{self.account_currency} conversion symbol on this account — "
                "refusing to size (fail closed)"
            )
        inverted, sid = pair
        mid = self._quote_by_id(sid)
        return 1.0 / mid if inverted else mid

    # ── quotes ──────────────────────────────────────────────────────────────

    def _record_spot(self, ev: Any) -> None:
        sid = int(ev.symbolId)
        with self._spot_cond:
            entry = self._spots.setdefault(sid, {"bid": None, "ask": None, "ts": 0.0})
            if _has(ev, "bid"):
                entry["bid"] = int(ev.bid)
            if _has(ev, "ask"):
                entry["ask"] = int(ev.ask)
            entry["ts"] = time.monotonic()
            self._spot_cond.notify_all()

    def _subscribe(self, sid: int) -> None:
        if sid in self._subscribed:
            return
        _, msgs, _ = _pb()
        self._call(msgs.ProtoOASubscribeSpotsReq(ctidTraderAccountId=self.creds.account_id, symbolId=[sid]),
                   ok_codes={"ALREADY_SUBSCRIBED"})
        self._subscribed.add(sid)

    def _resubscribe_spots(self) -> None:
        ids = list(self._subscribed)
        self._subscribed.clear()
        for sid in ids:
            try:
                self._subscribe(sid)
            except BrokerError as exc:
                logger.warning("[CTRADER] Spot resubscribe failed for #%d: %s", sid, exc)

    def _fresh_mid(self, sid: int) -> Optional[float]:
        e = self._spots.get(sid)
        if not e or e["bid"] is None or e["ask"] is None:
            return None
        if time.monotonic() - e["ts"] > self.quote_max_age:
            return None
        return (e["bid"] + e["ask"]) / 2 / PRICE_SCALE

    def _quote_by_id(self, sid: int) -> float:
        self._require_session()
        self._subscribe(sid)
        deadline = time.monotonic() + self.quote_timeout
        with self._spot_cond:
            mid = self._fresh_mid(sid)
            while mid is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._spot_cond.wait(remaining)
                mid = self._fresh_mid(sid)
        if mid is None or mid <= 0:
            raise BrokerError(f"Could not price symbolId {sid} (no spot within {self.quote_timeout:.0f}s — market closed?)")
        return mid

    def get_quote(self, symbol: str, exchange: Optional[str] = None) -> float:
        sid = self._symbol_id(symbol)
        mid = self._quote_by_id(sid)
        return round(mid, self._spec(sid).digits)

    # ── sizing ──────────────────────────────────────────────────────────────

    def size_order(self, symbol: str, notional: float, price: float) -> float:
        """Unlevered by default: notional × sizing_leverage (≥1, ≤ ESMA cap)."""
        inst = self._instrument(symbol)
        lev  = effective_leverage(self.sizing_leverage, inst)
        spec = self._spec(self._symbol_id(symbol))
        q2a  = self.quote_to_account_rate(inst.quote_ccy)
        volume = compute_volume(
            notional_acct=notional * lev, price=price, quote_to_account=q2a, spec=spec,
            inst=inst, equity=self.account_equity, max_min_lot_overshoot=self.max_min_lot_overshoot,
        )
        units = volume_to_units(volume)
        logger.info(
            "[CTRADER] Size %s: notional=%.2f %s ×lev %.0f (cap 1:%g) → %g units (volume=%d, min=%d step=%d)",
            inst.logical, notional, self.account_currency, lev, inst.leverage_cap, units,
            volume, spec.min_volume, spec.step_volume,
        )
        return units

    def size_order_risk(
        self,
        symbol: str,
        entry: float,
        stop_loss: float,
        risk_pct: Optional[float] = None,
    ) -> RiskSizing:
        """Risk-based sizing: lose ≈ equity × risk_pct if the stop is hit.

        risk_pct defaults to risk_per_trade_pct and is clamped at
        max_risk_per_trade_pct (== risk_per_trade_pct unless the opt-in risk
        ramp raised it with set_risk_ceiling). Fails
        closed (BrokerRejectedError) on a missing conversion pair, a min lot
        beyond the risk tolerance, or a min lot beyond the ESMA cap.
        """
        inst = self._instrument(symbol)
        spec = self._spec(self._symbol_id(symbol))
        pct  = self.risk_per_trade_pct if risk_pct is None else min(float(risk_pct), self.max_risk_per_trade_pct)
        q2a  = self.quote_to_account_rate(inst.quote_ccy)   # raises when the pair is missing
        sizing = compute_risk_volume(
            equity=self.account_equity, risk_pct=pct, entry=entry, stop=stop_loss,
            quote_to_account=q2a, spec=spec, inst=inst,
            max_min_lot_risk_overshoot=self.max_min_lot_risk_overshoot,
        )
        logger.info(
            "[CTRADER] Risk-size %s: equity=%.2f risk=%.2f%% (%.2f %s) stop=%g → %g units "
            "(volume=%d, min=%d step=%d) risk@stop=%.2f notional=%.2f (%.2fx equity, cap 1:%g)%s%s",
            inst.logical, self.account_equity, pct * 100, sizing.risk_budget, self.account_currency,
            abs(entry - stop_loss), sizing.units, sizing.volume, spec.min_volume, spec.step_volume,
            sizing.risk_at_stop, sizing.notional, sizing.notional_fraction, inst.leverage_cap,
            " [min lot]" if sizing.used_min_lot else "",
            " [shrunk to ESMA cap]" if sizing.capped_by_leverage else "",
        )
        return sizing

    def set_risk_ceiling(self, pct: float) -> None:
        """Risk ramp hook: allow size_order_risk() up to `pct` (never > 5%,
        never below risk_per_trade_pct)."""
        pct = float(pct)
        if not self.risk_per_trade_pct <= pct <= 0.05:
            raise BrokerConfigError(
                f"risk ceiling {pct} must be in [risk_per_trade_pct {self.risk_per_trade_pct}, 0.05]")
        self.max_risk_per_trade_pct = pct
        logger.info("[CTRADER] Risk ceiling set to %.2f%% (base %.2f%%)", pct * 100, self.risk_per_trade_pct * 100)

    def leverage_cap_for(self, symbol: str) -> float:
        """ESMA (or tighter settings) leverage cap of the symbol's asset class."""
        return self._instrument(symbol).leverage_cap

    # ── orders ──────────────────────────────────────────────────────────────

    def place_bracket(
        self,
        symbol: str,
        side: str,
        qty: float,
        entry: float,
        stop_loss: float,
        take_profit: Optional[float],
        exchange: Optional[str] = None,
        order_type: Optional[str] = None,
    ) -> BracketResult:
        """take_profit=None places the entry with a stop-loss only (used when a
        trailing stop runs with use_fixed_target=False); the SL is mandatory."""
        _, msgs, model = _pb()
        self._require_session()
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise BrokerRejectedError(f"Invalid side {side!r}")
        if stop_loss is None:
            raise BrokerRejectedError("cTrader bracket needs a stop-loss")
        is_long = side == "BUY"
        if take_profit is None:
            if is_long and not stop_loss < entry:
                raise BrokerRejectedError(f"BUY needs SL < entry (got {stop_loss}/{entry})")
            if not is_long and not entry < stop_loss:
                raise BrokerRejectedError(f"SELL needs entry < SL (got {entry}/{stop_loss})")
        elif is_long and not (stop_loss < entry < take_profit):
            raise BrokerRejectedError(f"BUY bracket needs SL < entry < TP (got {stop_loss}/{entry}/{take_profit})")
        elif not is_long and not (take_profit < entry < stop_loss):
            raise BrokerRejectedError(f"SELL bracket needs TP < entry < SL (got {take_profit}/{entry}/{stop_loss})")

        sid    = self._symbol_id(symbol)
        spec   = self._spec(sid)
        volume = round_volume(units_to_volume(qty), spec)
        if volume <= 0:
            raise BrokerRejectedError(f"{symbol}: {qty} units is below the venue minimum volume {spec.min_volume}")

        otype = (order_type or self.entry_order_type).upper()
        client_order_id = f"pan-{uuid.uuid4().hex[:12]}"
        kwargs: dict[str, Any] = dict(
            ctidTraderAccountId=self.creds.account_id,
            symbolId=sid,
            tradeSide=model.ProtoOATradeSide.Value(side),
            volume=volume,
            label=self.label,
            clientOrderId=client_order_id,
        )
        if otype == "MARKET":
            # cTrader rejects absolute SL/TP on market orders — use relative
            # distances (1/100000 price units) from the fill price instead.
            kwargs.update(
                orderType=model.ProtoOAOrderType.Value("MARKET"),
                relativeStopLoss=relative_distance(entry, stop_loss, spec.digits),
            )
            if take_profit is not None:
                kwargs["relativeTakeProfit"] = relative_distance(entry, take_profit, spec.digits)
        elif otype == "LIMIT":
            kwargs.update(
                orderType=model.ProtoOAOrderType.Value("LIMIT"),
                limitPrice=round(entry, spec.digits),
                stopLoss=round(stop_loss, spec.digits),
                timeInForce=model.ProtoOATimeInForce.Value("GOOD_TILL_CANCEL"),
            )
            if take_profit is not None:
                kwargs["takeProfit"] = round(take_profit, spec.digits)
        else:
            raise BrokerConfigError(f"Unsupported ctrader entry order type {otype!r} (MARKET|LIMIT)")

        res = self._call(msgs.ProtoOANewOrderReq(**kwargs))
        exec_type = model.ProtoOAExecutionType.Name(res.executionType) if hasattr(res, "executionType") else "?"
        if exec_type in ("ORDER_REJECTED", "ORDER_CANCELLED", "ORDER_EXPIRED"):
            raise BrokerRejectedError(f"cTrader order {exec_type} (errorCode={getattr(res, 'errorCode', '')})")
        order  = res.order
        fill   = float(order.executionPrice) if exec_type == "ORDER_FILLED" and order.executionPrice else entry
        status = "filled" if exec_type == "ORDER_FILLED" else "submitted"
        return BracketResult(
            order_id=str(order.orderId), symbol=self._logical_or(symbol), side=side,
            qty=volume_to_units(volume), entry_price=fill,
            stop_loss_price=round(stop_loss, spec.digits),
            take_profit_price=round(take_profit, spec.digits) if take_profit is not None else None,
            status=status,
            raw={"position_id": str(order.positionId) if order.positionId else None,
                 "client_order_id": client_order_id, "volume": volume, "order_type": otype,
                 "execution_type": exec_type},
        )

    def modify_position_sltp(
        self,
        position_id: Any,
        stop_loss: float,
        take_profit: Optional[float] = None,
        symbol: Optional[str] = None,
    ) -> None:
        """Amend an open position's protection (ProtoOAAmendPositionSLTPReq).

        cTrader REPLACES both levels on amend: take_profit=None removes any TP,
        so callers that keep a fixed target must pass it again. Prices are
        rounded to the symbol's digits when `symbol` is given. Raises
        BrokerRejectedError when the venue rejects the amend.
        """
        _, msgs, model = _pb()
        self._require_session()
        if stop_loss is None:
            raise BrokerRejectedError("refusing to amend a position without a stop-loss")
        digits = self._spec(self._symbol_id(symbol)).digits if symbol else None

        def _r(px: float) -> float:
            return round(float(px), digits) if digits is not None else float(px)

        kwargs: dict[str, Any] = dict(
            ctidTraderAccountId=self.creds.account_id, positionId=int(position_id), stopLoss=_r(stop_loss),
        )
        if take_profit is not None:
            kwargs["takeProfit"] = _r(take_profit)
        res = self._call(msgs.ProtoOAAmendPositionSLTPReq(**kwargs))
        exec_type = model.ProtoOAExecutionType.Name(res.executionType) if _has(res, "executionType") else ""
        if exec_type in ("ORDER_REJECTED", "ORDER_CANCELLED", "ORDER_EXPIRED") or _payload_name(res) in (
                "ProtoOAOrderErrorEvent", "ProtoOAErrorRes"):
            raise BrokerRejectedError(
                f"cTrader amend SL/TP of position {position_id} rejected "
                f"({exec_type or _payload_name(res)} errorCode={getattr(res, 'errorCode', '')})")
        logger.info("[CTRADER] Amended position %s SL=%s TP=%s", position_id, kwargs["stopLoss"],
                    kwargs.get("takeProfit", "none"))

    def _logical_or(self, symbol: str) -> str:
        return self.resolve_symbol(symbol) or symbol.upper()

    def cancel_order(self, order_id: int) -> None:
        _, msgs, _ = _pb()
        self._call(msgs.ProtoOACancelOrderReq(ctidTraderAccountId=self.creds.account_id, orderId=int(order_id)))

    def reconcile(self) -> Any:
        _, msgs, _ = _pb()
        self._require_session()
        self.last_reconcile = self._call(msgs.ProtoOAReconcileReq(ctidTraderAccountId=self.creds.account_id))
        return self.last_reconcile

    def cancel_all_pending(self) -> int:
        """Cancel every working entry order on the account.

        Position SL/TP are attributes of the position on cTrader (not separate
        child orders as on IBKR) and are deliberately LEFT IN PLACE, so a halt
        never strips the protective stops from open positions."""
        _, _, model = _pb()
        res = self.reconcile()
        protective = model.ProtoOAOrderType.Value("STOP_LOSS_TAKE_PROFIT")
        n = 0
        for o in getattr(res, "order", []):
            if o.orderType == protective or getattr(o, "closingOrder", False):
                continue
            try:
                self.cancel_order(o.orderId)
                n += 1
            except BrokerError as exc:
                logger.error("[CTRADER] cancel order %s failed: %s", o.orderId, exc)
        logger.warning("[CTRADER] Cancelled %d working order(s)", n)
        return n

    # ── portfolio ───────────────────────────────────────────────────────────

    def _name_for_id(self, sid: int) -> str:
        for logical, (_, rid) in self._resolved.items():
            if rid == sid:
                return logical
        for name, rid in self._broker_symbols.items():
            if rid == sid:
                return name
        return str(sid)

    def positions(self) -> list[BrokerPosition]:
        _, _, model = _pb()
        res = self.reconcile()
        sell = model.ProtoOATradeSide.Value("SELL")
        out: list[BrokerPosition] = []
        for p in getattr(res, "position", []):
            td    = p.tradeData
            sid   = int(td.symbolId)
            units = volume_to_units(int(td.volume))
            if units == 0:
                continue
            is_long = td.tradeSide != sell
            avg     = float(p.price)
            name    = self._name_for_id(sid)
            try:
                cur = self._quote_by_id(sid)
            except BrokerError:
                cur = avg
            # P&L in the instrument's quote currency, like IBKR's unrealizedPNL,
            # so Argus's pnl / (qty × avg_cost) percentage stays consistent.
            pnl = (cur - avg) * units * (1 if is_long else -1)
            out.append(BrokerPosition(
                symbol=name, side="LONG" if is_long else "SHORT", qty=units,
                avg_cost=avg, current_price=cur, unrealized_pnl=pnl,
                position_id=str(p.positionId) if _has(p, "positionId") else None,
                stop_loss=float(p.stopLoss) if _has(p, "stopLoss") else None,
                take_profit=float(p.takeProfit) if _has(p, "takeProfit") else None,
            ))
        return out

    def closing_fills(self) -> dict[str, list[dict]]:
        """Closing deals (those carrying closePositionDetail) in the lookback window."""
        _, msgs, model = _pb()
        self._require_session()
        now_ms  = int(time.time() * 1000)
        from_ms = now_ms - self.deal_lookback_days * 86_400_000
        res = self._call(msgs.ProtoOADealListReq(
            ctidTraderAccountId=self.creds.account_id, fromTimestamp=from_ms, toTimestamp=now_ms, maxRows=500,
        ))
        filled = {model.ProtoOADealStatus.Value("FILLED"), model.ProtoOADealStatus.Value("PARTIALLY_FILLED")}
        out: dict[str, list[dict]] = {}
        for d in getattr(res, "deal", []):
            if not _has(d, "closePositionDetail") or d.dealStatus not in filled:
                continue
            ts = datetime.fromtimestamp(int(d.executionTimestamp) / 1000, tz=timezone.utc).isoformat()
            out.setdefault(self._name_for_id(int(d.symbolId)), []).append({
                "price": float(d.executionPrice),
                "qty":   volume_to_units(int(d.filledVolume or d.volume)),
                "time":  ts,
            })
        for sym in out:
            out[sym].sort(key=lambda r: r.get("time") or "")
        return out


class _TokenExpired(BrokerConfigError):
    """Internal: access token invalid/expired — triggers one refresh + retry."""


def _has(msg: Any, field_name: str) -> bool:
    try:
        return bool(msg.HasField(field_name))
    except (AttributeError, ValueError):
        return getattr(msg, field_name, None) is not None


def _mask_account(account_id: int) -> str:
    s = str(account_id)
    return ("*" * max(0, len(s) - 3)) + s[-3:]
