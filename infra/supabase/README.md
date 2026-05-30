# Pantheon OS — Supabase Migrations

## Migration order

| File | Description |
|------|-------------|
| `001_schema.sql` | Full initial schema — all core tables, RLS, realtime |
| `002_seed_ticker_map.sql` | Seed ticker_map with initial S&P 500 symbols |
| `003_rpc.sql` | Stored procedures (get_trade_stats, etc.) |
| `004_seniority.sql` | Agent seniority tables + Grafana RPCs |
| `005_llm_usage_and_portfolio_fix.sql` | llm_usage table + portfolio_state singleton constraint |

## How to run

Paste into **Supabase Dashboard → SQL Editor → New Query** and click Run.  
Or via CLI: `supabase db push` (requires local Supabase CLI setup).

---

## ⚠️ MANDATORY: Grant pattern for new tables (Oct 30 2026 deadline)

**Background:** From May 30, 2026 new Supabase projects no longer auto-expose
`public` schema tables via PostgREST/supabase-js. From **October 30, 2026**
this applies to *new tables on all existing projects* — including this one.

**What breaks without grants:** `INSERT`/`SELECT` via `supabase_client.py` returns
`403 Forbidden` even though the service_role key is valid and RLS allows it.
RLS policies alone are NOT sufficient — you also need the `GRANT` statement.

**Every new `CREATE TABLE` must include:**

```sql
-- RLS
ALTER TABLE public.<table_name> ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all" ON public.<table_name>
    FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "anon_read" ON public.<table_name>
    FOR SELECT TO anon USING (true);   -- omit if dashboard doesn't need it

-- GRANT (mandatory from Oct 30 2026)
GRANT SELECT, INSERT, UPDATE, DELETE ON public.<table_name> TO service_role;
GRANT SELECT ON public.<table_name> TO anon;   -- omit if not public-readable
```

Use `TEMPLATE_migration_NNN.sql` as your starting point — it has the full
boilerplate pre-filled.

---

## Existing tables (pre-deadline — no action needed now)

All tables created before May 30, 2026 keep auto-exposure until Oct 30, 2026:

- `signals`, `filtered_signals`, `macro_context`
- `trades`, `decision_traces`
- `portfolio_state`, `portfolio_positions`
- `agent_health`, `circuit_breakers`
- `knowledge_documents`, `ticker_map`
- `llm_usage`
- `agent_seniority`, `agent_seniority_history`

These are safe. Only *future* tables need the explicit grant.
