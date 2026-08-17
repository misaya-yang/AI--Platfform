#!/usr/bin/env python3
"""Measure real Assistant first-event, first-thinking, and first-visible latency."""

from __future__ import annotations

import argparse
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from scripts.native_agent_parity_benchmark import (
    AIPlatformAdapter,
    BenchmarkError,
    _write_private_json,
)

ROOT = Path(__file__).resolve().parents[1]


def _percentile(values: list[float], percentile: float) -> float | None:
    """Return the nearest-rank percentile used by the release report."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return round(ordered[rank - 1], 6)


def _runtime_inputs(env_path: Path) -> dict[str, str]:
    file_env = dotenv_values(env_path) if env_path.exists() else {}

    def value(*names: str) -> str:
        for name in names:
            candidate = os.environ.get(name) or file_env.get(name)
            if isinstance(candidate, str) and candidate:
                return candidate
        return ""

    email = value("ASSISTANT_TTFT_EMAIL", "ASSISTANT_ISOLATION_EMAIL")
    password = value("ASSISTANT_TTFT_PASSWORD", "ASSISTANT_ISOLATION_PASSWORD")
    model_id = value("ASSISTANT_TTFT_MODEL", "ASSISTANT_ISOLATION_MODEL") or "qwen3.7-plus"
    if not email or not password:
        raise BenchmarkError("ttft_credentials_missing")
    return {"email": email, "password": password, "model_id": model_id}


def _metric_summary(trials: list[dict[str, Any]], key: str) -> dict[str, float | None]:
    values = [float(trial[key]) for trial in trials if isinstance(trial.get(key), (int, float))]
    return {
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "min": round(min(values), 6) if values else None,
        "max": round(max(values), 6) if values else None,
    }


def run_benchmark(
    *,
    env_path: Path,
    gateway_base_url: str,
    output_path: Path,
    prompt: str,
    trials: int,
    thinking_level: str,
    p50_ceiling_seconds: float,
) -> dict[str, Any]:
    if not 1 <= trials <= 100:
        raise BenchmarkError("ttft_trials_out_of_range")
    if thinking_level not in {"low", "medium", "high"}:
        raise BenchmarkError("ttft_thinking_must_be_enabled")
    runtime = _runtime_inputs(env_path)
    adapter = AIPlatformAdapter(
        gateway_base_url=gateway_base_url,
        email=runtime["email"],
        password=runtime["password"],
        model_id=runtime["model_id"],
        temperature=0.0,
        max_tokens=256,
        thinking_level=thinking_level,
        execution_profile="safe",
        max_approval_rounds=1,
    )
    prompt_sha256 = __import__("hashlib").sha256(prompt.encode()).hexdigest()
    results: list[dict[str, Any]] = []
    started_at = time.time()
    for ordinal in range(1, trials + 1):
        task_id = f"ttft.{ordinal}.{uuid.uuid4().hex}"
        try:
            adapter.start_task(task_id)
            result = adapter.run_turn(task_id, prompt)
            phase = result.metadata["timing"]["phases"][0]
            event_types = result.metadata["event_types"]
            thinking_observed = any(
                event_type in {"thinking_start", "thinking_delta"} for event_type in event_types
            )
            success = (
                result.terminal_status == "succeeded"
                and bool(result.text)
                and thinking_observed
            )
            usage = result.metadata.get("usage") or {}
            results.append(
                {
                    "ordinal": ordinal,
                    "success": success,
                    "terminal_status": result.terminal_status,
                    "thinking_observed": thinking_observed,
                    "first_event_seconds": phase.get("first_event_seconds"),
                    "first_thinking_seconds": phase.get("first_thinking_seconds"),
                    "ttft_seconds": result.metadata["timing"].get("ttft_seconds"),
                    "thinking_to_visible_seconds": phase.get("thinking_to_visible_seconds"),
                    "total_seconds": result.duration_seconds,
                    "input_tokens": usage.get("input_tokens"),
                    "cached_input_tokens": usage.get("cached_input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                }
            )
        except BenchmarkError as exc:
            results.append(
                {
                    "ordinal": ordinal,
                    "success": False,
                    "terminal_status": "infrastructure_error",
                    "reason": str(exc),
                }
            )
    successful = [trial for trial in results if trial.get("success") is True]
    metrics = {
        key: _metric_summary(successful, key)
        for key in (
            "first_event_seconds",
            "first_thinking_seconds",
            "ttft_seconds",
            "thinking_to_visible_seconds",
            "total_seconds",
        )
    }
    ttft_p50 = metrics["ttft_seconds"]["p50"]
    passed = len(successful) == trials and ttft_p50 is not None and ttft_p50 <= p50_ceiling_seconds
    summary = {
        "schema_version": "assistant-ttft-benchmark/v1",
        "model_id": runtime["model_id"],
        "thinking_level": thinking_level,
        "thinking_required": True,
        "execution_profile": "safe",
        "memory_mode": "off",
        "skills_enabled": False,
        "prompt_sha256": prompt_sha256,
        "prompt_chars": len(prompt),
        "trial_count": trials,
        "successful_trials": len(successful),
        "p50_ceiling_seconds": p50_ceiling_seconds,
        "passed": passed,
        "metrics": metrics,
        "trials": results,
        "wall_seconds": round(time.time() - started_at, 6),
    }
    _write_private_json(output_path, summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--gateway-base-url", default="http://127.0.0.1:8080/api/v1")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prompt", default="只回答数字：2+2等于多少？")
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--thinking-level", default="low")
    parser.add_argument("--p50-ceiling-seconds", type=float, default=3.41)
    args = parser.parse_args()
    try:
        summary = run_benchmark(
            env_path=args.env_file,
            gateway_base_url=args.gateway_base_url,
            output_path=args.output,
            prompt=args.prompt,
            trials=args.trials,
            thinking_level=args.thinking_level,
            p50_ceiling_seconds=args.p50_ceiling_seconds,
        )
    except (BenchmarkError, FileExistsError) as exc:
        print({"status": "infrastructure_error", "reason": str(exc)})
        return 2
    print(
        {
            "passed": summary["passed"],
            "successful_trials": summary["successful_trials"],
            "ttft": summary["metrics"]["ttft_seconds"],
        }
    )
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
