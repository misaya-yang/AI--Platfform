# 文件上传分析功能设计方案

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复前端 AI 助手无法读取上传文件内容的 bug，实现完整的文件分析功能

**Architecture:** 分类型处理（图像/短文档/长文档）+ 会话临时知识库 + 多模态消息构建

**Tech Stack:** unstructured (文档解析) + S3 (文件存储) + Qdrant (向量库) + qwen-vl-max (VLM)

---

## 1. 问题分析

### 当前问题
- 前端上传文件后，`file_paths` 正确传递到后端
- 后端 `_build_messages` 方法**完全忽略** `file_paths` 参数
- 文件内容没有被读取、解析或传递给模型

### 期望行为
- 图像文件：发送给 Vision 模型进行分析
- 文档文件：提取文本内容，注入到模型输入
- 长文档：向量化后通过 RAG 检索

---

## 2. 设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 文档处理 | 提取文本注入 | 兼容所有模型，无需 Vision 支持 |
| 图像处理 | Vision/VLM 混合 | Vision 模型直接传图，文本模型用 VLM 描述 |
| 解析库 | unstructured | 业界标准，支持 PDF/DOCX/HTML/MD/CSV |
| 长文档存储 | 会话临时 KB | 存入 Qdrant，会话结束可选保留/删除 |
| 文件存储 | S3 | 已有基础设施，原文件可追溯 |

---

## 3. 架构设计

### 3.1 文件处理流程

```
用户上传文件
     ↓
┌────────────────────────────────────────────────────┐
│ 1. 文件存储到 S3 (uploads/{user_id}/{file_id})      │
│    - 已实现: /api/v1/files/upload                  │
└────────────────────────────────────────────────────┘
     ↓
┌────────────────────────────────────────────────────┐
│ 2. 聊天时处理文件 (FileProcessor)                   │
│    ├─ 图像 → base64 (vision) / VLM描述 (文本模型)   │
│    ├─ 短文档 (< 32K chars) → 提取文本直接注入       │
│    └─ 长文档 (≥ 32K chars) → 创建会话临时 KB        │
└────────────────────────────────────────────────────┘
     ↓
┌────────────────────────────────────────────────────┐
│ 3. 构建多模态消息 (_build_messages 改造)            │
│    - Vision 模型: 构建 image_url content blocks    │
│    - 文本模型: 注入文本内容到 user message          │
└────────────────────────────────────────────────────┘
```

### 3.2 会话临时知识库

```
长文档处理流程:
1. 检测文档长度 ≥ 32K chars
2. 使用 unstructured 分块 (chunk_size=500, overlap=50)
3. 创建临时 KB: kb_session_{session_id}
4. 存入 Qdrant 向量库
5. 聊天时自动从临时 KB 检索
6. 会话结束时提示用户: 保留为永久 KB / 删除
```

---

## 4. 核心组件

### 4.1 FileProcessor 服务

**文件:** `src/services/assistant/file_processor.py`

```python
@dataclass
class ProcessedFiles:
    """文件处理结果"""
    images: List[ImageContent]           # base64 图像 (vision模型用)
    text_content: str                    # 提取的文本 (短文档)
    image_descriptions: List[str]        # 图像描述 (文本模型用)
    session_kb_id: Optional[str]         # 会话临时 KB ID (长文档)
    file_metadata: List[FileMetadata]    # 文件元信息

class FileProcessor:
    """处理上传文件，转换为模型可消费的格式"""

    def __init__(
        self,
        document_parser: DocumentParser,
        kb_service: KnowledgeService,
        vlm_service: Optional[VLMService] = None,
    ):
        self.parser = document_parser
        self.kb_service = kb_service
        self.vlm_service = vlm_service

    async def process_files(
        self,
        file_paths: List[str],
        session_id: str,
        user: UserContext,
        model_supports_vision: bool,
        max_text_chars: int = 32000,
    ) -> ProcessedFiles:
        """
        处理上传的文件列表

        Args:
            file_paths: 文件路径列表 (如 /uploads/admin/abc123.pdf)
            session_id: 会话 ID
            user: 用户上下文
            model_supports_vision: 模型是否支持 vision
            max_text_chars: 短文档阈值

        Returns:
            ProcessedFiles: 处理后的文件数据
        """
        images = []
        text_parts = []
        image_descriptions = []
        long_docs = []

        for file_path in file_paths:
            file_type = self._detect_file_type(file_path)

            if file_type == "image":
                if model_supports_vision:
                    # Vision 模型: 转 base64
                    base64_data = await self._read_image_as_base64(file_path)
                    images.append(ImageContent(
                        base64=base64_data,
                        mime_type=self._get_mime_type(file_path),
                    ))
                else:
                    # 文本模型: VLM 生成描述
                    description = await self._generate_image_description(file_path)
                    image_descriptions.append(description)

            elif file_type == "document":
                # 解析文档
                text = await self.parser.parse(file_path)

                if len(text) < max_text_chars:
                    # 短文档: 直接注入
                    text_parts.append(f"[文件: {Path(file_path).name}]\n{text}")
                else:
                    # 长文档: 标记需要 RAG
                    long_docs.append((file_path, text))

        # 处理长文档: 创建会话临时 KB
        session_kb_id = None
        if long_docs:
            session_kb_id = await self._create_session_kb(
                session_id=session_id,
                user=user,
                documents=long_docs,
            )

        return ProcessedFiles(
            images=images,
            text_content="\n\n".join(text_parts),
            image_descriptions=image_descriptions,
            session_kb_id=session_kb_id,
            file_metadata=[...],
        )
```

### 4.2 DocumentParser 文档解析器

**文件:** `src/services/assistant/document_parser.py`

```python
class DocumentParser:
    """使用 unstructured 解析各类文档"""

    SUPPORTED_TYPES = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".csv": "text/csv",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".html": "text/html",
    }

    async def parse(self, file_path: str) -> str:
        """
        解析文档，返回纯文本内容

        Args:
            file_path: 文件路径

        Returns:
            str: 提取的文本内容
        """
        from unstructured.partition.auto import partition

        # 获取实际文件路径
        actual_path = self._resolve_path(file_path)

        # 使用 unstructured 解析
        elements = partition(filename=str(actual_path))

        # 合并所有元素的文本
        text_parts = []
        for element in elements:
            if hasattr(element, "text") and element.text:
                text_parts.append(element.text)

        return "\n\n".join(text_parts)
```

### 4.3 _build_messages 改造

**文件:** `src/services/assistant/assistant_service.py`

```python
def _build_messages(
    self,
    message: str,
    history: List[Dict[str, str]],
    config: AssistantConfig,
    retrieved_contexts: List[RetrievedContext],
    web_search_context: Optional[str] = None,
    processed_files: Optional[ProcessedFiles] = None,  # 新增
    model_info: Optional[ModelInfo] = None,            # 新增
) -> List[ChatMessage]:
    """Build the message list for the model."""
    messages: List[ChatMessage] = []

    # System prompt (含 KB context)
    system_content = config.system_prompt or self.DEFAULT_SYSTEM_PROMPT
    if retrieved_contexts:
        context_text = self._format_context(retrieved_contexts)
        system_content = system_content + "\n\n" + self.CONTEXT_TEMPLATE.format(context=context_text)
    if web_search_context:
        system_content = system_content + "\n\n" + self.WEB_CONTEXT_TEMPLATE.format(context=web_search_context)

    messages.append(ChatMessage(role="system", content=system_content))

    # History
    for h in history:
        role = h.get("role", "user")
        content = h.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append(ChatMessage(role=role, content=content))

    # Current message - 根据是否有文件和模型能力构建
    if processed_files and model_info and model_info.supports_vision and processed_files.images:
        # 多模态消息格式 (OpenAI Vision API 格式)
        user_content = [
            {"type": "text", "text": message},
        ]
        for img in processed_files.images:
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{img.mime_type};base64,{img.base64}",
                }
            })
        messages.append(ChatMessage(role="user", content=user_content))
    else:
        # 纯文本消息 (可能包含文件内容注入)
        final_message = message

        if processed_files:
            # 注入文档文本
            if processed_files.text_content:
                final_message += f"\n\n---\n[上传文件内容]\n{processed_files.text_content}"

            # 注入图像描述 (文本模型)
            if processed_files.image_descriptions:
                descriptions = "\n".join(
                    f"- 图像 {i+1}: {desc}"
                    for i, desc in enumerate(processed_files.image_descriptions)
                )
                final_message += f"\n\n---\n[图像描述]\n{descriptions}"

        messages.append(ChatMessage(role="user", content=final_message))

    return messages
```

---

## 5. 实施任务清单

### Task 1: 安装依赖
- 安装 unstructured: `pip install unstructured[all-docs]`
- 验证 PDF/DOCX 解析能力

### Task 2: 实现 DocumentParser
- 创建 `src/services/assistant/document_parser.py`
- 实现 `parse()` 方法
- 支持 PDF, DOCX, TXT, MD, CSV, XLSX
- 添加单元测试

### Task 3: 实现 FileProcessor
- 创建 `src/services/assistant/file_processor.py`
- 实现文件类型检测
- 实现图像 base64 转换
- 实现 VLM 图像描述生成
- 实现会话临时 KB 创建

### Task 4: 改造 _build_messages
- 添加 `processed_files` 和 `model_info` 参数
- 实现多模态消息构建 (Vision 模型)
- 实现文本注入 (文本模型)

### Task 5: 集成到 chat_stream
- 在 `chat_stream` 方法中调用 `FileProcessor`
- 处理会话临时 KB 的自动检索
- 添加文件处理状态的 SSE 事件

### Task 6: 前端状态显示
- 添加文件处理进度显示
- 显示会话临时 KB 创建状态
- 会话结束时提示保留/删除临时 KB

### Task 7: 端到端测试
- 测试图像上传 + Vision 模型
- 测试图像上传 + 文本模型
- 测试短文档上传
- 测试长文档上传 + RAG

---

## 6. 参考资料

- [OpenAI PDF File Input](https://platform.openai.com/docs/guides/pdf-files)
- [ChatGPT Claude File Types](https://www.datastudios.org/post/file-types-in-chatgpt-and-claude-supported-uploads-analysis-capabilities-and-new-automation-featu)
- [unstructured 文档](https://docs.unstructured.io/)
- [DashScope VLM API](https://help.aliyun.com/zh/model-studio/developer-reference/vision-language-model)
