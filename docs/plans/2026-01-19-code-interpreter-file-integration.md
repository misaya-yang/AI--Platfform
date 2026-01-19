# Code Interpreter 文件集成设计

日期: 2026-01-19
状态: 设计完成，待实现

## 背景

当前 Code Interpreter 功能已有完整的 Docker 沙箱执行能力，但存在关键链路断开：
- 用户上传的文件（Excel/CSV/PDF）无法传入代码执行容器
- KB 检索的文档无法作为参考资料供代码访问

这导致 Agent 虽然能写代码，但无法真正分析用户上传的数据文件。

## 目标

实现"深度数据分析"能力：
1. 用户上传 Excel/CSV 文件
2. Agent 自动识别需要数据分析
3. 写 Python 代码（Pandas/Matplotlib）处理数据
4. 生成图表和分析结果

## 设计方案

### 1. 文件传递链路修复

**位置**: `src/services/assistant/assistant_service.py`

**当前代码** (第 878 行):
```python
result = await self.code_executor.execute(code=code)
```

**修改为**:
```python
result = await self.code_executor.execute(
    code=code,
    input_files=self._convert_to_input_files(processed_files, file_paths),
    kb_documents=self._convert_to_kb_documents(retrieved_contexts),
)
```

**新增方法**:

```python
def _convert_to_input_files(
    self,
    processed_files: Optional[ProcessedFiles],
    file_paths: List[str]
) -> Optional[List[InputFile]]:
    """将用户上传的文件转换为容器可访问的 InputFile"""
    if not file_paths:
        return None

    input_files = []
    for path in file_paths:
        # 从存储服务读取文件内容
        content = await self.file_storage.read(path)
        filename = path.split('/')[-1]
        mime_type = mimetypes.guess_type(filename)[0]

        input_files.append(InputFile(
            filename=filename,
            content=content,
            mime_type=mime_type,
        ))

    return input_files if input_files else None

def _convert_to_kb_documents(
    self,
    retrieved_contexts: List[RetrievedContext]
) -> Optional[List[KBDocument]]:
    """将 KB 检索结果转换为容器内的参考文档"""
    if not retrieved_contexts:
        return None

    kb_docs = []
    for ctx in retrieved_contexts:
        for i, chunk in enumerate(ctx.chunks):
            kb_docs.append(KBDocument(
                filename=f"{ctx.dataset_id}_chunk_{i}.txt",
                content=chunk.text.encode('utf-8'),
                document_id=chunk.document_id,
                metadata=chunk.metadata,
            ))

    return kb_docs if kb_docs else None
```

### 2. 增强工具提示词

**位置**: `src/services/assistant/tools/code_executor_tool.py`

```python
CODE_EXECUTOR_TOOL = ToolDefinition(
    name="execute_python_code",
    description="""Execute Python code in a secure sandbox for data analysis and visualization.

## When to Use
- Analyzing Excel/CSV data (calculations, statistics, trends)
- Creating charts and visualizations (matplotlib, plotly)
- Complex mathematical computations
- Data transformation and cleaning

## Available Files
- User uploads: /workspace/input/ (Excel, CSV, PDF, etc.)
- KB documents: /workspace/kb_docs/ (reference materials)

## Pre-installed Libraries
numpy, pandas, matplotlib, openpyxl, xlrd, scipy, seaborn

## Output
- Save charts to /workspace/output/ (auto-collected as artifacts)
- Print results to stdout (shown to user)

## Example
```python
import pandas as pd
import matplotlib.pyplot as plt

# Read uploaded Excel
df = pd.read_excel('/workspace/input/sales.xlsx')

# Analyze
growth = df['revenue'].pct_change().mean() * 100
print(f"Average growth rate: {growth:.2f}%")

# Visualize
plt.figure(figsize=(10, 6))
df.plot(x='month', y='revenue', kind='line')
plt.savefig('/workspace/output/trend.png', dpi=150)
```
""",
    parameters=[
        ToolParameter(
            name="code",
            type="string",
            description="Python code to execute",
            required=True
        )
    ],
    category=ToolCategory.ANALYSIS,
    risk_level=ToolRiskLevel.MEDIUM,
    requires_confirmation=False,
    timeout_seconds=60,
)
```

### 3. Docker 镜像配置

**文件**: `Dockerfile.code-interpreter`

```dockerfile
FROM python:3.11-slim

# 安装数据分析库
RUN pip install --no-cache-dir \
    numpy \
    pandas \
    matplotlib \
    seaborn \
    openpyxl \
    xlrd \
    scipy \
    plotly \
    python-docx

# 设置 matplotlib 非交互后端
ENV MPLBACKEND=Agg

WORKDIR /workspace
```

**构建命令**:
```bash
docker build -t ai-gateway-code-interpreter:latest -f Dockerfile.code-interpreter .
```

## 容器内文件布局

```
/workspace/
├── input/           ← 用户上传的原始文件
│   ├── sales_data.xlsx
│   └── report.csv
├── kb_docs/         ← 知识库相关文档（只读）
│   └── context_0.txt
├── output/          ← 代码生成的图表/文件
└── main.py          ← LLM 生成的分析代码
```

## 实现任务

| 任务 | 文件 | 优先级 |
|-----|------|--------|
| 文件传递链路 | assistant_service.py | P0 |
| 工具提示词增强 | code_executor_tool.py | P0 |
| Docker 镜像验证 | Dockerfile.code-interpreter | P1 |

## 测试场景

1. 上传 Excel 文件，询问"分析销售趋势"
2. 上传 CSV 文件，询问"计算增长率并绘图"
3. 同时上传多个文件，进行交叉分析
