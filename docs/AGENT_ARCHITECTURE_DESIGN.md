# AI Agent 架构改进设计

基于 Manus Context Engineering 和 OpenAI Agent 最佳实践的架构设计。

## 1. 核心设计原则

### 1.1 ReAct 模式 (Reasoning + Acting)

```
┌─────────────────────────────────────────────────────────────┐
│                     Agent Loop                               │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌────────┐ │
│  │  Think   │───>│   Act    │───>│ Observe  │───>│ Update │ │
│  │ (流式)   │    │ (工具)   │    │ (结果)   │    │ (状态) │ │
│  └──────────┘    └──────────┘    └──────────┘    └────────┘ │
│       ▲                                              │       │
│       └──────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────┘
```

**关键点：**
- Think 阶段必须流式输出到前端，用户可以看到 AI 的思考过程
- Act 阶段调用工具，前端显示工具执行状态
- Observe 阶段将结果追加到上下文
- Update 阶段更新 WorkingMemory (todo.md 模式)

### 1.2 todo.md 模式 (Manus 核心创新)

```python
# 每次迭代后更新 working_memory
working_memory = {
    "goal": "帮用户写一份 AI 开发计划书",
    "tasks": [
        {"id": "1", "description": "分析用户需求", "status": "completed"},
        {"id": "2", "description": "制定计划大纲", "status": "completed"},
        {"id": "3", "description": "撰写详细内容", "status": "in_progress"},
        {"id": "4", "description": "生成文档文件", "status": "pending"},
    ],
    "collected_info": [
        {"key": "需求", "value": "本地 AI 模型部署", "source": "user_input"}
    ],
    "notes": ["用户希望得到 DOCX 格式"]
}
```

**为什么有效：**
- 将目标"复述"到上下文末端，防止 "lost-in-the-middle" 问题
- 复杂任务平均 ~50 次工具调用，需要持续追踪目标
- 用户可以实时看到进度

## 2. 文档生成改进方案

### 2.1 当前问题

```
用户: "帮我写一个计划书"
     ↓
AI: 直接调用 generate_document(title="计划书", content="简单大纲")
     ↓
结果: 内容简陋，用户看不到思考过程
```

### 2.2 改进后的流程

```
用户: "帮我写一个计划书"
     ↓
AI Think (流式): "我来分析这个任务..."
     ↓
SSE: working_memory_update (任务1: 分析需求 - in_progress)
     ↓
AI Think (流式): "首先，我需要确定计划书的结构..."
     ↓
AI Think (流式): [完整的计划书内容，流式输出]
     ↓
SSE: working_memory_update (任务2: 撰写内容 - completed)
     ↓
AI Act: generate_document(title="...", content="[完整内容]")
     ↓
SSE: artifact_created
     ↓
结果: 用户看到完整思考过程 + 详细文档
```

## 3. 实现方案

### 3.1 后端改进：ReAct Agent Executor

```python
# src/services/assistant/react_executor.py

class ReActExecutor:
    """
    ReAct 模式的 Agent 执行器

    实现 Think -> Act -> Observe -> Update 循环
    """

    async def execute_with_react(
        self,
        user_message: str,
        config: ChatConfig,
        stream_callback: Callable,
    ) -> AsyncGenerator[StreamEvent, None]:
        """
        使用 ReAct 模式执行任务
        """
        # 初始化 working memory
        working_memory = WorkingMemory(
            goal=user_message,
            tasks=[],
            collected_info=[],
            notes=[]
        )

        # 发送初始任务规划
        plan = await self.task_planner.create_plan(user_message, self.available_tools)
        working_memory.tasks = [
            {"id": t.id, "description": t.description, "status": "pending"}
            for t in plan.tasks
        ]

        yield StreamEvent("task_planning", {
            "goal": plan.goal,
            "tasks": working_memory.tasks,
            "parallel_groups": plan.parallel_groups
        })

        # ReAct 循环
        for group in plan.parallel_groups:
            for task_id in group:
                task = plan.get_task(task_id)

                # 更新状态: in_progress
                self._update_task_status(working_memory, task_id, "in_progress")
                yield StreamEvent("working_memory_update", working_memory.to_dict())

                # THINK: 流式输出思考过程
                async for chunk in self._think(task, working_memory):
                    yield StreamEvent("text_delta", chunk)

                # ACT: 执行工具
                if task.tool:
                    yield StreamEvent("tool_call", {
                        "id": task.id,
                        "name": task.tool,
                        "arguments": task.parameters,
                        "status": "running"
                    })

                    result = await self._execute_tool(task)

                    yield StreamEvent("tool_result", {
                        "tool_call_id": task.id,
                        "name": task.tool,
                        "result": result,
                    })

                # OBSERVE & UPDATE
                self._update_task_status(working_memory, task_id, "completed")
                yield StreamEvent("working_memory_update", working_memory.to_dict())
```

### 3.2 文档生成特殊处理

```python
# 在 assistant_service.py 中

async def _handle_document_generation(
    self,
    user_message: str,
    config: ChatConfig,
) -> AsyncGenerator[StreamEvent, None]:
    """
    文档生成的两阶段处理

    阶段1: Think - 流式输出完整文档内容
    阶段2: Act - 调用 generate_document 工具
    """

    # 阶段1: 让 AI 先思考并输出完整内容
    thinking_prompt = f"""
用户请求: {user_message}

请先完整撰写文档内容，然后我会帮你生成文件。

要求:
1. 使用 Markdown 格式
2. 包含详细的章节和内容
3. 不少于 500 字
4. 内容要有深度和可操作性

请开始撰写:
"""

    document_content = ""
    async for chunk in self._stream_llm_response(thinking_prompt):
        document_content += chunk
        yield StreamEvent("text_delta", chunk)

    # 阶段2: 自动调用工具生成文档
    if len(document_content) > 100:  # 确保有足够内容
        yield StreamEvent("status", "正在生成文档文件...")

        result = await self.tool_executor.execute(ToolCallRequest(
            call_id=str(uuid.uuid4()),
            tool_name="generate_document",
            arguments={
                "title": self._extract_title(user_message),
                "content": document_content,
                "format": "docx"
            }
        ))

        if result.success:
            yield StreamEvent("artifact_created", {
                "artifact_id": result.metadata.get("artifact_id"),
                "type": "document",
                "title": result.metadata.get("title"),
                "download_url": result.metadata.get("download_url"),
            })
```

### 3.3 前端改进：流式状态显示

```typescript
// useChatSession.ts 中的 ReAct 状态处理

interface ReActState {
  phase: 'thinking' | 'acting' | 'observing';
  currentTask?: string;
  thinkingContent: string;
}

// 处理 SSE 事件
case SSEEventType.STATUS:
  // 显示当前阶段状态
  setReactState(prev => ({
    ...prev,
    phase: event.data.phase,
    currentTask: event.data.task,
  }));
  break;

case SSEEventType.THINKING_DELTA:
  // 流式显示思考内容
  setReactState(prev => ({
    ...prev,
    thinkingContent: prev.thinkingContent + event.data,
  }));
  // 同时更新消息内容
  setMessages(prev => prev.map(m =>
    m.id === currentMessageId
      ? { ...m, content: m.content + event.data }
      : m
  ));
  break;
```

## 4. KV-Cache 优化

### 4.1 稳定的 System Prompt

```python
# 避免在 system prompt 中包含易变内容
# BAD:
system_prompt = f"当前时间: {datetime.now()}"

# GOOD:
system_prompt = """你是一个 AI 助手..."""
# 时间信息放在 user message 中
```

### 4.2 工具定义稳定性

```python
# 避免动态修改工具定义
# 使用 logit masking 而不是删除工具
tool_mask = ["generate_document", "kb_search"]  # 当前允许的工具
```

## 5. 错误处理和恢复

### 5.1 保留错误信息

```python
# 错误信息保留在上下文中，让模型学习
if tool_result.error:
    context.append({
        "role": "tool",
        "content": f"Error: {tool_result.error}\nStack trace: {tool_result.stack_trace}"
    })
    # 不要删除错误，让模型看到并避免重复
```

### 5.2 自动重试逻辑

```python
async def execute_with_retry(self, task, max_retries=3):
    for attempt in range(max_retries):
        result = await self._execute_tool(task)
        if result.success:
            return result

        # 更新 working memory，记录失败
        self.working_memory.notes.append(
            f"任务 {task.id} 第 {attempt+1} 次尝试失败: {result.error}"
        )

        # 让模型看到错误并调整
        yield StreamEvent("text_delta", f"\n遇到错误: {result.error}，正在重试...\n")

    return result
```

## 6. 实施路线图

### Phase 1: 文档生成改进 (立即)
- [ ] 实现两阶段文档生成流程
- [ ] 添加 status 事件用于阶段提示
- [ ] 前端显示当前阶段状态

### Phase 2: ReAct 循环 (1-2周)
- [ ] 实现 ReActExecutor
- [ ] 集成 TaskPlanner 和 WorkingMemory
- [ ] 前端 AgentTaskTimeline 集成

### Phase 3: 上下文优化 (2-3周)
- [ ] KV-Cache 优化
- [ ] 错误保留和恢复
- [ ] 多智能体协调

## 参考资料

- [Context Engineering for AI Agents - Manus](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- [OpenAI Building Agents](https://developers.openai.com/tracks/building-agents/)
- [OpenManus Technical Analysis](https://llmmultiagents.com/en/blogs/OpenManus_Technical_Analysis)
- [Manus Technical Investigation](https://gist.github.com/renschni/4fbc70b31bad8dd57f3370239dccd58f)
