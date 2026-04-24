# Skills System Design — AI Assistant Skill Framework

> **Version**: 1.0
> **Date**: 2026-04-01
> **Target**: AI Assistant Module (`assistant-service`)

---

## 1. Current State Analysis

### 1.1 What We Already Have (OpenClaw)

The codebase already has a **partially built** skill system under `openclaw/skills/`:

```
✅ SkillManifest 数据模型 (name, title, description, entrypoint, permissions, tags)
✅ SkillRegistry (注册, 查询, token-overlap 匹配, DB 持久化)
✅ SkillBuilder (提案→审批工作流, 危险权限校验)
✅ SkillSelection 评分机制 (token overlap scoring)
✅ Agent Loop 集成 (Step 5 注入 system prompt)
✅ Context Cost Breakdown 中的 skills 成本追踪
✅ Feature Flag 控制 (ASSISTANT_OPENCLAW_SKILLS)
✅ 数据库表 (assistant_skills, assistant_skill_versions)
```

### 1.2 What's Missing

```
❌ Skill 执行引擎 — SkillManifest 有 entrypoint 字段但没有 executor
❌ Skill 作为 Tool 注册 — LLM 只在 prompt 里 "看到" skill metadata, 无法 function_call 调用
❌ SKILL.md 解析器 — 不支持 Claude Code 风格的 YAML frontmatter + markdown 格式
❌ 第三方 Skill 上传/安装 — 只有 DB 持久化, 没有 upload/install 入口
❌ Skill 创建器 (skill_create) — SkillBuilder 只做审批, 没有 AI 辅助创建
❌ Skill Marketplace — 没有发现/搜索/安装界面
❌ Progressive Disclosure — 当前全量注入 prompt, 没有按需加载
❌ Skill 运行时沙箱 — SandboxResolver 存在但未接入 skill 执行
❌ Skill 版本管理 — 表存在但无 API
```

### 1.3 Reference: Claude Code Skill Architecture

Claude Code 的 SKILL.md 采用 **progressive disclosure** 架构:

```
Level 1: Metadata (~100 tokens) — 扫描所有 skill 的 name + description
Level 2: Full Instructions (<5K tokens) — 匹配后加载 SKILL.md 全文
Level 3: Bundled Resources (on demand) — 需要时才加载关联文件
```

Skill 通过 meta-tool `Skill` 分发调用，注入 instructions 到 conversation context，动态修改 execution environment（allowed tools, model selection）。

---

## 2. Design Goals

1. **内置 Skills + 第三方 Skills 共存**: 系统自带 `skill_create`, `quiz`, `document_gen` 等内置 skill; 用户可上传/安装第三方 SKILL.md
2. **Skills 作为一等公民**: Skill 不只是 prompt 注入, 而是可被 LLM 通过 function_call/tool_use 直接调用的 Tool
3. **Progressive Disclosure**: 低 token 成本 — metadata 常驻, instructions 按需加载
4. **Context Engine 协同优化**: Skill 上下文纳入 ContextBudgetManager 的 token 分配
5. **安全隔离**: 第三方 skill 受 permission 和 sandbox 控制

---

## 3. Skill Specification Format

### 3.1 SKILL.md 标准格式

采用 Claude Code 兼容的 YAML frontmatter + Markdown 格式:

```markdown
---
name: sales-quiz-generator
title: Sales Quiz Generator
description: Generate quiz questions from sales knowledge base to test team knowledge
version: 1.0.0
tags: [quiz, sales, training, assessment]
permissions:
  - kb:read
  - llm:generate
trigger:
  patterns:
    - "出.*题"
    - "quiz"
    - "测验"
    - "test my knowledge"
  auto: true           # Auto-trigger when patterns match (vs explicit /command only)
config:
  max_questions: 10
  default_difficulty: medium
  supported_types: [mc_single, mc_multi, true_false, short_answer]
---

# Sales Quiz Generator

## When to Use
Use this skill when users want to test their knowledge about sales-related topics
from the knowledge base.

## Instructions
1. Ask the user which KB dataset to use (or auto-detect from context)
2. Generate questions using RAG retrieval from the specified dataset
3. Present questions one at a time with interactive feedback
4. Show final score with per-question explanations

## Output Format
Return a structured quiz object with questions, options, and correct answers.

## Examples
User: "从销售知识库出5道选择题"
Action: Generate 5 MC questions from sales KB dataset

User: "Quiz me on product pricing"
Action: Generate mixed questions about pricing from relevant KB
```

### 3.2 Skill Manifest (Extended)

```python
@dataclass
class SkillManifest:
    # === Existing Fields ===
    name: str                           # Unique ID, also /slash-command
    title: str                          # Display name
    description: str                    # Brief description (~100 tokens for L1)
    entrypoint: str                     # "builtin://quiz_generator" | "db://skill_name" | "md://path"
    version: str = "1.0.0"
    tags: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    enabled: bool = True

    # === New Fields ===
    summary: str = ""                   # Short one-liner for prompt injection
    instructions: str = ""              # Full markdown instructions (L2, loaded on demand)
    trigger: TriggerConfig | None = None  # Auto-trigger patterns
    config: dict = field(default_factory=dict)  # Skill-specific configuration
    author: str = ""                    # Creator info
    source: SkillSource = SkillSource.BUILTIN  # builtin | user | marketplace
    tool_schema: dict | None = None     # JSON Schema for function calling
    bundled_files: list[str] = field(default_factory=list)  # Associated resource files
    max_context_tokens: int = 2000      # Token budget for instructions


@dataclass
class TriggerConfig:
    patterns: list[str] = field(default_factory=list)  # Regex patterns
    auto: bool = False                  # Auto-trigger vs explicit only


class SkillSource(str, Enum):
    BUILTIN = "builtin"     # System-provided skills
    USER = "user"           # User-created skills
    MARKETPLACE = "marketplace"  # Third-party installed
```

---

## 4. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Skill System Architecture                    │
│                                                                     │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────────┐  │
│  │ Builtin     │  │ User Upload  │  │ Marketplace (future)      │  │
│  │ Skills      │  │ SKILL.md     │  │ Install from registry     │  │
│  └──────┬──────┘  └──────┬───────┘  └───────────┬───────────────┘  │
│         │                │                       │                  │
│         ▼                ▼                       ▼                  │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                    SkillRegistry (Enhanced)                  │   │
│  │  ┌──────────┐  ┌───────────┐  ┌────────────┐  ┌─────────┐  │   │
│  │  │ L1 Index │  │ Matcher   │  │ Loader     │  │ Persist │  │   │
│  │  │ metadata │  │ pattern + │  │ on-demand  │  │ DB/file │  │   │
│  │  │ ~100 tok │  │ semantic  │  │ L2 instrs  │  │         │  │   │
│  │  └──────────┘  └───────────┘  └────────────┘  └─────────┘  │   │
│  └──────────────────────────┬───────────────────────────────────┘   │
│                             │                                       │
│         ┌───────────────────┼────────────────────┐                  │
│         ▼                   ▼                    ▼                  │
│  ┌─────────────┐  ┌─────────────────┐  ┌──────────────────┐       │
│  │ Tool Bridge │  │ Context Engine  │  │ Skill Executor   │       │
│  │ register as │  │ inject instrs   │  │ run entrypoint   │       │
│  │ function_   │  │ into prompt     │  │ in sandbox       │       │
│  │ call tool   │  │ (budget-aware)  │  │                  │       │
│  └──────┬──────┘  └────────┬────────┘  └────────┬─────────┘       │
│         │                  │                     │                  │
│         ▼                  ▼                     ▼                  │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                      Agent Loop (Step 5-6)                   │   │
│  │  Context Building → Tool Registration → ReAct Execution      │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 5. Three-Layer Skill Loading (Progressive Disclosure)

```
┌──────────────────────────────────────────────────────────────────┐
│                  Token Budget Allocation                         │
│                                                                  │
│  L1: Metadata Index     [Always Loaded]    ~50-200 tokens total  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ Available Skills:                                          │  │
│  │ - sales-quiz@1.0: Quiz from sales KB                      │  │
│  │ - doc-generator@2.1: Generate Word/PDF documents          │  │
│  │ - data-analyzer@1.3: Analyze CSV/Excel data               │  │
│  └────────────────────────────────────────────────────────────┘  │
│                          │                                       │
│                    Pattern Match / LLM Select                    │
│                          │                                       │
│  L2: Full Instructions   [On-Demand]       ~500-2000 tokens     │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ # Sales Quiz Generator                                     │  │
│  │ ## When to Use                                             │  │
│  │ Use when users want to test knowledge from sales KB...     │  │
│  │ ## Instructions                                            │  │
│  │ 1. Retrieve relevant KB chunks...                          │  │
│  │ 2. Generate questions with structured output...            │  │
│  └────────────────────────────────────────────────────────────┘  │
│                          │                                       │
│                    Executor Needs More Detail                    │
│                          │                                       │
│  L3: Bundled Resources   [Rare, On-Demand]  variable            │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ prompt_templates/quiz_gen.md                               │  │
│  │ examples/sample_quiz.json                                  │  │
│  │ schemas/question_schema.json                               │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

### Integration with ContextBudgetManager

```python
# Current budget allocation:
# system: 35%, memory: 15%, history: 20%, request: 30%

# New allocation with skills:
# system: 30%, memory: 12%, skills: 8%, history: 20%, request: 30%
#
# Skills budget breakdown:
#   L1 metadata: ~200 tokens (always)
#   L2 instructions: ~2000 tokens (per activated skill, max 2 active)
#   L3 resources: from request budget (on demand)
```

---

## 6. Skill as Tool (Function Calling Bridge)

The key architectural shift: **Skills become callable tools**, not just prompt injections.

### 6.1 Tool Bridge

```python
class SkillToolBridge:
    """Register skills as function-callable tools in the ToolRegistry."""

    def __init__(self, skill_registry: SkillRegistry, tool_registry: ToolRegistry):
        self.skills = skill_registry
        self.tools = tool_registry

    def register_skill_as_tool(self, skill: SkillManifest):
        """Convert a skill into a callable tool definition."""

        # Auto-generate tool schema if not provided
        schema = skill.tool_schema or self._generate_schema(skill)

        definition = ToolDefinition(
            name=f"skill_{skill.name}",
            description=f"[Skill] {skill.description}",
            parameters=self._schema_to_params(schema),
            category=ToolCategory.SKILL,           # New category
            risk_level=self._assess_risk(skill.permissions),
            requires_confirmation=("write" in str(skill.permissions)),
            when_to_use=f"When user wants: {skill.summary}",
            when_not_to_use="When the request doesn't match this skill's purpose",
            examples=[],
            timeout_seconds=60,
            max_retries=1,
            is_async=True,
            required_permissions=skill.permissions,
        )

        executor = SkillToolExecutor(skill, self.skills)
        self.tools.register(definition, executor)

    def sync_all_skills(self):
        """Register all enabled skills as tools."""
        for skill in self.skills.list(enabled_only=True):
            self.register_skill_as_tool(skill)


class SkillToolExecutor:
    """Execute a skill when called as a tool by the LLM."""

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        skill = self.skill

        if skill.entrypoint.startswith("builtin://"):
            # Dispatch to built-in skill handler
            handler = BUILTIN_SKILL_HANDLERS.get(skill.name)
            return await handler(request)

        elif skill.entrypoint.startswith("db://") or skill.entrypoint.startswith("md://"):
            # Load instructions and inject into next LLM turn
            instructions = await self._load_instructions(skill)
            return ToolCallResult(
                success=True,
                result=instructions,
                result_type="skill_instructions",  # Special type: inject into context
            )
```

### 6.2 LLM Sees Skills as Tools

Before (current — prompt-only):
```
System: ... ## Available Skills Metadata
- sales-quiz@1.0: Quiz from sales KB

User: 出5道销售测验题
Assistant: (must figure out what to do on its own)
```

After (tool bridge):
```
System: ...
Tools: [
  {"name": "skill_sales_quiz", "description": "[Skill] Generate quiz from sales KB",
   "parameters": {"dataset_id": "string", "count": "integer", "topic": "string"}},
  {"name": "kb_search", ...},
  {"name": "web_search", ...}
]

User: 出5道销售测验题
Assistant: <tool_call name="skill_sales_quiz">{"dataset_id": "sales_kb", "count": 5}</tool_call>
```

---

## 7. Built-in Skills

### 7.1 skill_create (Core — Self-Bootstrapping)

The most important built-in skill: lets users create new skills through conversation.

```markdown
---
name: skill-create
title: Skill Creator
description: Create, edit, and manage custom skills for this AI assistant
version: 1.0.0
tags: [meta, creation, management]
permissions:
  - skill:create
  - skill:edit
  - llm:generate
trigger:
  patterns:
    - "创建.*skill"
    - "create.*skill"
    - "新建技能"
    - "make a skill"
  auto: true
---

# Skill Creator

## Instructions

Help the user create a new skill by gathering requirements and generating a SKILL.md file.

### Workflow
1. Ask what the skill should do (purpose, trigger scenarios)
2. Ask what tools/permissions it needs (kb:read, llm:generate, web:search, etc.)
3. Ask for example inputs/outputs
4. Generate the SKILL.md with proper frontmatter + instructions
5. Validate the manifest (check for dangerous permissions)
6. Submit for approval (if third-party) or register directly (if user-created)
7. Test the skill with a sample input

### Validation Rules
- name: kebab-case, 3-50 chars, unique
- permissions: no os:*, exec:*, filesystem:write*
- instructions: < 5000 tokens
- config values: must be JSON-serializable
```

### 7.2 Other Built-in Skills

| Skill | Purpose | Entrypoint |
|-------|---------|------------|
| `skill-create` | Create/edit skills | `builtin://skill_create` |
| `quiz-generator` | Generate quizzes from KB | `builtin://quiz_generator` |
| `doc-generator` | Generate Word/PDF docs | `builtin://doc_generator` |
| `pptx-generator` | Generate presentations | `builtin://pptx_generator` |
| `data-analyzer` | Analyze CSV/Excel files | `builtin://data_analyzer` |
| `web-researcher` | Deep web research | `builtin://web_researcher` |
| `code-reviewer` | Review code for issues | `builtin://code_reviewer` |

---

## 8. Third-Party Skill Upload & Install

### 8.1 Upload Flow

```
User uploads SKILL.md file (or .zip with SKILL.md + resources)
        │
        ▼
┌─ SkillParser.parse(file) ─────────────────────────┐
│  1. Parse YAML frontmatter → SkillManifest         │
│  2. Extract markdown instructions                   │
│  3. Inventory bundled files (if zip)                │
│  4. Validate manifest (name, permissions, size)     │
└────────────────────────┬───────────────────────────┘
                         │
              ┌──── Dangerous? ────┐
              │                    │
          Yes ▼                No ▼
   SkillBuilder.propose()   SkillRegistry.register()
   → PENDING_APPROVAL       → Immediately available
   → Admin reviews          → Stored in DB
   → mark_approved()        → Synced to ToolRegistry
```

### 8.2 API Endpoints

```
POST   /api/v1/assistant/skills/upload          # Upload SKILL.md or .zip
POST   /api/v1/assistant/skills/create           # Create via JSON (skill_create output)
GET    /api/v1/assistant/skills                   # List skills (builtin + user + marketplace)
GET    /api/v1/assistant/skills/:name             # Get skill details + instructions
PUT    /api/v1/assistant/skills/:name             # Update skill
DELETE /api/v1/assistant/skills/:name             # Remove skill
POST   /api/v1/assistant/skills/:name/test        # Test skill with sample input
GET    /api/v1/assistant/skills/:name/versions     # Version history

# Admin
POST   /api/v1/assistant/skills/proposals/:id/approve  # Approve pending skill
POST   /api/v1/assistant/skills/proposals/:id/reject   # Reject pending skill
GET    /api/v1/assistant/skills/proposals              # List pending proposals
```

### 8.3 SKILL.md Parser

```python
class SkillParser:
    """Parse SKILL.md (Claude Code compatible format)."""

    @staticmethod
    def parse(content: str) -> SkillManifest:
        """Parse YAML frontmatter + markdown body."""

        # Split frontmatter and body
        if content.startswith("---"):
            parts = content.split("---", 2)
            frontmatter = yaml.safe_load(parts[1])
            instructions = parts[2].strip()
        else:
            raise ValueError("SKILL.md must start with --- YAML frontmatter")

        return SkillManifest(
            name=frontmatter["name"],
            title=frontmatter.get("title", frontmatter["name"]),
            description=frontmatter["description"],
            version=frontmatter.get("version", "1.0.0"),
            tags=frontmatter.get("tags", []),
            permissions=frontmatter.get("permissions", []),
            trigger=TriggerConfig(**frontmatter["trigger"]) if "trigger" in frontmatter else None,
            config=frontmatter.get("config", {}),
            instructions=instructions,
            entrypoint=f"md://{frontmatter['name']}",
            source=SkillSource.USER,
            max_context_tokens=estimate_tokens(instructions),
        )

    @staticmethod
    def parse_zip(zip_path: str) -> tuple[SkillManifest, list[str]]:
        """Parse .zip containing SKILL.md + bundled resources."""
        # Extract, find SKILL.md, parse, inventory other files
        ...
```

---

## 9. Context Engine Optimizations

### 9.1 Skill-Aware Context Assembly

Modify `agent_loop.py` Step 5 to use progressive loading:

```python
# Current (all-at-once injection):
skill_lines = [f"- {s['name']}@{s['version']}: {s['summary'][:180]}" for s in skills]
system_prompt += "\n\n## Available Skills Metadata\n" + "\n".join(skill_lines)

# New (progressive disclosure):
async def _build_skill_context(self, ctx: ExecutionContext) -> SkillContextResult:
    """3-level progressive skill loading."""

    # L1: Always inject metadata index (~200 tokens)
    all_skills = self.skill_registry.list(enabled_only=True)
    l1_block = self._format_skill_index(all_skills)  # compact one-liners

    # L2: Match and load instructions for relevant skills
    activated_skills = []

    # Method A: Pattern matching (fast, 0ms)
    for skill in all_skills:
        if skill.trigger and skill.trigger.auto:
            if any(re.search(p, ctx.message, re.I) for p in skill.trigger.patterns):
                activated_skills.append(skill)

    # Method B: Semantic selection (fallback, uses existing select_for_query)
    if not activated_skills:
        selections = self.skill_registry.select_for_query(ctx.message, max_skills=2)
        activated_skills = [s.skill for s in selections if s.score > 0.5]

    # Load L2 instructions for activated skills (budget-aware)
    l2_blocks = []
    remaining_budget = ctx.skill_token_budget  # ~2000 tokens
    for skill in activated_skills[:2]:  # Max 2 active skills
        if skill.max_context_tokens <= remaining_budget:
            instructions = await self._load_skill_instructions(skill)
            l2_blocks.append(f"## Active Skill: {skill.title}\n{instructions}")
            remaining_budget -= skill.max_context_tokens

    # Register activated skills as callable tools
    for skill in activated_skills:
        self.skill_tool_bridge.register_skill_as_tool(skill)

    return SkillContextResult(
        l1_metadata=l1_block,
        l2_instructions="\n\n".join(l2_blocks),
        activated_skills=[s.name for s in activated_skills],
        tokens_used=ctx.skill_token_budget - remaining_budget,
    )
```

### 9.2 KV-Cache Optimization for Skills

```
Message Layout (Optimized for prefix caching):

┌─ System Message (STABLE — high cache hit) ──────────────────────┐
│  [Agent Identity]                                                │
│  [Core Behavior]                                                 │
│  [Guardrails]                                                    │
│  [Agent Freedom]                                                 │
│  ...                                                             │
│  ## Available Skills (L1 — ~200 tokens, SEMI-STABLE)             │
│  - quiz-generator@1.0: Generate quizzes from KB                  │
│  - doc-generator@2.1: Generate documents                         │
│  - skill-create@1.0: Create custom skills                        │
│                                                                  │
│  ## Active Skill: Quiz Generator (L2 — DYNAMIC, loaded on match) │
│  [Full instructions when activated]                              │
│                                                                  │
│  ## System Capability                                            │
│  [Tools, datasets, user role]                                    │
└──────────────────────────────────────────────────────────────────┘

┌─ Conversation History (APPEND-ONLY) ────────────────────────────┐
│  [Previous messages]                                             │
└──────────────────────────────────────────────────────────────────┘

┌─ Current Message (ALWAYS NEW) ──────────────────────────────────┐
│  [User's latest input]                                           │
└──────────────────────────────────────────────────────────────────┘
```

L1 metadata 放在 system prompt 的半稳定区域 — 同一会话内不变, 跨会话变化不频繁, 不会破坏前面的 KV-cache prefix。

L2 instructions 放在 L1 之后、System Capability 之前 — 仅当 skill 被激活时注入, 会导致后续内容的 cache miss, 但这是可接受的 trade-off (skill 激活是低频事件)。

### 9.3 Intent Analyzer Enhancement

Extend `QueryIntentAnalyzer` to detect skill invocation:

```python
# New intent patterns in query_intent_analyzer.py

SKILL_INVOKE_PATTERNS = [
    r"^/(\w[\w-]+)",                           # /slash-command
    r"(?:use|run|execute|invoke)\s+skill\s+(\w+)",  # explicit
    r"(?:用|使用|运行)\s*(?:技能|skill)\s*[：:]\s*(\w+)",  # Chinese
]

class QueryIntent:
    # ... existing fields ...
    skill_invocation: str | None = None   # Skill name if detected
    skill_confidence: float = 0.0
```

---

## 10. Implementation Files

### New Files

```
src/services/assistant/skills/
├── __init__.py
├── parser.py                    # SKILL.md YAML+MD parser
├── executor.py                  # Skill execution engine
├── tool_bridge.py               # Register skills as callable tools
├── builtin/                     # Built-in skills
│   ├── __init__.py
│   ├── skill_create.py          # Skill creator handler
│   ├── quiz_generator.py        # Quiz skill (links to quiz_service)
│   └── doc_generator.py         # Document generation skill
└── marketplace/                 # Future: marketplace client
    └── __init__.py

src/api/v1/skills.py             # Skill management API endpoints
src/api/schemas/skills.py        # Pydantic models for skill API

web/src/pages/assistant/components/
├── SkillSelector.tsx             # Skill picker in chat UI
├── SkillCard.tsx                 # Skill display card
└── SkillUploadDialog.tsx         # SKILL.md upload dialog

web/src/pages/skills/
└── SkillManagement.tsx           # Skill listing & management page
```

### Modified Files

```
src/services/assistant/openclaw/skills/registry.py    # Enhance with L1/L2 loading
src/services/assistant/openclaw/skills/models.py       # Extend SkillManifest
src/services/assistant/openclaw/skills/builder.py      # Add AI-assisted creation
src/services/assistant/agent_loop.py                   # Step 5: progressive skill loading
src/services/assistant/context_engine.py               # Skill-aware budget allocation
src/services/assistant/query_intent_analyzer.py        # Skill intent detection
src/services/assistant/tools/tool_registry.py          # Add SKILL category
src/api/router.py                                      # Register skills router
```

---

## 11. Implementation Phases

### Phase 1: Skill Execution Engine (3-4 days)
- [ ] `SkillParser` — 解析 SKILL.md format
- [ ] `SkillToolBridge` — 将 skill 注册为 function_call tool
- [ ] `SkillToolExecutor` — 执行 builtin + md-based skills
- [ ] 修改 `agent_loop.py` Step 5 实现 progressive disclosure (L1/L2)
- [ ] 修改 `ContextBudgetManager` 增加 skills 预算分配
- [ ] 实现 `skill_create` 内置 skill

### Phase 2: Upload & Management (2-3 days)
- [ ] `skills.py` API endpoints (upload, CRUD, test)
- [ ] `SkillUploadDialog.tsx` 前端上传
- [ ] `SkillManagement.tsx` 技能管理页面
- [ ] 第三方 skill 的 permission 审批流程
- [ ] Skill 版本管理 API

### Phase 3: Context Engine Optimization (1-2 days)
- [ ] `QueryIntentAnalyzer` 增加 skill invocation 检测
- [ ] KV-Cache 优化的 prompt layout
- [ ] Skill context cost tracking 集成到 ContextCostBreakdown
- [ ] Skill 执行 metrics (延迟、成功率) 采集

### Phase 4: Ecosystem & Marketplace (Future)
- [ ] Skill marketplace 浏览/搜索/安装界面
- [ ] Skill rating & review system
- [ ] Skill 使用统计 dashboard
- [ ] 跨租户 skill 共享机制

---

## 12. Trade-off Analysis

| Decision | Choice | Trade-off |
|----------|--------|-----------|
| Skill 格式 | SKILL.md (Claude Code 兼容) | 生态兼容性 vs 自定义灵活性; 选择兼容性 |
| Skill 调用 | Tool function_call | LLM 可靠调用 vs prompt-only 注入; function_call 更可控 |
| 匹配算法 | Pattern match + token overlap | 简单快速 vs embedding 语义匹配; 先用简单的, 性能不够再升级 |
| Context 注入 | Progressive disclosure 3层 | 复杂度 vs token 效率; 值得, 每次对话节省 ~2000 tokens |
| 第三方安全 | Permission whitelist + approval | 用户体验 vs 安全; 安全优先 |
| Entrypoint 执行 | In-process (builtin) + prompt injection (md) | 简单实现 vs 沙箱隔离; Phase 1 先 in-process |
