# ADR-003: Sub-Agent 编排系统 — 实现类 Claude Code 的 Explore/Task 子 Agent

**状态:** 已提议 (Proposed)
**日期:** 2026-04-02
**决策者:** Hejaz Financial Services AI Platform Team
**交付方式:** 交给 Claude Code 实现

---

## 1. 目标

在 AI Gateway 的 assistant 模块中实现类似 Claude Code 的 **Sub-Agent 编排系统**，支持：

1. **Explore Agent** — 快速信息搜索/代码分析，只读，轻量
2. **Task Agent** — 独立执行复杂子任务，有自己的 tool 访问权限
3. **Plan Agent** — 分析需求并输出执行计划
4. **上下文隔离** — 子 agent 有独立上下文窗口，不污染主 agent
5. **并行执行** — 多个子 agent 可同时运行
6. **实时状态流** — 前端实时展示每个子 agent 的进度、tool 调用、结果

---

## 2. 当前架构分析

### 2.1 我们已有的基础（可直接复用）

| 组件 | 文件 | 复用方式 |
|------|------|---------|
| **AgentLoop** | `agent/agent_loop.py` (4705行) | 子 agent 复用同一 loop，传入不同 config |
| **ReActExecutor** | `agent/react_executor.py` | 子 agent 的执行引擎 |
| **ToolRegistry** | `tools/tool_registry.py` | 按 agent type 过滤可用 tools |
| **SSE 事件系统** | `sse-events.ts` (50+ 事件类型) | 新增 `SUBAGENT_*` 事件系列 |
| **WorkingMemory** | 前后端已实现 | 子 agent 状态映射到 WorkingMemoryTask.subTasks |
| **AgentTaskTimeline** | `AgentTaskTimeline.tsx` | 扩展支持嵌套子 agent 展示 |
| **processSummary** | `types.ts` | 子 agent 作为嵌套 steps |

### 2.2 缺失的部分（需要新建）

| 组件 | 说明 |
|------|------|
| **SubAgentManager** | 子 agent 生命周期管理（创建、执行、销毁） |
| **SubAgentType 定义** | explore / task / plan / bash 等 agent 类型 |
| **上下文隔离层** | 独立 message history，结果摘要回传 |
| **并行执行器** | asyncio.gather 管理多个子 agent |
| **前端嵌套状态** | SubAgent 展开/折叠 UI |
| **SSE 子事件** | SUBAGENT_STARTED / SUBAGENT_STEP / SUBAGENT_FINISHED |

---

## 3. 架构设计

### 3.1 整体架构图

```
┌─────────────────────────────────────────────────────────┐
│                    Main Agent Loop                       │
│  (streaming-first, 完整 8-step, 所有 tools 可用)          │
│                                                          │
│  LLM 决定调用 "spawn_subagent" tool                       │
│     ├── type: "explore"                                  │
│     ├── prompt: "搜索所有与用户认证相关的文件"               │
│     └── config: { max_turns: 10, model: "qwen-turbo" }   │
│                                                          │
│  ┌──────────────────────────────────────────────────┐    │
│  │           SubAgentManager                         │    │
│  │                                                    │    │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐       │    │
│  │  │ Explore  │  │  Task    │  │  Plan    │       │    │
│  │  │ Agent    │  │  Agent   │  │  Agent   │       │    │
│  │  │          │  │          │  │          │       │    │
│  │  │ 独立context│ │ 独立context│ │ 独立context│       │    │
│  │  │ 只读 tools │ │ 全部 tools │ │ 只读 tools │       │    │
│  │  │ 快速模型  │  │ 强模型    │  │ 强模型    │       │    │
│  │  └────┬─────┘  └────┬─────┘  └────┬─────┘       │    │
│  │       │              │              │              │    │
│  │       └──── SSE events (SUBAGENT_*) ──────────────│───│──→ Frontend
│  │              │              │              │        │    │
│  │       ┌──────┴──────────────┴──────────────┴──┐   │    │
│  │       │        Result Summarizer              │   │    │
│  │       │  (摘要子agent输出，返回给主agent)        │   │    │
│  │       └───────────────────────────────────────┘   │    │
│  └──────────────────────────────────────────────────┘    │
│                                                          │
│  主 agent 收到摘要结果，继续推理                            │
└─────────────────────────────────────────────────────────┘
```

### 3.2 子 Agent 类型定义

```python
# src/services/assistant/agent/subagent_types.py

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class SubAgentType(str, Enum):
    EXPLORE = "explore"      # 快速搜索，只读
    TASK = "task"            # 独立执行子任务
    PLAN = "plan"            # 分析并输出计划
    BASH = "bash"            # 命令执行 (未来)

@dataclass
class SubAgentConfig:
    """子 Agent 配置"""
    agent_type: SubAgentType
    prompt: str                              # 子 agent 要完成的任务
    description: str = ""                    # 3-5 字简短描述

    # 执行限制
    max_turns: int = 10                      # 最大推理轮数
    max_tool_calls: int = 20                 # 最大 tool 调用次数
    timeout_seconds: int = 120               # 超时时间

    # 模型配置
    model_override: Optional[str] = None     # 覆盖模型 (explore 用快速模型)

    # 上下文
    parent_context: Optional[str] = None     # 父 agent 传递的上下文摘要
    include_history: bool = False            # 是否包含父 agent 对话历史

    # 隔离
    isolation: str = "context"               # "context" | "worktree" (未来)

@dataclass
class SubAgentState:
    """子 Agent 运行状态"""
    agent_id: str
    agent_type: SubAgentType
    description: str
    status: str = "pending"    # pending | running | completed | failed | cancelled

    # 进度追踪
    current_step: str = ""
    turns_completed: int = 0
    tool_calls_made: int = 0

    # 结果
    result: Optional[str] = None
    error: Optional[str] = None

    # 时间
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    duration_ms: Optional[float] = None

    # 子步骤 (用于前端展示)
    steps: list = field(default_factory=list)


# 每种 agent type 的默认配置
SUBAGENT_DEFAULTS = {
    SubAgentType.EXPLORE: {
        "max_turns": 8,
        "max_tool_calls": 15,
        "timeout_seconds": 60,
        "model_override": None,       # 用配置中的 fast model
        "allowed_tool_categories": ["RETRIEVAL", "UTILITY"],  # 只读
        "system_prompt_suffix": (
            "You are an Explore agent. Your job is to quickly find information "
            "by searching files, reading code, and analyzing structure. "
            "You CANNOT modify any files. Report findings concisely."
        ),
    },
    SubAgentType.TASK: {
        "max_turns": 15,
        "max_tool_calls": 30,
        "timeout_seconds": 180,
        "model_override": None,       # 用配置中的 strong model
        "allowed_tool_categories": None,  # 全部 (按用户权限)
        "system_prompt_suffix": (
            "You are a Task agent. Complete the assigned task autonomously. "
            "Use all available tools. Return your final result clearly."
        ),
    },
    SubAgentType.PLAN: {
        "max_turns": 5,
        "max_tool_calls": 10,
        "timeout_seconds": 60,
        "model_override": None,
        "allowed_tool_categories": ["RETRIEVAL", "UTILITY"],
        "system_prompt_suffix": (
            "You are a Plan agent. Analyze the codebase and design an "
            "implementation plan. Return step-by-step instructions."
        ),
    },
}
```

### 3.3 SubAgentManager — 核心编排器

```python
# src/services/assistant/agent/subagent_manager.py

import asyncio
import uuid
import time
from typing import AsyncGenerator, Optional
from .subagent_types import (
    SubAgentConfig, SubAgentState, SubAgentType, SUBAGENT_DEFAULTS
)

class SubAgentManager:
    """
    子 Agent 编排管理器

    职责:
    1. 创建子 agent (独立上下文)
    2. 执行子 agent (调用 AgentLoop 的简化版)
    3. 并行管理多个子 agent
    4. 流式事件转发 (SSE)
    5. 结果摘要回传
    """

    def __init__(
        self,
        tool_registry,
        mcp_manager,
        skill_registry,
        model_router,
        database,
    ):
        self.tool_registry = tool_registry
        self.mcp_manager = mcp_manager
        self.skill_registry = skill_registry
        self.model_router = model_router
        self.database = database
        self._active_agents: dict[str, SubAgentState] = {}

    async def spawn(
        self,
        config: SubAgentConfig,
        parent_context: "AgentLoopContext",
    ) -> AsyncGenerator[dict, None]:
        """
        启动一个子 agent 并流式返回事件。

        这是主 agent 调用 "spawn_subagent" tool 时的入口。
        返回的是 SSE event 流，由 agent_loop 透传给前端。
        """
        agent_id = f"sub_{uuid.uuid4().hex[:12]}"
        defaults = SUBAGENT_DEFAULTS.get(config.agent_type, {})

        state = SubAgentState(
            agent_id=agent_id,
            agent_type=config.agent_type,
            description=config.description or config.prompt[:50],
            status="running",
            started_at=time.time(),
        )
        self._active_agents[agent_id] = state

        # 1. 发送 SUBAGENT_STARTED 事件
        yield {
            "event_type": "subagent_started",
            "data": {
                "agent_id": agent_id,
                "agent_type": config.agent_type.value,
                "description": state.description,
                "prompt": config.prompt[:200],  # 截断
            }
        }

        try:
            # 2. 构建子 agent 的隔离上下文
            sub_messages = self._build_isolated_context(config, parent_context)
            sub_tools = self._get_allowed_tools(config, parent_context)
            sub_model = config.model_override or self._select_model(config)
            sub_system_prompt = self._build_system_prompt(config, defaults)

            # 3. 执行子 agent loop (简化版，无 8-step 预处理)
            result_text = ""
            async for event in self._execute_subagent_loop(
                agent_id=agent_id,
                state=state,
                messages=sub_messages,
                tools=sub_tools,
                model=sub_model,
                system_prompt=sub_system_prompt,
                config=config,
                defaults=defaults,
            ):
                # 转发子 agent 内部事件 (加上 agent_id 前缀)
                event["data"]["agent_id"] = agent_id
                yield event

                # 收集最终文本
                if event["event_type"] == "subagent_text_delta":
                    result_text += event["data"].get("text", "")

            # 4. 完成
            state.status = "completed"
            state.result = result_text
            state.finished_at = time.time()
            state.duration_ms = (state.finished_at - state.started_at) * 1000

            yield {
                "event_type": "subagent_finished",
                "data": {
                    "agent_id": agent_id,
                    "status": "completed",
                    "result_summary": self._summarize_result(result_text),
                    "duration_ms": state.duration_ms,
                    "turns": state.turns_completed,
                    "tool_calls": state.tool_calls_made,
                }
            }

        except asyncio.TimeoutError:
            state.status = "failed"
            state.error = f"Timeout after {config.timeout_seconds}s"
            yield {
                "event_type": "subagent_finished",
                "data": {
                    "agent_id": agent_id,
                    "status": "failed",
                    "error": state.error,
                }
            }
        except Exception as e:
            state.status = "failed"
            state.error = str(e)
            yield {
                "event_type": "subagent_finished",
                "data": {
                    "agent_id": agent_id,
                    "status": "failed",
                    "error": str(e),
                }
            }
        finally:
            del self._active_agents[agent_id]

    async def spawn_parallel(
        self,
        configs: list[SubAgentConfig],
        parent_context: "AgentLoopContext",
    ) -> AsyncGenerator[dict, None]:
        """
        并行启动多个子 agent。
        使用 asyncio.Queue 合并多个子 agent 的事件流。
        """
        queue: asyncio.Queue = asyncio.Queue()

        async def _run_agent(config: SubAgentConfig):
            async for event in self.spawn(config, parent_context):
                await queue.put(event)

        # 并行启动所有子 agent
        tasks = [
            asyncio.create_task(_run_agent(config))
            for config in configs
        ]

        # 等待所有完成，同时流式输出事件
        done_count = 0
        total = len(configs)

        while done_count < total:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
                yield event
                if event["event_type"] == "subagent_finished":
                    done_count += 1
            except asyncio.TimeoutError:
                # 检查是否有 task 异常退出
                for task in tasks:
                    if task.done() and task.exception():
                        done_count += 1

        # 清理
        for task in tasks:
            if not task.done():
                task.cancel()

    def _build_isolated_context(
        self, config: SubAgentConfig, parent: "AgentLoopContext"
    ) -> list[dict]:
        """
        构建子 agent 的独立消息历史。
        关键：不包含父 agent 的完整历史，实现上下文隔离。
        """
        messages = []

        # 可选：传入父 agent 的上下文摘要
        if config.parent_context:
            messages.append({
                "role": "user",
                "content": f"Context from parent agent:\n{config.parent_context}"
            })

        # 子 agent 的任务指令
        messages.append({
            "role": "user",
            "content": config.prompt,
        })

        return messages

    def _get_allowed_tools(
        self, config: SubAgentConfig, parent: "AgentLoopContext"
    ) -> list:
        """按 agent type 过滤可用 tools"""
        defaults = SUBAGENT_DEFAULTS.get(config.agent_type, {})
        allowed_categories = defaults.get("allowed_tool_categories")

        user = parent.user if hasattr(parent, 'user') else None
        all_tools = self.tool_registry.list_tools(user=user)

        if allowed_categories is None:
            return all_tools  # Task agent: 全部

        return [
            t for t in all_tools
            if t.category.value in allowed_categories
        ]

    def _select_model(self, config: SubAgentConfig) -> str:
        """根据 agent type 选择模型"""
        if config.agent_type == SubAgentType.EXPLORE:
            return "qwen-turbo"  # 快速模型，低延迟
        elif config.agent_type == SubAgentType.PLAN:
            return "deepseek-chat"  # 推理模型
        else:
            return "claude-sonnet"  # 强模型

    def _build_system_prompt(self, config: SubAgentConfig, defaults: dict) -> str:
        """构建子 agent 的 system prompt"""
        suffix = defaults.get("system_prompt_suffix", "")
        return f"""You are a specialized sub-agent within the Hejaz AI platform.

{suffix}

Rules:
- Stay focused on the assigned task
- Be concise in your responses
- Report progress clearly
- If you encounter errors, report them and suggest alternatives
- Maximum turns: {config.max_turns}
"""

    async def _execute_subagent_loop(
        self,
        agent_id: str,
        state: SubAgentState,
        messages: list,
        tools: list,
        model: str,
        system_prompt: str,
        config: SubAgentConfig,
        defaults: dict,
    ) -> AsyncGenerator[dict, None]:
        """
        子 agent 的执行循环。
        这是 AgentLoop._execute_streaming_first() 的简化版本。
        """
        for turn in range(config.max_turns):
            state.turns_completed = turn + 1

            yield {
                "event_type": "subagent_step",
                "data": {
                    "agent_id": agent_id,
                    "step": f"Turn {turn + 1}/{config.max_turns}",
                    "status": "running",
                }
            }

            # 调用 LLM
            tool_schemas = [t.to_openai_schema() for t in tools] if tools else None

            response_stream = await self.model_router.stream_chat(
                model=model,
                messages=[{"role": "system", "content": system_prompt}] + messages,
                tools=tool_schemas,
                temperature=0.3,
            )

            full_text = ""
            tool_calls = []

            async for chunk in response_stream:
                # 流式文本
                if chunk.get("type") == "text_delta":
                    delta = chunk["text"]
                    full_text += delta
                    yield {
                        "event_type": "subagent_text_delta",
                        "data": {
                            "agent_id": agent_id,
                            "text": delta,
                        }
                    }
                # tool call chunks
                elif chunk.get("type") == "tool_call":
                    tool_calls.append(chunk["tool_call"])

            # 如果没有 tool calls，子 agent 完成
            if not tool_calls:
                messages.append({"role": "assistant", "content": full_text})
                break

            # 执行 tool calls
            messages.append({
                "role": "assistant",
                "content": full_text,
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                state.tool_calls_made += 1

                yield {
                    "event_type": "subagent_tool_start",
                    "data": {
                        "agent_id": agent_id,
                        "tool_name": tc["function"]["name"],
                        "call_id": tc["id"],
                    }
                }

                # 执行 tool
                start = time.time()
                result = await self.tool_registry.execute(
                    tool_name=tc["function"]["name"],
                    arguments=tc["function"].get("arguments", {}),
                )
                duration = (time.time() - start) * 1000

                yield {
                    "event_type": "subagent_tool_result",
                    "data": {
                        "agent_id": agent_id,
                        "tool_name": tc["function"]["name"],
                        "call_id": tc["id"],
                        "success": result.success,
                        "duration_ms": duration,
                        "summary": self._summarize_result(
                            str(result.result)[:500] if result.success
                            else result.error
                        ),
                    }
                }

                # 添加 tool result 到消息历史
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result.result)[:2000],  # 截断
                })

                state.steps.append({
                    "tool": tc["function"]["name"],
                    "status": "completed" if result.success else "failed",
                    "duration_ms": duration,
                })

    def _summarize_result(self, text: str, max_length: int = 500) -> str:
        """摘要子 agent 输出，返回给主 agent"""
        if len(text) <= max_length:
            return text
        return text[:max_length] + "..."
```

### 3.4 注册为 Tool — 让 LLM 自动决定何时调用

```python
# src/services/assistant/tools/builtin/spawn_subagent.py

"""
spawn_subagent tool — 注册到 ToolRegistry，
让主 agent 的 LLM 在需要时自主决定是否启动子 agent。
"""

TOOL_DEFINITION = ToolDefinition(
    name="spawn_subagent",
    description=(
        "Spawn a specialized sub-agent to handle a complex sub-task. "
        "Use this when the task requires deep exploration, independent "
        "execution, or when you want to parallelize work. "
        "Available types: "
        "'explore' (fast search, read-only), "
        "'task' (full execution with all tools), "
        "'plan' (analyze and create implementation plan)."
    ),
    parameters=[
        ToolParameter(
            name="agent_type",
            type="string",
            description="Type of sub-agent: 'explore', 'task', or 'plan'",
            required=True,
            enum=["explore", "task", "plan"],
        ),
        ToolParameter(
            name="prompt",
            type="string",
            description="Detailed instructions for what the sub-agent should do",
            required=True,
        ),
        ToolParameter(
            name="description",
            type="string",
            description="Short 3-5 word description of the task",
            required=True,
        ),
        ToolParameter(
            name="context",
            type="string",
            description="Optional context from current conversation to pass to sub-agent",
            required=False,
        ),
    ],
    category=ToolCategory.UTILITY,
    risk_level=ToolRiskLevel.LOW,
    requires_confirmation=False,
    timeout_seconds=180,
    max_retries=0,
    is_async=True,
)


async def execute_spawn_subagent(request: ToolCallRequest) -> ToolCallResult:
    """
    在 agent_loop 的 streaming-first 流程中被调用。
    返回的不是简单字符串，而是一个 async generator 标记，
    agent_loop 检测到后会特殊处理，将子 agent 事件流透传。
    """
    args = request.arguments

    config = SubAgentConfig(
        agent_type=SubAgentType(args["agent_type"]),
        prompt=args["prompt"],
        description=args.get("description", ""),
        parent_context=args.get("context"),
    )

    # 返回特殊标记，让 agent_loop 知道这是子 agent 调用
    return ToolCallResult(
        call_id=request.call_id,
        tool_name="spawn_subagent",
        success=True,
        result={"__subagent__": True, "config": config},
        error=None,
        duration_ms=0,
        metadata={"is_subagent": True},
        output_files=[],
    )
```

### 3.5 AgentLoop 集成点

在 `agent_loop.py` 的 `_execute_streaming_first()` 方法中，处理 tool call result 时增加子 agent 分支：

```python
# agent_loop.py — 在 tool call 处理逻辑中增加

async def _handle_tool_call_result(self, tool_result, ctx):
    """处理 tool 执行结果，如果是子 agent 则特殊处理"""

    if (isinstance(tool_result.result, dict)
        and tool_result.result.get("__subagent__")):

        # ========== 子 Agent 执行路径 ==========
        config = tool_result.result["config"]

        subagent_result_text = ""
        async for event in self.subagent_manager.spawn(config, ctx):
            # 1. 透传子 agent 事件到前端 SSE 流
            yield AgentLoopEvent(
                phase=AgentLoopPhase.EXECUTION,
                event_type=event["event_type"],
                data=event["data"],
            )

            # 2. 收集子 agent 最终文本结果
            if event["event_type"] == "subagent_text_delta":
                subagent_result_text += event["data"].get("text", "")
            elif event["event_type"] == "subagent_finished":
                if event["data"].get("result_summary"):
                    subagent_result_text = event["data"]["result_summary"]

        # 3. 将摘要结果作为 tool result 返回给主 LLM
        # 这实现了「上下文隔离 + 结果回传」
        return ToolCallResult(
            call_id=tool_result.call_id,
            tool_name="spawn_subagent",
            success=True,
            result=subagent_result_text,
            error=None,
            duration_ms=tool_result.duration_ms,
            metadata={},
            output_files=[],
        )

    # 普通 tool result 原路返回
    return tool_result
```

---

## 4. 新增 SSE 事件定义

### 4.1 后端事件

```python
# 新增到 SSE 事件枚举
class SSEEventType:
    # ... 现有事件 ...

    # Sub-Agent 生命周期
    SUBAGENT_STARTED = "subagent_started"
    SUBAGENT_STEP = "subagent_step"
    SUBAGENT_TEXT_DELTA = "subagent_text_delta"
    SUBAGENT_TOOL_START = "subagent_tool_start"
    SUBAGENT_TOOL_RESULT = "subagent_tool_result"
    SUBAGENT_FINISHED = "subagent_finished"
```

### 4.2 前端事件类型

```typescript
// web/src/pages/assistant/sse-events.ts — 新增

// Sub-Agent Events
SUBAGENT_STARTED = "subagent_started",
SUBAGENT_STEP = "subagent_step",
SUBAGENT_TEXT_DELTA = "subagent_text_delta",
SUBAGENT_TOOL_START = "subagent_tool_start",
SUBAGENT_TOOL_RESULT = "subagent_tool_result",
SUBAGENT_FINISHED = "subagent_finished",
```

### 4.3 前端类型定义

```typescript
// web/src/pages/assistant/types.ts — 新增

interface SubAgentState {
  agentId: string;
  agentType: "explore" | "task" | "plan";
  description: string;
  status: "running" | "completed" | "failed";
  prompt?: string;

  // 进度
  currentStep?: string;
  turnsCompleted?: number;
  toolCallsMade?: number;

  // 子步骤 (tool calls)
  steps: SubAgentStep[];

  // 实时文本输出
  streamingText?: string;

  // 结果
  resultSummary?: string;
  error?: string;
  durationMs?: number;
}

interface SubAgentStep {
  toolName: string;
  callId: string;
  status: "running" | "completed" | "failed";
  summary?: string;
  durationMs?: number;
}

// 扩展 WorkingMemoryTask
interface WorkingMemoryTask {
  // ... 现有字段 ...

  // 新增: 子 agent 状态
  subAgent?: SubAgentState;
}

// 扩展 ChatMessage
interface ChatMessage {
  // ... 现有字段 ...

  // 新增: 活跃的子 agents
  activeSubAgents?: SubAgentState[];
}
```

---

## 5. 前端 UI 实现

### 5.1 useChatSession Hook 扩展

```typescript
// web/src/pages/assistant/hooks/useChatSession.ts — 新增事件处理

// 在 sendMessage() 的 event switch 中新增:

case SSEEventType.SUBAGENT_STARTED: {
  const { agent_id, agent_type, description, prompt } = parsed.data;

  // 创建子 agent 状态
  const subAgent: SubAgentState = {
    agentId: agent_id,
    agentType: agent_type,
    description,
    prompt: prompt?.slice(0, 200),
    status: "running",
    steps: [],
    turnsCompleted: 0,
    toolCallsMade: 0,
    streamingText: "",
  };

  // 添加到 workingMemory 的当前 task
  setWorkingMemory(prev => {
    if (!prev) return prev;
    const tasks = [...prev.tasks];
    // 找到当前 in_progress 的 task，或创建新 task
    const currentTask = tasks.find(t => t.status === "in_progress");
    if (currentTask) {
      currentTask.subAgent = subAgent;
    } else {
      tasks.push({
        id: agent_id,
        description: `${getAgentTypeIcon(agent_type)} ${description}`,
        status: "in_progress",
        subAgent,
      });
    }
    return { ...prev, tasks };
  });
  break;
}

case SSEEventType.SUBAGENT_STEP: {
  const { agent_id, step, status } = parsed.data;
  updateSubAgentField(agent_id, { currentStep: step });
  break;
}

case SSEEventType.SUBAGENT_TEXT_DELTA: {
  const { agent_id, text } = parsed.data;
  updateSubAgentField(agent_id, prev => ({
    streamingText: (prev.streamingText || "") + text,
  }));
  break;
}

case SSEEventType.SUBAGENT_TOOL_START: {
  const { agent_id, tool_name, call_id } = parsed.data;
  updateSubAgentField(agent_id, prev => ({
    steps: [...prev.steps, {
      toolName: tool_name,
      callId: call_id,
      status: "running",
    }],
    toolCallsMade: (prev.toolCallsMade || 0) + 1,
  }));
  break;
}

case SSEEventType.SUBAGENT_TOOL_RESULT: {
  const { agent_id, call_id, success, duration_ms, summary } = parsed.data;
  updateSubAgentField(agent_id, prev => ({
    steps: prev.steps.map(s =>
      s.callId === call_id
        ? { ...s, status: success ? "completed" : "failed", summary, durationMs: duration_ms }
        : s
    ),
  }));
  break;
}

case SSEEventType.SUBAGENT_FINISHED: {
  const { agent_id, status, result_summary, duration_ms, error } = parsed.data;
  updateSubAgentField(agent_id, {
    status,
    resultSummary: result_summary,
    error,
    durationMs: duration_ms,
  });

  // 同时更新对应的 workingMemory task
  setWorkingMemory(prev => {
    if (!prev) return prev;
    return {
      ...prev,
      tasks: prev.tasks.map(t =>
        t.subAgent?.agentId === agent_id
          ? { ...t, status: status === "completed" ? "completed" : "failed" }
          : t
      ),
    };
  });
  break;
}

// Helper
function updateSubAgentField(agentId: string, update: Partial<SubAgentState> | ((prev: SubAgentState) => Partial<SubAgentState>)) {
  setWorkingMemory(prev => {
    if (!prev) return prev;
    return {
      ...prev,
      tasks: prev.tasks.map(task => {
        if (task.subAgent?.agentId !== agentId) return task;
        const delta = typeof update === "function" ? update(task.subAgent) : update;
        return {
          ...task,
          subAgent: { ...task.subAgent, ...delta },
        };
      }),
    };
  });
}

function getAgentTypeIcon(type: string): string {
  switch (type) {
    case "explore": return "🔍";
    case "task": return "⚙️";
    case "plan": return "📋";
    default: return "🤖";
  }
}
```

### 5.2 SubAgentCard 组件

```tsx
// web/src/pages/assistant/components/SubAgentCard.tsx

import React, { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import type { SubAgentState } from "../types";

interface SubAgentCardProps {
  subAgent: SubAgentState;
}

const AGENT_TYPE_CONFIG = {
  explore: { icon: "🔍", label: "Explore", color: "blue" },
  task:    { icon: "⚙️", label: "Task",    color: "purple" },
  plan:    { icon: "📋", label: "Plan",    color: "green" },
};

export const SubAgentCard: React.FC<SubAgentCardProps> = ({ subAgent }) => {
  const [expanded, setExpanded] = useState(subAgent.status === "running");
  const config = AGENT_TYPE_CONFIG[subAgent.agentType];

  const statusIcon = {
    running: "⏳",
    completed: "✅",
    failed: "❌",
  }[subAgent.status];

  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: "auto" }}
      className={`
        border rounded-lg p-3 my-2
        ${subAgent.status === "running" ? "border-blue-300 bg-blue-50/50" : ""}
        ${subAgent.status === "completed" ? "border-green-200 bg-green-50/30" : ""}
        ${subAgent.status === "failed" ? "border-red-200 bg-red-50/30" : ""}
      `}
    >
      {/* Header — 点击折叠/展开 */}
      <div
        className="flex items-center justify-between cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-2">
          <span className="text-lg">{config.icon}</span>
          <span className="font-medium text-sm">{config.label}</span>
          <span className="text-gray-600 text-sm">{subAgent.description}</span>
        </div>
        <div className="flex items-center gap-2 text-xs text-gray-500">
          {subAgent.status === "running" && (
            <motion.div
              animate={{ rotate: 360 }}
              transition={{ repeat: Infinity, duration: 1, ease: "linear" }}
              className="w-3 h-3 border-2 border-blue-400 border-t-transparent rounded-full"
            />
          )}
          {statusIcon}
          {subAgent.durationMs && (
            <span>{formatDuration(subAgent.durationMs)}</span>
          )}
          <span>{expanded ? "▲" : "▼"}</span>
        </div>
      </div>

      {/* Expanded Content */}
      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="mt-2 ml-6"
          >
            {/* Tool Call 步骤列表 */}
            {subAgent.steps.length > 0 && (
              <div className="space-y-1">
                {subAgent.steps.map((step, i) => (
                  <div key={step.callId} className="flex items-center gap-2 text-xs">
                    <span>
                      {step.status === "running" ? "→" :
                       step.status === "completed" ? "✓" : "✗"}
                    </span>
                    <span className="font-mono text-gray-600">
                      {step.toolName}
                    </span>
                    {step.summary && (
                      <span className="text-gray-400 truncate max-w-[300px]">
                        {step.summary}
                      </span>
                    )}
                    {step.durationMs && (
                      <span className="text-gray-400">
                        {formatDuration(step.durationMs)}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}

            {/* 实时输出文本 (折叠显示) */}
            {subAgent.streamingText && (
              <div className="mt-2 p-2 bg-gray-50 rounded text-xs font-mono max-h-[200px] overflow-y-auto">
                {subAgent.streamingText.slice(-500)}
              </div>
            )}

            {/* 最终结果摘要 */}
            {subAgent.resultSummary && (
              <div className="mt-2 p-2 bg-white rounded border text-sm">
                {subAgent.resultSummary.slice(0, 300)}
              </div>
            )}

            {/* 错误信息 */}
            {subAgent.error && (
              <div className="mt-2 p-2 bg-red-50 rounded text-sm text-red-700">
                {subAgent.error}
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
};

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}
```

### 5.3 在 AgentTaskTimeline 中集成

```tsx
// web/src/pages/assistant/components/AgentTaskTimeline.tsx — 修改

// 在 TaskItem 渲染中增加子 agent 卡片:
{task.subAgent && (
  <SubAgentCard subAgent={task.subAgent} />
)}
```

---

## 6. 前端效果示意

完成后的 UI 展示效果：

```
┌──────────────────────────────────────────────────────┐
│ 🤖 Assistant                                          │
│                                                       │
│ ┌── 分析你的认证系统 ─────────────────────────────── │
│ │                                                     │
│ │  🔍 Explore  搜索认证相关文件          ✅ 2.3s     │
│ │  ┌─────────────────────────────────────────────┐   │
│ │  │ ✓ kb_search  "认证 auth middleware"  0.8s   │   │
│ │  │ ✓ file_search  "*.py auth*"          0.3s   │   │
│ │  │ ✓ read_file  "user_resolver.py"      0.1s   │   │
│ │  │                                              │   │
│ │  │ 找到 5 个相关文件:                            │   │
│ │  │ - src/core/auth/user_resolver.py              │   │
│ │  │ - src/core/auth/jwt_handler.py                │   │
│ │  │ - src/api/middleware/auth_middleware.py        │   │
│ │  └─────────────────────────────────────────────┘   │
│ │                                                     │
│ │  📋 Plan  设计改造方案                   ✅ 3.1s     │
│ │  ┌─────────────────────────────────────────────┐   │
│ │  │ ✓ read_file  "user_resolver.py"  0.1s       │   │
│ │  │ ✓ read_file  "jwt_handler.py"    0.1s       │   │
│ │  │                                              │   │
│ │  │ 方案: 1. 增加 refresh token...               │   │
│ │  └─────────────────────────────────────────────┘   │
│ │                                                     │
│ │  ⚙️ Task  实现 refresh token            ⏳ 运行中...│
│ │  ┌─────────────────────────────────────────────┐   │
│ │  │ ✓ code_execute  生成 migration       1.2s   │   │
│ │  │ → write_file   更新 jwt_handler.py          │   │
│ │  │ ⏳ test_run    等待中                        │   │
│ │  └─────────────────────────────────────────────┘   │
│ │                                                     │
│ └────────────────────────────────────────────────────│
│                                                       │
│ 基于子 agent 的分析，你的认证系统存在以下问题...        │
└──────────────────────────────────────────────────────┘
```

---

## 7. 实现步骤（给 Claude Code 的任务清单）

### Phase 1: 后端核心（预计 3-4 天）

```
1. 创建 src/services/assistant/agent/subagent_types.py
   - SubAgentType enum
   - SubAgentConfig dataclass
   - SubAgentState dataclass
   - SUBAGENT_DEFAULTS 配置

2. 创建 src/services/assistant/agent/subagent_manager.py
   - SubAgentManager class
   - spawn() 方法 (单个子 agent)
   - spawn_parallel() 方法 (并行)
   - 上下文隔离 (_build_isolated_context)
   - Tool 过滤 (_get_allowed_tools)
   - 子 agent 执行循环 (_execute_subagent_loop)
   - 结果摘要 (_summarize_result)

3. 创建 src/services/assistant/tools/builtin/spawn_subagent.py
   - TOOL_DEFINITION
   - execute_spawn_subagent() handler

4. 修改 src/services/assistant/tools/tool_registry.py
   - 注册 spawn_subagent tool

5. 修改 src/services/assistant/agent/agent_loop.py
   - 在 __init__ 中注入 SubAgentManager
   - 在 _execute_streaming_first() 中处理子 agent tool result
   - 透传子 agent SSE 事件
   - 将子 agent 摘要结果回传给主 LLM
```

### Phase 2: 前端 UI（预计 3-4 天）

```
6. 修改 web/src/pages/assistant/sse-events.ts
   - 新增 SUBAGENT_* 事件类型

7. 修改 web/src/pages/assistant/types.ts
   - 新增 SubAgentState interface
   - 新增 SubAgentStep interface
   - 扩展 WorkingMemoryTask (添加 subAgent 字段)

8. 修改 web/src/pages/assistant/hooks/useChatSession.ts
   - 新增 SUBAGENT_* 事件处理分支
   - updateSubAgentField() helper
   - 子 agent 状态与 workingMemory 同步

9. 创建 web/src/pages/assistant/components/SubAgentCard.tsx
   - 折叠/展开子 agent 详情
   - 实时 tool call 进度列表
   - 流式文本输出显示
   - 结果摘要显示
   - 状态动画 (spinner, checkmark)

10. 修改 web/src/pages/assistant/components/AgentTaskTimeline.tsx
    - 在 TaskItem 中嵌入 SubAgentCard
    - 处理嵌套动画
```

### Phase 3: 测试与优化（预计 2 天）

```
11. 单元测试
    - SubAgentManager.spawn() 基本流程
    - 上下文隔离验证 (子 agent 无法访问父历史)
    - 并行执行测试
    - 超时和错误处理

12. 集成测试
    - 端到端: 用户消息 → 主 agent 决定调用子 agent → 子 agent 执行 → 结果回传 → 主 agent 继续
    - SSE 事件流完整性
    - 前端状态渲染

13. 性能调优
    - 子 agent 模型选择优化 (explore 用快速模型)
    - 结果摘要长度调优
    - SSE 事件节流 (避免前端重渲染过频)
```

---

## 8. 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 子 agent 触发方式 | LLM 自主调用 tool | 与 Claude Code 一致，LLM 判断何时需要子 agent |
| 上下文隔离 | 独立消息历史 | 防止上下文污染，节省 token |
| 结果回传 | 文本摘要 | 只将最终结果传回主 agent，不传中间过程 |
| 并行方式 | asyncio.gather + Queue | 利用 Python 异步 IO，事件流合并 |
| 子 agent 模型 | 按 type 选择 | explore 用快速模型降低延迟和成本 |
| 前端展示 | 嵌入 WorkingMemoryTask | 复用现有 AgentTaskTimeline 组件，最小改动 |
| SSE 事件 | 新增 subagent_* 系列 | 与现有事件体系解耦，不影响已有逻辑 |

---

## 9. 与 Claude Code 的对比

| 能力 | Claude Code | 我们的实现 | 差异 |
|------|-------------|-----------|------|
| Explore Agent | ✅ 内置 | ✅ 实现 | 功能一致 |
| Task Agent | ✅ 内置 | ✅ 实现 | 功能一致 |
| Plan Agent | ✅ 内置 | ✅ 实现 | 功能一致 |
| Bash Agent | ✅ 内置 | ⏳ Phase 2 | 需 code_executor 集成 |
| 自定义 Agent | ✅ /agents 命令 | ⏳ Phase 2 | 需 admin UI |
| Agent Teams (Swarms) | ⚠️ 实验性 | ❌ 不做 | 当前规模不需要 |
| 并行执行 | ✅ 最多 7 个 | ✅ 可配置 | 受服务器资源限制 |
| 上下文隔离 | ✅ 独立 subprocess | ✅ 独立消息历史 | 我们是进程内隔离 |
| 实时状态 | ✅ CLI 显示 | ✅ Web UI 卡片 | 我们有更丰富的 UI |
| Git Worktree | ✅ 支持 | ❌ 不适用 | Web 端无需 |

---

## 参考

- [Claude Code Docs — Sub-agents](https://code.claude.com/docs/en/sub-agents)
- [Claude Code Task Tool Architecture](https://dev.to/bhaidar/the-task-tool-claude-codes-agent-orchestration-system-4bf2)
- [Task Tool Context Isolation (mini-claude-code)](https://deepwiki.com/shareAI-lab/mini-claude-code/6.2-task-tool-and-context-isolation)
- [Claude Agent SDK — Subagents](https://platform.claude.com/docs/en/agent-sdk/subagents)
- [LangGraph — Multi-agent Orchestration](https://docs.langchain.com/oss/python/langchain/multi-agent/index)
- [LangGraph — Graph Execution Frontend](https://docs.langchain.com/oss/python/langgraph/frontend/graph-execution)

---

*本文档供 Claude Code 直接作为实现参考。所有文件路径、类型定义、事件名称均已与现有代码库对齐。*
