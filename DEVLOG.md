# Pantheon OS — Dev Log

*A brutally honest account of deploying an autonomous trading system and everything that went wrong.*

---

## Session 1 — Getting the pipeline to actually run

### 1. Server survived a reboot — but we didn't know it

First crisis: SSH dropped mid-session. Spent 20 minutes writing a startup script before discovering `restart: always` in Docker Compose already handles reboots. The system came back up on its own.

**Lesson:** Read the compose file before writing systemd units.

---

### 2. ChromaDB silently running in-memory

Zeus's knowledge base appeared to work — 267 chunks loaded, research cycles completing. Then the container restarted: 0 chunks. ChromaDB was falling back to in-memory because `/home/pantheon` didn't exist.

```dockerfile
# Before (broken):
RUN useradd -r -u 1001 pantheon
# After (fixed):
RUN useradd -r -u 1001 -m pantheon && chown -R pantheon:pantheon /app
```

Without a home directory, ChromaDB couldn't create lock files and silently fell back to RAM. Every container restart wiped the knowledge base.

---

### 3. JSON parsing failed silently

Claude sometimes wraps JSON in markdown fences. The original `re.search(r"\{.*\}", raw, re.DOTALL)` failed on nested objects → `"No JSON found"` → fallback to pattern confidence → all signals rejected.

**Fix:** Balanced brace parser that walks character by character and finds the outermost `{}` regardless of surrounding markdown.

---

### 4. All signals had empty tickers

Every signal killed at Zeus. Reason: `"Empty ticker list renders this unexecutable"`. Icarus had a hardcoded map of 16 suppliers. Hermes was sending Cisco, Zoom, Workday, Google Cloud — none in the map.

First instinct: add more entries. Whack-a-mole.

**Real fix:** Inject Apollo's `get_ticker()` into Icarus as a resolver. Apollo queries `ticker_map.json`, does case-insensitive/partial matching, falls back to live yfinance search, caches results permanently. New supplier → resolved automatically and remembered.

---

### 5. Serial ticker lookups blocked the pipeline

10 signals × unknown suppliers × 2s yfinance lookup = 20s blocking before a single signal reached Hades.

**Fix:** Parallel lookup with `ThreadPoolExecutor`, max 4 workers, 10s timeout. Known suppliers never touched.

---

### 6. Zeus LLM was truncating responses

`max_tokens=800` on Haiku. Claude was hitting the limit mid-reasoning, producing `reasoning` fields of 40 characters. Auto-rejection threshold: 80 characters. Every signal rejected.

**Fix:** Raised to `max_tokens=1500`.

---

### 7. Bracketed paste mode corrupted every SSH command

Pasting multi-line commands prepended `^[[200~` to everything. Commands failed with bizarre syntax errors.

**Fix:** `printf '\e[?2004l'` disables bracketed paste mode for the session.

---

### 8. Docker Compose env file path resolution

Running `docker compose -f infra/hetzner/docker-compose.prod.yml --env-file .env` resolved `.env` relative to the compose file directory, not the working directory. Variables not found.

**Fix:** Symlink `ln -sf /opt/pantheon/.env /opt/pantheon/infra/hetzner/.env`.

---

### 9. Port 80 held by a ghost process

After redeploy, nginx wouldn't start. `ss -tlnp | grep :80` showed PIDs 724 and 727 — stale nginx from previous run Docker hadn't cleaned up.

**Fix:** `kill -9 724 727` then redeploy.

---

### 10. Zeus was an FAQ bot, not a trading director

After all plumbing fixed, Zeus was still rejecting everything — with sophisticated reasoning:

```
'This is the SEVENTH consecutive identical structural rejection of this exact
signal configuration. The pattern itself is a systemic failure signal.'
```

Zeus was right. But the problem was he had no real intelligence. He was querying ChromaDB for generic arXiv papers, getting no company-specific context, using Haiku, evaluating each signal in isolation, forgetting every decision immediately.

**Fix:** Complete rethink:
1. **Apollo enriches every signal** — before Zeus decides on Cisco, Apollo calls yfinance right now: P/E, revenue growth, analyst consensus, next earnings date.
2. **Zeus reads his own past decisions** — `query_ticker_history()` in KB. Before deciding on CSCO, Zeus sees his last CSCO trade and outcome.
3. **Zeus tracks the portfolio he's building** — intra-run approved trades list.
4. **Zeus reads his own self-critique** — Apollo's self-improvement loop writes bias analysis to `zeus_skills.md`.
5. **Upgraded to Sonnet** — the model that actually reasons.

---

### 11. Hermes signal IDs weren't valid UUIDs

Supabase stores `trace_id` as UUID. Hermes sends `"343427e0d2d26cc77edca2736d492dc7"` — 32-char hex, no dashes. Every decision trace insert failed.

**Fix:** `_sanitize_signal_id()` converts any non-UUID string to a deterministic `uuid5` hash. Same signal always gets same UUID, deduplication still works.

---

### 12. Google Cloud, AWS, Azure → empty tickers

"Google Cloud" is not a company. It's a division of Alphabet. Hermes sends many signals for cloud products with no direct ticker.

**Fix:** `_DIVISION_PARENT_MAP` — explicit mapping of product/division names to parent tickers. Google Cloud → GOOGL, AWS → AMZN, Azure → MSFT. Checked before the company map.

---

## Session 2 — IB Gateway connectivity, Supabase, Grafana

### 13. IB Gateway listening on IPv6 only

`ib_insync` connects IPv4 but IB Gateway Java listens on `:::4002` (IPv6 wildcard). TimeoutError every time.

**Fix:** socat bridge inside the container: `TCP-LISTEN:4004,fork TCP:127.0.0.1:4002`. Connect on port 4004.

---

### 14. IBC ReadOnlyApi=yes — can't place orders

IBC config template uses `envsubst` to fill variables. `${READ_ONLY_API:-no}` was sent literally because `envsubst` doesn't support bash default syntax.

**Fix:** Hardcode `ReadOnlyApi=no` directly in `config.ini.tmpl`.

---

### 15. eventkit asyncio import-time crash

`ib_insync` depends on `eventkit`, which calls `asyncio.get_event_loop_policy().get_event_loop()` at module import time. In Python 3.10+ non-main threads this raises `RuntimeError: There is no current event loop in thread`.

**Fix:** Call `asyncio.set_event_loop(asyncio.new_event_loop())` BEFORE `from ib_insync import IB`. Every request thread needs this pattern.

---

### 16. Always-disconnect pattern for ib_insync

Each HTTP request thread creates a new event loop. `ib.isConnected()` checks the old thread's loop and fails. Result: stale connections, TimeoutErrors on every second call.

**Fix:** Always disconnect and reconnect at the start of each `_get_connection()` call. No connection pooling — fresh connection every time.

---

### 17. NetLiquidation returning nothing

IB paper account doesn't return `NetLiquidation/BASE` — it returns `NetLiquidationByCurrency/BASE`. Ares and Argus were reading `€0` equity and refusing to trade.

**Fix:** Change tag from `"NetLiquidation"` to `"NetLiquidationByCurrency"` in both Ares and Argus.

---

### 18. Telegram recursion crash

`argus.send_alert()` → Zeus's `_send_alert` → `argus.send_alert()` → infinite loop → `RecursionError: maximum recursion depth exceeded`.

**Fix:** Remove `alert_fn=self._send_alert` from ArgusAgent init. Argus sends its own Telegram alerts directly.

---

### 19. Supabase RLS blocking service_role

`portfolio_state` returning 403 Forbidden even with service_role key. RLS was enabled with no policies.

**Fix:** `ALTER TABLE public.portfolio_state DISABLE ROW LEVEL SECURITY` + explicit GRANT statements.

---

### 20. Anthropic API credits depleted

Zeus LLM calls rejected: `"credit balance is too low"`. Pro subscription gives chat access only — API usage is billed separately at console.anthropic.com.

**Fix:** Top up $25 at console.anthropic.com. Added token/cost tracking to Grafana dashboard.

---

### 21. IB_PORT=4002 in .env overriding code default

Code defaulted to 4004 but `.env` had `IB_PORT=4002`. The env var always wins. Connection timeouts.

**Fix:** Update `.env` on server to `IB_PORT=4004` and force-recreate Zeus container.

---

### 22. Grafana dashboard volume not updating

`docker compose restart` doesn't rebuild the image. Dashboard JSON in git wasn't reaching the Grafana container because the provisioning volume mounts a different host path than the git repo.

**Fix:** `cp` updated JSON directly to `/opt/pantheon/grafana/dashboards/` (the actual volume mount) then restart Grafana.

---

### 23. Pipeline never ran automatically

Zeus was webhook-only — nothing called `POST /run`. n8n was never set up.

**Fix:** Added background scheduler thread to `main.py`. Reads `RUN_INTERVAL` env var (default 900s). Pipeline now self-triggers every 15 minutes without n8n.

---

### 24. NaN market price crashing Ares

IB paper account has no live market data subscription. `ticker.midpoint()` returns `NaN`. The guard `if mid is None or mid == 0` doesn't catch `NaN`. Order placed with `qty = int(1000000 * 0.02 / NaN)` → crash.

**Fix:** `import math` + `math.isnan(mid)` checks on midpoint, last, and close. Also added `ib.reqMarketDataType(3)` to request delayed data explicitly.

---

## Current Status — 2026-05-26

| What | Status |
|---|---|
| All containers running (Zeus, IB Gateway, Grafana, Kafka, Redis, Dashboard, Nginx) | ✓ |
| IB Gateway connected — paper account €1M | ✓ |
| Pipeline auto-running every 15 minutes | ✓ |
| Trades executing (paper) — GOOGL first trade placed | ✓ |
| ChromaDB persistent across restarts | ✓ |
| Supabase persisting portfolio state, agent health, decision traces | ✓ |
| Grafana live — equity curve, agent health, budget tracking | ✓ |
| Anthropic token/cost tracking in dashboard | ✓ |
| Agent seniority system — TRAINEE, levelling up as trades close | ✓ |

---

*Built with Claude Code. Deployed on Hetzner, Frankfurt.*
