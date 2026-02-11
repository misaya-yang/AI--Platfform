"""
Metrics Recorder Service - Central service for recording dashboard metrics to Redis

This service provides methods to record:
- Request metrics (count, latency, status)
- Token usage (input/output tokens)
- LangGraph run metrics (duration, status)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...persistence.redis import RedisStorage

logger = logging.getLogger(__name__)

# Global singleton instance
_metrics_recorder: MetricsRecorder | None = None

# Token pricing for cost estimation (per 1K tokens, USD)
TOKEN_PRICING = {
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    "claude-3-opus": {"input": 0.015, "output": 0.075},
    "claude-3-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
    "default": {"input": 0.001, "output": 0.002},
}

# TTL constants
TTL_24H = 86400  # 24 hours
TTL_48H = 172800  # 48 hours
TTL_7D = 604800  # 7 days


class MetricsRecorder:
    """Central service for recording metrics to Redis"""

    def __init__(self, redis: RedisStorage | None = None):
        self.redis = redis

    def set_redis(self, redis: RedisStorage) -> None:
        """Set or update the Redis storage instance"""
        self.redis = redis

    def _get_date_str(self) -> str:
        """Get current date as string (YYYY-MM-DD)"""
        return datetime.now().strftime("%Y-%m-%d")

    def _get_hour_str(self) -> str:
        """Get current hour as string (HH)"""
        return datetime.now().strftime("%H")

    async def record_request(
        self,
        method: str,
        path: str,
        status_code: int,
        duration_ms: float,
        user_id: str = "",
        service_id: str = "",
    ) -> None:
        """
        Record a single request metric

        Args:
            method: HTTP method (GET, POST, etc.)
            path: Request path
            status_code: HTTP response status code
            duration_ms: Request duration in milliseconds
            user_id: User ID (optional)
            service_id: Service ID (optional)
        """
        if not self.redis or not self.redis._client:
            return

        try:
            today = self._get_date_str()
            hour = self._get_hour_str()
            is_success = 200 <= status_code < 400

            # Use pipeline for atomic operations
            pipe = self.redis._client.pipeline()

            # Increment total requests
            total_key = f"metrics:requests:total:{today}"
            pipe.incr(total_key)
            pipe.expire(total_key, TTL_48H)

            # Increment success count if successful
            if is_success:
                success_key = f"metrics:requests:success:{today}"
                pipe.incr(success_key)
                pipe.expire(success_key, TTL_48H)

            # Add latency to sum
            latency_key = f"metrics:latency:sum:{today}"
            pipe.incrby(latency_key, int(duration_ms))
            pipe.expire(latency_key, TTL_48H)

            # Increment latency count for average calculation
            latency_count_key = f"metrics:latency:count:{today}"
            pipe.incr(latency_count_key)
            pipe.expire(latency_count_key, TTL_48H)

            # Increment hourly counter
            hour_key = f"metrics:requests:hour:{today}:{hour}"
            pipe.incr(hour_key)
            pipe.expire(hour_key, TTL_48H)

            # Track errors by hour
            if not is_success:
                error_key = f"metrics:errors:hour:{today}:{hour}"
                pipe.incr(error_key)
                pipe.expire(error_key, TTL_48H)

            # Track by service if provided
            if service_id:
                service_key = f"metrics:service:{service_id}:{today}"
                pipe.incr(service_key)
                pipe.expire(service_key, TTL_48H)

            # Track latency samples for percentile calculation (sorted set)
            latency_samples_key = "metrics:latency:samples"
            timestamp = datetime.now().timestamp()
            pipe.zadd(latency_samples_key, {f"{timestamp}:{duration_ms}": timestamp})
            # Keep only last 1000 samples
            pipe.zremrangebyrank(latency_samples_key, 0, -1001)

            await pipe.execute()

        except Exception as e:
            logger.warning(f"Failed to record request metric: {e}")

    async def record_tokens(
        self,
        user_id: str,
        service_id: str,
        input_tokens: int,
        output_tokens: int,
        model: str = "default",
    ) -> None:
        """
        Record token usage

        Args:
            user_id: User ID
            service_id: Service/Assistant ID
            input_tokens: Number of input/prompt tokens
            output_tokens: Number of output/completion tokens
            model: Model name for cost estimation
        """
        if not self.redis or not self.redis._client:
            return

        try:
            today = self._get_date_str()

            # Calculate cost
            pricing = TOKEN_PRICING.get(model, TOKEN_PRICING["default"])
            cost = (input_tokens / 1000 * pricing["input"]) + (
                output_tokens / 1000 * pricing["output"]
            )
            cost_cents = int(cost * 100)  # Store in cents for integer precision

            pipe = self.redis._client.pipeline()

            # Global token counters
            input_key = f"metrics:tokens:input:{today}"
            output_key = f"metrics:tokens:output:{today}"
            cost_key = f"metrics:tokens:cost:{today}"

            pipe.incrby(input_key, input_tokens)
            pipe.expire(input_key, TTL_7D)
            pipe.incrby(output_key, output_tokens)
            pipe.expire(output_key, TTL_7D)
            pipe.incrby(cost_key, cost_cents)
            pipe.expire(cost_key, TTL_7D)

            # Per-user token usage
            if user_id:
                user_input_key = f"metrics:tokens:user:{user_id}:input:{today}"
                user_output_key = f"metrics:tokens:user:{user_id}:output:{today}"
                pipe.incrby(user_input_key, input_tokens)
                pipe.expire(user_input_key, TTL_7D)
                pipe.incrby(user_output_key, output_tokens)
                pipe.expire(user_output_key, TTL_7D)

            # Per-service token usage
            if service_id:
                svc_input_key = f"metrics:tokens:service:{service_id}:input:{today}"
                svc_output_key = f"metrics:tokens:service:{service_id}:output:{today}"
                pipe.incrby(svc_input_key, input_tokens)
                pipe.expire(svc_input_key, TTL_7D)
                pipe.incrby(svc_output_key, output_tokens)
                pipe.expire(svc_output_key, TTL_7D)

            await pipe.execute()

        except Exception as e:
            logger.warning(f"Failed to record token usage: {e}")

    async def record_run(
        self,
        user_id: str,
        assistant_id: str,
        duration_ms: float,
        status: str = "success",
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """
        Record a LangGraph run execution

        Args:
            user_id: User ID
            assistant_id: Assistant/Agent ID
            duration_ms: Run duration in milliseconds
            status: Run status (success, error, cancelled)
            input_tokens: Input tokens used
            output_tokens: Output tokens used
        """
        if not self.redis or not self.redis._client:
            return

        try:
            today = self._get_date_str()
            is_success = status == "success"

            pipe = self.redis._client.pipeline()

            # Total run count
            runs_total_key = f"metrics:runs:total:{today}"
            pipe.incr(runs_total_key)
            pipe.expire(runs_total_key, TTL_48H)

            # Success count
            if is_success:
                runs_success_key = f"metrics:runs:success:{today}"
                pipe.incr(runs_success_key)
                pipe.expire(runs_success_key, TTL_48H)

            # Run duration sum for average
            duration_key = f"metrics:runs:duration:sum:{today}"
            pipe.incrby(duration_key, int(duration_ms))
            pipe.expire(duration_key, TTL_48H)

            duration_count_key = f"metrics:runs:duration:count:{today}"
            pipe.incr(duration_count_key)
            pipe.expire(duration_count_key, TTL_48H)

            # Per-assistant run count
            if assistant_id:
                assistant_key = f"metrics:runs:assistant:{assistant_id}:{today}"
                pipe.incr(assistant_key)
                pipe.expire(assistant_key, TTL_48H)

            await pipe.execute()

            # Record tokens if provided
            if input_tokens > 0 or output_tokens > 0:
                await self.record_tokens(
                    user_id=user_id,
                    service_id=assistant_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )

        except Exception as e:
            logger.warning(f"Failed to record run metric: {e}")

    async def get_latency_percentiles(self) -> dict[str, int]:
        """
        Calculate latency percentiles from recent samples

        Returns:
            Dictionary with p50, p95, p99 values in milliseconds
        """
        if not self.redis or not self.redis._client:
            return {"p50": 0, "p95": 0, "p99": 0}

        try:
            samples_key = "metrics:latency:samples"
            # Get all samples (format: "timestamp:latency_ms")
            samples = await self.redis._client.zrange(samples_key, 0, -1)

            if not samples:
                return {"p50": 0, "p95": 0, "p99": 0}

            # Extract latency values
            latencies = []
            for sample in samples:
                try:
                    _, latency_str = sample.rsplit(":", 1)
                    latencies.append(float(latency_str))
                except (ValueError, AttributeError):
                    continue

            if not latencies:
                return {"p50": 0, "p95": 0, "p99": 0}

            latencies.sort()
            n = len(latencies)

            def percentile(p: float) -> int:
                idx = int(n * p / 100)
                idx = min(idx, n - 1)
                return int(latencies[idx])

            return {
                "p50": percentile(50),
                "p95": percentile(95),
                "p99": percentile(99),
            }

        except Exception as e:
            logger.warning(f"Failed to calculate latency percentiles: {e}")
            return {"p50": 0, "p95": 0, "p99": 0}

    async def get_today_summary(self) -> dict[str, Any]:
        """
        Get today's metrics summary

        Returns:
            Dictionary with aggregated metrics for today
        """
        if not self.redis or not self.redis._client:
            return self._empty_summary()

        try:
            today = self._get_date_str()
            client = self.redis._client

            # Fetch all metrics with pipeline
            pipe = client.pipeline()

            # Request metrics
            pipe.get(f"metrics:requests:total:{today}")
            pipe.get(f"metrics:requests:success:{today}")
            pipe.get(f"metrics:latency:sum:{today}")
            pipe.get(f"metrics:latency:count:{today}")

            # Token metrics
            pipe.get(f"metrics:tokens:input:{today}")
            pipe.get(f"metrics:tokens:output:{today}")
            pipe.get(f"metrics:tokens:cost:{today}")

            # Run metrics
            pipe.get(f"metrics:runs:total:{today}")
            pipe.get(f"metrics:runs:success:{today}")
            pipe.get(f"metrics:runs:duration:sum:{today}")
            pipe.get(f"metrics:runs:duration:count:{today}")

            # Hourly data
            for hour in range(24):
                pipe.get(f"metrics:requests:hour:{today}:{hour:02d}")

            results = await pipe.execute()

            # Parse results
            total_requests = int(results[0] or 0)
            success_count = int(results[1] or 0)
            latency_sum = int(results[2] or 0)
            latency_count = int(results[3] or 0)
            input_tokens = int(results[4] or 0)
            output_tokens = int(results[5] or 0)
            cost_cents = int(results[6] or 0)
            total_runs = int(results[7] or 0)
            runs_success = int(results[8] or 0)
            runs_duration_sum = int(results[9] or 0)
            runs_duration_count = int(results[10] or 0)

            # Parse hourly data (indices 11-34)
            hourly_data = []
            for i in range(24):
                count = int(results[11 + i] or 0)
                hourly_data.append({"hour": f"{i:02d}:00", "count": count})

            # Calculate derived metrics
            success_rate = (
                round(success_count / total_requests * 100, 1) if total_requests > 0 else 100.0
            )
            avg_latency = int(latency_sum / latency_count) if latency_count > 0 else 0
            run_success_rate = (
                round(runs_success / total_runs * 100, 1) if total_runs > 0 else 100.0
            )
            avg_run_duration = (
                int(runs_duration_sum / runs_duration_count) if runs_duration_count > 0 else 0
            )

            # Get percentiles
            percentiles = await self.get_latency_percentiles()

            return {
                "total_requests": total_requests,
                "success_rate": success_rate,
                "avg_latency_ms": avg_latency,
                "latency_p50": percentiles["p50"],
                "latency_p95": percentiles["p95"],
                "latency_p99": percentiles["p99"],
                "total_tokens": input_tokens + output_tokens,
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "estimated_cost_usd": round(cost_cents / 100, 4),
                "total_runs": total_runs,
                "run_success_rate": run_success_rate,
                "avg_run_duration_ms": avg_run_duration,
                "requests_by_hour": hourly_data,
            }

        except Exception as e:
            logger.warning(f"Failed to get today's summary: {e}")
            return self._empty_summary()

    def _empty_summary(self) -> dict[str, Any]:
        """Return an empty metrics summary"""
        return {
            "total_requests": 0,
            "success_rate": 100.0,
            "avg_latency_ms": 0,
            "latency_p50": 0,
            "latency_p95": 0,
            "latency_p99": 0,
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "estimated_cost_usd": 0.0,
            "total_runs": 0,
            "run_success_rate": 100.0,
            "avg_run_duration_ms": 0,
            "requests_by_hour": [{"hour": f"{i:02d}:00", "count": 0} for i in range(24)],
        }


def get_metrics_recorder() -> MetricsRecorder:
    """Get the global MetricsRecorder singleton"""
    global _metrics_recorder
    if _metrics_recorder is None:
        _metrics_recorder = MetricsRecorder()
    return _metrics_recorder


def init_metrics_recorder(redis: RedisStorage) -> MetricsRecorder:
    """Initialize the global MetricsRecorder with Redis storage"""
    global _metrics_recorder
    if _metrics_recorder is None:
        _metrics_recorder = MetricsRecorder(redis)
    else:
        _metrics_recorder.set_redis(redis)
    return _metrics_recorder
