# Pantheon OS — Autonomous Trading Orchestrator

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat&logo=python&logoColor=white)
![Tests](https://img.shields.io/badge/Tests-189%20passing-brightgreen?style=flat)
![CI](https://img.shields.io/badge/CI%2FCD-GitHub%20Actions-2088FF?style=flat&logo=githubactions&logoColor=white)
![Status](https://img.shields.io/badge/Status-Paper%20Trading-orange?style=flat)
![Broker](https://img.shields.io/badge/Broker-Interactive%20Brokers-red?style=flat)
![Alerts](https://img.shields.io/badge/Alerts-Telegram-26A5E4?style=flat&logo=telegram&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-GHCR-2496ED?style=flat&logo=docker&logoColor=white)
![Supabase](https://img.shields.io/badge/DB-Supabase-3ECF8E?style=flat&logo=supabase&logoColor=white)
![Redis](https://img.shields.io/badge/Cache-Upstash%20Redis-DC382D?style=flat&logo=redis&logoColor=white)
![Grafana](https://img.shields.io/badge/Monitoring-Grafana-F46800?style=flat&logo=grafana&logoColor=white)

> **8-agent autonomous trading system. Eight gods, one mission. ZEUS is the supreme orchestrator — all agents report to it. Fully deployed, CI/CD green, live on `moremanamoreproblems.de`.**

---

![Pantheon Agent Lineup](screenshots/Screenshot%202026-05-22%20215144.png)

---

## Live URLs

| Service | URL |
|---|---|
| API health | `https://moremanamoreproblems.de/api/health` |
| Agent status | `https://moremanamoreproblems.de/api/agents` |
| Grafana dashboard | `https://moremanamoreproblems.de/grafana/` |
| WebSocket feed | `wss://moremanamoreproblems.de/ws` |

---

## Architecture

```
  Hermes (live Railway API — 590+ suppliers)
        ↓
  [1] Icarus  — Signal Watcher
        ↓  RawSignal
  [2] Hades   — Compliance Filter      ← OFAC · EU sanctions · ESG · LkSG
        ↓  FilteredSignal (or KILL)
  [3] Artemis — Macro Context          ← VIX · S&P500 regime · sector ETFs
        ↓  MacroContext (or SUPPRESS)
  [4] Pythia  — Pattern & Sizing       ← Supabase hit rates → Kelly-sized positions
        ↓  SizedSignal (or SKIP)
  [5] ZEUS    — LLM Reasoning          ← Claude Haiku · ChromaDB KB · past decisions
        ↓  approved / resized / rejected
  [6] Ares    — Trade Execution        ← IBKR bracket order (entry + SL + TP)
        ↓  TradeResult
  [7] Argus   — Portfolio Monitor      ← drawdown kill switch · Telegram alerts
        ↓  outcome → Pythia + KB (feedback loop)

  [Apollo] — Daily research cycle (runs parallel, not in signal path)
     ├── arXiv q-fin paper ingestion → ChromaDB
     ├── Hermes earnings enrichment → ChromaDB
     ├── Ticker map maintenance → data/ticker_map.json
     └── Self-improvement loop → analyses traces → updates zeus_skills.md

  Upstash Redis bridge → SpendLens (procurement intelligence platform)
     ├── zeus:macro:latest          — live market regime
     ├── zeus:decisions:recent      — ZEUS trade decisions as Icarus signals
     └── zeus:supplier_risk:{slug}  — Hades compliance per vendor
```

**ZEUS** owns the entire pipeline. No agent communicates with another directly. Only `zeus.py` imports from `agents/*`. All agents import from `core.types` only — no spaghetti.

---

## Agents

| # | Agent | Mythology | Role |
|---|---|---|---|
| 1 | **Icarus** | Flies closest to the sun — first to see market signals | Monitors the live Hermes API (590+ suppliers). Classifies events by category and severity. Deduplicates across poll cycles. |
| 2 | **Hades** | Lord of the underworld — judges who passes | Compliance firewall. OFAC, EU sanctions (BaFin/Reg 833/2014), ESG sector flags, LkSG violations → hard kill or severity downgrade. Full audit trail. |
| 3 | **Artemis** | Goddess of the hunt — tracks conditions, picks the moment | Fetches VIX, S&P 500 1-month return, and 6 sector ETFs. Classifies market regime (bull/bear/sideways). Suppresses signals that conflict with macro environment. 15-min cache. |
| 4 | **Pythia** | Oracle of Delphi — reads patterns, predicts outcomes | Learning agent. Every signal → outcome in Supabase. Derives position size from historical hit rates per `{category}×{regime}×{VIX band}`. Kelly-inspired sizing (capped at 5%). |
| 5 | **ZEUS** | King of Olympus — final word | LLM reasoning via Claude Haiku. Queries ChromaDB knowledge base. Approves, resizes, or rejects trades with structured JSON rationale. |
| 6 | **Ares** | God of decisive action — executes the strike | Places bracket orders on Interactive Brokers via `ib_insync`. Entry + 3% stop-loss + 6% take-profit. XETRA-aware. Paper port 7497 / live port 7496. |
| 7 | **Argus** | Hundred-eyed giant — watches everything, never sleeps | Tracks portfolio equity and drawdown in real time. Emergency halt + Telegram alert if drawdown ≥ 8%. Backfills closed-trade P&L into Pythia and ChromaDB. |
| 8 | **Apollo** | God of knowledge and truth — the librarian | Runs daily: ingests arXiv q-fin papers, crawls Hermes for earnings transcripts, maintains the live supplier→ticker map, runs the self-improvement loop. |

---

## Infrastructure

| Component | Detail |
|---|---|
| **Server** | Hostinger VPS — Ubuntu 24.04, 2 vCPU, 4 GB RAM |
| **Domain** | `moremanamoreproblems.de` → SSL via Let's Encrypt |
| **Containers** | `zeus` · `dashboard` · `grafana` · `redis` · `nginx` (Docker Compose) |
| **Image registry** | GitHub Container Registry (`ghcr.io/eugnmueller-87/pantheon`) |
| **Database** | Supabase (PostgreSQL + pgvector + RLS) — 11 tables |
| **Cache** | Upstash Redis — shared with SpendLens via RedisBridge |
| **Monitoring** | Grafana — live trading dashboard, reads Supabase directly |
| **CI/CD** | GitHub Actions: test (189 tests) → build (GHCR) → deploy (SSH) → frontend |

---

## CI/CD Pipeline

Every push to `main`:

```
test (189/189) → build → push to GHCR → SSH deploy to server → Cloudflare Pages (frontend)
```

All jobs must pass before deploy. The quality gate blocks shipping broken code.

---

## Tech Stack

| Component | Tool |
|---|---|
| Orchestration | `zeus.py` (plain Python — no LangGraph, no LangChain) |
| Signal source | Hermes (Railway) — 590+ procurement suppliers |
| Market data | yfinance — VIX, SPY, sector ETFs |
| Knowledge base | ChromaDB — local persistent vector store |
| Trade memory | Supabase PostgreSQL (SQLite fallback in dev) |
| LLM reasoning | Claude Haiku — ~$0.001/call, structured JSON output |
| Execution | Interactive Brokers via `ib_insync` |
| Alerts | Telegram Bot API |
| Intelligence bridge | Upstash Redis — shared with SpendLens |
| Reverse proxy | nginx — SSL termination, subpath routing |
| Monitoring | Grafana 11 — provisioned dashboards, Supabase datasource |

---

## Quickstart (local dev)

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

Copy `.env.example` to `.env` and fill in:

```env
ANTHROPIC_API_KEY=sk-ant-...
HERMES_API_KEY=
UPSTASH_REDIS_REST_URL=https://...
UPSTASH_REDIS_REST_TOKEN=...
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
SUPABASE_URL=https://...supabase.co
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=
```

### 3. Run tests

```bash
pytest tests/ -q --timeout=30
# 189 tests, all green
```

### 4. Start the pipeline server

```bash
python main.py
# ZEUS listens on http://localhost:8080
```

### 5. Start the dashboard

```bash
cd dashboard/frontend && npm install && npm run dev
# Dashboard → http://localhost:5173
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/run` | Trigger one pipeline cycle (Icarus → Argus) |
| `POST` | `/run/research` | Trigger Apollo's daily research cycle |
| `POST` | `/halt` | Emergency halt — cancels pending orders |
| `POST` | `/resume` | Resume after halt |
| `GET` | `/status` | Portfolio equity, drawdown, circuit breaker states |
| `GET` | `/health` | Liveness check |
| `GET` | `/api/agents` | Watchdog health report for all 8 agents |

---

## Pipeline Kill Switches

| Trigger | Action |
|---|---|
| Portfolio drawdown ≥ 8% | Emergency halt + Telegram alert |
| OFAC / EU sanctions match | Signal killed at Hades, logged for audit |
| ESG / LkSG flag | Signal severity downgraded |
| VIX ≥ 35 | All signals suppressed |
| Bear regime + high VIX | Positive signals suppressed |
| Agent failure (3 restarts) | Watchdog alert + graceful degradation via circuit breaker |
| `POST /halt` | Manual halt via API |

---

## Grafana Dashboard

Live at `https://moremanamoreproblems.de/grafana/` — provisioned automatically, reads Supabase directly.

| Panel | What it shows |
|---|---|
| Equity Curve | Total equity + peak over 7 days |
| Current Drawdown % | Live gauge with red/yellow/green thresholds |
| Total Equity | Latest value in € |
| Win Rate by Category | Historical win % per signal category |
| Kill Stage Distribution | Where signals die (Hades / Artemis / Pythia / Ares) |
| Agent Health | All 8 agents with live status and last-check timestamp |
| Recent Trades | Last 50 trades, WIN/LOSS/OPEN color-coded |
| Monthly Returns | Win rate + avg P&L % by month |

---

## Project Structure

```
ZEUS/
├── main.py                      # Webhook server + standalone mode
├── requirements.txt
├── Dockerfile
├── agents/
│   ├── zeus.py                  # Supreme orchestrator — owns the pipeline
│   ├── icarus.py                # Signal watcher — Hermes API
│   ├── hades.py                 # Compliance filter — OFAC, ESG, EU sanctions
│   ├── artemis.py               # Macro context — VIX, regime, sector momentum
│   ├── pythia.py                # Pattern learning — Kelly-sized positions
│   ├── ares.py                  # Trade execution — IBKR live/paper
│   ├── ares_mock.py             # Mock execution — no IB needed
│   ├── argus.py                 # Portfolio monitor — drawdown kill switch
│   └── apollo.py                # Research — KB seeding + self-improvement
├── core/
│   ├── types.py                 # Single source of truth for all data contracts
│   ├── knowledge_base.py        # ChromaDB wrapper (shared KB)
│   ├── agent_knowledge.py       # Per-agent private skills KB
│   ├── circuit_breaker.py       # Per-agent fault isolation
│   ├── watchdog.py              # Agent health daemon + auto-restart
│   └── redis_bridge.py          # ZEUS → SpendLens intelligence feed
├── dashboard/
│   ├── backend/server.py        # FastAPI WebSocket backend (port 8081)
│   └── frontend/                # React + Vite dashboard
├── infra/
│   └── hetzner/
│       ├── docker-compose.prod.yml
│       ├── nginx.prod.conf
│       └── grafana/             # Provisioned datasource + dashboards
├── tests/                       # 189 tests — full pipeline coverage
├── knowledge/                   # ChromaDB seed docs + per-agent skills
└── .github/workflows/deploy.yml # CI/CD pipeline
```

---

## SpendLens Integration

ZEUS writes live intelligence to a shared Upstash Redis instance, readable by [SpendLens](https://github.com/eugnmueller-87/PROCUREMENT).

```
ZEUS pipeline run
  → Hades assesses supplier      → zeus:supplier_risk:{slug}
  → Artemis classifies macro     → zeus:macro:latest
  → ZEUS approves/rejects trade  → zeus:decisions:recent (last 50)
```

---

## Roadmap

- [x] 8-agent pipeline (Icarus → Hades → Artemis → Pythia → ZEUS → Ares → Argus + Apollo)
- [x] Supabase PostgreSQL — 11 tables, pgvector, RLS
- [x] Circuit breakers + Watchdog daemon (zero-outage design)
- [x] Claude Haiku LLM reasoning step in ZEUS
- [x] CI/CD — GitHub Actions, 189 tests, auto-deploy to Hetzner
- [x] Docker image on GHCR, production stack on Hostinger VPS
- [x] SSL + domain (`moremanamoreproblems.de`) via Let's Encrypt
- [x] Grafana monitoring — provisioned dashboards, live Supabase connection
- [x] Executive dashboard — React + FastAPI WebSocket
- [x] Upstash Redis bridge → SpendLens intelligence feed
- [x] Apollo daily research cycle (arXiv, Hermes earnings, ticker map, self-improvement)
- [ ] IBKR paper trading account — connect Ares to live execution
- [ ] Cloudflare Pages — deploy React dashboard to CDN
- [ ] OpenBB swap in Artemis (DAX + EURO STOXX 50 coverage)
- [ ] Phase 2 — Redis Streams async event bus between agents
- [ ] Phase 3 — Crypto layer via Binance EU

---

## Notes

- **Germany-based**: Alpaca does not support German residents. Interactive Brokers (IBKR) is the execution layer — EU-regulated, German tax-compliant, paper trading available for free.
- **Paper trading by default**: `"paper_trading": true` and `"mock_execution": true` in settings. No real money at risk until explicitly opted in.
- **Pattern learning needs data**: Pythia requires ~10 historical trades per context key before learned hit rates replace default sizing. Run paper trading for 4–6 weeks before the learning layer becomes meaningful.
- **Vault rule**: Vault money only moves one direction — into it, never back to trading. ZEUS never moves Vault money autonomously.

---

*Built by [Eugen Mueller](https://github.com/eugnmueller-87) — Procurement Leader → AI Engineer*
