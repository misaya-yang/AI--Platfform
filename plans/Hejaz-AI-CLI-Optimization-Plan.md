# Hejaz AI CLI 全面优化计划书

> **版本**: v1.0 | **日期**: 2026-04-03 | **目标执行者**: Claude Code
> **审查范围**: CLI (TypeScript/Ink) + Gateway Backend (Python/FastAPI) + SDK
> **对标产品**: Claude Code CLI

---

## 一、审计发现：测试对话暴露的核心问题

基于对测试对话的逐条分析，发现以下关键缺陷：

### 问题 P1：时间感知完全缺失（严重）

**现象**：用户问"今天湖人打雷霆战况"，AI 返回 2024 年的数据，且自信地声称"今天是北京时间2024年11月30日"。

**根因**：
- `system_prompt_v2.py` 中 **没有注入当前日期/时间**
- `tavily_search.py` 向 Tavily API 发送请求时 **没有传入 date 参数**
- LLM 的 system prompt 没有 `Today is 2026-04-03` 这样的时间锚点
- 搜索 query 没有自动附加年份信息

**影响**：所有涉及时效性的对话都会返回过时信息，用户信任度归零。

### 问题 P2：搜索结果无法正确合成（严重）

**现象**：第三次搜索（同样的问题重试）直接返回"我已完成工具执行，但模型未返回最终文本"，5 次 web search 全部执行但无输出。

**根因**：
- 多次并行 search_web 调用消耗大量 token（4755 input tokens）
- 模型只返回 30 个 output tokens，说明 context window 被搜索结果挤满
- 缺乏搜索结果的 token 预算控制
- 没有搜索结果的摘要压缩机制

### 问题 P3：无 OS Agent 能力——文件操作纯靠嘴（严重）

**现象**：用户说"帮我在桌面写一个快速排序的python代码"，AI 只在聊天中输出代码。追问"生成文件到桌面"时，AI 回复"我是云端AI助手，无法直接访问本地系统"。

**根因**：
- CLI 端已有 8 个 OS 工具（read_file, write_file, edit_file, bash, glob, grep, list_dir, tree）
- 但 **gateway 端的 system prompt 没有告知 LLM 可以使用这些工具**
- `agent/loop.ts` 虽然发送了 `os_tools` schema，但 gateway 的 prompt 工程没有正确引导 LLM 优先使用本地工具
- LLM 仍然按照"云端助手"的人设在回复

### 问题 P4：首次回复过度冗余

**现象**：用户说"你好"，AI 返回一个完整的 JSON 状态块 + 能力清单，消耗 180 tokens。

**根因**：system prompt 中的 `CORE_BEHAVIOR` 或 `AGENT_IDENTITY` 部分鼓励 verbose 输出。

### 问题 P5：纠错后不会自动重试

**现象**：用户指出"今天是2026年"后，AI 道歉但没有自动重新搜索正确信息，而是反问用户"您能具体说明一下哪个结果有问题吗？"

**根因**：缺少 error recovery / auto-retry 机制。

---

## 二、与 Claude Code 的能力差距分析

| 能力维度 | Claude Code | Hejaz AI CLI | 差距评分 |
|---------|------------|-------------|---------|
| **本地文件读写** | 原生支持，自动执行 | 有工具但 LLM 不知道用 | 🔴 严重 |
| **Shell 命令执行** | 原生 Bash 工具 | 有 bash 工具但不主动使用 | 🔴 严重 |
| **时间感知** | system prompt 注入当前时间 | 完全缺失 | 🔴 严重 |
| **上下文压缩** | 智能 /compact（LLM 生成摘要） | 暴力截断前 200 字符 | 🟡 中等 |
| **权限系统** | 3 级权限 + 沙箱 | 有但不够精细 | 🟢 轻微 |
| **MCP 集成** | 原生支持 | 已实现 JSON-RPC 2.0 | 🟢 良好 |
| **Agent 系统** | Task/Explore/Plan 三种 agent | 已有 explore/task/plan | 🟢 良好 |
| **Skill 系统** | SKILL.md 格式 | 已实现但后端管理 | 🟡 中等 |
| **代码执行反馈** | 本地执行 + 实时输出 | Docker 远程沙箱 | 🟡 中等 |
| **项目记忆** | CLAUDE.md + rules/ | HEJAZ.md + rules/（已实现） | 🟢 良好 |
| **流式输出** | 实时 streaming | SSE streaming（已实现） | 🟢 良好 |
| **搜索质量** | 精准 + 时间感知 | 过时 + 无 token 预算 | 🔴 严重 |
| **错误恢复** | 自动重试 + 降级 | 直接失败 | 🔴 严重 |
| **TodoList/进度** | 内置进度追踪 | 无 | 🟡 中等 |
| **Diff 展示** | 完整 patch 视图 | 截断 50 字符 | 🟡 中等 |

---

## 三、优化计划（按优先级排序）

### Phase 1：紧急修复（预计 1-2 天）

#### 1.1 注入当前时间到 System Prompt

**文件**: `src/services/assistant/prompts/system_prompt_v2.py`

**操作**:
在 system prompt 的最前面（static section 之后、dynamic section 开始处）注入：

```python
import datetime

def get_time_context() -> str:
    now = datetime.datetime.now()
    return f"""
<current_time>
Current date and time: {now.strftime('%Y-%m-%d %H:%M:%S')} ({now.strftime('%A')})
Timezone: Server local time
IMPORTANT: Always use this date as "today". Never assume a different date.
When performing web searches, include the year {now.year} in time-sensitive queries.
</current_time>
"""
```

在 `build_system_prompt()` 或等价的组装函数中，将 `get_time_context()` 插入到 system prompt 的 **SYSTEM_CAPABILITY** 段之前。

#### 1.2 修复 Web Search 的时间感知

**文件**: `src/services/assistant/tools/tavily_search.py`

**操作**:
1. 在搜索 query 前自动检测是否包含时间敏感词（"今天"、"最新"、"今年"、"当前"等）
2. 如果检测到，在 query 后追加当前年份
3. 设置 Tavily 的 `days` 参数限制搜索范围

```python
import re
from datetime import datetime

TIME_SENSITIVE_PATTERNS = [
    r'今天|today|最新|latest|当前|current|本周|this week|今年|this year|昨天|yesterday',
    r'近期|recently|刚刚|just now|现在|now'
]

def enhance_time_query(query: str) -> tuple[str, int | None]:
    """增强时间敏感查询，返回 (enhanced_query, days_limit)"""
    is_time_sensitive = any(re.search(p, query, re.IGNORECASE) for p in TIME_SENSITIVE_PATTERNS)
    if is_time_sensitive:
        year = datetime.now().year
        enhanced = f"{query} {year}"
        return enhanced, 7  # 限制最近 7 天
    return query, None
```

在 `search()` 方法中，调用 `enhance_time_query()` 处理 query，并将 `days` 参数传给 Tavily payload。

#### 1.3 修复 OS Agent 的 System Prompt 引导

**文件**: `src/services/assistant/prompts/system_prompt_v2.py`

**操作**:
在 `SYSTEM_CAPABILITY` 段中，当检测到 `os_agent_enabled=True` 时，追加以下指令：

```python
OS_AGENT_INSTRUCTIONS = """
<os_agent>
You have direct access to the user's local file system and terminal through OS tools.
When the user asks you to create files, write code, or perform file operations:
1. ALWAYS use the appropriate OS tool (write_file, edit_file, bash) to execute directly
2. NEVER say "I cannot access your local system" — you CAN and SHOULD
3. NEVER just print code in chat when the user asks to "create a file" or "write to disk"
4. For file paths: use the user's working directory or the path they specify
5. Ask for confirmation before writing to sensitive locations

Available OS Tools:
- read_file: Read file contents (auto-approved)
- write_file: Create or overwrite files (requires user confirmation)
- edit_file: Modify existing files (requires user confirmation)
- bash: Execute shell commands (requires user confirmation)
- glob: Find files by pattern (auto-approved)
- grep: Search file contents (auto-approved)
- list_dir: List directory contents (auto-approved)
- tree: Show directory tree (auto-approved)

CRITICAL: You are NOT a cloud-only assistant. You have local execution capabilities.
When in doubt, prefer local tool execution over just chatting about it.
</os_agent>
"""
```

#### 1.4 修复搜索结果的 Token 溢出

**文件**: `src/services/assistant/tools/tavily_search.py` 及调用层

**操作**:
1. 限制每次搜索返回结果的 token 数（max 800 tokens per result）
2. 限制并行搜索数量（max 3 次）
3. 对搜索结果做 LLM 摘要压缩（或截断到合理长度）

```python
MAX_SEARCH_RESULTS = 5
MAX_CONTENT_LENGTH = 1500  # chars per result
MAX_TOTAL_SEARCH_TOKENS = 3000

def truncate_search_results(results: list[dict], max_total_chars: int = 6000) -> list[dict]:
    """截断搜索结果以控制 token 预算"""
    truncated = []
    total_chars = 0
    for r in results:
        content = r.get("content", "")[:MAX_CONTENT_LENGTH]
        if total_chars + len(content) > max_total_chars:
            break
        r["content"] = content
        truncated.append(r)
        total_chars += len(content)
    return truncated
```

---

### Phase 2：体验提升（预计 3-5 天）

#### 2.1 重写 /compact 命令

**文件**: `sdk/cli/src/app.tsx`（compact 处理段）

**当前实现**（极其粗暴）:
```typescript
// 当前：取每条消息前 200 字符，总共截断到 1500 字符
const summary = userMsgs.map(m => `${m.role}: ${m.content.slice(0, 200)}`).join("\n");
setMessages([{ role: "system", content: `[Compacted]\n\n${summary.slice(0, 1500)}` }]);
```

**优化方案**: 调用 gateway 的 LLM 做智能摘要：

```typescript
if (result.action === "compact") {
  const userMsgs = messages.filter(m => m.role !== "system");
  if (userMsgs.length <= 2) return "对话太短，无需压缩。";

  // 发送到 gateway 做 LLM 摘要
  const summaryResponse = await chat.summarize({
    messages: userMsgs,
    instruction: "请用 500 字以内总结以上对话的关键信息和上下文，保留所有重要的文件路径、代码片段和决策。"
  });

  setMessages([
    { role: "system", content: `[对话已压缩 ${userMsgs.length} 条消息]\n\n${summaryResponse}` }
  ]);
}
```

需要在 gateway 端新增 `/api/v1/assistant/summarize` 端点。

#### 2.2 增加自动错误恢复机制

**文件**: `src/services/assistant/assistant_service.py`

**操作**:
当 LLM 被用户纠正（检测到"你搞错了"、"不对"、"今天是XX年"等纠错模式）时，自动附加重试指令：

```python
CORRECTION_PATTERNS = [
    r'你搞错了|不对|错了|有问题|你的.*有误',
    r'今天是\d{4}年|现在是\d{4}',
    r"that's wrong|incorrect|you're wrong|not right"
]

def detect_user_correction(message: str) -> bool:
    return any(re.search(p, message, re.IGNORECASE) for p in CORRECTION_PATTERNS)

# 在 chat_stream() 中：
if detect_user_correction(user_message):
    # 自动在下一轮 system context 中追加纠错指令
    correction_context = (
        "The user has corrected your previous response. "
        "Please acknowledge the correction, re-execute any necessary tool calls "
        "with corrected parameters, and provide an updated answer. "
        "Do NOT just apologize — actually fix the issue."
    )
```

#### 2.3 优化首次回复（去除冗余）

**文件**: `src/services/assistant/prompts/system_prompt_v2.py`

**操作**:
在 `CORE_BEHAVIOR` 段中添加：

```python
GREETING_RULES = """
<greeting_behavior>
When the user sends a simple greeting (e.g., "你好", "hi", "hello"):
- Respond briefly and warmly (1-2 sentences max)
- Do NOT dump system status, JSON blocks, or capability lists
- Do NOT list your features unless asked
- Simply greet back and ask how you can help
Example: "你好！有什么我可以帮你的吗？"
</greeting_behavior>
"""
```

#### 2.4 增强 edit_file 的 Diff 展示

**文件**: `sdk/cli/src/tools/edit-file.ts` 及 `sdk/cli/src/ui/` 相关组件

**当前问题**: Diff 只显示截断到 50 字符的 old/new string。

**优化方案**:
```typescript
import { diffLines } from 'diff'; // 需要安装 diff 包

function renderDiff(oldContent: string, newContent: string): string {
  const changes = diffLines(oldContent, newContent);
  return changes.map(part => {
    const prefix = part.added ? '+' : part.removed ? '-' : ' ';
    const color = part.added ? 'green' : part.removed ? 'red' : 'gray';
    return `${prefix} ${part.value}`;
  }).join('');
}
```

在确认提示中显示完整 diff 而非截断字符串。

#### 2.5 增加 TodoList / 任务追踪能力

**新增文件**: `sdk/cli/src/tools/todo.ts`

**操作**: 参考 Claude Code 的 TodoWrite 工具，新增一个本地任务追踪工具：

```typescript
interface Todo {
  content: string;
  status: "pending" | "in_progress" | "completed";
  activeForm: string;
}

// 作为 OS tool 注册
const todoTool: ToolDefinition = {
  name: "todo_write",
  description: "Create and manage a task list to track progress on complex tasks",
  parameters: {
    type: "object",
    properties: {
      todos: {
        type: "array",
        items: {
          type: "object",
          properties: {
            content: { type: "string" },
            status: { type: "string", enum: ["pending", "in_progress", "completed"] },
            activeForm: { type: "string" }
          }
        }
      }
    }
  }
};
```

在 CLI 的 UI 中渲染为实时更新的进度条/清单。

---

### Phase 3：架构增强（预计 1-2 周）

#### 3.1 增加本地 LLM 支持（Ollama 集成）

**新增文件**: `src/services/llm/providers/ollama_provider.py`

**操作**:
在 ModelRegistry 中新增 Ollama provider，支持离线场景：

```python
class OllamaProvider:
    """本地 Ollama LLM 提供者"""
    base_url = "http://localhost:11434"

    async def chat_stream(self, messages, tools=None, **kwargs):
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST", f"{self.base_url}/api/chat",
                json={"model": kwargs.get("model", "qwen2.5:7b"),
                      "messages": messages, "stream": True, "tools": tools}
            ) as response:
                async for line in response.aiter_lines():
                    yield self._parse_delta(line)
```

CLI 端增加 `/model ollama:qwen2.5:7b` 支持。

#### 3.2 重构搜索系统——多源聚合 + 结果验证

**新增文件**: `src/services/assistant/tools/search_aggregator.py`

**操作**:
不再依赖单一 Tavily，建立搜索聚合层：

```python
class SearchAggregator:
    """多源搜索聚合器"""

    async def search(self, query: str, context: SearchContext) -> SearchResult:
        # 1. 增强 query（加时间、加上下文）
        enhanced_query = self.enhance_query(query, context.current_date)

        # 2. 并行搜索多个源
        results = await asyncio.gather(
            self.tavily_search(enhanced_query),
            self.serper_search(enhanced_query),  # 备选搜索引擎
            return_exceptions=True
        )

        # 3. 去重 + 排序 + token 预算控制
        merged = self.merge_and_deduplicate(results)
        truncated = self.apply_token_budget(merged, max_tokens=3000)

        # 4. 可选：LLM 摘要压缩
        if context.compress_results:
            truncated = await self.summarize_results(truncated, query)

        return truncated
```

#### 3.3 增加 /run 命令——本地代码即时执行

**新增 slash command**: `/run <language> <code>` 或 `/run <file>`

**文件**: `sdk/cli/src/agent/slash-commands.ts`

**操作**:
```typescript
case "run": {
  const [lang, ...rest] = args;
  const code = rest.join(" ");

  if (!lang) return "用法: /run python <code> 或 /run <file.py>";

  // 如果是文件路径
  if (lang.includes(".")) {
    const result = await executeTool("bash", {
      command: `python3 "${lang}"`,  // 或根据扩展名选择解释器
      timeout: 30000
    });
    return result;
  }

  // 如果是内联代码
  const interpreters: Record<string, string> = {
    python: "python3 -c",
    node: "node -e",
    js: "node -e",
    bash: "bash -c",
    sh: "sh -c"
  };
  const interpreter = interpreters[lang];
  if (!interpreter) return `不支持的语言: ${lang}`;

  const result = await executeTool("bash", {
    command: `${interpreter} ${JSON.stringify(code)}`,
    timeout: 30000
  });
  return result;
}
```

#### 3.4 增加 /init 命令——项目初始化

**新增 slash command**: `/init`

**操作**: 类似 Claude Code 的项目初始化，在当前目录生成 `HEJAZ.md`：

```typescript
case "init": {
  const cwd = process.cwd();
  const hejazMdPath = path.join(cwd, "HEJAZ.md");

  if (fs.existsSync(hejazMdPath)) {
    return "HEJAZ.md 已存在。使用编辑器修改或运行 /config 查看配置。";
  }

  // 扫描项目结构生成初始 HEJAZ.md
  const tree = await executeTool("tree", { path: cwd, depth: 2 });
  const content = `# Project: ${path.basename(cwd)}

## 项目描述
[请描述你的项目]

## 技术栈
[自动检测到的目录结构]
${tree}

## 开发规范
- [添加你的编码规范]

## 常用命令
- [添加常用的构建/测试命令]
`;

  await executeTool("write_file", { path: hejazMdPath, content });
  return `已创建 ${hejazMdPath}，请根据你的项目需求编辑内容。`;
}
```

#### 3.5 增加 Streaming 中断 + 优雅恢复

**文件**: `sdk/cli/src/app.tsx`（流处理部分）

**当前问题**: Ctrl+C 直接中断，没有恢复机制。

**优化方案**:
```typescript
// 第一次 Ctrl+C: 中断当前 LLM 输出，保留上下文
// 第二次 Ctrl+C (2秒内): 退出程序
// 中断后显示: "[输出已中断] 输入 /continue 继续，或输入新消息"

case "continue": {
  // 将中断前的部分输出作为 assistant message 保存
  // 追加 "Please continue from where you left off." 重新发送
  const lastPartial = interruptedMessageRef.current;
  if (!lastPartial) return "没有被中断的输出可以继续。";
  // 重新发送请求
  await sendMessage("请从上次中断的地方继续。", { resumeContext: lastPartial });
}
```

#### 3.6 增加命令历史持久化

**文件**: `sdk/cli/src/app.tsx`（输入处理部分）

**操作**:
```typescript
const HISTORY_FILE = path.join(os.homedir(), '.hejaz', 'command_history.json');
const MAX_HISTORY = 1000;

function loadHistory(): string[] {
  try {
    return JSON.parse(fs.readFileSync(HISTORY_FILE, 'utf-8'));
  } catch { return []; }
}

function saveHistory(history: string[]) {
  const dir = path.dirname(HISTORY_FILE);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(HISTORY_FILE, JSON.stringify(history.slice(-MAX_HISTORY)));
}
```

---

### Phase 4：高级功能（预计 2-4 周）

#### 4.1 增加 Computer Use 能力

**描述**: 集成屏幕截图 + 鼠标/键盘控制，实现桌面自动化。

**实现路径**:
1. 新增 MCP Server: `@hejaz/desktop-agent`
2. 使用 `screenshot-desktop` (Node.js) 或 `pyautogui` (Python) 做底层
3. 工具列表: `screenshot`, `click`, `type`, `scroll`, `key`
4. 集成到 OS tools 中，LLM 可以通过 tool calling 控制桌面

#### 4.2 增加 /chat 多会话管理

**描述**: 支持同时维护多个对话上下文，类似 tmux 的多窗口。

**新增命令**:
- `/chat list` — 列出活跃会话
- `/chat switch <id>` — 切换会话
- `/chat new <name>` — 创建命名会话
- `/chat delete <id>` — 删除会话

#### 4.3 增加 /diff 命令

**描述**: 查看 AI 对文件的所有修改记录。

```typescript
case "diff": {
  // 使用 git diff 或内部变更记录
  const changes = fileChangeLog.getAll();
  for (const change of changes) {
    renderColorDiff(change.before, change.after, change.filePath);
  }
}
```

#### 4.4 增加 /cost 命令

**描述**: 实时显示当前会话的 token 消耗和费用估算。

```typescript
case "cost": {
  const usage = sessionUsage;
  const cost = calculateCost(usage.inputTokens, usage.outputTokens, currentModel);
  return `
当前会话消耗:
  输入: ${usage.inputTokens} tokens
  输出: ${usage.outputTokens} tokens
  预估费用: $${cost.toFixed(4)}
  模型: ${currentModel}
`;
}
```

#### 4.5 增加 Pipe 模式

**描述**: 支持 Unix pipe 输入，让 CLI 可以与其他工具组合使用。

```bash
# 示例用法
cat error.log | hejaz "分析这个错误日志"
git diff | hejaz "review 这个代码变更"
curl api.example.com/data | hejaz "总结这个 API 返回的数据"
```

**实现**: 在 `cli.tsx` 的入口检测 stdin 是否为 pipe 模式：

```typescript
if (!process.stdin.isTTY) {
  // Pipe mode: 读取 stdin 作为上下文
  const input = fs.readFileSync(0, 'utf-8');
  const prompt = process.argv.slice(2).join(' ');
  // 非交互模式：发送 input + prompt，输出结果后退出
  await oneShot(input, prompt);
  process.exit(0);
}
```

---

## 四、实施优先级矩阵

```
          紧急                    不紧急
  ┌──────────────────┬──────────────────┐
  │  P1: 时间注入      │  P3.4: /init 命令  │
重│  P1: 搜索时间感知   │  P3.5: 中断恢复    │
要│  P1: OS Agent引导  │  P3.6: 历史持久化   │
  │  P1: Token溢出修复  │  P4.5: Pipe模式    │
  ├──────────────────┼──────────────────┤
  │  P2.3: 首条回复优化  │  P4.1: Computer Use │
不│  P2.2: 错误恢复     │  P4.2: 多会话管理   │
重│  P2.1: /compact重写 │  P4.3: /diff 命令   │
要│  P2.5: TodoList    │  P4.4: /cost 命令   │
  └──────────────────┴──────────────────┘
```

---

## 五、验收标准

每个 Phase 完成后，用以下测试对话验证：

### Phase 1 验收测试

```
测试 1: 时间感知
  输入: "今天是几号？"
  期望: 正确返回 2026 年的日期

测试 2: 搜索时效性
  输入: "今天湖人的比赛结果"
  期望: 返回 2026 年 4 月 3 日的比赛信息（如有），而非历史数据

测试 3: OS Agent 文件操作
  输入: "在桌面创建一个 hello.py 文件，内容是 print('hello world')"
  期望: 调用 write_file 工具直接创建文件，而非在聊天中打印代码

测试 4: 搜索 Token 控制
  输入: 连续发送 3 个搜索请求
  期望: 每次搜索都能正常返回文本总结，不出现"未返回最终文本"

测试 5: 简洁问候
  输入: "你好"
  期望: 1-2 句自然回复，不包含 JSON 或能力清单
```

### Phase 2 验收测试

```
测试 6: /compact 智能压缩
  操作: 进行 10 轮对话后执行 /compact
  期望: 压缩后的摘要保留关键信息和上下文

测试 7: 纠错自动重试
  输入: "你搜错了，应该搜2026年的"
  期望: AI 自动重新搜索并返回正确结果

测试 8: Diff 展示
  操作: 让 AI 编辑一个文件
  期望: 确认提示显示完整 diff，不截断
```

---

## 六、技术债务清单（顺便修复）

| 项目 | 文件 | 描述 |
|------|------|------|
| grep 用 JS regex 代替 ripgrep | `sdk/cli/src/tools/grep.ts` | 性能差，大项目搜索慢。考虑使用 `@vscode/ripgrep` |
| tool result 截断 500 字符 | `sdk/cli/src/agent/loop.ts` L150 | 太短，关键信息可能丢失。提升到 2000 |
| 没有 rate limiting per user | `src/core/ratelimit/` | 单用户可耗尽全局配额 |
| artifact download 只打印 URL | `sdk/cli/src/agent/slash-commands.ts` | 应自动下载到当前目录 |
| MCP tools 没有权限检查 | `sdk/cli/src/mcp/manager.ts` | MCP 工具绕过权限系统 |
| 没有优雅降级 | `src/services/assistant/assistant_service.py` | KB/搜索失败时整个 chat 失败 |

---

## 七、执行指令（给 Claude Code）

将此文档交给 Claude Code 时，使用以下 prompt：

```
请按照 plans/Hejaz-AI-CLI-Optimization-Plan.md 中的优化计划执行。
从 Phase 1 开始，逐个实施每个修复项。
每完成一项，运行对应的验收测试确认效果。
完成 Phase 1 全部项后，继续 Phase 2。
如果遇到架构冲突或需要确认的设计决策，暂停并询问我。
```

---

*本文档由系统审计自动生成，基于对 Hejaz AI Gateway 完整代码库的分析。*
