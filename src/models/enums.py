from enum import Enum


class ServiceType(str, Enum):
    CONVERSATIONAL = "conversational"
    GENERATIVE = "generative"
    PROCESSING = "processing"
    EMBEDDING = "embedding"
    CLASSIFICATION = "classification"
    LANGGRAPH = "langgraph"
    CUSTOM = "custom"


class InvocationMode(str, Enum):
    SYNC = "sync"
    ASYNC = "async"
    STREAM = "stream"
    WEBHOOK = "webhook"


class ConnectorType(str, Enum):
    HTTP = "http"
    GRPC = "grpc"
    WEBSOCKET = "websocket"
    MESSAGE_QUEUE = "mq"
    IN_PROCESS = "in_process"


class ContentType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"
    JSON = "json"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


class StreamEventType(str, Enum):
    """Streaming event types."""

    TEXT_DELTA = "text_delta"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_END = "tool_call_end"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"
    FINAL = "final"


class DatasetVisibility(str, Enum):
    PRIVATE = "private"
    TENANT = "tenant"
    PUBLIC = "public"


class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    PARSING = "parsing"
    CLEANING = "cleaning"
    SPLITTING = "splitting"
    SEGMENTING = "segmenting"  # alias for splitting
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class SegmentStatus(str, Enum):
    """Segment indexing status"""
    WAITING = "waiting"
    INDEXING = "indexing"
    COMPLETED = "completed"
    ERROR = "error"


class DatasetPermission(str, Enum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class RetrievalMethod(str, Enum):
    """Retrieval method types - matches Dify's RetrievalMethod"""
    SEMANTIC_SEARCH = "semantic_search"
    FULL_TEXT_SEARCH = "full_text_search"
    HYBRID_SEARCH = "hybrid_search"


class DataSourceType(str, Enum):
    """Document data source types"""
    UPLOAD_FILE = "upload_file"
    NOTION_IMPORT = "notion_import"
    WEBSITE_CRAWL = "website_crawl"
    TEXT_INPUT = "text"
    URL_IMPORT = "url"
