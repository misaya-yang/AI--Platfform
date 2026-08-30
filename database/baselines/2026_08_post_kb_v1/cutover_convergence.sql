-- ============================================================================
-- 2026_08_post_kb_v1 cutover: schema convergence change (PRD ARC-03 §3A)
-- ============================================================================
-- Runs against EXISTING databases only, after their legacy ledger is proven
-- complete up to the freeze point, and before fingerprint comparison /
-- adoption.  Fresh installs never execute this file: init.sql already
-- carries the converged shape plus the same ownership tail.
--
-- The change is idempotent and fail-closed:
--   * same-named objects in two platform schemas abort the cutover (no
--     automatic merge, no automatic drop);
--   * every persistent object in the platform schemas is re-owned to the
--     NOLOGIN owner role;
--   * SECURITY DEFINER functions get a pinned safe search_path;
--   * PUBLIC loses CREATE on schemas and every residual grant.
-- The runner owns the transaction; this file carries no BEGIN/COMMIT.
-- ============================================================================

-- 0. Fail closed on same-named objects across platform schemas ------------
DO $$
DECLARE
    duplicates TEXT;
BEGIN
    SELECT string_agg(DISTINCT c.relname, ', ' ORDER BY c.relname)
    INTO duplicates
    FROM (
        SELECT c.relname, count(DISTINCT n.nspname) AS schema_count
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('public', 'gateway', 'assistant', 'knowledge')
          AND c.relkind IN ('r', 'p', 'm', 'S', 'v', 'f')
        GROUP BY c.relname
        HAVING count(DISTINCT n.nspname) > 1
    ) AS ambiguous
    JOIN pg_class AS c ON c.relname = ambiguous.relname;

    IF duplicates IS NOT NULL THEN
        RAISE EXCEPTION
            'schema convergence refused: same-named objects exist in multiple platform schemas: %',
            duplicates;
    END IF;
END
$$;

-- 1. Platform schemas exist -------------------------------------------------
CREATE SCHEMA IF NOT EXISTS gateway;
CREATE SCHEMA IF NOT EXISTS assistant;
CREATE SCHEMA IF NOT EXISTS knowledge;

-- 2. Object ownership convergence -------------------------------------------
-- Every relation (table, view, materialized view, sequence, foreign table)
-- in the platform schemas becomes owned by the NOLOGIN owner.  The migrator
-- never owns business objects permanently; it SET ROLEs when DDL is needed.
DO $$
DECLARE
    target CONSTANT TEXT := 'ai_gateway_owner';
    r RECORD;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = target) THEN
        RAISE EXCEPTION
            'convergence refused: role % does not exist; run bootstrap/roles.sql first',
            target;
    END IF;

    FOR r IN
        SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind,
               pg_get_userbyid(c.relowner) AS current_owner
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('public', 'gateway', 'assistant', 'knowledge')
          AND c.relkind IN ('r', 'p', 'm', 'S', 'v', 'f')
        ORDER BY n.nspname, c.relname
    LOOP
        IF r.current_owner <> target THEN
            EXECUTE format(
                'ALTER %s %I.%I OWNER TO %I',
                CASE r.kind
                    WHEN 'S' THEN 'SEQUENCE'
                    WHEN 'v' THEN 'VIEW'
                    WHEN 'm' THEN 'MATERIALIZED VIEW'
                    WHEN 'f' THEN 'FOREIGN TABLE'
                    ELSE 'TABLE'
                END,
                r.schema, r.name, target
            );
        END IF;
    END LOOP;

    -- Functions and types follow the same rule.
    FOR r IN
        SELECT p.oid::regprocedure AS signature,
               pg_get_userbyid(p.proowner) AS current_owner
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname IN ('public', 'gateway', 'assistant', 'knowledge')
        ORDER BY p.oid::regprocedure::text
    LOOP
        IF r.current_owner <> target THEN
            EXECUTE format('ALTER FUNCTION %s OWNER TO %I', r.signature, target);
        END IF;
    END LOOP;

    FOR r IN
        SELECT n.nspname AS schema, t.typname AS name,
               pg_get_userbyid(t.typowner) AS current_owner
        FROM pg_type AS t
        JOIN pg_namespace AS n ON n.oid = t.typnamespace
        WHERE n.nspname IN ('gateway', 'assistant', 'knowledge')
          AND t.typtype IN ('e', 'd', 'c')
        ORDER BY n.nspname, t.typname
    LOOP
        IF r.current_owner <> target THEN
            EXECUTE format('ALTER TYPE %I.%I OWNER TO %I', r.schema, r.name, target);
        END IF;
    END LOOP;

    -- Platform schemas themselves are owned by the owner so future DDL via
    -- SET ROLE works without granting CREATE to LOGIN roles.
    FOR r IN
        SELECT nspname AS schema, pg_get_userbyid(nspowner) AS current_owner
        FROM pg_namespace
        WHERE nspname IN ('gateway', 'assistant', 'knowledge')
    LOOP
        IF r.current_owner <> target THEN
            EXECUTE format('ALTER SCHEMA %I OWNER TO %I', r.schema, target);
        END IF;
    END LOOP;
END
$$;

-- 3. SECURITY DEFINER hardening ---------------------------------------------
-- Every SECURITY DEFINER function is owned by the NOLOGIN owner and pinned
-- to pg_catalog so unqualified names inside the body cannot be hijacked.
DO $$
DECLARE
    target CONSTANT TEXT := 'ai_gateway_owner';
    f RECORD;
BEGIN
    FOR f IN
        SELECT p.oid::regprocedure AS signature
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname IN ('public', 'gateway', 'assistant', 'knowledge')
          AND p.prosecdef
        ORDER BY p.oid::regprocedure::text
    LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO %I', f.signature, target);
        EXECUTE format('ALTER FUNCTION %s SET search_path = pg_catalog', f.signature);
    END LOOP;
END
$$;

-- 4. REVOKE PUBLIC ------------------------------------------------------------
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA gateway FROM PUBLIC;
REVOKE ALL ON SCHEMA assistant FROM PUBLIC;
REVOKE ALL ON SCHEMA knowledge FROM PUBLIC;

DO $$
DECLARE
    s TEXT;
BEGIN
    FOREACH s IN ARRAY ARRAY['public', 'gateway', 'assistant', 'knowledge']
    LOOP
        EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM PUBLIC', s);
        EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM PUBLIC', s);
        EXECUTE format('REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA %I FROM PUBLIC', s);
    END LOOP;
END
$$;

-- Future objects created by the owner must not inherit PUBLIC grants.
ALTER DEFAULT PRIVILEGES FOR ROLE ai_gateway_owner IN SCHEMA public
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE ai_gateway_owner IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE ai_gateway_owner IN SCHEMA public
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE ai_gateway_owner IN SCHEMA gateway
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE ai_gateway_owner IN SCHEMA gateway
    REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE ai_gateway_owner IN SCHEMA gateway
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE ai_gateway_owner IN SCHEMA assistant
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE ai_gateway_owner IN SCHEMA assistant
    REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE ai_gateway_owner IN SCHEMA assistant
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE ai_gateway_owner IN SCHEMA knowledge
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE ai_gateway_owner IN SCHEMA knowledge
    REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE ai_gateway_owner IN SCHEMA knowledge
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
