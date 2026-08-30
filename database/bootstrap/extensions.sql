-- ============================================================================
-- AI Gateway extension bootstrap (PRD ARC-03 §3A)
-- ============================================================================
-- Allowlisted extensions only; owner and version are verifiable through the
-- authority's extensions fingerprint.  Anything outside the allowlist
-- (database/authority/constants.py EXTENSION_ALLOWLIST) fails verification.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

-- Extension scripts may grant PUBLIC EXECUTE explicitly, bypassing the
-- owner's default ACL.  Fresh installs must match cutover convergence.
REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA public FROM PUBLIC;
