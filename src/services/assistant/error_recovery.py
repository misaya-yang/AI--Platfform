"""
Error Recovery System for Enterprise Agent.

Provides intelligent error handling and recovery:
1. Retry with backoff - Transient errors
2. Self-repair - Quality issues detected by guardrails
3. Graceful degradation - Fallback strategies
4. Error context - Rich error information for debugging

Agent decides HOW to recover, guardrails define WHEN recovery is needed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, TypeVar, Generic
import functools

from ...core.observability.logging import get_logger

logger = get_logger(__name__)

T = TypeVar('T')


class ErrorType(str, Enum):
    """Classification of errors for recovery strategy selection."""

    TRANSIENT = "transient"           # Network, timeout - retry with backoff
    RATE_LIMIT = "rate_limit"         # API rate limit - wait and retry
    QUALITY_VIOLATION = "quality"     # Guardrail violation - self-repair
    VALIDATION_ERROR = "validation"   # Input/output validation - fix and retry
    RESOURCE_ERROR = "resource"       # Memory, disk - reduce load
    PERMANENT = "permanent"           # Unrecoverable - fail gracefully


class RecoveryStrategy(str, Enum):
    """Recovery strategies the Agent can use."""

    RETRY = "retry"                   # Simple retry
    RETRY_WITH_BACKOFF = "backoff"    # Exponential backoff
    SELF_REPAIR = "repair"            # Agent fixes the issue
    REDUCE_COMPLEXITY = "simplify"    # Reduce task complexity
    FALLBACK = "fallback"             # Use alternative approach
    FAIL_GRACEFULLY = "fail"          # Give up with good error message


@dataclass
class ErrorContext:
    """Rich context about an error for intelligent recovery."""

    error_type: ErrorType
    message: str
    original_error: Optional[Exception] = None
    attempt_number: int = 1
    max_attempts: int = 3
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "error_type": self.error_type.value,
            "message": self.message,
            "attempt_number": self.attempt_number,
            "max_attempts": self.max_attempts,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }


@dataclass
class RecoveryResult(Generic[T]):
    """Result of a recovery attempt."""

    success: bool
    value: Optional[T] = None
    error_context: Optional[ErrorContext] = None
    strategy_used: Optional[RecoveryStrategy] = None
    total_attempts: int = 0


class ErrorRecoveryManager:
    """
    Manages error recovery with intelligent strategy selection.

    Agent decides recovery approach, system provides tools.

    Usage:
        manager = ErrorRecoveryManager()

        result = await manager.execute_with_recovery(
            operation=my_async_function,
            error_classifier=classify_my_errors,
        )

        if result.success:
            print(f"Success after {result.total_attempts} attempts")
        else:
            print(f"Failed: {result.error_context.message}")
    """

    # Default retry configuration
    DEFAULT_MAX_RETRIES = 3
    DEFAULT_BASE_DELAY = 1.0  # seconds
    DEFAULT_MAX_DELAY = 30.0  # seconds

    def __init__(
        self,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_delay: float = DEFAULT_BASE_DELAY,
        max_delay: float = DEFAULT_MAX_DELAY,
    ):
        """
        Initialize recovery manager.

        Args:
            max_retries: Maximum retry attempts
            base_delay: Base delay for exponential backoff
            max_delay: Maximum delay between retries
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay

    async def execute_with_recovery(
        self,
        operation: Callable[..., Any],
        *args,
        error_classifier: Optional[Callable[[Exception], ErrorType]] = None,
        recovery_handlers: Optional[Dict[ErrorType, Callable]] = None,
        **kwargs,
    ) -> RecoveryResult:
        """
        Execute operation with automatic error recovery.

        Args:
            operation: Async function to execute
            error_classifier: Function to classify errors
            recovery_handlers: Custom handlers for specific error types
            *args, **kwargs: Arguments for operation

        Returns:
            RecoveryResult with success status and value/error
        """
        error_classifier = error_classifier or self._default_error_classifier
        recovery_handlers = recovery_handlers or {}
        attempt = 0
        last_error: Optional[ErrorContext] = None

        while attempt < self.max_retries:
            attempt += 1
            try:
                result = await operation(*args, **kwargs)
                return RecoveryResult(
                    success=True,
                    value=result,
                    total_attempts=attempt,
                )

            except Exception as e:
                error_type = error_classifier(e)
                last_error = ErrorContext(
                    error_type=error_type,
                    message=str(e),
                    original_error=e,
                    attempt_number=attempt,
                    max_attempts=self.max_retries,
                )

                logger.warning(
                    f"Error on attempt {attempt}/{self.max_retries}: "
                    f"{error_type.value} - {e}"
                )

                # Check for custom handler
                if error_type in recovery_handlers:
                    try:
                        handler_result = await recovery_handlers[error_type](e, last_error)
                        if handler_result is not None:
                            return RecoveryResult(
                                success=True,
                                value=handler_result,
                                error_context=last_error,
                                strategy_used=RecoveryStrategy.FALLBACK,
                                total_attempts=attempt,
                            )
                    except Exception as handler_error:
                        logger.error(f"Recovery handler failed: {handler_error}")

                # Select recovery strategy
                strategy = self._select_strategy(error_type, attempt)
                last_error.metadata["strategy"] = strategy.value

                if strategy == RecoveryStrategy.FAIL_GRACEFULLY:
                    break

                if strategy in (RecoveryStrategy.RETRY, RecoveryStrategy.RETRY_WITH_BACKOFF):
                    if strategy == RecoveryStrategy.RETRY_WITH_BACKOFF:
                        delay = self._calculate_backoff(attempt)
                        logger.info(f"Waiting {delay:.1f}s before retry...")
                        await asyncio.sleep(delay)
                    continue

        # All retries exhausted
        return RecoveryResult(
            success=False,
            error_context=last_error,
            strategy_used=RecoveryStrategy.FAIL_GRACEFULLY,
            total_attempts=attempt,
        )

    def _default_error_classifier(self, error: Exception) -> ErrorType:
        """
        Default error classification.

        Args:
            error: The exception to classify

        Returns:
            ErrorType classification
        """
        error_str = str(error).lower()
        error_type_str = type(error).__name__.lower()

        # Rate limit errors
        if any(kw in error_str for kw in ["rate limit", "too many requests", "429"]):
            return ErrorType.RATE_LIMIT

        # Transient errors
        if any(kw in error_str for kw in ["timeout", "connection", "network", "temporary"]):
            return ErrorType.TRANSIENT

        if any(kw in error_type_str for kw in ["timeout", "connection", "network"]):
            return ErrorType.TRANSIENT

        # Validation errors
        if any(kw in error_str for kw in ["validation", "invalid", "schema"]):
            return ErrorType.VALIDATION_ERROR

        # Quality violations (from guardrails)
        if any(kw in error_str for kw in ["quality", "guardrail", "insufficient"]):
            return ErrorType.QUALITY_VIOLATION

        # Resource errors
        if any(kw in error_str for kw in ["memory", "disk", "resource"]):
            return ErrorType.RESOURCE_ERROR

        # Default to transient (can retry)
        return ErrorType.TRANSIENT

    def _select_strategy(self, error_type: ErrorType, attempt: int) -> RecoveryStrategy:
        """
        Select recovery strategy based on error type and attempt.

        Agent-like decision making about recovery approach.

        Args:
            error_type: Type of error
            attempt: Current attempt number

        Returns:
            RecoveryStrategy to use
        """
        # Permanent errors - fail immediately
        if error_type == ErrorType.PERMANENT:
            return RecoveryStrategy.FAIL_GRACEFULLY

        # Last attempt - fail gracefully
        if attempt >= self.max_retries:
            return RecoveryStrategy.FAIL_GRACEFULLY

        # Rate limit - always backoff
        if error_type == ErrorType.RATE_LIMIT:
            return RecoveryStrategy.RETRY_WITH_BACKOFF

        # Transient errors - simple retry first, then backoff
        if error_type == ErrorType.TRANSIENT:
            if attempt <= 1:
                return RecoveryStrategy.RETRY
            return RecoveryStrategy.RETRY_WITH_BACKOFF

        # Quality violations - self-repair
        if error_type == ErrorType.QUALITY_VIOLATION:
            return RecoveryStrategy.SELF_REPAIR

        # Validation errors - retry with backoff
        if error_type == ErrorType.VALIDATION_ERROR:
            return RecoveryStrategy.RETRY_WITH_BACKOFF

        # Resource errors - reduce complexity
        if error_type == ErrorType.RESOURCE_ERROR:
            return RecoveryStrategy.REDUCE_COMPLEXITY

        return RecoveryStrategy.RETRY_WITH_BACKOFF

    def _calculate_backoff(self, attempt: int) -> float:
        """
        Calculate exponential backoff delay.

        Args:
            attempt: Current attempt number

        Returns:
            Delay in seconds
        """
        delay = self.base_delay * (2 ** (attempt - 1))
        return min(delay, self.max_delay)


def with_recovery(
    max_retries: int = 3,
    base_delay: float = 1.0,
    error_classifier: Optional[Callable[[Exception], ErrorType]] = None,
):
    """
    Decorator for automatic error recovery.

    Usage:
        @with_recovery(max_retries=3)
        async def my_operation():
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            manager = ErrorRecoveryManager(
                max_retries=max_retries,
                base_delay=base_delay,
            )
            result = await manager.execute_with_recovery(
                func,
                *args,
                error_classifier=error_classifier,
                **kwargs,
            )
            if result.success:
                return result.value
            raise result.error_context.original_error or Exception(
                result.error_context.message
            )
        return wrapper
    return decorator


# Module-level convenience instance
_default_manager: Optional[ErrorRecoveryManager] = None


def get_error_recovery_manager() -> ErrorRecoveryManager:
    """Get the default error recovery manager."""
    global _default_manager
    if _default_manager is None:
        _default_manager = ErrorRecoveryManager()
    return _default_manager
