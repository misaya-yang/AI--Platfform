# Multimodal RAG Enhancement Plan

## Overview

This document describes the implementation of unified multimodal RAG capabilities for the Knowledge Base module, enabling cross-modal retrieval (text↔image) in a shared vector space.

## Architecture

```
Document (PDF/DOCX/HTML/Confluence)
                │
    ┌───────────┼───────────┐
    ▼           ▼           ▼
┌────────┐ ┌─────────┐ ┌──────────┐
│  Text  │ │ Images  │ │  Tables  │
│ Chunks │ │Extracted│ │Structured│
└────┬───┘ └────┬────┘ └────┬─────┘
     │          │           │
     │    ┌─────┴─────┐     │
     │    ▼           │     │
     │  VLM Description     │
     │  (qwen-vl-max)       │
     └──────────┬───────────┘
                ▼
┌─────────────────────────────────────────────┐
│       UNIFIED MULTIMODAL EMBEDDING          │
│    (tongyi-embedding-vision-plus / 1024D)   │
│  Text and images in SAME vector space       │
└─────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────┐
│         QDRANT HYBRID COLLECTION            │
│  - Dense vectors (unified embedding)        │
│  - BM25 sparse index (text only)            │
│  - Payload: content_type, image_url         │
└─────────────────────────────────────────────┘
                │
    ┌───────────┼───────────┬───────────┐
    ▼           ▼           ▼           ▼
Text→Text   Text→Image  Image→Text  Image→Image
(standard)  (cross)     (cross)     (visual)
```

## Implemented Components

### 1. UnifiedMultimodalEmbedding

**File**: `src/services/knowledge/embedding.py`

Embeds both text and images into the same 1024-dimensional vector space, enabling true cross-modal retrieval.

```python
from src.services.knowledge.embedding import create_unified_embedding

# Create embedder
embedder = create_unified_embedding(model_name="tongyi-embedding-vision-plus")

# Embed text
text_vectors = await embedder.embed_texts(["What is machine learning?"])

# Embed images
image_vectors = await embedder.embed_images([image_bytes])

# Mixed batch embedding
results = await embedder.embed_mixed_batch([
    {"type": "text", "content": "AI explanation"},
    {"type": "image", "content": image_bytes},
])
```

**Features**:
- Text embedding via DashScope text-embedding-v3 or multimodal model
- Image embedding via tongyi-embedding-vision-plus
- Mixed batch processing with automatic type routing
- Context-aware image embedding (image + surrounding text)

### 2. DocumentImageExtractor

**File**: `src/services/knowledge/ingestion/document_image_extractor.py`

Extracts images from PDF, DOCX, and HTML documents with metadata preservation.

```python
from src.services.knowledge.ingestion import DocumentImageExtractor

extractor = DocumentImageExtractor()

# Extract from file
result = await extractor.extract_from_file("document.pdf")

# Or from bytes
result = await extractor.extract("doc.pdf", pdf_bytes, document_type="pdf")

# Access results
for image in result.images:
    print(f"Image: {image.image_id}, page: {image.page_number}")
    print(f"Size: {image.width}x{image.height}, MIME: {image.mime_type}")
```

**Supported Formats**:
| Format | Library | Features |
|--------|---------|----------|
| PDF | PyMuPDF (fitz) | Page numbers, DPI control, embedded/inline images |
| DOCX | python-docx | Inline images, relationship extraction |
| HTML | BeautifulSoup | Base64 data URIs, external URL resolution |

**Image Constraints** (following Dify 1.11 standards):
- Max size: 2MB
- Min dimensions: 50x50 pixels
- Supported types: JPEG, PNG, GIF, WebP, BMP

### 3. Cross-Modal API Integration

**File**: `src/api/v1/kb_tools.py`

The KB search API now supports multimodal queries:

```python
# Text query returning images
POST /api/v1/kb/search
{
    "query": "architecture diagram",
    "dataset_id": "docs",
    "include_images": true,
    "content_type_filter": ["image"]
}

# Image-based query (coming soon)
POST /api/v1/kb/image-search
{
    "image_url": "https://example.com/diagram.png",
    "dataset_id": "docs",
    "include_text_results": true
}
```

**New Request Fields**:
- `query_image_url`: URL of image to use as query
- `cross_modal`: Enable cross-modal retrieval (default: true)
- `include_associated_images`: Include images linked to text chunks

## Usage Examples

### Ingesting Documents with Images

```python
from src.services.knowledge.ingestion import DocumentImageExtractor
from src.services.knowledge.embedding import create_unified_embedding

# Extract images
extractor = DocumentImageExtractor()
result = await extractor.extract_from_file("manual.pdf")

# Create unified embeddings
embedder = create_unified_embedding()

# Embed text chunks
text_vectors = await embedder.embed_texts(result.text_chunks)

# Embed extracted images
image_vectors = await embedder.embed_images([
    img.image_data for img in result.images
])

# Store in same collection for cross-modal search
```

### Cross-Modal Retrieval

```python
from src.services.knowledge import KnowledgeService

kb_service = KnowledgeService(...)

# Text query finding relevant images
results, meta = await kb_service.retrieve_with_images(
    dataset_id="docs",
    query="network architecture",
    top_k=5,
    include_images=True,
    content_type_filter=["text", "image"],
)

# Results include both text chunks and relevant images
for r in results:
    print(f"Type: {r.content_type}, Score: {r.score}")
    if r.image_url:
        print(f"Image: {r.image_url}")
```

## Configuration

### Environment Variables

```bash
# Multimodal embedding model
UNIFIED_EMBEDDING_MODEL=tongyi-embedding-vision-plus

# Image extraction settings
MAX_IMAGE_SIZE_BYTES=2097152  # 2MB
MIN_IMAGE_WIDTH=50
MIN_IMAGE_HEIGHT=50

# VLM for image descriptions
VLM_MODEL=qwen-vl-max
```

### Embedding Provider Setup

```python
from src.services.knowledge.embedding import create_embedding

# Create unified multimodal embedding
embedding = create_embedding(
    provider="unified_multimodal",
    model_name="tongyi-embedding-vision-plus",
)
```

## Testing

Run multimodal RAG tests:

```bash
# Unit tests
pytest tests/api/test_kb_tools.py -v

# Specific multimodal tests
pytest tests/api/test_kb_tools.py::test_kb_search_with_images -v
```

## Dependencies

Required packages for document processing:

```
PyMuPDF>=1.24.0      # PDF image extraction
python-docx>=1.1.0   # DOCX image extraction
beautifulsoup4>=4.12 # HTML parsing
Pillow>=10.0.0       # Image processing
```

## Future Enhancements

- [ ] MultimodalIngestionPipeline for unified document processing
- [ ] ModalityAwareRetriever for advanced cross-modal scoring
- [ ] Image-to-image similarity search
- [ ] VLM description generation during ingestion
- [ ] Frontend image display in search results

## Related Documentation

- [Agent Integration Guide](agent_integration.md) - LangGraph/LangChain integration
- [Image Multimodal Plan](image-multimodal-plan.md) - Original design document
