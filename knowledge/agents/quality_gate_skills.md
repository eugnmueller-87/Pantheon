# Quality Gate Skills — Pantheon OS
# Rules for keeping the system non-fragile. Read before any pipeline change.

## The Golden Rules of Non-Spaghetti

Rule 1: Tests must gate deployment. No test pass = no ship.
  - GitHub Actions runs `pytest tests/` before Docker build AND before Cloudflare deploy.
  - A green local run is not enough. CI must be green.
  - Never use --no-verify or skip CI.

Rule 2: Import discipline — one direction only.
  - agents/ imports only from core/
  - zeus.py is the ONLY file allowed to import from agents/
  - If you need agent A to know about agent B: route through ZEUS or core/types
  - Violation = circular imports = spaghetti = system crashes at 3am

Rule 3: Types are the contract.
  - Every agent function signature uses types from core/types.py
  - Never pass raw dicts between agents — always dataclasses
  - If you change a type field, update ALL tests that use it

Rule 4: Circuit breakers everywhere.
  - Every agent call in zeus.py goes through self.cb.call("agent_name", fn=..., fallback=...)
  - Fallback must be a safe default — never None unless the next stage handles it
  - Test the fallback path, not just the happy path

Rule 5: Never let state leak between tests.
  - SQLite test databases use tmp_path fixture (isolated per test)
  - No test reads from real data/trade_log.db
  - ChromaDB/KB tests use isolated in-memory collections

## Test File Map

| File | What it protects |
|------|-----------------|
| test_circuit_breaker.py | CLOSED→OPEN→HALF_OPEN lifecycle, failure counting, reset |
| test_hades.py | OFAC block, ESG downgrade, audit trail completeness |
| test_types.py | All enums, dataclasses, field defaults |
| test_pattern.py | Kelly math correctness, SQLite isolation, context key uniqueness |
| test_trend.py | Regime classification from VIX + price data, suppression logic |
| test_execution_mock.py | Bracket math, buy/sell direction, R/R ratio |
| test_pipeline_integration.py | Full signal → trade path, kill at each stage |
| test_redis_bridge.py | Key namespace isolation, payload structure, fire-and-forget |

## When Tests Fail

DO NOT comment out the test. Fix the code.
DO NOT change the test to match broken behavior.

If a test is genuinely wrong (caught a spec change, not a bug):
1. Update core/types.py first if the contract changed
2. Update the test to match the new spec
3. Update ALL agents that depend on the changed type
4. Commit types + test + agents in one atomic commit

## Adding New Agents

Checklist before a new agent is considered done:
- [ ] Imports only from core/types and core/agent_knowledge
- [ ] health() method returns AgentHealth enum value
- [ ] Registered in zeus.py watchdog
- [ ] Circuit breaker call in zeus.py with a safe fallback
- [ ] At least 5 unit tests covering: happy path, edge case, kill case
- [ ] Knowledge file at knowledge/agents/{name}_skills.md
- [ ] Entry in AGENTS.md

## What "Non-Fragile" Actually Means

Fragile: one agent dies → whole system dies
Non-fragile: one agent dies → circuit breaker opens → fallback activates → ZEUS logs warning → Watchdog alerts → pipeline continues degraded

Fragile: deploy a bug → users see broken dashboard
Non-fragile: deploy a bug → CI catches it → build blocked → deploy never happens

Fragile: Pythia loses all trade history when server restarts
Non-fragile: Pythia reads from Supabase → survives container restart → learning persists

The MilestoneManager is the final backstop. Even if all agents produce garbage,
the kill switch stops the loss before it becomes catastrophic.
Capital preservation > uptime > performance.
