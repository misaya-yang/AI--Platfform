# AI Assistant 三大支柱架构诊断报告

基于 Manus Context Engineering 的三大支柱：**卸载 (Offloading)**、**缩减 (Reduction)**、**隔离 (Isolation)**，对现有 AI Assistant 进行深度诊断。

---

## 一、执行摘要

### 整体评估：基础扎实，集成不足

| 评估维度 | 当前状态 | 评分 | 主要问题 |
|----------|----------|------|----------|
| **支柱一：卸载** | 组件存在，未强制执行 | 6/10 | 无统一 ToolInvoker，主循环未强制工具优先 |
| **支柱二：缩减** | 模块实现，未连接 | 5/10 | ContextCompressor 未集成，RAG 结果直接注入 |
| **支柱三：隔离** | 基础具备，缺乏持久化 | 4/10 | 无 TaskManager，任务状态仅存内存 |

**核心发现**：现有代码库已经实现了许多必要组件（ContextEngine, MemoryManager, TaskPlanner, ToolOrchestrator, ReActExecutor），但这些组件**各自独立存在，未形成统一的 Agent 循环**。

---

## 二、详细诊断

### 2.1 支柱一：卸载 (Offloading) - 主动工具调用

#### 设计目标
> 将所有与外部世界的交互"卸载"出去，转变为结构化的工具调用。每个响应都应该是工具调用，而非被动的文本生成。

#### 现有组件 ✅

| 组件 | 文件 | 功能 | 评估 |
|------|------|------|------|
| **ToolRegistry** | `tools/tool_registry.py` | 工具注册和执行 | ✅ 完善 |
| **ToolOrchestrator** | `tool_orchestrator.py` | 并行工具执行，依赖协调 | ✅ 完善 |
| **ReActExecutor** | `react_executor.py` | Think-Act-Observe 循环 | ✅ 完善 |
| **TaskPlanner** | `task_planner.py` | 任务分解，依赖分析 | ✅ 完善 |

#### 存在问题 ❌

**问题 1：无统一 ToolInvoker 接口**
```
当前状态:
├── ToolRegistry.execute() - 直接工具执行
├── ToolOrchestrator.execute_plan() - 计划执行
├── ReActExecutor.execute() - ReAct 循环
└── AssistantService 中直接调用

问题: 三种不同的工具调用路径，无统一入口
```

**问题 2：主控制循环未强制工具优先**
- `assistant_service.py` 是 38K+ tokens 的巨型文件
- `chat_stream()` 方法有多个分支路径
- ReActExecutor 存在但未被主流程强制使用
- 模型通过 function calling 被动决定是否调用工具

**问题 3：缺乏"思考优先"模式**
```python
# 当前模式（被动）
user_message → model.generate() → maybe_tool_call → response

# 目标模式（主动）
user_message → THINK(what_do_I_need) → DECIDE(tool_or_answer) → ACT → OBSERVE → response
```

#### 重构建议

1. **创建 `ToolInvoker` 统一接口**
```python
class ToolInvoker:
    """统一的工具调用接口，所有工具调用必须通过此接口"""

    async def invoke(self, tool_name: str, params: dict, context: AgentContext) -> ToolResult:
        """执行工具调用，自动处理重试、错误、日志"""
        pass

    async def invoke_with_reasoning(self, user_intent: str, tools: list) -> ToolResult:
        """先推理需要什么工具，再调用"""
        pass
```

2. **重构主循环为强制 Agent 模式**
```python
class AgentLoop:
    """强制的 Think-Act-Observe 循环"""

    async def run(self, user_message: str) -> AsyncGenerator[AgentEvent, None]:
        # 1. THINK: 分析需求，决定策略
        thinking = await self.think(user_message)
        yield ThinkingEvent(thinking)

        # 2. DECIDE: 需要工具还是直接回答
        decision = await self.decide(thinking)

        if decision.needs_tool:
            # 3. ACT: 调用工具
            result = await self.tool_invoker.invoke(decision.tool, decision.params)
            yield ToolResultEvent(result)

            # 4. OBSERVE: 分析结果，决定下一步
            observation = await self.observe(result)

            if observation.needs_more:
                # 继续循环
                pass

        # 5. RESPOND: 生成最终回复
        yield ResponseEvent(await self.respond(context))
```

---

### 2.2 支柱二：缩减 (Reduction) - 高效记忆管理

#### 设计目标
> 保护宝贵的上下文窗口，只加载与当前任务最相关、信息密度最高的内容。实现"最小有效上下文"。

#### 现有组件 ✅

| 组件 | 文件 | 功能 | 评估 |
|------|------|------|------|
| **ContextManager** | `context_manager.py` | 滑动窗口，token 截断 | ✅ 基础 |
| **ContextCompressor** | `memory/compressor.py` | 智能压缩，结构保留 | ✅ 完善但未使用 |
| **ContextEngine** | `context_engine.py` | 4层上下文，KV-Cache优化 | ✅ 完善 |
| **MemoryManager** | `memory/memory_manager.py` | 三层记忆系统 | ✅ 完善但未使用 |
| **ScenarioAwareRetriever** | `scenario_aware_retriever.py` | 场景驱动检索 | ✅ 完善但未集成 |

#### 存在问题 ❌

**问题 1：ContextCompressor 未集成到主流程**
```python
# compressor.py 实现了:
# - 对话摘要生成
# - 结构保留（URLs, code blocks, tables）
# - LLM-based 压缩

# 但 ContextManager.process_history() 只做:
# - 滑动窗口截断
# - Token 计数
# 未调用 ContextCompressor!
```

**问题 2：RAG 结果直接注入，无压缩**
```python
# 当前流程:
kb_search() → results (可能很多) → 直接注入 prompt

# 问题:
# - 无相关性过滤
# - 无去重
# - 无摘要
# - 浪费 token
```

**问题 3：MemoryManager 与对话流断开**
```python
# MemoryManager 实现了:
memory.remember("key", value, layer="working")  # 存储
memory.recall("key")  # 检索
memory.search_all("query")  # 搜索

# 但 chat_stream() 中:
# - 未初始化 MemoryManager
# - 未加载用户偏好
# - 未存储中间结果
```

**问题 4：ScenarioAwareRetriever 未接入**
```python
# scenario_aware_retriever.py 实现了:
# - 基于场景的查询扩展
# - 多轮并行检索
# - 结果去重排序

# 但 _retrieve_kb_context() 中:
# 直接调用 kb_service.search()，未使用 ScenarioAwareRetriever
```

#### 重构建议

1. **集成 ContextCompressor 到 ContextManager**
```python
class ContextManager:
    def __init__(self, compressor: ContextCompressor = None):
        self.compressor = compressor

    async def process_history(self, history, ...):
        if len(history) > self.compression_threshold:
            # 使用 ContextCompressor 压缩旧消息
            compressed = await self.compressor.compress(
                messages=history[:-self.preserve_recent],
                target_tokens=self.max_compressed_tokens,
            )
            # 将压缩后的摘要 + 最近消息返回
            return ContextResult(
                summary=compressed.summary,
                preserved_elements=compressed.preserved_urls,
                recent_messages=history[-self.preserve_recent:],
            )
```

2. **集成 ScenarioAwareRetriever**
```python
async def _retrieve_kb_context(self, query, scenario, dataset_ids):
    # 使用场景感知检索器
    retrieval_context = await self.scenario_retriever.retrieve(
        user_query=query,
        scenario=scenario,
        dataset_ids=dataset_ids,
        top_k=self.config.kb_top_k,
    )

    # 返回格式化的上下文
    return retrieval_context.to_formatted_context()
```

3. **激活 MemoryManager**
```python
async def chat_stream(self, ...):
    # 初始化记忆管理器
    memory_manager = MemoryManager(
        db=self.db,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        session_id=session_id,
    )

    # 加载用户偏好
    preferences = await memory_manager.get_user_preferences()

    # 注入到上下文
    context = inject_user_preferences(base_context, preferences)
```

---

### 2.3 支柱三：隔离 (Isolation) - 健壮的任务管理

#### 设计目标
> 确保多任务并行时，各自的上下文、状态和记忆互不干扰。每个任务有独立的 task_id 和上下文边界。

#### 现有组件 ✅

| 组件 | 文件 | 功能 | 评估 |
|------|------|------|------|
| **WorkingMemory** | `working_memory.py` | 任务状态追踪，todo.md 模式 | ✅ 完善但仅内存 |
| **SessionMemoryLayer** | `memory/memory_manager.py` | 会话级持久化 | ✅ 有接口无实现 |
| **TaskPlanner** | `task_planner.py` | 任务分解和依赖 | ✅ 完善 |

#### 存在问题 ❌

**问题 1：无显式 TaskManager**
```python
# 当前状态:
# - 任务在 WorkingMemory 中追踪
# - 但 WorkingMemory 是会话级别，不是任务级别
# - 没有 task_id 作为主键的上下文隔离

# 目标:
TaskManager.create_task(goal, user_id, session_id) → task_id
TaskManager.load_task_context(task_id) → isolated_context
TaskManager.save_task_state(task_id, state)
TaskManager.resume_task(task_id)
```

**问题 2：任务状态无持久化**
```python
# WorkingMemory 当前:
class WorkingMemory:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.goal = None
        self.tasks = []  # 仅内存
        self.collected_info = []  # 仅内存

# 问题:
# - 服务重启后丢失
# - 无法跨会话恢复任务
# - 长时间任务无法中断续传
```

**问题 3：上下文泄露风险**
```python
# 当前: 一个会话中的多个"任务"共享同一个 WorkingMemory
session_1:
  ├── 任务A: "生成报告"
  │   └── 收集的信息: [data_a, data_b]
  └── 任务B: "查找价格"
      └── 可能看到任务A的 data_a, data_b  # 上下文泄露!

# 目标: 每个任务有独立的上下文边界
```

#### 重构建议

1. **创建 TaskManager 模块**
```python
class TaskManager:
    """任务隔离管理器 - 确保任务间上下文不污染"""

    def __init__(self, db: DatabaseStorage):
        self.db = db

    async def create_task(
        self,
        goal: str,
        user_id: str,
        session_id: str,
        parent_task_id: Optional[str] = None,
    ) -> str:
        """创建新任务，返回唯一 task_id"""
        task_id = f"task_{uuid.uuid4().hex[:12]}"

        await self.db.store_task(
            task_id=task_id,
            goal=goal,
            user_id=user_id,
            session_id=session_id,
            parent_task_id=parent_task_id,
            status="created",
            context={},
            created_at=datetime.now(),
        )

        return task_id

    async def load_task_context(self, task_id: str) -> TaskContext:
        """加载任务的隔离上下文 - 应用缩减原则"""
        task = await self.db.get_task(task_id)

        # 只加载该任务相关的上下文
        return TaskContext(
            task_id=task_id,
            goal=task["goal"],
            status=task["status"],
            working_memory=WorkingMemory.from_dict(task["context"]),
            collected_info=task.get("collected_info", []),
            errors=task.get("errors", []),  # 保留错误记录
        )

    async def save_task_state(self, task_id: str, state: TaskContext) -> None:
        """保存任务状态 - 支持中断续传"""
        await self.db.update_task(
            task_id=task_id,
            status=state.status,
            context=state.working_memory.to_dict(),
            collected_info=state.collected_info,
            errors=state.errors,
            updated_at=datetime.now(),
        )

    async def switch_task(
        self,
        from_task_id: str,
        to_task_id: str,
    ) -> TaskContext:
        """任务切换 - 保存旧任务，加载新任务"""
        # 保存当前任务状态
        current_context = await self.load_task_context(from_task_id)
        current_context.status = "suspended"
        await self.save_task_state(from_task_id, current_context)

        # 加载新任务
        new_context = await self.load_task_context(to_task_id)
        new_context.status = "in_progress"

        return new_context
```

2. **修改 WorkingMemory 支持持久化**
```python
class WorkingMemory:
    def __init__(self, task_id: str):  # 改为 task_id 而非 session_id
        self.task_id = task_id
        ...

    async def persist(self, db: DatabaseStorage) -> None:
        """持久化到数据库"""
        await db.store_working_memory(
            task_id=self.task_id,
            data=self.to_dict(),
        )

    @classmethod
    async def load(cls, task_id: str, db: DatabaseStorage) -> "WorkingMemory":
        """从数据库加载"""
        data = await db.get_working_memory(task_id)
        if data:
            return cls.from_dict(data)
        return cls(task_id=task_id)
```

---

## 三、集成架构设计

### 3.1 目标架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         AgentLoop (新增)                         │
│  ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐         │
│  │  THINK  │ → │ DECIDE  │ → │   ACT   │ → │ OBSERVE │ → loop  │
│  └─────────┘   └─────────┘   └─────────┘   └─────────┘         │
└─────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
           ┌────────────┐   ┌────────────┐   ┌────────────┐
           │ ToolInvoker│   │ TaskManager│   │ContextMgr  │
           │   (卸载)    │   │   (隔离)    │   │   (缩减)   │
           └────────────┘   └────────────┘   └────────────┘
                 │               │               │
                 ▼               ▼               ▼
           ┌────────────┐   ┌────────────┐   ┌────────────┐
           │ToolRegistry│   │ Database   │   │Compressor  │
           │ToolOrchest │   │ MemoryMgr  │   │ContextEng  │
           └────────────┘   └────────────┘   └────────────┘
```

### 3.2 请求处理流程（重构后）

```python
async def handle_request(user_message: str, user: User, session_id: str):
    # 1. 任务管理 (隔离)
    task_manager = TaskManager(db)
    task_id = await task_manager.create_task(
        goal=user_message,
        user_id=user.user_id,
        session_id=session_id,
    )
    task_context = await task_manager.load_task_context(task_id)

    # 2. 上下文准备 (缩减)
    context_manager = ContextManager(compressor=ContextCompressor(llm))

    # 加载用户偏好和长期记忆
    memory = MemoryManager(db, user.tenant_id, user.user_id, session_id)
    preferences = await memory.get_user_preferences()

    # 压缩历史对话
    compressed_history = await context_manager.process_history(
        history=session.messages,
        enable_compression=True,
    )

    # 场景检测和知识库检索
    scenario = await scenario_analyzer.detect(user_message)
    kb_context = await scenario_retriever.retrieve(
        user_query=user_message,
        scenario=scenario,
        dataset_ids=config.kb_dataset_ids,
    )

    # 3. Agent 循环 (卸载)
    agent_loop = AgentLoop(
        tool_invoker=ToolInvoker(tool_registry),
        task_context=task_context,
    )

    async for event in agent_loop.run(
        user_message=user_message,
        context=ContextStructure(
            system_prompt=build_system_prompt_v2(...),
            user_preferences=preferences,
            task_state=task_context.working_memory.to_markdown(),
            conversation_history=compressed_history.messages,
            current_context=kb_context.to_formatted_context(),
        ),
    ):
        # 处理事件
        yield event

        # 实时保存任务状态
        if event.type in ("tool_result", "step_finished"):
            await task_manager.save_task_state(task_id, task_context)

    # 4. 完成任务
    task_context.status = "completed"
    await task_manager.save_task_state(task_id, task_context)
```

---

## 四、重构优先级

### Phase 1：统一入口 (高优先级)
**目标**: 创建统一的 Agent 循环，强制工具优先模式

| 任务 | 文件 | 复杂度 | 影响 |
|------|------|--------|------|
| 创建 ToolInvoker | `tool_invoker.py` (新) | 中 | 高 |
| 创建 AgentLoop | `agent_loop.py` (新) | 高 | 高 |
| 重构 chat_stream | `assistant_service.py` | 高 | 高 |

### Phase 2：激活缩减 (中优先级)
**目标**: 集成压缩器和场景检索

| 任务 | 文件 | 复杂度 | 影响 |
|------|------|--------|------|
| 集成 ContextCompressor | `context_manager.py` | 低 | 中 |
| 接入 ScenarioAwareRetriever | `assistant_service.py` | 中 | 中 |
| 激活 MemoryManager | `assistant_service.py` | 中 | 中 |

### Phase 3：任务隔离 (中优先级)
**目标**: 实现任务级上下文隔离

| 任务 | 文件 | 复杂度 | 影响 |
|------|------|--------|------|
| 创建 TaskManager | `task_manager.py` (新) | 中 | 高 |
| 修改 WorkingMemory | `working_memory.py` | 低 | 中 |
| 添加数据库表 | `database/` | 中 | 中 |

### Phase 4：清理优化 (低优先级)
**目标**: 减少代码复杂度，提高可维护性

| 任务 | 文件 | 复杂度 | 影响 |
|------|------|--------|------|
| 拆分 assistant_service.py | 多个文件 | 高 | 低 |
| 统一错误处理 | 全局 | 中 | 低 |
| 添加集成测试 | `tests/` | 中 | 低 |

---

## 五、结论

### 好消息
现有代码库已经实现了绝大部分必要组件：
- ContextEngine ✅
- MemoryManager ✅
- ContextCompressor ✅
- WorkingMemory ✅
- TaskPlanner ✅
- ToolOrchestrator ✅
- ReActExecutor ✅
- ScenarioAwareRetriever ✅

### 主要问题
这些组件**各自独立，未形成统一的 Agent 循环**。需要：
1. 创建统一的 AgentLoop 作为主控制器
2. 将现有组件"串联"起来
3. 添加 TaskManager 实现任务隔离

### 工作量估算
- Phase 1 (统一入口): 3-4 天
- Phase 2 (激活缩减): 2-3 天
- Phase 3 (任务隔离): 2-3 天
- Phase 4 (清理优化): 2-3 天

**总计: 9-13 天**

---

*报告生成时间: 2026-01-21*
*基于 Manus Context Engineering 三大支柱设计*
