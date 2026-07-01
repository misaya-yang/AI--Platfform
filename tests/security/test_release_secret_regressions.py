from __future__ import annotations

from pathlib import Path

from src.core.middleware.streaming import StreamingAdmissionConfig, StreamingRateLimitConfig

ROOT = Path(__file__).resolve().parents[2]


def test_account_migration_does_not_seed_known_admin_password() -> None:
    migration = (ROOT / "database/migrations/005_account_permission_system.sql").read_text()

    assert "Password: 123456.dc" not in migration
    assert "123456.dc" not in migration
    assert "$2b$12$ORXIEYVft.OQ5v55S6WiFukZGEk.1QkB/fElA.0IMM5shEpBoyhWC" not in migration
    assert "password_hash = EXCLUDED.password_hash" not in migration
    assert "force_password_change = EXCLUDED.force_password_change" not in migration


def test_helm_secret_template_requires_explicit_secret_values() -> None:
    template = (ROOT / "deploy/helm/ai-gateway/templates/secret.yaml").read_text()

    assert 'default "change-me"' not in template
    assert 'default "change-me-in-production"' not in template
    assert "required " in template


def test_prometheus_metrics_are_not_publicly_exposed() -> None:
    main_py = (ROOT / "src/main.py").read_text()
    ingress = (ROOT / "deploy/helm/ai-gateway/templates/ingress.yaml").read_text()
    whitelist = main_py.split("whitelist_paths=[", 1)[1].split("]", 1)[0]
    metrics_route = main_py.split('@app.get("/metrics"', 1)[1].split("metrics_collector", 1)[0]

    assert '"/metrics"' not in whitelist
    assert "path: /metrics" not in ingress
    assert "/metrics" not in StreamingRateLimitConfig().whitelist_paths
    assert "/metrics" not in StreamingAdmissionConfig().whitelist_paths
    assert "Depends(get_auth_context)" in metrics_route
    assert "Capability.GATEWAY_METRICS_READ" in metrics_route
