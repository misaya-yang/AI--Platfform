-- ============================================================================
-- AI Gateway role bootstrap (PRD ARC-03 §3D)
-- ============================================================================
-- Creates the platform's NOLOGIN object owner and the LOGIN application
-- roles.  Idempotent by design; run by a one-time PostgreSQL admin (local
-- Compose) or pre-created by a DBA (managed PostgreSQL, receipt without
-- secrets).
--
-- Hard rules encoded here:
--   * ai_gateway_migrator is the only DDL executor and NEVER has CREATEROLE
--     or cluster-admin rights; it can SET ROLE into the NOLOGIN owner.
--   * Application roles get no CREATE rights and no broad PUBLIC grants.
--   * Role-level search_path starts with pg_catalog and ends with public.
--   * Passwords are never part of this file; credential generation is a
--     runtime input (scripts/new/init-env.sh / managed DBA), not repo state.
--
-- The canonical prefix is ``ai_gateway_``.  Managed deployments may
-- namespace roles through AI_GATEWAY_ROLE_PREFIX; the authority renders
-- this file with the configured prefix before executing it.
-- ============================================================================

-- 1. NOLOGIN object owner ---------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_gateway_owner') THEN
        CREATE ROLE ai_gateway_owner NOLOGIN;
    END IF;
END
$$;
ALTER ROLE ai_gateway_owner NOLOGIN NOCREATEDB NOCREATEROLE;

-- 2. Migrator: sole DDL identity --------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_gateway_migrator') THEN
        CREATE ROLE ai_gateway_migrator LOGIN;
    END IF;
END
$$;
ALTER ROLE ai_gateway_migrator LOGIN NOCREATEDB NOCREATEROLE;
-- The migrator applies DDL by temporarily becoming the object owner; it does
-- not own objects permanently.
GRANT ai_gateway_owner TO ai_gateway_migrator;

-- 3. Application LOGIN roles -------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_gateway_gateway') THEN
        CREATE ROLE ai_gateway_gateway LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_gateway_runtime') THEN
        CREATE ROLE ai_gateway_runtime LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_gateway_capability_worker') THEN
        CREATE ROLE ai_gateway_capability_worker LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_gateway_knowledge_api') THEN
        CREATE ROLE ai_gateway_knowledge_api LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_gateway_knowledge_worker') THEN
        CREATE ROLE ai_gateway_knowledge_worker LOGIN;
    END IF;
END
$$;

ALTER ROLE ai_gateway_gateway LOGIN NOCREATEDB NOCREATEROLE;
ALTER ROLE ai_gateway_runtime LOGIN NOCREATEDB NOCREATEROLE;
ALTER ROLE ai_gateway_capability_worker LOGIN NOCREATEDB NOCREATEROLE;
ALTER ROLE ai_gateway_knowledge_api LOGIN NOCREATEDB NOCREATEROLE;
ALTER ROLE ai_gateway_knowledge_worker LOGIN NOCREATEDB NOCREATEROLE;

-- 4. Role search_path: pg_catalog first, public last, no CREATE anywhere ----
ALTER ROLE ai_gateway_migrator SET search_path = pg_catalog, gateway, assistant, knowledge, public;
ALTER ROLE ai_gateway_gateway SET search_path = pg_catalog, gateway, assistant, knowledge, public;
ALTER ROLE ai_gateway_runtime SET search_path = pg_catalog, gateway, assistant, knowledge, public;
ALTER ROLE ai_gateway_capability_worker SET search_path = pg_catalog, gateway, assistant, knowledge, public;
ALTER ROLE ai_gateway_knowledge_api SET search_path = pg_catalog, knowledge, gateway, assistant, public;
ALTER ROLE ai_gateway_knowledge_worker SET search_path = pg_catalog, knowledge, gateway, assistant, public;
