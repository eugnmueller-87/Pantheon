"""
Pantheon OS — Main Entry Point

Two run modes:
  1. n8n webhook server (default) — n8n triggers ZEUS via HTTP POST
  2. Standalone loop — python main.py --standalone (for local testing)

Endpoints:
  POST /run    → one pipeline cycle
  POST /halt   → emergency halt
  GET  /status → portfolio + agent health state
  GET  /health → liveness check
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv

from agents.zeus import ZeusConfig, ZeusOrchestrator
from config.settings import load_settings
from core.logging_setup import configure_logging

load_dotenv()   # loads .env file if present

logger = logging.getLogger("main")

_API_KEY = os.getenv("ZEUS_API_KEY")  # if unset → auth disabled (local dev)


def build_zeus() -> ZeusOrchestrator:
    settings = load_settings()
    config = ZeusConfig(
        max_portfolio_drawdown_pct = settings.get("max_drawdown_pct", 0.08),
        max_open_positions         = settings.get("max_open_positions", 10),
        paper_trading              = settings.get("paper_trading", True),
        mock_execution             = settings.get("mock_execution", True),
        use_llm_reasoning          = settings.get("use_llm_reasoning", True),
        hermes_base_url            = settings.get("hermes_base_url"),
        default_account_equity     = settings.get("default_account_equity", 100_000.0),
        starting_equity            = settings.get("starting_equity", 100_000.0),
        stop_loss_pct              = settings.get("stop_loss_pct", 0.03),
        take_profit_pct            = settings.get("take_profit_pct", 0.06),
    )
    return ZeusOrchestrator(config)


# ---------------------------------------------------------------------------
# n8n Webhook Server
# ---------------------------------------------------------------------------

# Single-process design: _zeus is module-level state shared between the HTTP
# handler threads and the auto-run daemon. This is intentional — the server
# runs as one process with workers=1. Do not scale to multiple workers without
# replacing this with a proper job queue (e.g. Celery + Redis).
_zeus: ZeusOrchestrator | None = None
_run_lock = threading.Lock()  # prevents concurrent run_once() calls


class ZeusHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logger.debug("HTTP %s", format % args)

    def _check_api_key(self) -> bool:
        if not _API_KEY:
            return True  # auth disabled in local dev
        provided = self.headers.get("X-API-Key", "")
        if provided != _API_KEY:
            self._json_response(401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self):
        if self.path == "/status":
            self._handle_status()
        elif self.path == "/health":
            self._json_response(200, {"status": "ok", "pipeline": _zeus.status.value if _zeus else "not started"})
        elif self.path == "/agents":
            self._handle_agents()
        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self):
        if not self._check_api_key():
            return
        if self.path == "/run":
            self._handle_run()
        elif self.path in ("/run/research", "/run/research/historical"):
            historical = self.path.endswith("/historical")
            self._handle_research(historical=historical)
        elif self.path == "/run/backtest":
            self._handle_backtest()
        elif self.path == "/run/replay":
            self._handle_replay()
        elif self.path == "/halt":
            self._handle_halt()
        elif self.path == "/resume":
            self._handle_resume()
        elif self.path == "/alert":
            self._handle_alert()
        else:
            self._json_response(404, {"error": "not found"})

    def _handle_run(self):
        try:
            if not _run_lock.acquire(blocking=False):
                self._json_response(409, {"error": "pipeline already running"})
                return
            try:
                runs = _zeus.run_once()
            finally:
                _run_lock.release()
            summary = [
                {
                    "run_id":      r.run_id,
                    "killed_at":   r.killed_at_stage,
                    "kill_reason": r.kill_reason,
                    "reasoning":   r.trace.zeus_reasoning if r.trace else None,
                    "trade": {
                        "symbol":   r.trade_result.symbol,
                        "side":     r.trade_result.side,
                        "order_id": r.trade_result.order_id,
                        "fill":     r.trade_result.fill_price,
                    } if r.trade_result and r.trade_result.symbol else None,
                }
                for r in runs
            ]
            self._json_response(200, {"pipeline_runs": summary, "count": len(runs)})
        except Exception as exc:
            logger.exception("[MAIN] /run failed")
            self._json_response(500, {"error": str(exc)})

    def _handle_research(self, historical: bool = False):
        try:
            summary = _zeus.run_research_cycle(historical=historical)
            self._json_response(200, {"status": "ok", "historical": historical, "research": summary})
        except Exception as exc:
            logger.exception("[MAIN] /run/research failed")
            self._json_response(500, {"error": str(exc)})

    def _handle_backtest(self):
        try:
            summary = _zeus.run_backtest()
            self._json_response(200, {"status": "ok", "backtest": summary})
        except Exception as exc:
            logger.exception("[MAIN] /run/backtest failed")
            self._json_response(500, {"error": str(exc)})

    def _handle_replay(self):
        try:
            summary = _zeus.run_replay()
            self._json_response(200, {"status": "ok", "replay": summary})
        except Exception as exc:
            logger.exception("[MAIN] /run/replay failed")
            self._json_response(500, {"error": str(exc)})

    def _handle_alert(self):
        """POST /alert — send a Telegram alert via Argus.
        Body: {"message": "...", "source": "..."} (source is optional label)
        Used by n8n VPS watchdog and any external monitor.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            message = body.get("message", "").strip()
            source  = body.get("source", "external")
            if not message:
                self._json_response(400, {"error": "message field required"})
                return
            full_msg = f"[{source}] {message}"
            _zeus.argus.send_alert(full_msg)
            logger.info("[MAIN] /alert sent from %s: %s", source, message[:80])
            self._json_response(200, {"status": "sent", "message": full_msg})
        except Exception as exc:
            logger.exception("[MAIN] /alert failed")
            self._json_response(500, {"error": str(exc)})

    def _handle_halt(self):
        _zeus.halt(reason="n8n manual halt")
        self._json_response(200, {"status": "halted"})

    def _handle_resume(self):
        _zeus.resume()
        self._json_response(200, {"status": "running"})

    def _handle_status(self):
        state = _zeus.argus.portfolio_state()
        cb_status = _zeus.cb.status()
        self._json_response(200, {
            "pipeline_status":  _zeus.status.value,
            "open_positions":   _zeus.argus.open_position_count(),
            "equity":           state.total_equity,
            "drawdown_pct":     round(state.current_drawdown_pct * 100, 2),
            "paper_trading":    _zeus.config.paper_trading,
            "mock_execution":   _zeus.config.mock_execution,
            "circuit_breakers": cb_status,
            "seniority":        _zeus.get_seniority_report(),
        })

    def _handle_agents(self):
        reports = _zeus.get_health_reports()
        self._json_response(200, {
            "agents": [
                {
                    "name":    r.agent_name,
                    "status":  r.status.value,
                    "message": r.message,
                    "checked": r.checked_at.isoformat(),
                }
                for r in reports
            ]
        })

    def _json_response(self, code: int, body: dict):
        payload = json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(payload))
        self.end_headers()
        self.wfile.write(payload)


def _is_market_open() -> bool:
    """
    Returns True if the NYSE is currently open for regular trading.
    Hours: Mon–Fri 09:30–16:00 ET (UTC-4 in summer, UTC-5 in winter).
    Uses a simple UTC offset — no external dependency required.
    """
    # US Eastern: UTC-4 (EDT, Mar–Nov) or UTC-5 (EST, Nov–Mar)
    now_utc = datetime.now(timezone.utc)
    # Approximate EDT/EST switch: second Sunday of March / first Sunday of November
    # Good enough for trading purposes — a few days of error at DST boundary is fine
    month = now_utc.month
    et_offset = -4 if 3 <= month <= 10 else -5
    now_et = now_utc + timedelta(hours=et_offset)

    # Weekend check
    if now_et.weekday() >= 5:  # 5=Sat, 6=Sun
        return False

    # Regular session: 09:30–16:00 ET
    open_time  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_time = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_time <= now_et < close_time


def _auto_run_loop(interval_seconds: int):
    """Background thread: run the pipeline on a fixed schedule."""
    logger.info("[MAIN] Auto-run scheduler started — every %ds", interval_seconds)
    time.sleep(30)  # give Zeus time to fully initialise before first run
    while True:
        try:
            if not _is_market_open():
                logger.info("[MAIN] Auto-run skipped — market closed")
            elif _zeus and _zeus.status.value != "halted":
                if _run_lock.acquire(blocking=False):
                    try:
                        logger.info("[MAIN] Auto-run triggered")
                        runs = _zeus.run_once()
                        logger.info("[MAIN] Auto-run complete — %d signal(s) processed", len(runs))
                    finally:
                        _run_lock.release()
                else:
                    logger.info("[MAIN] Auto-run skipped — pipeline already running")
        except Exception as exc:
            logger.exception("[MAIN] Auto-run error: %s", exc)
        time.sleep(interval_seconds)


def run_webhook_server(host: str = "0.0.0.0", port: int = 8080):
    import threading
    from socketserver import ThreadingMixIn

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    global _zeus
    _zeus = build_zeus()

    # Auto-run pipeline every RUN_INTERVAL seconds (default 15 min)
    interval = int(os.getenv("RUN_INTERVAL", "900"))
    t = threading.Thread(target=_auto_run_loop, args=(interval,), daemon=True)
    t.start()
    logger.info("[MAIN] Auto-scheduler: pipeline every %ds", interval)

    server = ThreadedHTTPServer((host, port), ZeusHandler)
    logger.info("[MAIN] ZEUS webhook server on %s:%d", host, port)
    logger.info("[MAIN] n8n → POST http://localhost:%d/run", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("[MAIN] Shutting down.")
        _zeus.watchdog.stop()


# ---------------------------------------------------------------------------
# Standalone loop
# ---------------------------------------------------------------------------

def run_standalone(interval_seconds: int = 900):
    zeus = build_zeus()
    logger.info("[MAIN] Standalone mode — every %ds", interval_seconds)
    while True:
        try:
            runs = zeus.run_once()
            logger.info("[MAIN] Cycle complete — %d run(s).", len(runs))
        except Exception as exc:
            logger.exception("[MAIN] Cycle error: %s", exc)
        time.sleep(interval_seconds)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    configure_logging()
    parser = argparse.ArgumentParser(description="ZEUS Trading Orchestrator")
    parser.add_argument("--standalone", action="store_true")
    parser.add_argument("--interval", type=int, default=900)
    parser.add_argument("--port",     type=int, default=8080)
    args = parser.parse_args()

    if args.standalone:
        run_standalone(interval_seconds=args.interval)
    else:
        run_webhook_server(port=args.port)
