# 智能网关知识库管理系统 (Knowledge Base Management System) 实现计划

## 1. 核心目标与价值

在网关层实现一个类 **Dify** 的统一知识库系统，为网关接入的大量 Agent 提供标准化的 RAG（检索增强生成）能力，解决权限孤岛、模型碎片化和多模态扩展问题。

---

## 2. 系统架构设计 (升级版)

### 2.1 技术栈与基础设施
*   **后端**: FastAPI (现有) + Celery/TaskWorker (现有)。
*   **向量数据库**: **Qdrant** (作为高性能专用向量引擎，支持多向量、多模态索引)。
*   **Embedding 模型抽象层**: 
    *   **阿里系列**: `text-embedding-v4`, `qwen2.5-vl-embedding` (多模态)。
    *   **OpenAI 系列**: `text-embedding-3-large` (最高质量)。
    *   **支持多模态**: 架构上兼容图像/视频向量化存储。
*   **前端 UI**: React + TailwindCSS，**完全复刻 Dify 的三栏式管理风格**。

### 2.2 逻辑架构
1.  **管理层 (Management Layer)**: 负责知识库定义、文档上传、分段调整、命中测试。
2.  **处理层 (Processing Layer)**: 异步任务处理文档解析、Embedding 生成、写入向量库（利用现有的 `TaskWorker`）。
3.  **检索层 (Retrieval Layer)**: 供 Agent 调用的标准检索接口，支持混合搜索（全文+向量）。

---

## 3. 核心功能实现计划

### 3.1 数据模型设计
*   **Dataset (知识库)**: 包含名称、描述、Embedding 配置、索引配置、权限范围。
*   **Document (文档)**: 文档元数据（文件名、类型、来源）、状态（解析中/已完成/失败）。
*   **Segment (片段/Chunk)**: 文本片段、向量 ID、Token 数、位置元数据。
*   **Permission (权限)**: 关联 User/Role 与 Dataset 的访问关系（OWNER, EDITOR, VIEWER）。

### 3.2 灵活的模型切换机制
在网关配置中心，支持为每个知识库（Dataset）独立配置：
*   **Embedding Provider**: OpenAI / DashScope (Aliyun) / Local。
*   **Dimension 自适应**: 自动根据选定模型调整 Qdrant 集合维度。

### 3.3 Dify 风格 UI 设计
*   **Dataset 视图**: 卡片式展示知识库列表。
*   **Document 处理流**: 实时展示“解析 -> 分段 -> 向量化”的进度条。
*   **命中测试**: 在管理端直接输入 Query，可视化展示各个片段的分数和来源。

---

## 4. 与 Agent (LangGraph) 的集成

Agent 将以 **Tool (工具)** 的形式使用知识库：
1.  网关为每个知识库生成一个唯一的 `retrieval_tool`。
2.  Agent 在运行时根据需要调用该 Tool，传入用户 Query。
3.  网关在 Tool 执行时校验当前用户的 Session/User 是否具备该知识库的访问权限。

---

## 5. 针对您现有 Agent 的适配方案 (Code-Specific)

您的 `customerAgent` 是基于 LangGraph 构建的，目前通过 `src/agent/knowledge_base.py` 直接调用本地的 Qdrant。为了接入网关统一管理，我们采用 **“透明代理”** 方案，无需重构整个 Agent。

### 5.1 代理知识库类 (Proxy KnowledgeBase)
在 Agent 端实现一个轻量的 `RemoteKnowledgeBase` 类，保持与现有 `kb` 对象接口一致：

```python
# C:\Projects\Langgraph_Agents\customeragent\customerAgent\src\agent\knowledge_base.py

class RemoteKnowledgeBase:
    def search(self, query: str, k: int = 5, **kwargs):
        # 这里的 config 可以通过 LangGraph 的 context 获取，或者从环境变量读取
        gateway_url = os.getenv("GATEWAY_URL", "http://gateway-api")
        dataset_id = os.getenv("DATASET_ID") 
        
        # 调用网关 API
        response = requests.post(
            f"{gateway_url}/v1/knowledge/{dataset_id}/retrieve",
            json={"query": query, "top_k": k},
            headers={"Authorization": f"Bearer {os.getenv('GATEWAY_TOKEN')}"}
        )
        data = response.json()
        return data["results"], data["metadata"]

# 通过环境变量切换本地/远程模式
if os.getenv("KB_MODE") == "remote":
    kb = RemoteKnowledgeBase()
else:
    kb = _LazyKnowledgeBase()
```

### 5.2 利用 LangGraph `configurable` 传递参数
网关在调用 Agent 时，会将当前所需的 `dataset_id` 和鉴权信息注入 `config`：

```python
# 网关端 src/adapters/langgraph.py
config = {
    "configurable": {
        "dataset_id": "hejaz_prod_manual",
        "gateway_token": "internal_service_token_xxx",
        "thread_id": "..."
    }
}
await self.graph.ainvoke(input, config)
```

### 5.3 工具端适配
修改 `src/agent/subagents/rag_agent.py` 中的工具，使其能从 `config` 中读取这些动态参数：

```python
@tool
def retrieve_product_info(query: str, config: RunnableConfig) -> str:
    # 从 config 中获取网关动态注入的参数
    configurable = config.get("configurable", {})
    # 这样即使同一个 Agent，也可以根据调用者不同，访问不同的网关知识库
    results, metadata = kb.search(query, k=3, configurable=configurable)
    # ... 原有逻辑保持不变 ...
```

---

## 6. 详细实施路线图 (更新)

### 第一阶段：后端核心与数据层 (预计 1 周)
*   [ ] **Qdrant 动态管理**: 支持按模型维度动态创建 Collection。
*   [ ] **模型适配层**: 完成 OpenAI (text-embedding-3-large) 和 DashScope (包括多模态) 的封装。
*   [ ] 实现 `Dataset` 与 `Document` 的 CRUD 接口。
*   [ ] 扩展现有的 RBAC 逻辑以支持数据集权限校验。

### 第二阶段：检索增强与 API (预计 3-5 天)
*   [ ] 实现统一检索接口 (`/v1/knowledge/retrieve`)。
*   [ ] 封装检索工具供 LangGraph 代理使用。
*   [ ] 实现“命中测试”接口，支持可视化分数对比。

### 第三阶段：Dify 风格前端管理后台 (预计 1 周)
*   [ ] **复刻三栏式布局**: 导航 -> 列表 -> 详情。
*   [ ] **文档处理流**: 实时展示上传、解析、向量化状态。
*   [ ] **分段编辑器**: 手动修改/删除不合规的知识片段。

---
*由 Claude 编写，已根据用户反馈完善。*
