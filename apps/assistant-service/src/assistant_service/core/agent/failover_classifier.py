"""Hermes-inspired 24-reason failover taxonomy and error classification engine.

Categorizes LLM provider exceptions, network anomalies, tool execution faults,
and context overflows into actionable recovery paths for the streaming agent loop.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class FailoverReason(str, enum.Enum):
    """Granular classification of execution and provider failure modes."""

    # Rate limiting & Quotas
    RATE_LIMIT_429 = "rate_limit_429"
    TPM_LIMIT_EXCEEDED = "tpm_limit_exceeded"
    RPM_LIMIT_EXCEEDED = "rpm_limit_exceeded"
    INSUFFICIENT_QUOTA = "insufficient_quota"
    BILLING_EXHAUSTED = "billing_exhausted"

    # Context & Token Window
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"
    PROMPT_TOO_LONG = "prompt_too_long"
    MAX_TOKENS_OVERFLOW = "max_tokens_overflow"

    # Authentication & Access
    AUTH_INVALID_KEY = "auth_invalid_key"
    AUTH_PERMISSION_DENIED = "auth_permission_denied"
    MODEL_NOT_FOUND = "model_not_found"
    MODEL_ACCESS_DENIED = "model_access_denied"

    # Provider Outage & Network
    SERVER_ERROR_500 = "server_error_500"
    BAD_GATEWAY_502 = "bad_gateway_502"
    SERVICE_UNAVAILABLE_503 = "service_unavailable_503"
    GATEWAY_TIMEOUT_504 = "gateway_timeout_504"
    CONNECTION_TIMEOUT = "connection_timeout"
    CONNECTION_RESET = "connection_reset"

    # Structured Output & Parsing
    TOOL_SCHEMA_VIOLATION = "tool_schema_violation"
    JSON_PARSE_ERROR = "json_parse_error"
    MALFORMED_TOOL_CALL = "malformed_tool_call"

    # Content Safety & Moderation
    CONTENT_FILTER_TRIGGERED = "content_filter_triggered"
    SAFETY_BLOCK = "safety_block"

    # Execution & Cancellation
    CLIENT_CANCELLED = "client_cancelled"
    RUN_BUDGET_EXCEEDED = "run_budget_exceeded"
    UNKNOWN_FAILURE = "unknown_failure"


class SuggestedAction(str, enum.Enum):
    """Actionable recommendation for agent loop recovery."""

    RETRY_WITH_BACKOFF = "retry_with_backoff"
    TRIGGER_CONTEXT_COMPACTION = "trigger_context_compaction"
    FALLBACK_SECONDARY_PROVIDER = "fallback_secondary_provider"
    SWITCH_LOCAL_FALLBACK = "switch_local_fallback"
    REFINE_PROMPT_SCHEMA = "refine_prompt_schema"
    FAIL_FAST_USER_ALERT = "fail_fast_user_alert"


@dataclass(frozen=True)
class FailoverClassification:
    """Structured result of classifying an execution error."""

    reason: FailoverReason
    suggested_action: SuggestedAction
    retryable: bool
    status_code: int | None
    clean_message: str
    raw_error_type: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.value,
            "suggested_action": self.suggested_action.value,
            "retryable": self.retryable,
            "status_code": self.status_code,
            "clean_message": self.clean_message,
            "raw_error_type": self.raw_error_type,
            "metadata": self.metadata,
        }


def classify_provider_error(exc: Exception | None) -> FailoverClassification:
    """Classify any exception encountered during agent loop execution."""
    if exc is None:
        return FailoverClassification(
            reason=FailoverReason.UNKNOWN_FAILURE,
            suggested_action=SuggestedAction.FAIL_FAST_USER_ALERT,
            retryable=False,
            status_code=None,
            clean_message="Unknown execution state",
            raw_error_type="None",
            metadata={},
        )

    exc_type = type(exc).__name__
    exc_str = str(exc).lower()

    # Extract status code if available
    status_code = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if status_code is None:
        # Check for numeric status in string representation
        for code in (429, 400, 401, 403, 404, 413, 500, 502, 503, 504):
            if f"status code {code}" in exc_str or f" {code} " in exc_str or f"error {code}" in exc_str:
                status_code = code
                break

    # 1. Rate limits & Quotas
    if (
        status_code == 429
        or "rate limit" in exc_str
        or "too many requests" in exc_str
        or "tpm" in exc_str
        or "rpm" in exc_str
        or "tokens per minute" in exc_str
        or "requests per minute" in exc_str
        or "quota" in exc_str
    ):
        if "tpm" in exc_str or "tokens per minute" in exc_str:
            reason = FailoverReason.TPM_LIMIT_EXCEEDED
        elif "rpm" in exc_str or "requests per minute" in exc_str:
            reason = FailoverReason.RPM_LIMIT_EXCEEDED
        elif "quota" in exc_str or "insufficient" in exc_str or "billing" in exc_str:
            reason = FailoverReason.INSUFFICIENT_QUOTA
        else:
            reason = FailoverReason.RATE_LIMIT_429

        is_billing = reason in (FailoverReason.INSUFFICIENT_QUOTA, FailoverReason.BILLING_EXHAUSTED)
        return FailoverClassification(
            reason=reason,
            suggested_action=(
                SuggestedAction.FALLBACK_SECONDARY_PROVIDER
                if is_billing
                else SuggestedAction.RETRY_WITH_BACKOFF
            ),
            retryable=not is_billing,
            status_code=429,
            clean_message="Provider rate limit or quota exceeded",
            raw_error_type=exc_type,
            metadata={"quota_issue": is_billing},
        )

    # 2. Context Window & Token Overflow
    if (
        status_code in (400, 413)
        and (
            "context" in exc_str
            or "maximum context length" in exc_str
            or "too long" in exc_str
            or "token" in exc_str
            or "content length" in exc_str
        )
        or "context_window_exceeded" in exc_str
        or "prompt is too long" in exc_str
    ):
        return FailoverClassification(
            reason=FailoverReason.CONTEXT_WINDOW_EXCEEDED,
            suggested_action=SuggestedAction.TRIGGER_CONTEXT_COMPACTION,
            retryable=True,
            status_code=status_code or 413,
            clean_message="Prompt exceeds model context window; compaction required",
            raw_error_type=exc_type,
            metadata={"needs_compaction": True},
        )

    # 3. Authentication & Model Access
    if status_code in (401, 403) or "unauthorized" in exc_str or "invalid api key" in exc_str:
        return FailoverClassification(
            reason=FailoverReason.AUTH_INVALID_KEY,
            suggested_action=SuggestedAction.FAIL_FAST_USER_ALERT,
            retryable=False,
            status_code=status_code or 401,
            clean_message="Provider authentication failed or API key invalid",
            raw_error_type=exc_type,
            metadata={},
        )

    if status_code == 404 or "model not found" in exc_str or "does not exist" in exc_str:
        return FailoverClassification(
            reason=FailoverReason.MODEL_NOT_FOUND,
            suggested_action=SuggestedAction.FALLBACK_SECONDARY_PROVIDER,
            retryable=False,
            status_code=404,
            clean_message="Requested model was not found on provider",
            raw_error_type=exc_type,
            metadata={},
        )

    # 4. Provider Outages & Network Timeouts
    if status_code == 500 or "internal server error" in exc_str:
        return FailoverClassification(
            reason=FailoverReason.SERVER_ERROR_500,
            suggested_action=SuggestedAction.RETRY_WITH_BACKOFF,
            retryable=True,
            status_code=500,
            clean_message="Provider internal error (500)",
            raw_error_type=exc_type,
            metadata={},
        )
    if status_code == 502 or "bad gateway" in exc_str:
        return FailoverClassification(
            reason=FailoverReason.BAD_GATEWAY_502,
            suggested_action=SuggestedAction.FALLBACK_SECONDARY_PROVIDER,
            retryable=True,
            status_code=502,
            clean_message="Bad gateway from upstream provider (502)",
            raw_error_type=exc_type,
            metadata={},
        )
    if status_code == 503 or "service unavailable" in exc_str or "overloaded" in exc_str:
        return FailoverClassification(
            reason=FailoverReason.SERVICE_UNAVAILABLE_503,
            suggested_action=SuggestedAction.FALLBACK_SECONDARY_PROVIDER,
            retryable=True,
            status_code=503,
            clean_message="Provider service temporarily unavailable (503)",
            raw_error_type=exc_type,
            metadata={},
        )
    if status_code == 504 or "gateway timeout" in exc_str:
        return FailoverClassification(
            reason=FailoverReason.GATEWAY_TIMEOUT_504,
            suggested_action=SuggestedAction.RETRY_WITH_BACKOFF,
            retryable=True,
            status_code=504,
            clean_message="Provider gateway timeout (504)",
            raw_error_type=exc_type,
            metadata={},
        )
    if "timeout" in exc_str or "timed out" in exc_str or "connecttimeouterror" in exc_str:
        return FailoverClassification(
            reason=FailoverReason.CONNECTION_TIMEOUT,
            suggested_action=SuggestedAction.RETRY_WITH_BACKOFF,
            retryable=True,
            status_code=408,
            clean_message="Connection to provider timed out",
            raw_error_type=exc_type,
            metadata={},
        )

    # 5. Structured Output / JSON Parsing
    if (
        "jsondecodeerror" in exc_str
        or "jsondecodeerror" in exc_type.lower()
        or "invalid json" in exc_str
        or "parse error" in exc_str
        or "expecting value" in exc_str
    ):
        return FailoverClassification(
            reason=FailoverReason.JSON_PARSE_ERROR,
            suggested_action=SuggestedAction.REFINE_PROMPT_SCHEMA,
            retryable=True,
            status_code=None,
            clean_message="Model returned invalid JSON or malformed tool payload",
            raw_error_type=exc_type,
            metadata={},
        )

    # 6. Safety & Content Filter
    if "content_filter" in exc_str or "safety" in exc_str or "blocked" in exc_str:
        return FailoverClassification(
            reason=FailoverReason.CONTENT_FILTER_TRIGGERED,
            suggested_action=SuggestedAction.FAIL_FAST_USER_ALERT,
            retryable=False,
            status_code=400,
            clean_message="Prompt or output flagged by content safety filter",
            raw_error_type=exc_type,
            metadata={},
        )

    # 7. Cancellation & Budget
    if "cancelled" in exc_str or exc_type in ("CancelledError", "AsyncioCancelledError"):
        return FailoverClassification(
            reason=FailoverReason.CLIENT_CANCELLED,
            suggested_action=SuggestedAction.FAIL_FAST_USER_ALERT,
            retryable=False,
            status_code=499,
            clean_message="Execution cancelled by client",
            raw_error_type=exc_type,
            metadata={},
        )
    if "budget" in exc_str or "runbudgetexceeded" in exc_str:
        return FailoverClassification(
            reason=FailoverReason.RUN_BUDGET_EXCEEDED,
            suggested_action=SuggestedAction.FAIL_FAST_USER_ALERT,
            retryable=False,
            status_code=402,
            clean_message="Execution run budget exceeded",
            raw_error_type=exc_type,
            metadata={},
        )

    # Fallback / Default
    return FailoverClassification(
        reason=FailoverReason.UNKNOWN_FAILURE,
        suggested_action=SuggestedAction.FAIL_FAST_USER_ALERT,
        retryable=False,
        status_code=status_code,
        clean_message="Unclassified runtime exception",
        raw_error_type=exc_type,
        metadata={"raw_message": str(exc)[:200]},
    )
