"""
结构化日志模块

提供：
- 结构化日志输出（JSON 格式）
- 上下文信息自动注入（trace_id, user_id 等）
- 按模块配置日志级别
- 敏感信息脱敏
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# 上下文变量，用于在请求生命周期内传递日志上下文
_log_context: ContextVar[LogContext | None] = ContextVar("log_context", default=None)


@dataclass
class LogContext:
    """
    日志上下文

    在请求处理过程中携带上下文信息，自动注入到日志中。
    """

    trace_id: str | None = None
    span_id: str | None = None
    user_id: str | None = None
    tenant_id: str | None = None
    service_id: str | None = None
    session_id: str | None = None
    client_ip: str | None = None

    # 额外的自定义字段
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        result = {}

        if self.trace_id:
            result["trace_id"] = self.trace_id
        if self.span_id:
            result["span_id"] = self.span_id
        if self.user_id:
            result["user_id"] = self.user_id
        if self.tenant_id:
            result["tenant_id"] = self.tenant_id
        if self.service_id:
            result["service_id"] = self.service_id
        if self.session_id:
            result["session_id"] = self.session_id
        if self.client_ip:
            result["client_ip"] = self.client_ip

        if self.extra:
            result.update(self.extra)

        return result

    def with_span(self, span_id: str) -> LogContext:
        """创建带有新 span_id 的上下文副本"""
        return LogContext(
            trace_id=self.trace_id,
            span_id=span_id,
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            service_id=self.service_id,
            session_id=self.session_id,
            client_ip=self.client_ip,
            extra=self.extra.copy(),
        )


def get_log_context() -> LogContext | None:
    """获取当前日志上下文"""
    return _log_context.get()


def set_log_context(context: LogContext) -> None:
    """设置当前日志上下文"""
    _log_context.set(context)


def clear_log_context() -> None:
    """清除当前日志上下文"""
    _log_context.set(None)


# 敏感字段列表（这些字段的值会被脱敏）
SENSITIVE_FIELDS: set[str] = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
    "access_token",
    "refresh_token",
}


def _mask_sensitive(value: Any, field_name: str = "") -> Any:
    """
    脱敏敏感数据

    Args:
        value: 待处理的值
        field_name: 字段名称（用于判断是否需要脱敏）
    """
    if field_name.lower() in SENSITIVE_FIELDS:
        if isinstance(value, str) and len(value) > 4:
            return f"{value[:2]}***{value[-2:]}"
        return "***"

    if isinstance(value, dict):
        return {k: _mask_sensitive(v, k) for k, v in value.items()}

    if isinstance(value, list):
        return [_mask_sensitive(item) for item in value]

    return value


class StructuredFormatter(logging.Formatter):
    """
    结构化日志格式化器

    输出 JSON 格式的日志，包含：
    - 时间戳
    - 日志级别
    - 模块名称
    - 消息
    - 上下文信息（trace_id, user_id 等）
    - 额外字段
    """

    def __init__(
        self,
        include_timestamp: bool = True,
        include_level: bool = True,
        include_logger: bool = True,
        mask_sensitive: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.include_timestamp = include_timestamp
        self.include_level = include_level
        self.include_logger = include_logger
        self.mask_sensitive = mask_sensitive

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录"""
        # 基础字段
        log_data: dict[str, Any] = {}

        if self.include_timestamp:
            log_data["timestamp"] = datetime.utcnow().isoformat() + "Z"

        if self.include_level:
            log_data["level"] = record.levelname

        if self.include_logger:
            log_data["logger"] = record.name

        log_data["message"] = record.getMessage()

        # 添加上下文信息
        context = get_log_context()
        if context:
            log_data.update(context.to_dict())

        # 添加 record 中的额外字段
        extra_fields = {}
        for key, value in record.__dict__.items():
            if key not in {
                "name",
                "msg",
                "args",
                "created",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "exc_info",
                "exc_text",
                "thread",
                "threadName",
                "message",
            }:
                extra_fields[key] = value

        if extra_fields:
            if self.mask_sensitive:
                extra_fields = _mask_sensitive(extra_fields)
            log_data["extra"] = extra_fields

        # 添加异常信息
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data, ensure_ascii=False, default=str)


class SimpleFormatter(logging.Formatter):
    """
    简单日志格式化器（用于开发环境）

    输出人类可读的格式，同时包含上下文信息。
    """

    COLORS = {
        "DEBUG": "\033[36m",  # 青色
        "INFO": "\033[32m",  # 绿色
        "WARNING": "\033[33m",  # 黄色
        "ERROR": "\033[31m",  # 红色
        "CRITICAL": "\033[35m",  # 紫色
    }
    RESET = "\033[0m"

    def __init__(self, use_colors: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录"""
        # 时间和级别
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        level = record.levelname

        if self.use_colors:
            color = self.COLORS.get(level, "")
            level_str = f"{color}{level:8}{self.RESET}"
        else:
            level_str = f"{level:8}"

        # 基础消息
        message = record.getMessage()

        # 上下文信息
        context_parts = []
        context = get_log_context()
        if context:
            if context.trace_id:
                context_parts.append(f"trace={context.trace_id[:8]}")
            if context.user_id:
                context_parts.append(f"user={context.user_id}")
            if context.service_id:
                context_parts.append(f"service={context.service_id}")

        context_str = f" [{', '.join(context_parts)}]" if context_parts else ""

        # 组合输出
        output = f"{timestamp} {level_str} {record.name}{context_str} - {message}"

        # 异常信息
        if record.exc_info:
            output += "\n" + self.formatException(record.exc_info)

        return output


class ContextLogger(logging.Logger):
    """
    支持上下文的 Logger

    自动注入当前请求的上下文信息到日志中。
    """

    def _log(
        self,
        level: int,
        msg: str,
        args,
        exc_info=None,
        extra=None,
        stack_info=False,
        stacklevel=1,
    ) -> None:
        """覆写 _log 方法，注入上下文"""
        if extra is None:
            extra = {}

        # 注入上下文
        context = get_log_context()
        if context:
            context_dict = context.to_dict()
            for key, value in context_dict.items():
                if key not in extra:
                    extra[key] = value

        super()._log(level, msg, args, exc_info, extra, stack_info, stacklevel + 1)


def get_logger(name: str) -> logging.Logger:
    """
    获取 Logger 实例

    Args:
        name: Logger 名称（通常使用 __name__）

    Returns:
        配置好的 Logger 实例
    """
    return logging.getLogger(name)


def configure_structured_logging(
    level: str = "INFO",
    format_type: str = "json",
    module_levels: dict[str, str] | None = None,
    mask_sensitive: bool = True,
    log_to_file: bool = True,
    log_dir: str | None = None,
    log_file_max_bytes: int = 10 * 1024 * 1024,  # 10MB
    log_file_backup_count: int = 5,
) -> None:
    """
    配置结构化日志

    Args:
        level: 默认日志级别
        format_type: 格式类型 ("json" 或 "simple")
        module_levels: 模块级别的日志级别配置
        mask_sensitive: 是否脱敏敏感信息
        log_to_file: 是否同时写入文件
        log_dir: 日志目录（默认为项目根目录下的 logs/）
        log_file_max_bytes: 单个日志文件最大大小
        log_file_backup_count: 保留的日志文件数量
    """
    # 设置自定义 Logger 类
    logging.setLoggerClass(ContextLogger)

    # 配置根 Logger
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(getattr(logging, level.upper()))

    # 创建控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    if format_type == "json":
        console_formatter = StructuredFormatter(mask_sensitive=mask_sensitive)
    else:
        console_formatter = SimpleFormatter(use_colors=sys.stdout.isatty())
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # 创建文件处理器
    if log_to_file:
        if log_dir is None:
            # 默认日志目录：项目根目录/logs
            project_root = Path(__file__).parent.parent.parent.parent
            log_dir = str(project_root / "logs")

        # 确保日志目录存在
        Path(log_dir).mkdir(parents=True, exist_ok=True)

        # 主日志文件（所有级别）
        log_file = os.path.join(log_dir, "app.log")
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=log_file_max_bytes,
            backupCount=log_file_backup_count,
            encoding="utf-8",
        )
        # 文件使用简单格式，不带颜色
        file_formatter = SimpleFormatter(use_colors=False)
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.DEBUG)  # 文件记录所有级别
        root_logger.addHandler(file_handler)

        # 错误日志文件（仅 ERROR 及以上）
        error_log_file = os.path.join(log_dir, "error.log")
        error_handler = RotatingFileHandler(
            error_log_file,
            maxBytes=log_file_max_bytes,
            backupCount=log_file_backup_count,
            encoding="utf-8",
        )
        error_handler.setFormatter(file_formatter)
        error_handler.setLevel(logging.ERROR)
        root_logger.addHandler(error_handler)

        # 打印日志文件位置
        print(f"📝 Logging to: {log_file}")
        print(f"📝 Error log: {error_log_file}")

    # 配置模块级别
    if module_levels:
        for module_name, module_level in module_levels.items():
            logging.getLogger(module_name).setLevel(getattr(logging, module_level.upper()))

    # 降低第三方库的日志级别
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
