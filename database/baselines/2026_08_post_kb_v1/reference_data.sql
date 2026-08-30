-- ARC-03 pending-live-freeze sentinel.
-- System-owned RBAC reference rows must be exported from the converged source,
-- never copied by hand or inferred from historical migration order.
DO $$
BEGIN
    RAISE EXCEPTION
        'ARC03 baseline is pending live freeze; reference data is not frozen';
END
$$;
