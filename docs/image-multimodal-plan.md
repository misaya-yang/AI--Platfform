# 知识库图片处理与多模态支持实施计划

> **状态**: 已评审，待实施
> **创建日期**: 2025-01-15
> **评审日期**: 2025-01-15

## 一、需求概述

### 用户需求
1. 知识库文档中包含图片（PDF、DOCX、Confluence）
2. 将图片存储到 AWS S3
3. 在文档内容中替换图片为 S3 URL
4. 检索结果在前端展示图片
5. 对话模块支持多模态输入（Gemini 2.0 Flash）

### 确认的范围
- **S3 存储**: AWS S3
- **图片来源**: 所有文档类型 + Confluence
- **优先级**: 图片存储与多模态同步实现

---

## 二、架构设计

### 数据流

```
文档上传 (PDF/DOCX)
       │
       ▼
┌──────────────────┐
│  ImageExtractor  │ ──────► 提取图片
│  (图片提取器)     │
└──────────────────┘
       │
       ▼
┌──────────────────┐
│  S3StorageService│ ──────► 上传到 S3
│  (存储服务)       │
└──────────────────┘
       │
       ├─────────────────────────────┐
       ▼                             ▼
┌──────────────────┐         ┌──────────────────┐
│  images 表       │         │  documents 表    │
│  (图片元数据)     │         │  (文本+占位符)    │
└──────────────────┘         └──────────────────┘
                                     │
                                     ▼
                             ┌──────────────────┐
                             │  分块 & 向量化    │
                             └──────────────────┘
                                     │
                                     ▼
                             ┌──────────────────┐
                             │  检索 API        │ ──────► 返回文本 + 图片URL
                             └──────────────────┘
```

### 多模态对话流程

```
用户上传图片
       │
       ▼
┌──────────────────┐
│  前端上传到 S3   │
└──────────────────┘
       │
       ▼
┌──────────────────┐
│  对话 API        │ ──────► 图片 URL + 文本
└──────────────────┘
       │
       ▼
┌──────────────────┐
│  Gemini 2.0 Flash│ ──────► 多模态理解
└──────────────────┘
       │
       ▼
┌──────────────────┐
│  返回响应        │
└──────────────────┘
```

---

## 三、数据库设计

### 已创建：005_images_support.sql

#### 1. images 表（图片记录）
```sql
CREATE TABLE images (
    image_id VARCHAR(255) PRIMARY KEY,
    tenant_id VARCHAR(255) NOT NULL,
    source_type VARCHAR(50) NOT NULL,      -- 'document', 'confluence', 'chat'
    source_id VARCHAR(255) NOT NULL,       -- document_id / page_id / session_id
    filename VARCHAR(500),
    content_type VARCHAR(100),
    size_bytes BIGINT,
    width INTEGER,
    height INTEGER,
    s3_bucket VARCHAR(255) NOT NULL,
    s3_key VARCHAR(1000) NOT NULL,
    position_index INTEGER DEFAULT 0,
    context_text TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

#### 2. segment_images 表（分片-图片关联）
```sql
CREATE TABLE segment_images (
    id VARCHAR(255) PRIMARY KEY,
    segment_id VARCHAR(255) REFERENCES segments(segment_id),
    image_id VARCHAR(255) REFERENCES images(image_id),
    position_in_segment INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(segment_id, image_id)
);
```

#### 3. chat_images 表（对话图片）
```sql
CREATE TABLE chat_images (
    image_id VARCHAR(255) PRIMARY KEY,
    session_id VARCHAR(255) NOT NULL,
    message_index INTEGER NOT NULL,
    filename VARCHAR(500),
    content_type VARCHAR(100),
    size_bytes BIGINT,
    s3_bucket VARCHAR(255) NOT NULL,
    s3_key VARCHAR(1000) NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

#### 4. 现有表扩展
- `documents` 表：添加 `image_count` 字段
- `segments` 表：添加 `has_images` 字段

#### 5. S3 清理队列表
```sql
CREATE TABLE s3_cleanup_queue (
    id SERIAL PRIMARY KEY,
    s3_bucket VARCHAR(255) NOT NULL,
    s3_key VARCHAR(1000) NOT NULL,
    source_type VARCHAR(50),
    source_id VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);
```

#### 6. 删除触发器
- 文档删除时自动：
  1. 将图片 S3 key 加入清理队列
  2. 删除 images 表记录（级联删除 segment_images）
- S3 文件由异步任务从清理队列中处理

---

## 四、后端实现计划

### Phase 1: 基础设施

#### 1.1 S3 配置 (settings.py)
```python
class S3Settings(BaseModel):
    enabled: bool = False
    bucket: str = ""
    region: str = "us-east-1"
    access_key: str = ""
    secret_key: str = ""
    endpoint_url: Optional[str] = None  # 支持 MinIO
    presigned_url_expiry: int = 3600    # 预签名 URL 过期时间
    max_image_size_mb: int = 10         # 最大图片大小
```

#### 1.2 Gemini 配置 (settings.py)
```python
class GeminiSettings(BaseModel):
    enabled: bool = False
    api_key: str = ""
    model: str = "gemini-2.0-flash"
    max_tokens: int = 8192
    temperature: float = 0.7
```

#### 1.3 S3 存储服务
**新建文件**: `src/services/storage/s3_service.py`

核心方法：
- `upload_image(data, key, content_type)` - 上传图片
- `get_presigned_url(key)` - 获取预签名访问 URL (用于前端展示)
- `get_image_bytes(key)` - 获取图片二进制数据 (用于多模态调用)
- `delete_image(key)` - 删除图片
- `delete_images_batch(keys)` - 批量删除 (用于清理)
- `generate_key(tenant_id, source_type, source_id, filename)` - 生成唯一 key

### Phase 2: 图片提取

#### 2.1 图片提取器
**新建文件**: `src/services/knowledge/image_extractor.py`

```python
@dataclass
class ExtractedImage:
    data: bytes           # 图片二进制数据
    filename: str         # 文件名
    content_type: str     # MIME 类型
    position: int         # 在文档中的位置
    context_before: str   # 图片前 200 字文本 ⬅️ 新增
    context_after: str    # 图片后 200 字文本 ⬅️ 新增

class ImageExtractor:
    CONTEXT_CHARS = 200  # 上下文捕获长度

    def extract_from_pdf(self, pdf_bytes: bytes) -> Tuple[str, List[ExtractedImage]]
    def extract_from_docx(self, docx_bytes: bytes) -> Tuple[str, List[ExtractedImage]]
    def extract_from_html(self, html: str) -> Tuple[str, List[str]]  # 用于 Confluence
```

**图片占位符格式**: `[IMAGE_0]`, `[IMAGE_1]`, ...

**上下文捕获** (检索增强关键):
```python
# 提取图片时捕获周围文本
context_before = text[max(0, pos-200):pos]
context_after = text[pos:pos+200]
```

处理后替换为 Markdown 格式: `![image](https://presigned-url...)`

#### 2.2 集成到知识服务
**修改文件**: `src/services/knowledge/knowledge_service.py`

修改点：
1. `_extract_text_from_pdf_bytes()` - 调用 ImageExtractor
2. `_extract_text_from_docx_bytes()` - 调用 ImageExtractor
3. `create_document_from_upload()` - 处理图片上传流程

新增方法：
- `_extract_text_with_images()` - 提取文本和图片
- `_save_image_record()` - 保存图片记录到数据库
- `get_document_images()` - 获取文档关联的图片
- `get_segment_images()` - 获取分片关联的图片

### Phase 3: 检索增强

#### 3.1 检索 API 增加图片返回
**修改文件**: `src/api/v1/knowledge.py`

```python
@router.post("/{dataset_id}/search")
async def search_knowledge(
    dataset_id: str,
    request: SearchRequest,
    include_images: bool = Query(default=True)  # 新增参数
):
    results = await knowledge_service.search(...)
    if include_images:
        # 联表查询，一次性获取图片，避免 N+1 问题
        segment_ids = [r.segment_id for r in results]
        images_map = await get_segment_images_batch(segment_ids)
        for result in results:
            if result.segment_id in images_map:
                # 动态生成预签名 URL
                result.images = [
                    s3.get_presigned_url(img.s3_key)
                    for img in images_map[result.segment_id]
                ]
    return results
```

**联表查询** (避免 N+1):
```sql
SELECT si.segment_id, i.*
FROM segment_images si
JOIN images i ON si.image_id = i.image_id
WHERE si.segment_id = ANY($1)
ORDER BY si.position_in_segment
```

#### 3.2 响应模型扩展
```python
class SearchResultItem(BaseModel):
    segment_id: str
    content: str
    score: float
    images: Optional[List[str]] = None  # 图片预签名 URL 列表 (动态生成)
```

### Phase 4: 多模态对话

#### 4.1 Gemini 客户端 (使用 LangChain)
**新建文件**: `src/services/llm/multimodal_client.py`

> ⚠️ **关键修正**: 使用 `langchain-google-genai` 而非自定义实现

```python
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, AIMessage
import base64

class MultimodalLLMClient:
    """多模态 LLM 客户端 - 基于 LangChain"""

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash"):
        self.llm = ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key,
        )
        self.s3_service = None  # 注入 S3 服务

    async def invoke_with_images(
        self,
        text: str,
        s3_keys: List[str],
        history: List[Dict] = None
    ) -> str:
        """
        调用多模态模型

        关键：图片从 S3 读取转 Base64，不使用预签名 URL
        """
        content = [{"type": "text", "text": text}]

        for key in s3_keys:
            # 从 S3 获取二进制数据并转为 base64
            image_bytes = await self.s3_service.get_image_bytes(key)
            image_b64 = base64.b64encode(image_bytes).decode()
            content_type = self._guess_content_type(key)

            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{content_type};base64,{image_b64}"}
            })

        message = HumanMessage(content=content)
        response = await self.llm.ainvoke([message])
        return response.content
```

**存储策略** (避免状态膨胀):
```python
# 对话历史中只存储 s3_key，不存 Base64
chat_history = [
    {"role": "user", "content": "分析这张图", "image_keys": ["images/xxx/1.png"]},
    {"role": "assistant", "content": "这是一张..."}
]
# 调用模型时才读取 S3 转 Base64
```

#### 4.2 对话服务修改
**修改文件**: `src/services/conversations.py`

```python
async def process_message(
    session_id: str,
    message: str,
    images: Optional[List[str]] = None  # 新增图片参数
):
    if images and self.gemini_enabled:
        # 使用 Gemini 多模态
        response = await self.multimodal_client.chat(...)
    else:
        # 使用原有 LLM
        response = await self.llm_client.chat(...)
```

#### 4.3 对话 API 修改
**修改文件**: `src/api/v1/conversations.py`

新增端点：
```python
@router.post("/sessions/{session_id}/images")
async def upload_chat_image(session_id: str, file: UploadFile):
    # 上传图片到 S3，返回 URL
```

修改消息发送：
```python
class SendMessageRequest(BaseModel):
    message: str
    images: Optional[List[str]] = None  # 图片 URL 列表
```

### Phase 5: Confluence 图片处理

**修改文件**: `src/services/knowledge/confluence/sync_service.py`

1. 解析 Confluence Storage Format 中的 `<ac:image>` 标签
2. 下载图片并上传到 S3
3. 替换文档中的图片 URL

### Phase 6: 完善

1. 错误处理和重试机制
2. 图片大小限制和压缩
3. 预签名 URL 刷新机制
4. 图片清理策略（删除文档时清理关联图片）

---

## 五、前端实现计划

### 5.1 检索结果图片展示

```tsx
interface SearchResult {
  segment_id: string;
  content: string;
  score: number;
  images?: string[];  // 新增
}

// 图片展示组件
{result.images?.map((url, idx) => (
  <img
    key={idx}
    src={url}
    className="h-32 w-auto rounded-lg cursor-pointer"
    onClick={() => openImagePreview(url)}
  />
))}
```

### 5.2 对话图片上传

功能：
- 图片选择按钮（支持多选）
- 图片预览和删除
- 上传到 S3 获取 URL
- 发送多模态消息

---

## 六、文件变更清单

### 新增文件

| 文件 | 说明 |
|------|------|
| `database/migrations/005_images_support.sql` | 图片表迁移 ✅ 已创建 |
| `src/services/storage/__init__.py` | 存储模块 |
| `src/services/storage/s3_service.py` | S3 存储服务 |
| `src/services/knowledge/image_extractor.py` | 图片提取器 |
| `src/services/llm/__init__.py` | LLM 模块 |
| `src/services/llm/multimodal_client.py` | 多模态 LLM 客户端 |
| `web/src/api/chat.ts` | 对话图片上传 API |

### 修改文件

| 文件 | 修改内容 |
|------|----------|
| `src/config/settings.py` | 添加 S3Settings, GeminiSettings |
| `src/container.py` | 注册 S3StorageService |
| `src/services/knowledge/knowledge_service.py` | 集成图片提取 |
| `src/services/knowledge/confluence/sync_service.py` | Confluence 图片处理 |
| `src/services/conversations.py` | 多模态消息支持 |
| `src/api/v1/knowledge.py` | 检索返回图片 |
| `src/api/v1/conversations.py` | 图片上传端点 |
| `src/api/schemas/knowledge.py` | 响应模型扩展 |
| `web/src/pages/Playground.tsx` | 图片上传 UI |
| `requirements.txt` | 添加依赖 |

---

## 七、依赖添加

```txt
# requirements.txt 新增
boto3>=1.34.0
langchain-google-genai>=2.0.0    # LangChain Gemini 集成
Pillow>=10.0.0
```

> **注意**: 使用 `langchain-google-genai` 而非 `google-generativeai`，与现有 LangGraph 架构保持一致

---

## 八、配置示例

```yaml
# config/settings.yaml 或环境变量

# S3 配置
# GATEWAY_S3__ENABLED=true
# GATEWAY_S3__BUCKET=agent-gateway-images
# GATEWAY_S3__REGION=ap-northeast-1
# GATEWAY_S3__ACCESS_KEY=your-access-key
# GATEWAY_S3__SECRET_KEY=your-secret-key
# GATEWAY_S3__PRESIGNED_URL_EXPIRY=3600

# Gemini 配置
# GATEWAY_GEMINI__ENABLED=true
# GATEWAY_GEMINI__API_KEY=your-gemini-api-key
# GATEWAY_GEMINI__MODEL=gemini-2.0-flash
```

---

## 九、专家评审意见及决策

### 评审总结
- **评级**: A (准生产级)
- **核心风险**: LangGraph 状态膨胀、预签名 URL 过期处理

### 关键修正点

#### 1. 多模态消息协议 ✅ 采纳
**问题**: Gemini API 要求公网可访问 URL，私有 S3 预签名 URL 可能失败

**决策**:
- 使用 `langchain-google-genai` 的 `ChatGoogleGenerativeAI`
- 后端读取 S3 图片转为 Base64 传给模型
- 传输在内网完成，更稳定

```python
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage

# 正确做法：转为 base64
image_data = await s3_service.get_image_base64(key)
content.append({
    "type": "image_url",
    "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}
})
```

#### 2. LangGraph 状态膨胀 ✅ 采纳
**问题**:
- Base64 图片会导致 checkpoint 表快速膨胀到 GB 级
- 预签名 URL 过期后历史图片无法显示

**决策**:
- 数据库存储 `s3_key` 而非 URL 或 Base64
- API 返回时动态生成预签名 URL
- 调用模型时才读取 S3 转 Base64

```python
# 存储时
state["images"] = ["s3_key_1", "s3_key_2"]  # 只存 key

# 返回前端时
for msg in history:
    if msg.images:
        msg.image_urls = [s3.get_presigned_url(k) for k in msg.images]
```

#### 3. 图片上下文捕获 ✅ 采纳
**决策**:
- ImageExtractor 捕获图片前后各 200 字文本
- 检索时通过 segment_images 联表查询
- 一次性返回分片 + 关联图片，避免二次请求

#### 4. 清理策略 ✅ 采纳
**决策**:
- 删除 documents 时级联删除关联图片
- 通过数据库外键 ON DELETE CASCADE 实现
- S3 清理通过异步任务完成

#### 5. 前端 URL 刷新 ✅ 采纳
**决策**:
- 前端不处理刷新逻辑
- 后端 API 每次请求重新生成 1 小时有效 URL
- 图片 URL 不缓存在前端

---

## 十、实施顺序建议

1. **Phase 1**: 基础设施 (S3 服务 + 配置)
2. **Phase 2**: 图片提取 (ImageExtractor + 知识服务集成)
3. **Phase 3**: 检索增强 (API 修改 + 前端展示)
4. **Phase 4**: 多模态对话 (Gemini 客户端 + 对话服务)
5. **Phase 5**: Confluence 图片处理
6. **Phase 6**: 完善和测试

---

## 十一、风险与注意事项

1. **S3 成本**: 图片存储和流量会产生费用
2. **图片大小**: 建议限制单张 10MB，大图自动压缩
3. **Gemini API**: 注意调用频率限制和 token 消耗
4. **预签名 URL**: 需要处理过期后的刷新逻辑
5. **数据隐私**: 确保图片存储符合合规要求
6. **PDF 提取**: 某些 PDF 的图片可能无法正确提取
