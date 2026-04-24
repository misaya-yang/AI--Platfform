# Phase 4 Deploy Lessons — uv workspaces vs. pip in Docker

**Date:** 2026-04-23
**Context:** End of Phase 4, Assistant Service True Isolation migration (`plans/Assistant-Service-True-Isolation-Plan.md`). The code landed clean locally, unit tests green, `uv sync` succeeded. Production deploy surfaced **three sequential `ModuleNotFoundError` regressions plus one `PermissionError`**, all from the same root cause: **pip does not understand uv workspace sources.**

If you are about to do a similar migration — splitting a service into workspace packages, moving modules between workspace members, or renaming a workspace package — **read this before you push**.

---

## What Phase 4 changed (relevant slice)

- `packages/ai-gateway-core/` became a real uv workspace member (ex-Phase-2 carve-out of `src/core/`).
- `apps/assistant-service/` was already a workspace member; Phase 4.8 rewrote gateway-side imports from `src.services.assistant.*` → `assistant_service.core.*`.
- Gateway `pyproject.toml` declares both as `[tool.uv.sources] { workspace = true }`.
- `uv sync` in dev: Just Works. Everything resolves via the uv workspace.
- `pip install .` in Docker: **Does not resolve workspace sources.** Pip sees `ai-gateway-core` as a distribution name and has nothing to match it against.

The Dockerfiles were written for the pre-Phase-2 monolith. They never got updated for workspace deps.

---

## The three regressions

### #1 — Gateway Dockerfile missing `ai-gateway-core` (commit `c3d1c01`)

- Crash: `ModuleNotFoundError: No module named 'ai_gateway_core'` at gateway container startup.
- Any `src/core/**` or `src/api/**` module that did `from ai_gateway_core...` imploded on import.
- Fix: `COPY packages/ ./packages/` + `RUN pip install --no-cache-dir ./packages/ai-gateway-core` **before** the top-level `pip install ".[all]"`.

### #2 — Assistant-service Dockerfile missing the same (commit `d6cfaf8`)

- Same class of crash, different container: the assistant-service image had never needed `ai_gateway_core` until Phase 4.1 aligned it to use the shared Protocols.
- Also missing: `apps/assistant-service/README.md` — pyproject referenced it as the long description, so `pip install` of the app package was failing during build if the file was absent.
- Fix: same `COPY packages/` + `pip install ./packages/ai-gateway-core`, plus ensure the README ships in the build context.

### #3 — Gateway Dockerfile missing `assistant-service` itself (commit `2b6c905`)

- After Phase 4.8 rewrote gateway imports to `assistant_service.core.*`, the `assistant_service` Python package has to be **physically installed** in the gateway's venv.
- The name similarity to the microservice container confuses things: this is not about running assistant-service, it's about the gateway importing its `core/` subtree directly (a temporary Phase-4 coupling; Phase 5 will remove it).
- Fix: `COPY apps/assistant-service/ ./apps/assistant-service/` + `RUN pip install --no-cache-dir ./apps/assistant-service`.

### Bonus — `log_dir` derived from `__file__` (commit `c0a2071`)

- `ai_gateway_core.logging._core.configure_structured_logging` defaulted `log_dir` to `Path(__file__).parent.parent.parent.parent / "logs"`.
- Pre-Phase-2 (`src/core/observability/logging.py`, 4 parents up from `src/`): resolved to `/app/logs` in the container. Writable. Fine.
- Post-Phase-2 (module now inside `site-packages/ai_gateway_core/logging/_core.py`): resolves to `/opt/venv/lib/python3.12/logs`. Read-only to the non-root container user. `PermissionError` at startup.
- Fix: default to `os.getenv("AI_GATEWAY_LOG_DIR") or Path.cwd() / "logs"`. Workspace-package modules must never derive writable paths from `__file__`.

---

## Root cause (one sentence)

**`[tool.uv.sources]` is a uv-only concept; `pip` can't satisfy workspace deps.** Every Dockerfile that uses `pip` must explicitly `pip install <path>` each workspace dep of its service before installing the service itself.

---

## The pattern (copy this into new Dockerfiles)

```dockerfile
# Install workspace deps FIRST so the top-level install sees them satisfied.
COPY packages/ ./packages/
RUN pip install --no-cache-dir ./packages/ai-gateway-core

# Apps that import another workspace member — e.g. gateway importing assistant_service.core.*
COPY apps/assistant-service/ ./apps/assistant-service/
RUN pip install --no-cache-dir ./apps/assistant-service

# Now the top-level install resolves cleanly.
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[all]"
```

Rule of thumb: **every workspace dep listed under `[tool.uv.sources]` needs a matching `pip install <path>` line in the Dockerfile.**

---

## How we should have caught this pre-push

These two commands would have caught #1 and #3 locally:

```bash
uv run python -c "from src.main import create_app; create_app()"
make test-isolation    # includes tests/integration/test_gateway_boot.py
```

`test_gateway_boot.py` exists specifically because pytest collection does not import `src/api/v1/*.py` — so a broken top-level import can pass unit tests and only crash at container boot. Regression #2 (assistant-service) needed an equivalent boot test in that service; add one if you touch its imports.

**Add to your pre-push checklist for any workspace-touching change:**

1. `uv run python -c "from src.main import create_app; create_app()"`
2. `make test-isolation`
3. `grep -rn "pip install" Dockerfile apps/*/Dockerfile packages/*/Dockerfile` — confirm every workspace dep in the service's `pyproject.toml` has a matching line.
4. If you added/renamed a workspace package, add it to the rebuild-matrix table in `reference_server_deployment.md`.

---

## Rebuild matrix impact

Both `packages/ai-gateway-core/**` and `apps/assistant-service/**` now trigger rebuilds of **both** the gateway and the assistant-service containers. Until Phase 5 drops the `assistant_service` bundling from the gateway image, this coupling is real and must be honored on every deploy.

See `reference_server_deployment.md` → "The three deploy blocks" → "Cross-block coupling" for the canonical table.

---

## Commits (chronological)

| SHA | Title |
|---|---|
| `c3d1c01` | fix(docker): install ai-gateway-core workspace pkg in gateway image |
| `d6cfaf8` | fix(docker): install ai-gateway-core workspace pkg in assistant-service image |
| `2b6c905` | fix(docker): install assistant-service workspace pkg in gateway image |
| `c0a2071` | fix(logging): log_dir no longer derived from __file__ |

---

## If you are doing a similar migration

1. Before writing any Dockerfile changes, list every workspace dep of every service. That list is your `pip install <path>` checklist.
2. Never derive writable paths from `__file__` in a module that might live in site-packages. Env var → CWD fallback.
3. Write a `test_<svc>_boot.py` that imports the service entrypoint. Run it in CI and pre-push. It is cheap and catches the exact failure mode this document is about.
4. After Phase N, grep for stale `src.services.<x>` imports. Phase 4.8 found them via `plans/phase4-remaining-src-imports.md`; do the same.
