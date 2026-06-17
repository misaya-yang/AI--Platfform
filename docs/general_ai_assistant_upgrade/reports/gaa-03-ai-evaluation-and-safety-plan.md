# GAA-03 AI Evaluation and Safety Plan

**Phase:** GAA-03 AI Evaluation and Safety

**Feature:** GAA-F004

**Date:** 2026-06-16

## Selected Slice

Add deterministic safety/eval contract coverage for assistant runtime boundaries that do not require live providers:

- Untrusted memory snippets cannot break context fences or inject control characters.
- Sensitive user data is redacted before runtime memory persistence.
- Oversized tool output is capped with neutral non-instructional truncation metadata.
- KB tool results keep a bounded, ranked model-facing summary instead of unbounded raw payload.

## Planned Edits

| File | Intended Change |
| --- | --- |
| `tests/services/assistant/test_eval_safety_contracts.py` | New deterministic contract tests for GAA-F004 safety/eval behavior. |
| `docs/general_ai_assistant_upgrade/**` | Record GAA-03 evidence, oracle status, source facts, continuity decisions, and handoff notes. |

## Validation Plan

| Gate | Command |
| --- | --- |
| Targeted eval slice | `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_eval_safety_contracts.py` |
| Agent loop golden | `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_agent_loop_golden.py` |
| Assistant safety targets | `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_guardrails.py tests/services/assistant/test_safe_fetch.py tests/services/assistant/test_tool_orchestrator.py` |
| Assistant memory targets | `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_memory_manager.py tests/services/assistant/test_working_memory.py` |
| Touched-file ruff | `uv run ruff check tests/services/assistant/test_eval_safety_contracts.py` |
| Harness validator | `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_upgrade --strict --quality-score` |

## Baseline Notes

- Required GAA-03 pytest groups currently pass: 5 + 110 + 96 tests.
- Broad ruff over all assistant-service tests has pre-existing lint blockers in existing files; this phase will use touched-file ruff unless production code changes are required.
- No live provider calls, PII fixtures, production data, migrations, or deployments are needed.
