"""Generate the Harness Engineering training deck via LLM planner.

Runs the full docgen pipeline with a real LLM (DeepSeek, OpenAI-compatible
via ``LLM_BASE_URL`` / ``LLM_API_KEY`` / ``LLM_MODEL``). The planner
decides slide count, layouts, narrative flow; this script only hands it
the subject-matter brief.

Usage:
    python3 scripts/docgen_harness_ppt.py

Writes to ``DOCGEN_HARNESS_OUT_DIR`` or ``tmp/docgen/harness``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from src.services.assistant.docgen.planners import Brief  # noqa: E402
from src.services.assistant.docgen.pipeline import DocgenPipeline  # noqa: E402


OUT_DIR = Path(os.getenv("DOCGEN_HARNESS_OUT_DIR", REPO / "tmp/docgen/harness"))

BRIEF_BODY = """
# 2025 was Agents. 2026 is Agent Harnesses.
Model is the engine; Harness is the car.

# What is an Agent Harness?
The harness is everything around the model that turns a demo into production:
context engineering, tool orchestration, guardrails, observation, state memory,
modularity. Without it, intelligence is just a demo.

# Why now?
Stripe ships 1300 AI PRs per week. It is not because the model got smarter —
it is because the harness got better. Manus rewrote their harness five times
in six months on the same models and reliability kept climbing each time.

# Layer 1: Context Engineering
Three-tier memory (working / session / long-term), stable prefix design
(Manus pattern: system prompt first, current query last, maximizing KV-cache
hit rate), isolation / reduction / retrieval / compaction techniques, token
budget per layer, differentiated init vs continuation prompts.

# Layer 2: Tool Orchestration
Vercel insight: removing 80% of tools produced better results — fewer steps,
fewer tokens, faster, higher success rate. Parallel execution with dependency
coordination. Unified tool abstraction via registry and invocation context.

# Layer 3: Guardrails
Input (schema, PII, blocklists, jailbreak detection under 10ms), process
(confidence gating, scope enforcement), output (format validation, citation
verification, banned phrases), procedure enforcement for irreversible actions.
Guardrails belong in harness, not in the prompt — independently updatable.

# Layer 4: Observation & Verification
Retrieval metrics, token cost per user and model, cache hit rates,
LLM-as-judge for async quality scoring, tracing pipeline writing per-turn
records to a database for data-driven optimization. You cannot improve what
you cannot measure.

# Layer 5: State & Memory Management
Session persistence via checkpointer (Postgres / SQLite / memory), cross-session
user profiles, working memory with the todo.md pattern, attention manipulation
through recitation (Manus re-injects working memory as markdown to refocus
the agent mid-task).

# Layer 6: Modularity & Lifecycle
Component toggleability via env vars, independent deployment of prompts / tools
/ guardrails, multi-model support with fallback chains, streaming-first
architecture from proxy to adapter to agent to client, sandbox isolation for
file and code operations.

# Maturity Model
Level 1 Prompt-only. Level 2 Prompt plus Tools. Level 3 Basic Harness with
guardrails and context management. Level 4 Production Harness with full
six-layer plus metrics plus error recovery. Level 5 Self-improving — the
harness tunes its own prompts, retrieval, and routing via feedback loops.

# Where Hejaz stands today
Current assessment: Level 4 Production. 53 out of 60 across the six layers.
Gaps to Level 5: self-built tracing pipeline, semantic response quality
scoring, differentiated init vs continuation prompts.

# Anti-patterns to avoid
Baking all behavioural control into the prompt (fragile, non-updatable).
Too many tools (combinatorial explosion, LLM decision fatigue). Skipping
output validation. No observation layer. Monolithic agent preventing A-B
testing of components.

# Closing
The model is the engine. The harness is the car. Neither wins a race alone.

# References
Martin Fowler on harness engineering. Anthropic on effective harnesses for
long-running agents. OpenAI Codex on harness engineering. Parallel.ai on
what is an agent harness. Aakash Gupta coined the 2026 framing.
""".strip()


class DeepSeekLLM:
    """Minimal LLMCaller implementation against the OpenAI-compatible endpoint.

    Uses ``response_format: {type: "json_object"}`` which DeepSeek supports.
    Retries once on transient errors; repairs common JSON-in-text wrapping.
    """

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float = 180.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    async def generate_json(self, *, system: str, user: str, max_tokens: int = 4000) -> dict:
        """Call the LLM with up to 3 attempts on transient network errors.

        DeepSeek / other OpenAI-compatible endpoints occasionally close
        long responses mid-stream (``RemoteProtocolError``) or return 5xx
        under load. Retry with exponential backoff keeps the production
        happy path free from noisy fallbacks.
        """
        import asyncio
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.4,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(f"{self._base_url}/chat/completions", headers=headers, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                text = data["choices"][0]["message"]["content"]
                return _parse_json(text)
            except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < 2:
                    backoff = 2 ** attempt  # 1s, 2s
                    print(f"LLM call attempt {attempt+1} failed ({type(exc).__name__}: {exc}); retrying in {backoff}s", file=sys.stderr)
                    await asyncio.sleep(backoff)
                    continue
                raise
        raise last_exc if last_exc else RuntimeError("unreachable")


def _parse_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Some models still wrap with ```json ... ``` even in JSON mode
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"LLM did not return JSON: {text[:200]}")


def _load_env(path: Path) -> None:
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> int:
    env_path = Path(os.getenv("DOCGEN_ENV_FILE", REPO / ".env"))
    if not env_path.exists():
        print(f"Missing env file: {env_path}", file=sys.stderr)
        return 1
    _load_env(env_path)
    base = os.environ.get("LLM_BASE_URL")
    key = os.environ.get("LLM_API_KEY")
    model = os.environ.get("LLM_MODEL")
    if not (base and key and model):
        print("Missing LLM_BASE_URL / LLM_API_KEY / LLM_MODEL in .env", file=sys.stderr)
        return 1
    print(f"LLM: model={model}  base={base}")

    llm = DeepSeekLLM(base_url=base, api_key=key, model=model)

    brief = Brief(
        doc_type="pptx",
        title="Agent Harness Engineering — 2026 Internal Training",
        goal=(
            "30-minute internal training deck for Hejaz engineers and product. "
            "Audience knows LLM / RAG / Agents at a working level but is new to "
            "the 2026 'harness' framing. Deck must be professional, technical, "
            "Stripe / Anthropic engineering-deck tone. 12–15 slides. Bilingual: "
            "English section titles, Chinese body bullets. Include at least one "
            "comparison table. Cite sources (Martin Fowler / Anthropic / OpenAI "
            "Codex / Stripe / Vercel / Manus)."
        ),
        body_markdown=BRIEF_BODY,
        palette_name="carbon",
        font_pair_name="helvetica-helvetica",
        style_hints={"design_system": "claude", "length": "medium"},
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pipeline = DocgenPipeline(llm=llm, critic=None, verify=False)

    tmp = OUT_DIR / ".harness_build"
    tmp.mkdir(parents=True, exist_ok=True)
    print("Planning + rendering (LLM path)…")
    result = await pipeline.run(brief, tmp)

    final = OUT_DIR / "Harness_Engineering_Training.pptx"
    if final.exists():
        final.unlink()
    shutil.copy2(result.path, final)
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n✓ Wrote {final}")
    print(f"  bytes      : {result.bytes_size:,}")
    print(f"  plan (ms)  : {result.plan_ms}")
    print(f"  render (ms): {result.render_ms}")
    print(f"  llm used   : {result.used_llm}")
    print(f"\nOutline:")
    for line in result.plan_text.splitlines():
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
