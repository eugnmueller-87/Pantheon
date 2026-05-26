"""
Pantheon OS — Dashboard WebSocket Server

Runs alongside main.py on port 8081.
Streams real-time pipeline events to the React dashboard via WebSocket.
Also exposes REST endpoints for historical data.

Events pushed to clients:
  pipeline_event  — stage transitions (icarus_fetched, hades_kill, trade_placed, …)
  status_update   — portfolio equity, drawdown, agent health (every 5s)
  agent_health    — watchdog reports (every 30s)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

# Make parent importable when running standalone
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

logger = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

app = FastAPI(title="Pantheon OS Dashboard", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Event bus — in-memory ring buffer shared between ZEUS pipeline and WS clients
# ---------------------------------------------------------------------------

MAX_EVENTS = 500

class EventBus:
    def __init__(self):
        self._events: deque[dict] = deque(maxlen=MAX_EVENTS)
        self._clients: list[WebSocket] = []

    def record(self, event_type: str, data: dict) -> None:
        event = {
            "id":        int(time.time() * 1000),
            "type":      event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        self._events.append(event)
        asyncio.create_task(self._broadcast(event))

    async def _broadcast(self, event: dict) -> None:
        dead: list[WebSocket] = []
        for ws in self._clients:
            try:
                await ws.send_text(json.dumps(event, default=str))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.remove(ws)

    def recent(self, limit: int = 100) -> list[dict]:
        events = list(self._events)
        return events[-limit:]

    def subscribe(self, ws: WebSocket) -> None:
        self._clients.append(ws)

    def unsubscribe(self, ws: WebSocket) -> None:
        if ws in self._clients:
            self._clients.remove(ws)


bus = EventBus()


# ---------------------------------------------------------------------------
# ZEUS bridge — lazy import so dashboard can start without full ZEUS stack
# ---------------------------------------------------------------------------

_zeus = None

def get_zeus():
    global _zeus
    if _zeus is None:
        try:
            from agents.zeus import ZeusOrchestrator
            from config.settings import load_settings
            settings = load_settings()
            from agents.zeus import ZeusConfig
            config = ZeusConfig(
                max_portfolio_drawdown_pct=settings.get("max_drawdown_pct", 0.08),
                max_open_positions=settings.get("max_open_positions", 10),
                paper_trading=settings.get("paper_trading", True),
                mock_execution=settings.get("mock_execution", True),
                use_llm_reasoning=settings.get("use_llm_reasoning", True),
            )
            _zeus = ZeusOrchestrator(config)
            _zeus.icarus._base_url = settings.get(
                "hermes_base_url", "https://hermes-agent-production-114e.up.railway.app"
            )
            if not _zeus.icarus._api_key:
                _zeus.icarus._api_key = os.getenv("HERMES_API_KEY", "")
        except Exception as exc:
            logger.warning("ZEUS not available: %s", exc)
    return _zeus


# ---------------------------------------------------------------------------
# Instrumented pipeline runner — injects events into bus
# ---------------------------------------------------------------------------

async def _run_pipeline_cycle() -> dict:
    zeus = get_zeus()
    if zeus is None:
        bus.record("error", {"message": "ZEUS not initialised"})
        return {"error": "zeus_unavailable"}

    bus.record("pipeline_start", {"message": "Pipeline cycle starting"})

    try:
        # Monkey-patch the internal _process_signal to emit events
        original_process = zeus._process_signal

        def instrumented_process(raw_signal):
            bus.record("icarus_signal", {
                "supplier": raw_signal.supplier,
                "headline": raw_signal.headline[:120],
                "category": raw_signal.category.value,
                "severity": raw_signal.severity.value,
                "tickers":  raw_signal.affected_tickers,
            })
            run = original_process(raw_signal)
            if run.killed_at_stage:
                bus.record("signal_killed", {
                    "stage":   run.killed_at_stage,
                    "reason":  run.kill_reason,
                    "supplier": raw_signal.supplier,
                })
            elif run.trade_result and run.trade_result.symbol:
                bus.record("trade_placed", {
                    "symbol":    run.trade_result.symbol,
                    "side":      run.trade_result.side,
                    "fill":      run.trade_result.fill_price,
                    "order_id":  run.trade_result.order_id,
                    "reasoning": run.trace.zeus_reasoning if run.trace else None,
                    "confidence": run.sized_signal.confidence if run.sized_signal else None,
                    "size_pct":  run.sized_signal.position_size_pct if run.sized_signal else None,
                })
            return run

        zeus._process_signal = instrumented_process
        runs = zeus.run_once()
        zeus._process_signal = original_process

        bus.record("pipeline_complete", {
            "runs":   len(runs),
            "trades": sum(1 for r in runs if r.trade_result and r.trade_result.symbol),
            "kills":  sum(1 for r in runs if r.killed_at_stage),
        })
        return {"runs": len(runs)}
    except Exception as exc:
        bus.record("error", {"message": str(exc)})
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Background status poller — pushes portfolio state every 5s
# ---------------------------------------------------------------------------

async def _status_poller():
    while True:
        await asyncio.sleep(5)
        zeus = get_zeus()
        if zeus is None:
            continue
        try:
            state    = zeus.argus._state
            cb_status = zeus.cb.status()
            bus.record("status_update", {
                "pipeline_status": zeus.status.value,
                "equity":          state.total_equity,
                "drawdown_pct":    round(state.current_drawdown_pct * 100, 2),
                "open_positions":  zeus.argus.open_position_count(),
                "paper_trading":   zeus.config.paper_trading,
                "circuit_breakers": cb_status,
            })
        except Exception as exc:
            logger.debug("Status poll error: %s", exc)


async def _health_poller():
    while True:
        await asyncio.sleep(30)
        zeus = get_zeus()
        if zeus is None:
            continue
        try:
            reports = zeus.get_health_reports()
            bus.record("agent_health", {
                "agents": [
                    {
                        "name":    r.agent_name,
                        "status":  r.status.value,
                        "message": r.message,
                    }
                    for r in reports
                ]
            })
        except Exception as exc:
            logger.debug("Health poll error: %s", exc)


@app.on_event("startup")
async def startup():
    asyncio.create_task(_status_poller())
    asyncio.create_task(_health_poller())
    logger.info("Dashboard server started — WebSocket on /ws")


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    bus.subscribe(ws)
    logger.info("Client connected: %s", ws.client)

    # Send last 50 events on connect so client has history immediately
    recent = bus.recent(50)
    for evt in recent:
        await ws.send_text(json.dumps(evt, default=str))

    try:
        while True:
            data = await ws.receive_text()
            cmd = json.loads(data)
            if cmd.get("action") == "run_pipeline":
                asyncio.create_task(_run_pipeline_cycle())
            elif cmd.get("action") == "halt":
                zeus = get_zeus()
                if zeus:
                    zeus.halt("dashboard manual halt")
                    bus.record("halt", {"message": "Pipeline halted via dashboard"})
            elif cmd.get("action") == "resume":
                zeus = get_zeus()
                if zeus:
                    zeus.resume()
                    bus.record("resume", {"message": "Pipeline resumed via dashboard"})
    except WebSocketDisconnect:
        bus.unsubscribe(ws)
        logger.info("Client disconnected: %s", ws.client)


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/status")
def api_status():
    zeus = get_zeus()
    if zeus is None:
        return JSONResponse({"available": False})
    state = zeus.argus._state
    return {
        "available":        True,
        "pipeline_status":  zeus.status.value,
        "equity":           state.total_equity,
        "drawdown_pct":     round(state.current_drawdown_pct * 100, 2),
        "open_positions":   zeus.argus.open_position_count(),
        "paper_trading":    zeus.config.paper_trading,
        "mock_execution":   zeus.config.mock_execution,
        "circuit_breakers": zeus.cb.status(),
    }


@app.get("/api/events")
def api_events(limit: int = 100):
    return {"events": bus.recent(limit)}


@app.get("/api/agents")
def api_agents():
    zeus = get_zeus()
    if zeus is None:
        return JSONResponse({"available": False})
    reports = zeus.get_health_reports()
    return {
        "agents": [
            {
                "name":    r.agent_name,
                "status":  r.status.value,
                "message": r.message,
                "checked": r.checked_at.isoformat(),
            }
            for r in reports
        ]
    }


@app.get("/api/health")
def api_health():
    return {"status": "ok", "service": "pantheon-dashboard"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8081, reload=False)
