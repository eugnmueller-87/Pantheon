-- ============================================================
-- Pantheon OS — Supabase Migration 006: GRANT fixes
-- ============================================================
-- Tables created before Oct 30 2026 keep auto-exposure until that
-- deadline, BUT Supabase already enforces GRANTs on this project
-- for tables where RLS is disabled (llm_usage) or for tables that
-- returned 403 in practice.
--
-- Run in: Supabase Dashboard → SQL Editor
-- https://supabase.com/dashboard/project/ehbliqdzveeflaidvprr/sql
-- ============================================================

-- ── llm_usage ────────────────────────────────────────────────────────────────
-- Created in migration 005 with RLS disabled. Returning 403 on INSERT.
-- Fix: grant service_role full access.

GRANT SELECT, INSERT, UPDATE, DELETE ON public.llm_usage TO service_role;
GRANT SELECT ON public.llm_usage TO anon;

-- Sequence grant (needed for BIGSERIAL auto-increment via service_role)
GRANT USAGE, SELECT ON SEQUENCE public.llm_usage_id_seq TO service_role;


-- ── signals ───────────────────────────────────────────────────────────────────
-- Hermes writes signals here. Ensure service_role can INSERT.

GRANT SELECT, INSERT, UPDATE, DELETE ON public.signals TO service_role;
GRANT SELECT ON public.signals TO anon;


-- ── decision_traces ──────────────────────────────────────────────────────────
-- Zeus writes decision traces. Already working but add explicit grant
-- to be safe ahead of Oct 30 2026 deadline.

GRANT SELECT, INSERT, UPDATE, DELETE ON public.decision_traces TO service_role;
GRANT SELECT ON public.decision_traces TO anon;


-- ── trades ───────────────────────────────────────────────────────────────────

GRANT SELECT, INSERT, UPDATE, DELETE ON public.trades TO service_role;
GRANT SELECT ON public.trades TO anon;


-- ── portfolio_state ──────────────────────────────────────────────────────────

GRANT SELECT, INSERT, UPDATE, DELETE ON public.portfolio_state TO service_role;
GRANT SELECT ON public.portfolio_state TO anon;


-- ── portfolio_positions ──────────────────────────────────────────────────────

GRANT SELECT, INSERT, UPDATE, DELETE ON public.portfolio_positions TO service_role;
GRANT SELECT ON public.portfolio_positions TO anon;


-- ── agent_health ─────────────────────────────────────────────────────────────

GRANT SELECT, INSERT, UPDATE, DELETE ON public.agent_health TO service_role;
GRANT SELECT ON public.agent_health TO anon;


-- ── circuit_breakers ─────────────────────────────────────────────────────────

GRANT SELECT, INSERT, UPDATE, DELETE ON public.circuit_breakers TO service_role;
GRANT SELECT ON public.circuit_breakers TO anon;


-- ── knowledge_documents ──────────────────────────────────────────────────────

GRANT SELECT, INSERT, UPDATE, DELETE ON public.knowledge_documents TO service_role;
GRANT SELECT ON public.knowledge_documents TO anon;


-- ── ticker_map ───────────────────────────────────────────────────────────────

GRANT SELECT, INSERT, UPDATE, DELETE ON public.ticker_map TO service_role;
GRANT SELECT ON public.ticker_map TO anon;


-- ── agent_seniority + history ────────────────────────────────────────────────

GRANT SELECT, INSERT, UPDATE, DELETE ON public.agent_seniority TO service_role;
GRANT SELECT ON public.agent_seniority TO anon;

GRANT SELECT, INSERT, UPDATE, DELETE ON public.agent_seniority_history TO service_role;
GRANT SELECT ON public.agent_seniority_history TO anon;


-- ── macro_context ────────────────────────────────────────────────────────────

GRANT SELECT, INSERT, UPDATE, DELETE ON public.macro_context TO service_role;
GRANT SELECT ON public.macro_context TO anon;


-- ── filtered_signals ─────────────────────────────────────────────────────────

GRANT SELECT, INSERT, UPDATE, DELETE ON public.filtered_signals TO service_role;
GRANT SELECT ON public.filtered_signals TO anon;


-- ── trade_hit_rates (view or table) ──────────────────────────────────────────

GRANT SELECT ON public.trade_hit_rates TO service_role;
GRANT SELECT ON public.trade_hit_rates TO anon;
