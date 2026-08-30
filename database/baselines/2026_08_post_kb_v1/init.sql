-- ARC-03 pending-live-freeze sentinel.
-- This is intentionally non-executable. The live freeze tool replaces it
-- only after converged-source and second-empty-database fingerprints match.
DO $$
BEGIN
    RAISE EXCEPTION
        'ARC03 baseline is pending live freeze; init.sql is not an installation source';
END
$$;
