# Pantheon OS — Dev Log: Getting Zeus to Actually Trade

*A brutally honest account of deploying an autonomous trading system and everything that went wrong.*

---

## What We Were Trying to Do

Deploy a 6-agent autonomous paper trading system on a Hetzner server:
- **Icarus** fetches signals from a live news API (Hermes)
- **Hades** runs compliance filtering
- **Artemis** reads macro conditions (VIX, regime, sector momentum)
- **Pythia** sizes positions from historical pattern data
- **Zeus** makes the final call via Claude LLM
- **Ares** places bracket orders to Interactive Brokers

Simple enough on paper.

---

## The Challenges (in order of discovery)

### 1. The server survived a reboot — but we didn't know it

First crisis: SSH dropped mid-session. Spent 20 minutes trying to write a startup script before discovering that `restart: always` in Docker Compose already handles reboots. The system came back up on its own. We just didn't know.

**Lesson:** Read the compose file before writing systemd units.

---

### 2. ChromaDB was silently running in-memory

Zeus's knowledge base appeared to work — 267 chunks loaded, research cycles completing. Then the container restarted and we had 0 chunks. Turns out ChromaDB was falling back to in-memory because `/home/pantheon` didn't exist.

The `useradd` command in the Dockerfile was missing the `-m` flag:
```dockerfile
# Before (broken):
RUN useradd -r -u 1001 pantheon

# After (fixed):
RUN useradd -r -u 1001 -m pantheon && chown -R pantheon:pantheon /app
```

Without a home directory, ChromaDB couldn't create its lock files and silently fell back to RAM. Every container restart wiped the entire knowledge base.

**Fix:** `6fcfc0c` — add `-m` to useradd.

---

### 3. JSON parsing failed silently

Zeus calls Claude LLM, gets a response, parses JSON. Simple. Except Claude sometimes wraps its JSON in markdown fences:

````
```json
{"approved": false, ...}
```
````

The original parser used `re.search(r"\{.*\}", raw, re.DOTALL)` which failed on nested JSON objects. Result: `"No JSON found in Claude response"` → fallback to pattern confidence → all signals rejected.

**Fix:** `42c571d` — balanced brace parser that walks character by character and finds the outermost `{}` object regardless of surrounding markdown.

---

### 4. All signals had empty tickers

Pipeline ran. Every signal killed. Zeus's reasoning: `null`. Dug into the logs:

```
approved=False confidence=0.08
flags=['CRITICAL: Empty ticker list (Tickers: []) renders this operationally unexecutable']
```

Zeus was right. Icarus had a hardcoded map of 16 suppliers. Hermes was sending signals for Cisco, Zoom, Workday, Google Cloud — none of them in the map. So `affected_tickers = []` and Ares has nothing to trade.

First instinct: add more entries to the hardcoded map. That's whack-a-mole. Any new supplier Hermes adds breaks things again.

**Real fix:** `9db13c4` — inject Apollo's `get_ticker()` into Icarus as a resolver. Apollo queries `ticker_map.json`, does case-insensitive/partial matching, falls back to live yfinance search, and **caches the result** so the next lookup is instant. New supplier? Resolved automatically and remembered forever.

---

### 5. Serial ticker lookups blocked the pipeline

10 signals × unknown suppliers × 2s yfinance lookup = 20s blocking before a single signal reached Hades.

**Fix:** `67b2416` — parallel lookup with `ThreadPoolExecutor`, max 4 workers, 10s total timeout. Known suppliers (static map hits) are never touched.

---

### 6. Zeus LLM was truncating responses

`max_tokens=800` on Haiku. Claude was hitting the limit mid-reasoning, producing JSON with a `reasoning` field of 40 characters. Auto-rejection threshold: 80 characters. Every signal rejected.

**Fix:** Same commit — raised to `max_tokens=1500`.

---

### 7. Bracketed paste mode corrupted every command on the server

Pasting multi-line commands into the SSH terminal prepended `^[[200~` to everything. Commands would fail with bizarre syntax errors.

**Fix:** `printf '\e[?2004l'` disables bracketed paste mode for the session.

---

### 8. Docker Compose env file path resolution

Running `docker compose -f infra/hetzner/docker-compose.prod.yml --env-file .env` resolved `.env` relative to the compose file directory, not the working directory. Variables not found, containers wouldn't start.

**Fix:** Symlink `ln -sf /opt/pantheon/.env /opt/pantheon/infra/hetzner/.env`.

---

### 9. Port 80 held by a ghost process

After a redeploy, nginx wouldn't start. Port 80 already in use. `ss -tlnp | grep :80` showed PIDs 724 and 727 — a stale nginx from a previous run that Docker hadn't cleaned up.

**Fix:** `kill -9 724 727` then redeploy.

---

### 10. Zeus was an FAQ bot, not a trading director

After all the plumbing was fixed, Zeus was still rejecting everything — but now with *very* sophisticated reasoning about why:

```
'This is the SEVENTH consecutive identical structural rejection of this exact
signal configuration. The pattern itself is a systemic failure signal —
Icarus is repeatedly submitting structurally defective signals without
remediation. ESCALATION FLAG: This requires process remediation, not
another cycle-by-cycle rejection.'
```

Zeus was right again. But the problem wasn't Zeus — it was that Zeus had no real intelligence. He was:
- Querying ChromaDB for generic arXiv papers
- Getting no company-specific context
- Using Haiku (fast, cheap, shallow)
- Evaluating each signal in complete isolation
- Forgetting every decision the moment the pipeline run ended

He was an FAQ bot with governance authority.

**Fix:** `d0abf7b` — a complete rethink:

1. **Apollo enriches every signal** — before Zeus decides on Cisco, Apollo calls yfinance right now, pulls P/E ratio, revenue growth, analyst consensus, next earnings date, and recent Hermes news for that specific company. Zeus gets real fundamentals, not theory.

2. **Zeus reads his own past decisions** — `614fa8a` adds `query_ticker_history()` to the KB. Before deciding on CSCO, Zeus sees: "Last time I traded CSCO: bull regime, VIX 16.3, outcome +2.1% win, because: earnings beat on strong balance sheet." He asks himself: same circumstances now?

3. **Zeus tracks the portfolio he's building** — intra-run approved trades list. If Zeus already approved two semiconductor longs this cycle, he knows it when the third one arrives.

4. **Zeus reads his own self-critique** — Apollo's self-improvement loop writes bias analysis to `zeus_skills.md`. Zeus reads this before every decision and is explicitly asked: "does this match a known failure pattern?"

5. **Upgraded to Sonnet** — the model that actually reasons.

---

### 11. Hermes signal IDs weren't valid UUIDs

Supabase stores `trace_id` as a UUID column. Hermes sends IDs like `"343427e0d2d26cc77edca2736d492dc7"` — 32-char hex, no dashes. Every decision trace insert failed with `22P02: invalid input syntax for type uuid`.

**Fix:** `f14f9d1` — `_sanitize_signal_id()` converts any non-UUID string to a deterministic `uuid5` hash. Same signal always gets the same UUID, so deduplication still works.

---

### 12. Google Cloud, AWS, Azure → empty tickers

"Google Cloud" is not a company. It's a division of Alphabet. Hermes sends many signals for cloud products and divisions that have no direct ticker. Zeus correctly rejected all of them as "no symbol identifier means no trade."

**Fix:** `_DIVISION_PARENT_MAP` — explicit mapping of product/division names to parent tickers. Google Cloud → GOOGL, AWS → AMZN, Azure → MSFT, Workday HCM → WDAY. Checked before the company map.

---

## Current Status

| What | Status |
|---|---|
| All 7 containers running | ✓ |
| IB Gateway connected (paper account DUQ422443) | ✓ |
| ChromaDB persistent (survives restarts) | ✓ |
| Signal ticker resolution (any supplier) | ✓ |
| Apollo enriching every signal with live fundamentals | ✓ |
| Zeus decision ledger (ticker history fingerprints) | ✓ |
| Zeus self-critique via zeus_skills.md | ✓ |
| Zeus using Sonnet for final decisions | ✓ |
| Supabase UUID fix | ✓ |
| Division name → parent ticker mapping | ✓ |

---

## Still To Do

| What | Why |
|---|---|
| Scheduled pipeline runs (cron at 0/6/12/18h Berlin) | Nothing calls `/run` automatically yet |
| Investigate NVDA trade: `side=""`, `fill=null` | Ares placed the order but IB Gateway returned no fill |
| Supabase `get_hit_rates` returns `NoneType has no attribute 'data'` | Pythia confidence query fails — no closed trades yet to compute win rate |
| `PRODUCT_RELEASE` signals from Google Cloud | Should map to indirect GOOGL exposure, not a direct trade |
| Apollo KB seeding after each deploy | ChromaDB starts empty — needs research cycle immediately after deploy |

---

## Architecture Insight

The hardest problem wasn't any individual bug. It was that the agents weren't a team — they were isolated silos that happened to run in sequence. Icarus couldn't resolve a ticker, Apollo had the answer, but they didn't talk. Zeus made decisions without knowing what he'd decided 5 minutes ago.

Real intelligence requires real collaboration. Each agent now actively contributes to decisions it doesn't own:
- Apollo researches companies Icarus flags
- Zeus checks his own past decisions before making new ones
- The portfolio state is visible to Zeus before he adds to it

That's the architecture. The bugs were just the tax we paid to get there.

---

*Built with Claude Code. Deployed on Hetzner CX21, Frankfurt.*
