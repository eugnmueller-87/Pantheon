-- ============================================================
-- Pantheon OS — Supabase Migration NNN: <short description>
-- ============================================================
-- ⚠️  SUPABASE GRANT REQUIREMENT (enforced Oct 30 2026)
--
-- From Oct 30, 2026 new tables in public schema REQUIRE explicit GRANTs
-- to be accessible via PostgREST/supabase-js. The template below is the
-- mandatory boilerplate — copy it for every new table you create.
--
-- Run in: Supabase Dashboard → SQL Editor
-- ============================================================


-- ── 1. Create table ───────────────────────────────────────────────────────────

CREATE TABLE public.<table_name> (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- ... your columns ...
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_<table_name>_created_at ON public.<table_name>(created_at DESC);
-- ... add more indexes as needed ...


-- ── 2. Trigger: auto-update updated_at ───────────────────────────────────────
-- (set_updated_at() function defined in migration 001)

CREATE TRIGGER trg_<table_name>_updated_at
    BEFORE UPDATE ON public.<table_name>
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ── 3. RLS (mandatory — service_role writes, anon reads) ─────────────────────
--
-- ⚠️  CRITICAL: RLS alone does NOT grant PostgREST access.
--     You need both RLS policies AND the GRANT below.

ALTER TABLE public.<table_name> ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_all" ON public.<table_name>
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Add anon read-only if dashboard needs to query this table:
CREATE POLICY "anon_read" ON public.<table_name>
    FOR SELECT TO anon USING (true);


-- ── 4. PostgREST / supabase-js access grant ──────────────────────────────────
--
-- ⚠️  MANDATORY from Oct 30 2026 — without this, INSERT/SELECT via
--     supabase-js or PostgREST returns 403 even with a valid service_role key.
--
-- service_role key → full CRUD
GRANT SELECT, INSERT, UPDATE, DELETE ON public.<table_name> TO service_role;
-- anon key (dashboard read-only) → SELECT only
GRANT SELECT ON public.<table_name> TO anon;


-- ── 5. Realtime (optional — only if dashboard subscribes live) ───────────────

-- ALTER PUBLICATION supabase_realtime ADD TABLE public.<table_name>;
