from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.auth.user_resolver import UserContext
from src.core.gateway.rate_policy import RatePolicyResolver
from src.proxy.config_loader import ProxyServiceConfig


def _request(*, rules: list[dict], api_key_hash: str = "hash-a") -> SimpleNamespace:
    request = SimpleNamespace()
    request.state = SimpleNamespace(api_key_hash=api_key_hash)
    request.app = SimpleNamespace()
    request.app.state = SimpleNamespace(database=None, rate_limit_rules=rules)
    return request


def _user(*, tenant_id: str = "tenant-a", user_id: str = "user-a") -> UserContext:
    return UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        tier="normal",
        is_authenticated=True,
        roles=["user"],
        ip="127.0.0.1",
    )


def _service_config(*, enabled: bool = False) -> ProxyServiceConfig:
    return ProxyServiceConfig(
        service_id="imam",
        service_name="imam",
        upstream_url="http://localhost:2024",
        rate_limit_enabled=enabled,
        rate_limit_requests=5,
        rate_limit_window=60,
    )


@pytest.mark.asyncio
async def test_service_level_rule_overrides_global_defaults() -> None:
    resolver = RatePolicyResolver()

    policies = await resolver.resolve(
        request=_request(
            rules=[
                {
                    "scope": "global",
                    "requests": 1000,
                    "window_seconds": 60,
                    "enabled": True,
                }
            ]
        ),
        user=_user(),
        service_name="imam",
        operation="run_wait",
        service_config=_service_config(enabled=True),
    )

    assert [policy.dimension for policy in policies] == ["service:imam"]
    assert policies[0].requests == 5
    assert "tenant-a" in policies[0].key


@pytest.mark.asyncio
async def test_api_key_rule_applies_only_to_authenticated_key_hash() -> None:
    resolver = RatePolicyResolver()
    rules = [
        {
            "scope": "api_key",
            "scope_id": "hash-a",
            "requests": 7,
            "window": 60,
            "enabled": True,
        }
    ]

    matching = await resolver.resolve(
        request=_request(rules=rules, api_key_hash="hash-a"),
        user=_user(),
        service_name="imam",
        operation="run_wait",
        service_config=_service_config(),
    )
    other = await resolver.resolve(
        request=_request(rules=rules, api_key_hash="hash-b"),
        user=_user(),
        service_name="imam",
        operation="run_wait",
        service_config=_service_config(),
    )

    assert [policy.dimension for policy in matching] == ["api_key"]
    assert other == []


@pytest.mark.asyncio
async def test_tenant_rule_applies_only_inside_that_tenant() -> None:
    resolver = RatePolicyResolver()
    rules = [
        {
            "scope": "tenant",
            "scope_id": "tenant-a",
            "requests": 11,
            "window": 60,
            "enabled": True,
        }
    ]

    matching = await resolver.resolve(
        request=_request(rules=rules),
        user=_user(tenant_id="tenant-a"),
        service_name="imam",
        operation="run_wait",
        service_config=_service_config(),
    )
    other = await resolver.resolve(
        request=_request(rules=rules),
        user=_user(tenant_id="tenant-b"),
        service_name="imam",
        operation="run_wait",
        service_config=_service_config(),
    )

    assert [policy.dimension for policy in matching] == ["tenant:tenant-a"]
    assert other == []


@pytest.mark.asyncio
async def test_disabled_rule_is_ignored() -> None:
    resolver = RatePolicyResolver()

    policies = await resolver.resolve(
        request=_request(
            rules=[
                {
                    "scope": "global",
                    "requests": 1,
                    "window": 60,
                    "enabled": False,
                }
            ]
        ),
        user=_user(),
        service_name="imam",
        operation="run_wait",
        service_config=_service_config(),
    )

    assert policies == []


@pytest.mark.asyncio
async def test_rule_deletion_invalidates_runtime_policy_immediately() -> None:
    rules = [{"scope": "global", "requests": 10, "window": 60, "enabled": True}]
    resolver = RatePolicyResolver()
    request = _request(rules=rules)

    first = await resolver.resolve(
        request=request,
        user=_user(),
        service_name="imam",
        operation="run_wait",
        service_config=_service_config(),
    )
    rules.clear()
    resolver.invalidate()
    second = await resolver.resolve(
        request=request,
        user=_user(),
        service_name="imam",
        operation="run_wait",
        service_config=_service_config(),
    )

    assert [policy.dimension for policy in first] == ["global"]
    assert second == []


@pytest.mark.asyncio
async def test_burst_only_increases_effective_limit_for_burst_strategies() -> None:
    resolver = RatePolicyResolver()

    policies = await resolver.resolve(
        request=_request(
            rules=[
                {
                    "scope": "global",
                    "requests": 10,
                    "window": 60,
                    "burst": 5,
                    "strategy": "sliding_window",
                    "enabled": True,
                },
                {
                    "scope": "operation",
                    "scope_id": "run_wait",
                    "requests": 10,
                    "window": 60,
                    "burst": 5,
                    "strategy": "sliding_window_with_burst",
                    "enabled": True,
                },
            ]
        ),
        user=_user(),
        service_name="imam",
        operation="run_wait",
        service_config=_service_config(),
    )

    by_dimension = {policy.dimension: policy for policy in policies}
    assert by_dimension["global"].requests == 10
    assert by_dimension["operation:run_wait"].requests == 15
