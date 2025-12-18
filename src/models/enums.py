from enum import Enum


class ServiceType(str, Enum):
    CONVERSATIONAL = "conversational"
    GENERATIVE = "generative"
    PROCESSING = "processing"
    EMBEDDING = "embedding"
    CLASSIFICATION = "classification"
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
    SEGMENTING = "segmenting"
    EMBEDDING = "embedding"
    COMPLETED = "completed"
    FAILED = "failed"


class DatasetPermission(str, Enum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"
