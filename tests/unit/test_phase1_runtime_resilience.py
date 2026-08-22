"""Unit tests for Phase 1 runtime resilience and SOTA architectural optimizations.

Covers:
1. Hermes-inspired FailoverClassifier and 24-reason error taxonomy
2. SubAgentConcurrencyLimiter memory & Redis distributed leasing
3. Anti-injection SUMMARY_PREFIX preamble in ContextAssembler
4. Startup configuration for database connection pool elastic limits
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from assistant_service.config.startup_fingerprint import resolve_startup_config
from assistant_service.core.agent.failover_classifier import (
    FailoverReason,
    SuggestedAction,
    classify_provider_error,
)
from assistant_service.core.agent.subagent_dispatch_runtime import (
    DispatchScope,
    SubAgentConcurrencyExceeded,
    SubAgentConcurrencyLimiter,
)
from assistant_service.core.runtime.context.assembler import ContextAssemblerV2

# ============================================================================
# 1. Failover Classifier Tests
# ============================================================================


def test_classify_none_exception() -> None:
    classification = classify_provider_error(None)
    assert classification.reason == FailoverReason.UNKNOWN_FAILURE
    assert not classification.retryable


def test_classify_rate_limit_429() -> None:
    class MockRateLimitError(Exception):
        status_code = 429

    exc = MockRateLimitError("Rate limit exceeded: 429 Too Many Requests")
    res = classify_provider_error(exc)
    assert res.reason == FailoverReason.RATE_LIMIT_429
    assert res.retryable is True
    assert res.suggested_action == SuggestedAction.RETRY_WITH_BACKOFF


def test_classify_tpm_limit() -> None:
    exc = Exception("Tokens per minute (TPM) limit exceeded for your tier")
    res = classify_provider_error(exc)
    assert res.reason == FailoverReason.TPM_LIMIT_EXCEEDED
    assert res.retryable is True


def test_classify_quota_exhausted() -> None:
    exc = Exception("Error 429: You exceeded your current quota, please check your billing")
    res = classify_provider_error(exc)
    assert res.reason == FailoverReason.INSUFFICIENT_QUOTA
    assert res.retryable is False
    assert res.suggested_action == SuggestedAction.FALLBACK_SECONDARY_PROVIDER


def test_classify_context_window_overflow() -> None:
    class MockBadRequestError(Exception):
        status_code = 400

    exc = MockBadRequestError("Invalid request: This model maximum context length is 128000 tokens, but your prompt resulted in 135000 tokens")
    res = classify_provider_error(exc)
    assert res.reason == FailoverReason.CONTEXT_WINDOW_EXCEEDED
    assert res.retryable is True
    assert res.suggested_action == SuggestedAction.TRIGGER_CONTEXT_COMPACTION
    assert res.metadata.get("needs_compaction") is True


def test_classify_auth_failure() -> None:
    class MockAuthError(Exception):
        status_code = 401

    exc = MockAuthError("Unauthorized: Invalid API key provided")
    res = classify_provider_error(exc)
    assert res.reason == FailoverReason.AUTH_INVALID_KEY
    assert res.retryable is False
    assert res.suggested_action == SuggestedAction.FAIL_FAST_USER_ALERT


def test_classify_provider_outages() -> None:
    for code, expected_reason, expected_action in [
        (500, FailoverReason.SERVER_ERROR_500, SuggestedAction.RETRY_WITH_BACKOFF),
        (502, FailoverReason.BAD_GATEWAY_502, SuggestedAction.FALLBACK_SECONDARY_PROVIDER),
        (503, FailoverReason.SERVICE_UNAVAILABLE_503, SuggestedAction.FALLBACK_SECONDARY_PROVIDER),
        (504, FailoverReason.GATEWAY_TIMEOUT_504, SuggestedAction.RETRY_WITH_BACKOFF),
    ]:
        class MockHttpError(Exception):
            status_code = code

        exc = MockHttpError(f"HTTP {code} Server Error")
        res = classify_provider_error(exc)
        assert res.reason == expected_reason
        assert res.suggested_action == expected_action
        assert res.retryable is True


def test_classify_json_parse_error() -> None:
    exc = json.JSONDecodeError("Expecting value", "{\"tool_call\": ", 14)
    res = classify_provider_error(exc)
    assert res.reason == FailoverReason.JSON_PARSE_ERROR
    assert res.suggested_action == SuggestedAction.REFINE_PROMPT_SCHEMA
    assert res.retryable is True


def test_classification_to_dict() -> None:
    exc = Exception("Connection timed out after 30000ms")
    res = classify_provider_error(exc)
    data = res.to_dict()
    assert data["reason"] == FailoverReason.CONNECTION_TIMEOUT.value
    assert data["suggested_action"] == SuggestedAction.RETRY_WITH_BACKOFF.value
    assert data["retryable"] is True
    assert "metadata" in data


def test_unknown_classification_never_echoes_exception_text() -> None:
    secret = "Authorization: Bearer private-provider-token"
    data = classify_provider_error(RuntimeError(secret)).to_dict()

    assert secret not in json.dumps(data)
    assert data["reason"] == FailoverReason.UNKNOWN_FAILURE.value


# ============================================================================
# 2. SubAgent Concurrency Limiter Tests (Memory & Redis)
# ============================================================================


def test_subagent_limiter_memory_accounting() -> None:
    limiter = SubAgentConcurrencyLimiter(tenant_limit=5, session_limit=3)
    scope1 = DispatchScope(tenant_id="tenant_a", session_id="session_1")

    # Acquire 2 leases
    lease1 = limiter.acquire(scope1, 2)
    assert lease1.count == 2

    # Acquire 1 more (total 3 for session_1, hits session limit)
    lease2 = limiter.acquire(scope1, 1)

    # Exceed session limit
    with pytest.raises(SubAgentConcurrencyExceeded, match="session sub-agent concurrency exhausted"):
        limiter.acquire(scope1, 1)

    # Release lease1
    lease1.release()

    # Now we can acquire 1 more for session_1
    lease3 = limiter.acquire(scope1, 1)
    lease2.release()
    lease3.release()


def test_subagent_limiter_tenant_exhaustion() -> None:
    limiter = SubAgentConcurrencyLimiter(tenant_limit=3, session_limit=3)
    scope1 = DispatchScope(tenant_id="tenant_x", session_id="session_1")
    scope2 = DispatchScope(tenant_id="tenant_x", session_id="session_2")

    lease1 = limiter.acquire(scope1, 2)
    lease2 = limiter.acquire(scope2, 1)

    # Tenant limit (3) is now full across sessions
    with pytest.raises(SubAgentConcurrencyExceeded, match="tenant sub-agent concurrency exhausted"):
        limiter.acquire(scope2, 1)

    lease1.release()
    lease2.release()


def test_subagent_limiter_redis_distributed_backend() -> None:
    mock_redis = MagicMock()
    # Mock redis tracking
    storage: dict[str, int] = {}

    def mock_incrby(key: str, amount: int) -> int:
        storage[key] = storage.get(key, 0) + amount
        return storage[key]

    def mock_decrby(key: str, amount: int) -> int:
        storage[key] = max(0, storage.get(key, 0) - amount)
        return storage[key]

    mock_redis.incrby.side_effect = mock_incrby
    mock_redis.decrby.side_effect = mock_decrby
    mock_redis.expire.return_value = True
    mock_redis.delete.return_value = True

    limiter = SubAgentConcurrencyLimiter(
        tenant_limit=4,
        session_limit=2,
        redis_client=mock_redis,
        ttl_seconds=120,
    )
    scope = DispatchScope(tenant_id="tenant_dist", session_id="session_dist")

    lease = limiter.acquire(scope, 2)
    assert lease.count == 2
    assert storage.get("subagent:concurrency:tenant:tenant_dist") == 2
    assert storage.get("subagent:concurrency:session:tenant_dist:session_dist") == 2

    # Release lease
    lease.release()
    assert storage.get("subagent:concurrency:tenant:tenant_dist") == 0
    assert storage.get("subagent:concurrency:session:tenant_dist:session_dist") == 0


# ============================================================================
# 3. Context Assembler Anti-Injection Preamble Tests
# ============================================================================


def test_context_assembler_compaction_summary_preamble() -> None:
    raw_summary = "User discussed quarterly sales targets and queried PostgreSQL table `revenue`."
    rendered, records = ContextAssemblerV2._compose_request_context(
        current_context=None,
        user_preferences=None,
        long_term_memory=None,
        task_state=None,
        injected_files=None,
        skills_metadata=None,
        memory_snippets=None,
        source_summaries=None,
        tool_result_summaries=None,
        artifact_summaries=None,
        compaction_summary=raw_summary,
    )

    # Verify that the compaction summary record contains the anti-injection preamble
    summary_record = next((r for r in records if r.get("kind") == "compaction_summary"), None)
    assert summary_record is not None
    content = summary_record.get("content", "")
    assert "[CONTEXT COMPACTION SNAPSHOT:" in content
    assert "situational grounding only" in content
    assert raw_summary in content


# ============================================================================
# 4. Database Connection Pool Configuration Tests
# ============================================================================


def test_startup_fingerprint_db_pool_settings() -> None:
    snapshot = resolve_startup_config({})
    assert snapshot.int_value("ASSISTANT_DB_POOL_MIN_SIZE") == 2
    assert snapshot.int_value("ASSISTANT_DB_POOL_MAX_SIZE") == 20
