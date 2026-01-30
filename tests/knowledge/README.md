# 知识库测试工具

本目录包含知识库模块的测试和工具脚本。

## 测试脚本

### 1. test_kb_qa.py - 问答功能测试
```bash
# 单次查询测试
python tests/knowledge/test_kb_qa.py --dataset imam --query "marriage in Islam"

# 多模态检索测试（含图片）
python tests/knowledge/test_kb_qa.py --dataset imam --query "diagram" --multimodal

# 交互模式
python tests/knowledge/test_kb_qa.py --dataset imam --interactive

# 列出所有文档
python tests/knowledge/test_kb_qa.py --dataset imam --list-docs
```

### 2. test_knowledge_base.py - 全流程测试
```bash
# 自动测试完整流程：健康检查 -> 文件上传 -> 处理等待 -> 检索/问答测试
python tests/knowledge/test_knowledge_base.py
```

### 3. test_full_pipeline.py - PDF 处理集成测试
```bash
python tests/knowledge/test_full_pipeline.py /path/to/file.pdf
```

### 4. verify_fix.py - 占位脚本
```bash
python tests/knowledge/verify_fix.py
```

## 批量入库工具

批量入库脚本位于 `scripts/batch_ingest.py`：

```bash
# 单个文件入库
python scripts/batch_ingest.py --dataset imam --files "/path/to/file.pdf"

# 批量入库
python scripts/batch_ingest.py --dataset imam --files "/path/to/*.pdf" --concurrency 3

# 目录递归扫描
python scripts/batch_ingest.py --dataset imam --dir ./documents --recursive
```

## 环境变量

```bash
export GATEWAY_BASE_URL="http://localhost:8080"
export GATEWAY_API_KEY="your-api-key"
```
