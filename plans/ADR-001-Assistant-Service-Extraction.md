# ADR-001: Should We Extract AI Assistant Into a Separate Microservice?

**Status:** Proposed
**Date:** 2026-04-01
**Deciders:** Engineering Team Lead, AI Engineer

---

## Context

### Current State of the Gateway Monolith

The AI Gateway is a FastAPI monolith containing **140,887 lines of Python** and **67,891 lines of TypeScript/TSX** frontend code. Two modules dominate the backend:

```
Backend Service Line Distribution:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
assistant    ██████████████████████ 41,355 lines (87 files)  — 29.4%
knowledge    █████████████████████ 40,964 lines (53 files)  — 29.1%
persistence  ████████             ~15,000 lines             — 10.7%
api layer    ████████             17,222 lines              — 12.2%
metrics      ██                    3,299 lines              —  2.3%
billing      █                     2,222 lines              —  1.6%
storage      █                     2,500 lines              —  1.8%
others       ██                   ~18,325 lines             — 13.0%
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

The Knowledge Service has **already been split** into an independent microservice (port 8092), but its code still exists in the Gateway for in-process access. The Assistant module (41.4K lines) is now the single largest service in the monolith.

### Identified Problems

1. **Size & Cognitive Load**: assistant_service.py alone is 4,568 lines; agent_loop.py is 4,715 lines. Developers must understand the entire monolith to work on any part.

2. **Deployment Coupling**: A change to the Quiz feature (new) requires redeploying the entire Gateway, including proxy, auth, billing, and all other unrelated modules.

3. **Resource Contention**: LLM streaming sessions (SSE), agent loops, and code execution compete with lightweight API proxy requests for the same process pool.

4. **Scaling Mismatch**: The assistant (long-running, memory-heavy, GPU/LLM-bound) has fundamentally different scaling needs vs. the proxy/routing layer (short-lived, CPU-light, high-throughput).

5. **Growing Feature Surface**: New features like Quiz, Task Planning, Memory Service, Tool Orchestration, and Code Execution are all accumulating inside the assistant module with no sign of slowing.

### Existing Architecture

```
                    ┌──────────────────────────────────┐
                    │        Docker Compose             │
                    │                                   │
  ┌─────────────────┤  ai-gateway (port 8080)          │
  │                 │  ├─ API Proxy/Router              │
  │                 │  ├─ Auth & Billing                │
  │                 │  ├─ Assistant Service (41K LOC) ◄─┼── THE PROBLEM
  │                 │  ├─ Knowledge (in-process copy)   │
  │                 │  ├─ Metrics                       │
  │                 │  ├─ Session                       │
  │                 │  └─ Task Management               │
  │                 │                                   │
  │                 │  kb-service (port 8092)           │
  │                 │  └─ Knowledge CRUD & Retrieval    │
  │                 │                                   │
  │                 │  islamic-service (port 8091)      │
  │                 │  └─ Quran/Dua/Hadith              │
  │                 └──────────────────────────────────┘
  │
  ▼
┌──────┐  ┌───────┐  ┌────────┐
│ PG   │  │ Redis │  │ Qdrant │
└──────┘  └───────┘  └────────┘
```

### Assistant Module Dependency Map

```
assistant_service.py Constructor (17 parameters!):
───────────────────────────────────────────────
  model_registry        ← Required (internal)
  kb_service            ← Optional (KnowledgeService)
  tavily_api_key        ← Optional (external API)
  session_manager       ← Optional (DatabaseSessionManager)
  context_config        ← Optional (config)
  code_executor         ← Optional (CodeExecutorService)
  task_planner          ← Optional (TaskPlanner)
  tool_orchestrator     ← Optional (ToolOrchestrator)
  db                    ← Optional (DatabaseStorage)
  vlm_service           ← Optional (VLM)
  redis_client          ← Optional (cache)
  memory_service        ← Optional (MemoryService)
  quality_guardrails    ← Optional
  tool_constraint_validator ← Optional
  execution_gateway     ← Optional
  request_router        ← Optional
```

Key observation: **16 of 17 parameters are Optional**. This is characteristic of a God Object — it can do everything, depends on everything, but nothing is strictly required.

---

## Decision

**Recommended: Option B — Progressive Modular Extraction (Strangler Fig Pattern)**

Do NOT do a big-bang microservice split. Instead, progressively extract the assistant into a deployable module over 3 phases, using the Strangler Fig pattern already proven by the KB service extraction.

---

## Options Considered

### Option A: Keep as Monolith (Refactor Internally)

| Dimension | Assessment |
|-----------|------------|
| Complexity | **Low** — No new infra, no new deployment targets |
| Cost | **Low** — Zero migration cost |
| Scalability | **Poor** — Cannot scale assistant independently |
| Team familiarity | **High** — Current working model |
| Risk | **Low** — No breaking changes |

**What to do:** Split the 41K-line assistant module into internal sub-packages with clear interfaces, but keep it in the same process.

```
src/services/assistant/
├── core/                    # AssistantService (trimmed)
│   ├── assistant_service.py
│   └── config.py
├── agent/                   # Agent loop & execution
│   ├── agent_loop.py
│   ├── react_executor.py
│   └── error_recovery.py
├── rag/                     # RAG-specific logic
│   ├── context_engine.py
│   ├── scenario_analyzer.py
│   └── rag_metrics.py
├── tools/                   # Tool system
│   ├── tool_invoker.py
│   ├── tool_orchestrator.py
│   └── tools/
├── memory/                  # Memory subsystem
├── tasks/                   # Task planning
├── quiz/                    # New quiz feature
└── content/                 # Content generation
```

**Pros:**
- Zero operational complexity increase
- Immediately actionable
- Improves code organization without risk

**Cons:**
- Does not solve deployment coupling
- Does not solve resource contention
- Does not enable independent scaling
- 41K lines still in one process; cognitive load remains high at deploy boundary

---

### Option B: Progressive Modular Extraction (Strangler Fig) ⭐ RECOMMENDED

| Dimension | Assessment |
|-----------|------------|
| Complexity | **Medium** — Phased approach reduces risk |
| Cost | **Medium** — Incremental investment spread over weeks |
| Scalability | **Good** — Assistant scales independently |
| Team familiarity | **Medium** — Same pattern as KB extraction |
| Risk | **Medium** — Each phase is independently rollback-able |

**Strategy:** Apply the same pattern used for KB service extraction. Keep in-process code working while building an HTTP boundary, then flip traffic.

**Phase 1 — Internal Refactoring (Week 1-2):**
Same as Option A. Clean up internal structure, define clear interfaces.

**Phase 2 — Interface Abstraction (Week 2-3):**
Create an `AssistantClient` abstraction that can call either in-process or remote:

```python
# src/services/assistant/client.py
class AssistantClientProtocol(Protocol):
    async def chat(self, request: ChatRequest, user: UserContext) -> ChatResponse: ...
    async def chat_stream(self, request: ChatRequest, user: UserContext) -> AsyncIterator: ...
    async def generate_quiz(self, request: QuizRequest, user: UserContext) -> QuizResponse: ...

class InProcessAssistantClient:
    """Current behavior — direct function call"""
    def __init__(self, service: AssistantService):
        self.service = service

    async def chat(self, request, user):
        return await self.service.chat(request, user)

class RemoteAssistantClient:
    """Future — HTTP call to assistant microservice"""
    def __init__(self, base_url: str):
        self.base_url = base_url

    async def chat(self, request, user):
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/chat", ...)
            return ChatResponse(**resp.json())
```

**Phase 3 — Extract & Deploy (Week 3-5):**

```
┌────────────────────────────────────────────────────────┐
│                  Docker Compose (After)                 │
│                                                        │
│  ai-gateway (port 8080) — SLIM                         │
│  ├─ API Proxy/Router                                   │
│  ├─ Auth & Billing                                     │
│  ├─ Metrics                                            │
│  ├─ AssistantClient (HTTP → assistant-service)         │
│  └─ KnowledgeClient (HTTP → kb-service)                │
│                                                        │
│  assistant-service (port 8093) — NEW                   │
│  ├─ Chat / Streaming                                   │
│  ├─ Agent Loop & Tool Execution                        │
│  ├─ Quiz System                                        │
│  ├─ Memory Service                                     │
│  ├─ Task Planning                                      │
│  └─ Content Generation                                 │
│                                                        │
│  kb-service (port 8092) — EXISTING                     │
│  └─ Knowledge CRUD & Retrieval                         │
│                                                        │
│  islamic-service (port 8091) — EXISTING                │
│  └─ Quran/Dua/Hadith                                   │
└────────────────────────────────────────────────────────┘
```

**Pros:**
- Each phase delivers standalone value
- Gateway drops from ~140K to ~60K lines (assistant + knowledge removed)
- Assistant can scale horizontally for LLM workloads
- New features (Quiz, etc.) only redeploy assistant-service
- Proven pattern (KB service was already extracted this way)
- Phase 1 alone improves maintainability even if you stop there

**Cons:**
- Network latency added for chat calls (~1-5ms, negligible vs LLM latency of seconds)
- SSE streaming requires proxy passthrough (solvable, Gateway already does this for KB)
- Shared database initially (same PG); full DB split is Phase 4+ if ever needed
- More containers to manage in deployment

---

### Option C: Full Microservice Decomposition

| Dimension | Assessment |
|-----------|------------|
| Complexity | **Very High** — Multiple new services, service mesh, distributed tracing |
| Cost | **High** — Weeks of pure infra work before any feature value |
| Scalability | **Excellent** — Each component scales independently |
| Team familiarity | **Low** — Significant paradigm shift |
| Risk | **High** — Big-bang migration, many moving parts |

**What to do:** Split everything: Agent Service, Memory Service, Tool Execution Service, Quiz Service, RAG Service, Session Service — each as independent microservices.

```
gateway → agent-service → tool-service
                        → memory-service
                        → quiz-service
                        → rag-service → kb-service
```

**Pros:**
- Maximum flexibility and independent scaling
- Clean separation of concerns
- Each team can own a service

**Cons:**
- Massive operational complexity for a small team
- Distributed transactions are hard (agent loop calls tools, memory, RAG in sequence)
- Network latency compounds across hops
- Service mesh / observability overhead
- Premature for current team size and load
- 6-8 weeks minimum before any user-facing value

---

## Trade-off Analysis

| Factor | Option A (Monolith) | Option B (Strangler Fig) ⭐ | Option C (Full Split) |
|--------|--------------------|-----------------------------|----------------------|
| Time to value | Immediate | 1-2 weeks (Phase 1) | 6-8 weeks |
| Deployment independence | ❌ None | ✅ Assistant independent | ✅ Everything independent |
| Scaling flexibility | ❌ Uniform only | ✅ Assistant scales separately | ✅ Fine-grained |
| Operational complexity | Low | Medium (+1 service) | Very High (+5 services) |
| Risk | None | Low (phased, rollback-able) | High (big-bang) |
| Gateway size after | 140K lines | ~60K lines | ~30K lines |
| Team size needed | Current | Current | Need dedicated DevOps |
| SSE/Streaming impact | None | Low (proxy passthrough) | Complex (multi-hop) |

### Why Option B wins:

1. **Proven pattern**: You already did this with KB service. The team knows how.
2. **Right-sized risk**: One new service, not five. One network boundary, not a mesh.
3. **Biggest bang for effort**: Removing 41K lines (29%) from the gateway is the single highest-impact extraction you can do.
4. **Phase 1 is free**: Internal refactoring has zero downside and can start immediately.
5. **The 17-parameter constructor is screaming**: That God Object needs to die. Option B gives it a new home; Option A just rearranges the furniture.

---

## Consequences

### What becomes easier:
- Deploying assistant features (Quiz, Memory, new tools) without touching Gateway
- Scaling LLM workload independently (add more assistant-service replicas)
- Onboarding new engineers (smaller codebases to understand)
- Gateway becomes a thin routing/auth layer (~60K lines)
- Testing assistant in isolation

### What becomes harder:
- Local development needs docker-compose with 4+ services
- Debugging cross-service SSE streaming issues
- Shared database migrations need coordination
- End-to-end testing requires all services running

### What we'll need to revisit:
- Whether to split the shared PostgreSQL database (Phase 4+, not urgent)
- Whether Memory Service should become its own microservice (monitor growth)
- Event-driven architecture if services need async communication (currently all sync HTTP)

---

## Action Items

### Phase 1 — Internal Refactoring (Week 1-2)
1. [ ] Reorganize `src/services/assistant/` into sub-packages (core, agent, rag, tools, memory, tasks, quiz, content)
2. [ ] Break `assistant_service.py` (4,568 lines) into focused modules with explicit interfaces
3. [ ] Break `agent_loop.py` (4,715 lines) into pipeline stages
4. [ ] Define `AssistantClientProtocol` interface
5. [ ] Add integration tests for each sub-package boundary

### Phase 2 — Interface Abstraction (Week 2-3)
6. [ ] Implement `InProcessAssistantClient` wrapping current behavior
7. [ ] Swap API layer to use `AssistantClientProtocol` instead of direct service access
8. [ ] Verify zero behavior change with existing tests
9. [ ] Create `assistant-service/` directory with its own Dockerfile, requirements, and FastAPI app

### Phase 3 — Extract & Deploy (Week 3-5)
10. [ ] Implement `RemoteAssistantClient` with HTTP + SSE proxy
11. [ ] Add assistant-service to docker-compose.yml (port 8093)
12. [ ] Feature flag: `ASSISTANT_MODE=in_process|remote` for gradual rollout
13. [ ] Run shadow traffic (both paths) in staging, compare results
14. [ ] Flip to remote, monitor latency & error rates
15. [ ] Remove in-process assistant code from Gateway

### Phase 4 — Future (When Needed)
16. [ ] Evaluate dedicated database for assistant-service
17. [ ] Evaluate Memory Service extraction if it grows beyond 5K lines
18. [ ] Evaluate event bus (Redis Streams / NATS) for async tool execution
