# AI 助手 Agent + Code Interpreter 设计方案

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为 AI 助手添加代码执行能力，打造类似 GPT-4 Code Interpreter 的全能体验

**Architecture:** 基于现有 AssistantService 扩展，新增 CodeExecutorService 处理 Docker 沙箱执行，前端增加 Artifacts 面板展示执行结果

**Tech Stack:** Docker SDK (Python), Monaco Editor, AG-Grid, FastAPI SSE

---

## 一、需求总结

| 项目 | 选择 |
|------|------|
| 核心方向 | Agent 能力 |
| 核心功能 | 代码执行 (Code Interpreter) |
| 执行环境 | Docker 沙箱 |
| 结果展示 | Artifacts 面板 |
| 知识库联动 | 是，代码可访问 KB 文档 |
| 执行模式 | 自动执行 |

---

## 二、整体架构

```
┌────────────────────────────────────────────────────────────────┐
│                          前端 (React)                           │
├─────────────────┬──────────────────────────┬──────────────────┤
│   会话列表      │       聊天区域           │   Artifacts 面板  │
│   - 历史会话    │   - 消息显示             │   - 代码预览     │
│   - 新建对话    │   - 代码执行状态         │   - 图表渲染     │
│                 │   - 输入框               │   - 表格展示     │
│                 │                          │   - 文件下载     │
└─────────────────┴──────────────────────────┴──────────────────┘
                              ↓
┌────────────────────────────────────────────────────────────────┐
│                    AssistantService (扩展)                      │
│   ├─ chat_stream() ─────→ 现有流式对话                         │
│   ├─ execute_code() ────→ 新增代码执行工具                     │
│   └─ ToolRegistry ──────→ 注册 code_executor 工具              │
└────────────────────────────────────────────────────────────────┘
                              ↓
┌────────────────────────────────────────────────────────────────┐
│                    CodeExecutorService (新增)                   │
│   ├─ Docker 容器管理                                           │
│   ├─ 沙箱安全策略                                              │
│   ├─ 文件 I/O 处理                                             │
│   └─ 与 KnowledgeService 联动                                  │
└────────────────────────────────────────────────────────────────┘
```

**核心设计原则**：
1. **复用现有架构**：在 AssistantService 基础上扩展，不重写
2. **工具化集成**：代码执行作为一个 Tool 注册到 ToolRegistry
3. **安全隔离**：每次执行在独立 Docker 容器中，资源限制 + 网络隔离
4. **KB 联动**：沙箱可访问用户选中的知识库文档

---

## 三、后端设计

### 3.1 CodeExecutorService

```python
# src/services/assistant/code_executor.py

@dataclass
class CodeExecutionConfig:
    """代码执行配置"""
    container_image: str = "python:3.11-slim"
    memory_limit: str = "512m"
    cpu_limit: float = 0.5
    timeout_seconds: int = 30
    network_disabled: bool = True

@dataclass
class CodeExecutionResult:
    """执行结果"""
    success: bool
    stdout: str
    stderr: str
    output_files: List[OutputFile]  # 生成的图表、表格等
    execution_time_ms: int
    memory_used_mb: float

@dataclass
class OutputFile:
    """输出文件"""
    filename: str
    content_type: str  # image/png, text/csv, etc.
    size_bytes: int
    url: str  # 临时下载 URL

class CodeExecutorService:
    """Docker 沙箱代码执行服务"""

    def __init__(self, config: CodeExecutionConfig = None):
        self.config = config or CodeExecutionConfig()
        self.docker_client = docker.from_env()

    async def execute(
        self,
        code: str,
        language: str = "python",
        files: List[FileAttachment] = None,
        kb_documents: List[KBDocument] = None,
    ) -> CodeExecutionResult:
        """
        执行代码并返回结果

        流程：
        1. 创建临时工作目录
        2. 准备输入文件（用户上传 + KB 文档）
        3. 启动 Docker 容器
        4. 执行代码
        5. 收集输出（stdout, stderr, 生成的文件）
        6. 清理容器
        """
        pass

    async def prepare_kb_documents(
        self,
        dataset_ids: List[str],
        query: str,
        top_k: int = 10
    ) -> List[KBDocument]:
        """检索并准备 KB 文档供沙箱使用"""
        pass
```

### 3.2 安全策略

- **容器隔离**：每次执行使用独立容器
- **网络隔离**：禁止外网访问 (`network_disabled=True`)
- **资源限制**：
  - 内存：512MB
  - CPU：0.5 核
  - 执行时间：30 秒
- **文件系统**：
  - 只读挂载系统目录
  - 工作目录可写但大小受限
- **禁止特权模式**

### 3.3 工具注册

```python
# 在 ToolRegistry 中注册
code_executor_tool = ToolDefinition(
    name="execute_python_code",
    description="执行 Python 代码，可以进行数据分析、生成图表、处理文件等",
    parameters=[
        ToolParameter(name="code", type="string", description="要执行的 Python 代码"),
    ],
    category=ToolCategory.ANALYSIS,
    risk_level=ToolRiskLevel.MEDIUM,
    requires_confirmation=False,  # 自动执行
    when_to_use="当需要进行数据分析、计算、生成图表或处理文件时",
)
```

---

## 四、API 设计

### 4.1 新增 SSE 事件类型

```python
class StreamEventType(str, Enum):
    # 现有事件
    TEXT_DELTA = "text_delta"
    CONTEXT_RETRIEVED = "context_retrieved"
    WEB_SEARCH_RESULTS = "web_search_results"
    RAG_EVALUATION = "rag_evaluation"
    USAGE = "usage"
    DONE = "done"

    # 新增代码执行事件
    CODE_EXECUTION_START = "code_execution_start"
    CODE_EXECUTION_OUTPUT = "code_execution_output"
    CODE_EXECUTION_RESULT = "code_execution_result"
    ARTIFACT_CREATED = "artifact_created"
```

### 4.2 事件数据结构

```python
# code_execution_start
{
    "event_type": "code_execution_start",
    "data": {
        "execution_id": "exec_abc123",
        "language": "python",
        "code": "import pandas as pd\n..."
    }
}

# code_execution_output (实时 stdout)
{
    "event_type": "code_execution_output",
    "data": {
        "execution_id": "exec_abc123",
        "output": "Loading data...\n"
    }
}

# code_execution_result
{
    "event_type": "code_execution_result",
    "data": {
        "execution_id": "exec_abc123",
        "success": true,
        "execution_time_ms": 1234,
        "memory_used_mb": 45.2
    }
}

# artifact_created
{
    "event_type": "artifact_created",
    "data": {
        "artifact_id": "art_xyz789",
        "type": "chart",  # chart | table | file | code
        "format": "png",
        "title": "销售趋势图",
        "url": "/api/v1/assistant/artifacts/art_xyz789"
    }
}
```

### 4.3 新增 API 端点

```python
# 获取 Artifact
@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str) -> ArtifactResponse:
    """获取 artifact 元信息"""
    pass

# 下载 Artifact
@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: str) -> FileResponse:
    """下载 artifact 文件"""
    pass
```

---

## 五、前端设计

### 5.1 Artifacts 面板组件结构

```
web/src/components/artifacts/
├── ArtifactsPanel.tsx       # 主容器（右侧面板）
├── ArtifactTabs.tsx         # Tab 导航
├── CodeViewer.tsx           # 代码展示 (Monaco Editor)
├── ChartViewer.tsx          # 图表渲染
├── TableViewer.tsx          # 表格展示 (AG-Grid)
├── FileList.tsx             # 文件下载列表
├── ExecutionStatus.tsx      # 执行状态指示器
└── index.ts
```

### 5.2 面板布局

```
┌─────────────────────────────────────────────────────────────┐
│ Artifacts Panel                                    [×] 关闭  │
├─────────────────────────────────────────────────────────────┤
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Tab Bar                                                 │ │
│ │  [代码] [图表] [表格] [文件]                            │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Content Area                                            │ │
│ │                                                          │ │
│ │  代码视图: Monaco Editor (语法高亮 + 行号)              │ │
│ │  图表视图: 渲染的 PNG/SVG + 全屏按钮                   │ │
│ │  表格视图: AG-Grid 交互式表格                          │ │
│ │  文件视图: 下载链接列表                                 │ │
│ │                                                          │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ 操作栏                                                  │ │
│ │  [📋 复制代码] [📥 下载文件] [🔄 重新执行]              │ │
│ └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### 5.3 状态管理

```typescript
interface ArtifactsState {
  // Artifact 列表（当前会话）
  artifacts: Artifact[];

  // 当前选中的 Artifact
  selectedArtifactId: string | null;

  // 执行状态
  executionStatus: 'idle' | 'running' | 'success' | 'error';

  // 实时输出
  executionOutput: string;
}

interface Artifact {
  id: string;
  type: 'code' | 'chart' | 'table' | 'file';
  format: string;
  title: string;
  url: string;
  createdAt: Date;
}
```

---

## 六、知识库联动

### 6.1 联动机制

```
用户选择知识库 → 检索相关文档 → 挂载到沙箱 → 代码可读取
```

### 6.2 沙箱内 KB 访问

```python
# 沙箱内的目录结构
/workspace/
├── input/           # 用户上传的文件
│   └── data.csv
├── kb_docs/         # KB 检索的文档
│   ├── policy_01.txt
│   └── policy_02.txt
├── output/          # 生成的输出文件
│   └── chart.png
└── main.py          # 要执行的代码
```

用户代码可以这样访问：
```python
# 读取 KB 文档
import os
kb_files = os.listdir("/workspace/kb_docs/")
for f in kb_files:
    with open(f"/workspace/kb_docs/{f}") as file:
        content = file.read()
        # 处理文档内容...
```

---

## 七、执行流程示例

```
用户: "分析这份销售数据，画个趋势图"

[1] 模型分析需求，决定执行代码
[2] text_delta: "我来帮你分析这份销售数据..."
[3] code_execution_start: {"language": "python", "code": "..."}
[4] code_execution_output: "Loading data..." (实时 stdout)
[5] code_execution_result: {"success": true, "time_ms": 1234}
[6] artifact_created: {"type": "chart", "title": "销售趋势图"}
[7] text_delta: "根据数据分析，销售呈上升趋势..."
[8] 前端展示 Artifacts 面板，显示图表
```

---

## 八、依赖项

### 8.1 后端依赖

```
docker>=6.1.0        # Docker SDK
aiofiles>=23.0.0     # 异步文件操作
```

### 8.2 前端依赖

```
@monaco-editor/react  # 代码编辑器
ag-grid-react         # 表格组件
```

### 8.3 系统依赖

- Docker Engine (服务器端)
- 预构建的 Python 镜像 (包含常用库)

---

## 九、实施计划概要

### Phase 1: 后端代码执行服务 (核心)
1. 创建 CodeExecutorService
2. 实现 Docker 容器管理
3. 实现文件 I/O 处理
4. 添加安全策略

### Phase 2: 工具集成
1. 注册 code_executor 工具到 ToolRegistry
2. 扩展 AssistantService 支持代码执行
3. 添加新的 SSE 事件类型

### Phase 3: API 扩展
1. 新增 artifacts 端点
2. 扩展 chat/stream 支持代码执行事件

### Phase 4: 前端 Artifacts 面板
1. 创建 Artifacts 组件
2. 集成到 AssistantPage
3. 实现各类型 Viewer

### Phase 5: KB 联动
1. 实现 KB 文档检索和挂载
2. 测试端到端流程

---

## 十、风险与缓解

| 风险 | 缓解措施 |
|------|----------|
| 代码执行安全 | Docker 沙箱 + 网络隔离 + 资源限制 |
| 容器资源消耗 | 执行完立即清理 + 并发数限制 |
| 大文件处理 | 输出文件大小限制 (10MB) |
| 执行超时 | 硬性 30 秒超时 + 优雅终止 |

---

## 十一、验收标准

1. 用户可以通过自然语言请求执行 Python 代码
2. 代码在 Docker 沙箱中安全执行
3. 支持 matplotlib/plotly 图表生成并在 Artifacts 面板展示
4. 支持 pandas DataFrame 以交互式表格展示
5. 代码可访问用户选中的知识库文档
6. 执行过程实时流式展示
7. 生成的文件可下载
