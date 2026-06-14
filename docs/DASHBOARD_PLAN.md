# ZEUS EXP + Trading Dashboard — Implementation Blueprint

*A single, buildable spec for a sophisticated trading dashboard with per-agent
EXP bars and levels. Every load-bearing fact below was verified against the live
repo. Where the original design panel was over-optimistic, the corrections from
the adversarial review are folded in and marked **[CORRECTED]**.*

> **Golden rule, stated once and never broken:** EXP is **cosmetic**. The
> real-money gate stays `system_level.max_position_pct()` in
> [`agents/zeus.py`](../agents/zeus.py) Stage 5b. `exp_level` must never touch
> position sizing or live-trading authority. EXP is a motivation layer; seniority
> is the safety gate. See [`docs/LESSONS.md`](LESSONS.md) for the seniority gate.

---

## 1. Platform decision — extend the existing React/Vite SPA in place

A scored design panel (4 options, 3 independent judges each) ranked the choices:

| Rank | Option | Score |
|---|---|---|
| **1** | **Extend `dashboard/frontend` (React 18 + Vite 5) in place** | **41/50** |
| 2 | Hybrid: Grafana (ops) + rebuilt React showpiece | 39/50 |
| 3 | Next.js 15 + shadcn + Tremor (full rewrite) | 36/50 |
| 4 | Grafana pushed to its ceiling | 32/50 |

**Why extend-in-place wins:** the plumbing is half-built and correct, so we
finish wiring rather than rewrite.

- EXP-bar primitives already exist: `KpiRing` ([App.jsx:62](../dashboard/frontend/src/App.jsx)) and `StatRow` (App.jsx:85).
- The Supabase realtime hook is **already written but never imported**: [`dashboard/frontend/src/hooks/useSupabaseRealtime.js`](../dashboard/frontend/src/hooks/useSupabaseRealtime.js). `@supabase/supabase-js` is already a dependency.
- `agent_seniority` already has anon SELECT GRANTs and is already on the `supabase_realtime` publication — live promotions are real, not faked.
- A dead `SYSTEM LEVEL` block (App.jsx:369) already reads `status.seniority.*`; the backend just never sends it.

**Why not Grafana:** it structurally cannot do RPG game-feel — no client JS,
sanitized HTML, 30s poll. Great analytics cockpit, wrong tool for the headline
gamification. Grafana stays as-is for ops monitoring.

**Honest weak point:** maintainability. `App.jsx` is a 663-line god-component,
100% inline styles, no lint/tests, with an abandoned parallel `src/components/`
+ `src/hooks/` layer. **Phase 1 pays this debt down first** so the result reads
as deliberate architecture, not a patched SPA.

### Stack additions (libraries, not a new platform)

| Add | ~size (gz) | Purpose |
|---|---|---|
| `@tanstack/react-query` v5 | 13 KB | Durable cache; realtime handlers `setQueryData` so pushes survive reload |
| `framer-motion` | 40 KB | The moat vs Grafana — spring XP count-ups, level-up bursts, tab transitions |
| `lightweight-charts` | 45 KB | Real equity line + peak overlay; candles with trade markers |
| `canvas-confetti` (optional) | 6 KB | Level-up particle burst |
| CSS custom properties | 0 | `src/theme/tokens.css` — palette + rank-tier colors + glows |
| ESLint + Prettier (+ Vitest) | dev | Make a large redesign safe to iterate |

Keep **Recharts** (already installed) for donuts/histograms/funnels. Code-split
per tab so first paint stays fast (~150 KB total growth).

---

## 2. Phase 0 — deploy blockers (½ day, HARD GATE) ✅ verified real

Nothing ships until these are fixed. **All three confirmed against the files:**

1. **Wrong build dir.** Vite emits `dist` (no `outDir` override in `vite.config.js`), but [`dashboard/Dockerfile.frontend:10`](../dashboard/Dockerfile.frontend) copies `/app/build` → nginx ships an **empty webroot**. Fix: `COPY --from=builder /app/dist /usr/share/nginx/html`.
2. **Wrong env prefix.** `Dockerfile.frontend:6` sets `REACT_APP_WS_URL`, but `App.jsx:8` reads `import.meta.env.VITE_WS_URL`. Vite only inlines `VITE_*` at build. Fix: `ENV VITE_WS_URL=...`.
3. **Missing Supabase build env.** `useSupabaseRealtime.js` needs `VITE_SUPABASE_URL` + `VITE_SUPABASE_ANON_KEY` **at build time** (Vite inlines them into the bundle). The Dockerfile passes neither → realtime can never work even once wired. Fix: add both as build args/env.
4. Replace `YOUR_DOMAIN` placeholders in `infra/cloudflare/_redirects` + `wrangler.toml`.

> Note: the dead `src/hooks/useWebSocket.js` uses `REACT_APP_WS_URL` but is not
> imported by the live `App.jsx`. Leave or delete in Phase 1; it's not the cause.

---

## 3. The EXP / level system

### Two numbers per agent

1. **EXP** — lifetime, monotonic, never decreases. Maps to an *EXP-level* 1–50 via a curve. Cosmetic; can run ahead of seniority for a strong agent.
2. **PROGRESS** — 0–100% toward the *next seniority rung's* criteria (moves both ways). Fills the bar's "to next RANK" segment.

The dashboard renders **one bar per agent** overlaying both: fill = PROGRESS to
next rank; glowing tick + badge = current EXP-level. **Rank label always comes
from authoritative `agent_seniority.level_int`** (never the denormalized copy).

### XP formula (per CLOSED trade)

Minted only on trade close — tied to realized performance, never hope.
`R = pnl_pct / risk_pct`, where `risk_pct = (fill_price − stop_loss)/fill_price`,
falling back to `risk_pct = position_pct` when stop is NULL.

```
base_xp        = 100
outcome_xp     = WIN  →  round(60 * clamp(R, 0, 5))
                 LOSS →  -round(20 * clamp(|R|, 0, 3))
conviction_mult= 1 + 0.5 * (confidence - 0.5)     # 0.75 .. 1.25
size_mult      = 1 + position_pct                  # 1.00 .. 1.05   [see unit note]
streak_bonus   = WIN ? min(win_streak - 1, 5) * 25 : 0
trade_xp       = max(0, round((base_xp + outcome_xp) * conviction_mult * size_mult) + streak_bonus)
```

> **[CORRECTED] `position_pct` is a FRACTION, not a percent.** Verified: `ares.py:371`
> computes `account_val * sized.position_size_pct` directly (no `/100`); `*100`
> appears only for display (`ares.py:381`, `pythia.py:80`), and
> `max_position_pct()` returns `0.03`/`0.05`. So `size_mult` = 1.00–1.05 as
> intended. **Do not** treat `position_pct` as 2.0 — that would be a 100× bug.

### Milestone bonuses (one-time, idempotent via `source_ref`)

First live trade +500; every 50 closed trades +400; new equity peak +300;
**seniority PROMOTION +1000 "RANK UP" jackpot** (the level-up moment). The
jackpot's `source_ref` must be deterministic — e.g. `promo:{agent}:{to_level}` —
so a re-eval never double-fires it, and a demote→re-promote restores the badge
without re-minting.

### Per-agent role XP — only credit signals that exist TODAY

> **[CORRECTED] Several seniority criteria are stubbed/unearnable** (verified in
> `core/seniority.py`): Artemis `regime_accuracy` returns `-1.0`; Hades
> `violations`/`false_positive` are hardcoded `0`; Zeus
> `override_documentation_rate` is vacuously `1.0`. **Only credit real-today
> signals.** Mark the rest "pending wiring" in the quest log so a stuck bar reads
> as honest, not broken.

Real-today role awards (all reference columns that exist):
- **ZEUS** — +150 correct verdict (`decision_traces.zeus_approved` vs outcome); +250 correct override; full trade-spine XP.
- **HADES** — +120 per correct compliance kill (`filtered_signals.ofac_flag`/`esg_flag`); +40 clean pass that later won.
- **ICARUS** — +30 approval→placed; +80 if it wins.
- **PYTHIA** — XP when a mature-context (`≥20 trades`) trade hits its `trade_hit_rates` expectation.
- **ARTEMIS** — +60 regime call aligned with next-period S&P move (only once regime history is logged).
- **APOLLO** — +90 per self-improve cycle; +150 per verified `ticker_map` entry that later trades *(define granularity — see correction)*; +40 per KB doc (daily cap).
- **ARES** — +50 low-slippage fill (`|fill−expected|/expected < 0.1%`); +150 per 50 clean placements.
- **ARGUS** — +200 drawdown-kill preventing breach; +30 per healthy refresh-streak day.

> **[CORRECTED] Apollo ticker-map granularity:** `ticker_map` PK is composite
> `(supplier_name, exchange)` (007), so one supplier can have OTC + XETRA rows.
> Credit **per supplier**, not per row, or every dual-listed German blue-chip
> double-credits.

### Level curve & seniority anchoring

```
xp_for_level(L) = round(500 * L^1.6)    # cumulative XP to REACH level L
exp_level(xp)   = largest L where xp_for_level(L) <= xp   (clamp 1..50)
```

Each seniority rung pins a band so EXP can't show Director flair on a
Senior-gated agent:

```
SENIOR    (0) → EXP 1–9    (cap 9 until promoted)
PRINCIPAL (1) → EXP 10–24  (floor 10 on promotion, cap 24)
MANAGING  (2) → EXP 25–39  (floor 25, cap 39)
DIRECTOR  (3) → EXP 40–50  (floor 40, ZEUS only)
```

> **[CORRECTED] Bar-fill must use the DISPLAYED (capped) level, not raw `exp_level(xp)`.**
> A SENIOR agent with 20k XP has raw L=10 but displayed L=9 (band cap). Compute
> `bar_fill` and the `L{n}·{into}/{next}` label from the **capped** level so the
> fill, label, and rendered tier never disagree. Cover this with a Vitest case.

> **Honesty caveat on the loss floor:** `trade_xp` is floored at 0, and `base_xp`
> gives "showing up" credit, so a long losing streak still slowly grows the bar.
> Mitigation is not just cosmetic — show `lifetime_wins/losses` + streak **and**
> consider freezing `exp_level` advancement (not total_xp) during a net-negative
> rolling window, so the LEVEL stays correlated with skill. Decide before Phase 3.

### Backend wiring — the part the original plan got wrong

> **[CORRECTED — biggest fix] The trade-close hook is NOT in Argus.** It's
> `core/shadow_learning.py` `_backfill_supabase` (a `@staticmethod`), which calls
> `update_trade_pnl(order_id, pnl_pct, hit, closed_at)` — that UPDATEs `trades`
> by `order_id` with only those 3 columns. **`fill_price`, `stop_loss`,
> `confidence`, `position_pct` are NOT in scope there.** To compute the XP
> formula you must **SELECT the `trades` row back by `order_id`** before minting.
> Budget this as a real backend feature with its own failure mode, not a free hook.

> **[CORRECTED] `win_streak` does not exist anywhere** (grep: only markdown).
> It must be built from scratch in `agent_exp`. Closes arrive **out of order**
> (`shadow_learning` resolves whichever positions disappeared, not entry order),
> so "consecutive wins" needs an explicit ordering decision — **define streak
> order by `trades.closed_at`** (recompute streak from the ordered close history
> on each mint), not by arrival order.

New module **`core/exp.py`**: `award_xp(agent, event_type, xp, source_ref, metadata)`
(idempotent on `source_ref`) + `recompute_agent_exp(agent)` (re-derives level,
streak from `closed_at` order, progress). Persistence helpers in
`core/supabase_client.py` mirroring `upsert_agent_seniority` (`:394`).

> **Idempotency is load-bearing.** `UNIQUE(agent_name, event_type, source_ref)`
> is the only guard against double-minting on replays/reconnects. Every award
> needs a stable ref (trade `order_id`, `filtered_signal_id`, deterministic promo
> ref). No ref → no award.

---

## 4. Data & backend

### Source-of-truth split (the one non-negotiable rule)

- **WebSocket** (`dashboard/backend/server.py`, port 8081) = **live pipeline theatre + heartbeats only.** Ephemeral, sub-second. The only thing that can show pipeline staging live. `server.py` does **zero DB reads** today — keep it that way except one cheap addition (below).
- **Supabase Realtime** (anon key, via the dormant hook) = **all persisted history + EXP/seniority.** These tables have anon SELECT and are on the realtime publication.

This keeps DB credentials out of `server.py`.

### Three channels

1. **WS (unchanged transport):** pipeline events + `status_update` (5s) + `agent_health` (10s). **One required change:** add a `seniority` summary key to the `status_update` payload (`server.py:197`) so the dead `App.jsx:369` SYSTEM LEVEL block renders.
   > **[CORRECTED] `get_seniority_report()` is NOT guaranteed cheap.** If
   > `_seniority_report is None` it lazily triggers a full `evaluate()` (ChromaDB
   > + SQLite + Supabase + Redis + source files). In the dashboard backend
   > process it may be None/stale on first call. So this read must be
   > **cached + timeout-bounded + fail-soft** (omit the `seniority` key on error)
   > exactly like the EXP read — the original plan applied that caveat only to EXP.
2. **Supabase Realtime (wake the dormant hook):** today it subscribes to `trades`, `decision_traces`, `portfolio_state`, `agent_health`. **Add two it lacks:** `agent_seniority` UPDATE (level-up trigger) and `agent_exp` UPDATE (XP push). Wrap each in a TanStack Query key; handlers call `setQueryData`.
3. **RPC polling (60s, NOT realtime):** heavy aggregates — win-rate-by-category, monthly returns, kill-stage, R-multiples, performance stats.

### New EXP tables — created after Oct 30 2026 → **MUST have explicit GRANTs** or PostgREST 403s ([[supabase-grant-requirement]])

**`agent_exp_ledger`** (append-only audit): `id BIGSERIAL PK, agent_name, event_type, xp INT, source_ref, metadata JSONB, created_at` — `UNIQUE(agent_name, event_type, source_ref)`.

**`agent_exp`** (one row per agent, the dashboard read): `agent_name PK, total_xp BIGINT, exp_level INT, xp_into_level INT, xp_to_next INT, lifetime_wins INT, lifetime_losses INT, current_win_streak INT, best_win_streak INT, seniority_level_int INT, progress_to_next_pct FLOAT8, last_event_at, updated_at`.

**VIEW `system_exp`:** `SUM(total_xp), AVG(exp_level), MIN(exp_level) AS floor_exp_level, MAX(updated_at)`.
> **[CORRECTED] Views are not on the realtime publication** — `system_exp` won't
> push; the client recomputes it when `agent_exp` rows push. Don't treat it as a
> live source.

GRANTs (mirror `006_grant_fixes.sql`): service_role full + anon SELECT on both
tables and the view; `USAGE,SELECT` on the ledger sequence; RLS (service_role
FOR ALL, anon SELECT); `ALTER PUBLICATION supabase_realtime ADD TABLE agent_exp;`.

### Existing-table changes & new RPCs

- **`agent_seniority` +2 columns:** `metrics JSONB DEFAULT '{}'` (persist the raw counts the evaluator currently discards) and `progress_pct FLOAT8 DEFAULT 0`. Rides existing `upsert_agent_seniority()`; extend `AgentScore` with `metrics` + `progress_pct`. New columns need a GRANT.
- **`equity_snapshots`** (append-only, written by Argus) — **REQUIRED.**
  > **[CORRECTED] `portfolio_state` is actively erroring, not just flat.** 005
  > added `UNIQUE(state_id DEFAULT 'singleton')` but `upsert_portfolio_state`
  > strips `state_id` and does a plain `.insert()` → every write after the first
  > violates the constraint and fails (swallowed by try/except). So there is **no
  > reliable equity time-series and no reliable `peak_equity`** until
  > `equity_snapshots` lands. The "new equity peak +300" milestone AND the hero
  > equity curve both depend on this — ship it in Phase 2 before they're built.
  > Columns: `snapshot_id, total_equity, peak_equity, current_drawdown_pct, open_positions, recorded_at`.
- **New RPCs** (`STABLE`, `GRANT EXECUTE TO anon`): `get_r_multiple_distribution()` (guard divide-by-zero with `NULLIF`, exclude NULL-stop rows), `get_performance_stats()` (label Sharpe "per-trade", not annualized — honest), `get_exp_report()`.
  > **[CORRECTED] `get_exp_report` must ORDER BY authoritative seniority, not the
  > denormalized `agent_exp.seniority_level_int`** (which can drift between a
  > promotion and the next EXP recompute). Join to `agent_seniority` for ordering.

> **[CORRECTED] Bull/bear debate has no separate columns.** It's folded into
> `decision_traces.zeus_reasoning` as `' | DEBATE — BULL: … || BEAR: …'`
> (300-char/side truncation, `zeus.py:624`). Either string-parse it for the
> debate panel, OR add `bull_case`/`bear_case` columns for a clean two-column
> display (preferred — cleaner and removes the truncation).

> **Two migration dirs.** Every new object MUST land in **both**
> `infra/supabase/` (live set) and `supabase/migrations/` (clean set) or the live
> dashboard 403/404s. Files: `008_agent_exp.sql` + `009_dashboard_analytics.sql`
> (and their `supabase/migrations/` twins).

---

## 5. The dashboard UI

### Architecture: keep the app, kill the monolith

`App.jsx` → thin shell: `<ThemeProvider>` → `<QueryClientProvider>` →
`<DataProvider>` (owns BOTH transports) → CSS-grid `<Layout>` composing ~14 panels
under a new `src/panels/`. Revive the abandoned `src/components/` skeletons as seeds.

**Layout:** a persistent **TopBar** (KPI strip + dual-transport pills +
RUN/HALT/RESUME) over **four tab-routed views**, cross-faded with framer-motion
`<AnimatePresence mode="wait">`. Default landing = PANTHEON.

### Visual language — "AAA dark trading terminal meets RPG"

`src/theme/tokens.css`:
```
--bg #060a12 · --panel #0b0f1a · --border #1a2540
--blue #63b3ed · --purple #9f7aea · --green #48bb78 · --red #fc8181 · --amber #f6ad55
--rank-senior #48bb78 · --rank-principal #63b3ed · --rank-md #9f7aea · --rank-director #f6ad55
```
Keep `'Courier New'` for data; add a display weight for headers. Rounded 6px
panels, tier-colored glows, `tabular-nums`. Motion is the moat: spring count-ups,
level-up bursts, staggered reveals, traveling pipeline dots.

### TAB 1 — PANTHEON (gamified hero) ★

- **8 `AgentCard`s** with rank-tiered glowing borders, rank ribbon, the **EXP bar** (upgraded `StatRow`: spring-driven width count-up + shimmer + level ticks).
- **Card flip → "WHAT'S NEEDED TO LEVEL UP" quest log.**
  > **[CORRECTED] Quest log degrades to sparse.** `agent_seniority.criteria` is
  > populated only for the rung *currently being attempted* (early-return
  > gating), and rich "412/500" strings live in `notes[]` only when a gate
  > *fails*. So an agent that cleared its gate shows a near-empty list. Handle
  > explicitly: render "All current-rung criteria met — awaiting promotion eval"
  > rather than an empty box, so it reads as correct, not broken.
- **Level-up juice:** Supabase Realtime UPDATE on `agent_seniority`/`agent_exp` → on `level_int` increase, orchestrated framer-motion sequence (gold flash, glow/confetti, bar fill-to-100-then-reset, ribbon cross-fade, "PANTHEON PROMOTION" toast).
- **SYSTEM LEVEL hero banner** (wires `App.jsx:369`): system level = MIN over agents, the `max_position_pct` cap, and a **LIVE-TRADING CLEARANCE badge** (locked "PAPER ONLY" → green "LIVE ENABLED" at Senior→Principal).
- **Promotion timeline** from `get_promotion_history(90)` — use `agent_seniority_history` as source of truth (in-memory `_last_levels` resets on restart).

### TAB 2 — PORTFOLIO & P&L
Real equity curve (lightweight-charts, from `equity_snapshots`); candlestick +
volume with trade markers *(degrades to entry/exit markers — no OHLC source
in-repo, `trades` has `fill_price` only)*; DrawdownMeter vs 8% kill line;
allocation donut from **real** `portfolio_positions` (stop faking
`positions*3%`); per-position table; win-rate heat-tiles, R-multiple histogram,
monthly returns, performance KPIs.

### TAB 3 — LIVE PIPELINE (WS-sourced)
Horizontal `PipelineFlow` Icarus→Argus with traveling dots on `icarus_signal`
events; kill funnel from `get_kill_stage_stats`; ZEUS verdict panel (typewriter
+ structured BULL vs BEAR); kill-switch / circuit-breaker strip.

### TAB 4 — COST & HEALTH
LLM spend from `llm_usage` (today's cost, over-time area, model/symbol
breakdown, budget ring); agent-health board (8 cards); circuit-breaker matrix;
paper-vs-live + transport-health strip.

---

## 6. Phased build plan

- **Phase 0 — Unblock deploy (½ day, gate).** Fix `Dockerfile.frontend` (`dist`, `VITE_*`, Supabase build env); replace `YOUR_DOMAIN`. Confirm a clean build reaches the browser with the anon key.
- **Phase 1 — Decompose + theme + tooling (1–2 days).** Split `App.jsx` into `src/panels/`; extract `tokens.css`; add react-query, framer-motion, ESLint/Prettier. WS-only still works — debt paid, no behavior change.
- **Phase 2 — Wire Supabase history (1–2 days).** Import + extend `useSupabaseRealtime.js` (+`agent_seniority`, +`agent_exp`), wrap in react-query; repoint history/P&L/cost panels to real tables; ship `equity_snapshots` + Argus writer. History survives reload.
- **Phase 3 — THE EXP SYSTEM (3–4 days).** Ship `008_agent_exp.sql` (both dirs, GRANTs). Build `core/exp.py` (SELECT-back, `closed_at`-ordered streak, idempotent ledger) + persistence; mint at `shadow_learning` backfill + `zeus._run_seniority_evaluation` (jackpot). Add the 2 `agent_seniority` columns + `get_exp_report`. Build the PANTHEON tab. **This is the screenshot.** Vitest the XP math + capped-level bar-fill + demotion round-trip before deploy.
- **Phase 4 — Chart depth (1–2 days).** lightweight-charts equity + candles; `009_dashboard_analytics.sql` (R-multiple + performance RPCs); analytical panels.
- **Phase 5 — Polish (ongoing).** Code-split per tab; more tests; confetti/sound; responsive degrade.

---

## 7. Concrete next steps (verified)

1. **Fix `dashboard/Dockerfile.frontend`** — `COPY --from=builder /app/dist ...`; `ENV VITE_WS_URL=...`; pass `VITE_SUPABASE_URL` + `VITE_SUPABASE_ANON_KEY` as build args.
2. **Confirm the live migration set** on project `ehbliqdzveeflaidvprr`, and that `portfolio_state` is the broken singleton (it is) → confirms `equity_snapshots` is required.
3. **Write `008_agent_exp.sql`** (both dirs): 2 tables + `system_exp` view + GRANTs + RLS + realtime publication.
4. **Write `core/exp.py`** — `award_xp` (idempotent on `source_ref`) + `recompute_agent_exp` (SELECT-back trade row; streak from `closed_at` order); add `upsert_agent_exp` to `supabase_client.py`.
5. **Hook minting** into `shadow_learning._backfill_supabase` (trade spine + role attribution) and `zeus._run_seniority_evaluation()` (deterministic `+1000` jackpot ref).
6. **Extend `AgentScore` + `upsert_agent_seniority`** with `metrics` + `progress_pct`; ship `009_dashboard_analytics.sql` (2 columns + 3 RPCs + `equity_snapshots`).
7. **Add the cached/fail-soft `seniority` key** to `server.py:197` `status_update`.
8. **Frontend Phases 0→1→2→3** in order; import the dormant hook + the two missing subscriptions; build PANTHEON.

---

*Verified anchors: `dashboard/frontend/src/App.jsx` (`:8` VITE_WS_URL, `:62` KpiRing,
`:85` StatRow, `:101` area-chart-mislabeled-CandleChart, `:236` faked donut, `:369`
dead SYSTEM LEVEL, `:564` typewriter) · `useSupabaseRealtime.js` (dormant) ·
`dashboard/backend/server.py:188` (DB-free poller) · `core/seniority.py:59` (Level
enum), `:76` (max_position_pct fractions) · `core/shadow_learning.py` (`_backfill_supabase`
— the real trade-close hook) · `core/supabase_client.py:394` (upsert pattern) ·
`agents/ares.py:371` (position_pct is a fraction) · `infra/supabase/005` (broken
portfolio_state insert) · `006_grant_fixes.sql` (GRANT template) · `007` (composite
ticker PK) · `dashboard/Dockerfile.frontend` (broken build dir/env).*
