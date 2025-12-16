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
    """流式事件类型"""
    TEXT_DELTA = "text_delta"  # 文本增量
    TOOL_CALL_START = "tool_call_start"  # 工具调用开始
    TOOL_CALL_DELTA = "tool_call_delta"  # 工具调用参数增量
    TOOL_CALL_END = "tool_call_end"  # 工具调用结束
    TOOL_RESULT = "tool_result"  # 工具执行结果
    THINKING = "thinking"  # 思考过程
    FINAL = "final"  # 最终结果
