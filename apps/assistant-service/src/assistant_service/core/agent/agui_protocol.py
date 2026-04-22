"""
AG-UI Protocol Implementation for AI Gateway.

This module implements the AG-UI (Agent-UI) Protocol for real-time
agent-to-frontend communication via Server-Sent Events (SSE).

AG-UI Protocol defines a standardized set of events for:
- Lifecycle management (run start/finish, step tracking)
- Text streaming (message start/content/end)
- Tool calls (start/args/result/end)
- State management (snapshots, deltas)
- Artifacts (files, documents, images)

Reference: https://docs.ag-ui.com/concepts/architecture

Usage:
    from assistant_service.core.agent.agui_protocol import AGUIEventEmitter

    emitter = AGUIEventEmitter(request_id="req-123")

    # Emit run start
    yield emitter.run_started(run_id="run-456")

    # Emit text streaming
    yield emitter.text_message_start(message_id="msg-789")
    yield emitter.text_message_content("Hello, ")
    yield emitter.text_message_content("world!")
    yield emitter.text_message_end()

    # Emit tool call
    yield emitter.tool_call_start(tool_call_id="tc-1", tool_name="search")
    yield emitter.tool_call_args('{"query": "weather"}')
    yield emitter.tool_call_end(result={"temp": 25})

    # Emit run finish
    yield emitter.run_finished()
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ai_gateway_core.logging import get_logger
from ai_gateway_core.enums import StreamEventType

logger = get_logger(__name__)


# =============================================================================
# AG-UI Event Data Classes
# =============================================================================


@dataclass
class BaseEvent:
    """Base class for all AG-UI events."""

    event: str
    timestamp: float = field(default_factory=time.time)
    raw_event: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert event to dictionary for JSON serialization."""
        result = {
            "event": self.event,
            "timestamp": self.timestamp,
        }
        if self.raw_event:
            result["raw_event"] = self.raw_event
        return result


@dataclass
class RunLifecycleEvent(BaseEvent):
    """Run lifecycle events (started, finished, error)."""

    run_id: str = ""
    thread_id: str | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result["run_id"] = self.run_id
        if self.thread_id:
            result["thread_id"] = self.thread_id
        if self.metadata:
            result["metadata"] = self.metadata
        return result


@dataclass
class StepEvent(BaseEvent):
    """Step lifecycle events."""

    step_id: str = ""
    step_name: str = ""
    step_type: str = ""
    parent_step_id: str | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result["step_id"] = self.step_id
        result["step_name"] = self.step_name
        result["step_type"] = self.step_type
        if self.parent_step_id:
            result["parent_step_id"] = self.parent_step_id
        if self.metadata:
            result["metadata"] = self.metadata
        return result


@dataclass
class TextMessageEvent(BaseEvent):
    """Text message events."""

    message_id: str = ""
    role: str = "assistant"
    content: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result["message_id"] = self.message_id
        result["role"] = self.role
        if self.content:
            result["content"] = self.content
        return result


@dataclass
class ToolCallEvent(BaseEvent):
    """Tool call events."""

    tool_call_id: str = ""
    tool_name: str = ""
    arguments: str = ""
    result: Any | None = None
    error: str | None = None
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result["tool_call_id"] = self.tool_call_id
        result["tool_name"] = self.tool_name
        if self.arguments:
            result["arguments"] = self.arguments
        if self.result is not None:
            result["result"] = self.result
        if self.error:
            result["error"] = self.error
        result["status"] = self.status
        return result


@dataclass
class StateEvent(BaseEvent):
    """State management events."""

    state: dict[str, Any] | None = None
    delta: list[dict[str, Any]] | None = None  # JSON Patch operations

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if self.state is not None:
            result["state"] = self.state
        if self.delta is not None:
            result["delta"] = self.delta
        return result


@dataclass
class ArtifactEvent(BaseEvent):
    """Artifact creation events."""

    artifact_id: str = ""
    artifact_type: str = ""  # file, document, image, code
    name: str = ""
    mime_type: str | None = None
    url: str | None = None
    content: str | None = None
    size: int | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result["artifact_id"] = self.artifact_id
        result["artifact_type"] = self.artifact_type
        result["name"] = self.name
        if self.mime_type:
            result["mime_type"] = self.mime_type
        if self.url:
            result["url"] = self.url
        if self.content:
            result["content"] = self.content
        if self.size:
            result["size"] = self.size
        if self.metadata:
            result["metadata"] = self.metadata
        return result


@dataclass
class StatusEvent(BaseEvent):
    """Status update events (ReAct phases, progress)."""

    status: str = ""
    message: str = ""
    progress: float | None = None
    phase: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result["status"] = self.status
        result["message"] = self.message
        if self.progress is not None:
            result["progress"] = self.progress
        if self.phase:
            result["phase"] = self.phase
        return result


# =============================================================================
# AG-UI Event Emitter
# =============================================================================


class AGUIEventEmitter:
    """
    AG-UI Protocol event emitter.

    Creates properly formatted SSE events following the AG-UI protocol.
    Each method returns a formatted SSE string ready to be yielded in a streaming response.

    Usage:
        emitter = AGUIEventEmitter(request_id="req-123")

        async def stream_generator():
            yield emitter.run_started(run_id="run-456")
            yield emitter.text_message_start(message_id="msg-789")
            for token in tokens:
                yield emitter.text_message_content(token)
            yield emitter.text_message_end()
            yield emitter.run_finished()
    """

    def __init__(
        self,
        request_id: str,
        run_id: str | None = None,
        thread_id: str | None = None,
    ):
        """
        Initialize the event emitter.

        Args:
            request_id: Unique request identifier
            run_id: Optional run identifier (generated if not provided)
            thread_id: Optional thread/conversation identifier
        """
        self.request_id = request_id
        self.run_id = run_id or str(uuid.uuid4())
        self.thread_id = thread_id
        self.chunk_index = 0
        self._current_message_id: str | None = None
        self._current_step_id: str | None = None

    def _format_sse(self, event_type: str, data: dict[str, Any]) -> str:
        """
        Format event data as SSE string.

        Args:
            event_type: The event type for SSE "event:" field
            data: The event data to serialize

        Returns:
            Formatted SSE string: "event: {type}\ndata: {json}\n\n"
        """
        # Create a copy to avoid mutating the input dict
        # This prevents data corruption if the same dict is reused
        output_data = {
            **data,
            "request_id": self.request_id,
            "chunk_index": self.chunk_index,
        }
        self.chunk_index += 1

        json_data = json.dumps(output_data, ensure_ascii=False, default=str)
        return f"event: {event_type}\ndata: {json_data}\n\n"

    # =========================================================================
    # Lifecycle Events
    # =========================================================================

    def run_started(
        self,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit RUN_STARTED event."""
        if run_id:
            self.run_id = run_id

        event = RunLifecycleEvent(
            event=StreamEventType.RUN_STARTED.value,
            run_id=self.run_id,
            thread_id=self.thread_id,
            metadata=metadata,
        )
        return self._format_sse(StreamEventType.RUN_STARTED.value, event.to_dict())

    def run_finished(
        self,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit RUN_FINISHED event."""
        event = RunLifecycleEvent(
            event=StreamEventType.RUN_FINISHED.value,
            run_id=self.run_id,
            thread_id=self.thread_id,
            metadata=metadata,
        )
        return self._format_sse(StreamEventType.RUN_FINISHED.value, event.to_dict())

    def run_error(
        self,
        error: str,
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit RUN_ERROR event."""
        meta = metadata or {}
        meta["error"] = error
        if error_code:
            meta["error_code"] = error_code

        event = RunLifecycleEvent(
            event=StreamEventType.RUN_ERROR.value,
            run_id=self.run_id,
            thread_id=self.thread_id,
            metadata=meta,
        )
        return self._format_sse(StreamEventType.RUN_ERROR.value, event.to_dict())

    # =========================================================================
    # Step Events
    # =========================================================================

    def step_started(
        self,
        step_name: str,
        step_type: str = "task",
        step_id: str | None = None,
        parent_step_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit STEP_STARTED event."""
        self._current_step_id = step_id or str(uuid.uuid4())

        event = StepEvent(
            event=StreamEventType.STEP_STARTED.value,
            step_id=self._current_step_id,
            step_name=step_name,
            step_type=step_type,
            parent_step_id=parent_step_id,
            metadata=metadata,
        )
        return self._format_sse(StreamEventType.STEP_STARTED.value, event.to_dict())

    def step_finished(
        self,
        step_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit STEP_FINISHED event."""
        event = StepEvent(
            event=StreamEventType.STEP_FINISHED.value,
            step_id=step_id or self._current_step_id or "",
            step_name="",
            step_type="",
            metadata=metadata,
        )
        return self._format_sse(StreamEventType.STEP_FINISHED.value, event.to_dict())

    # =========================================================================
    # Text Message Events
    # =========================================================================

    def text_message_start(
        self,
        message_id: str | None = None,
        role: str = "assistant",
    ) -> str:
        """Emit TEXT_MESSAGE_START event."""
        self._current_message_id = message_id or str(uuid.uuid4())

        event = TextMessageEvent(
            event=StreamEventType.TEXT_MESSAGE_START.value,
            message_id=self._current_message_id,
            role=role,
        )
        return self._format_sse(StreamEventType.TEXT_MESSAGE_START.value, event.to_dict())

    def text_message_content(self, content: str) -> str:
        """Emit TEXT_MESSAGE_CONTENT event (streaming text token)."""
        event = TextMessageEvent(
            event=StreamEventType.TEXT_MESSAGE_CONTENT.value,
            message_id=self._current_message_id or "",
            content=content,
        )
        return self._format_sse(StreamEventType.TEXT_MESSAGE_CONTENT.value, event.to_dict())

    def text_message_end(self) -> str:
        """Emit TEXT_MESSAGE_END event."""
        event = TextMessageEvent(
            event=StreamEventType.TEXT_MESSAGE_END.value,
            message_id=self._current_message_id or "",
        )
        return self._format_sse(StreamEventType.TEXT_MESSAGE_END.value, event.to_dict())

    # Legacy text delta support
    def text_delta(self, content: str) -> str:
        """Emit TEXT_DELTA event (legacy format)."""
        data = {
            "event": StreamEventType.TEXT_DELTA.value,
            "content": content,
            "message_id": self._current_message_id or "",
        }
        return self._format_sse(StreamEventType.TEXT_DELTA.value, data)

    # =========================================================================
    # Tool Call Events
    # =========================================================================

    def tool_call_start(
        self,
        tool_call_id: str,
        tool_name: str,
    ) -> str:
        """Emit TOOL_CALL_START event."""
        event = ToolCallEvent(
            event=StreamEventType.TOOL_CALL_START.value,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            status="running",
        )
        return self._format_sse(StreamEventType.TOOL_CALL_START.value, event.to_dict())

    def tool_call_args(
        self,
        tool_call_id: str,
        arguments: str | dict[str, Any],
    ) -> str:
        """Emit TOOL_CALL_ARGS event (streaming tool arguments)."""
        args_str = arguments if isinstance(arguments, str) else json.dumps(arguments)

        event = ToolCallEvent(
            event=StreamEventType.TOOL_CALL_ARGS.value,
            tool_call_id=tool_call_id,
            arguments=args_str,
            status="running",
        )
        return self._format_sse(StreamEventType.TOOL_CALL_ARGS.value, event.to_dict())

    def tool_call_result(
        self,
        tool_call_id: str,
        tool_name: str,
        result: Any,
    ) -> str:
        """Emit TOOL_CALL_RESULT event."""
        event = ToolCallEvent(
            event=StreamEventType.TOOL_CALL_RESULT.value,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            result=result,
            status="completed",
        )
        return self._format_sse(StreamEventType.TOOL_CALL_RESULT.value, event.to_dict())

    def tool_call_end(
        self,
        tool_call_id: str,
        tool_name: str,
        result: Any | None = None,
        error: str | None = None,
    ) -> str:
        """Emit TOOL_CALL_END event."""
        event = ToolCallEvent(
            event=StreamEventType.TOOL_CALL_END.value,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            result=result,
            error=error,
            status="error" if error else "completed",
        )
        return self._format_sse(StreamEventType.TOOL_CALL_END.value, event.to_dict())

    def tool_error(
        self,
        tool_call_id: str,
        tool_name: str,
        error: str,
        error_code: str | None = None,
    ) -> str:
        """Emit TOOL_ERROR event."""
        data = {
            "event": StreamEventType.TOOL_ERROR.value,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "error": error,
            "status": "error",
        }
        if error_code:
            data["error_code"] = error_code
        return self._format_sse(StreamEventType.TOOL_ERROR.value, data)

    # =========================================================================
    # State Management Events
    # =========================================================================

    def state_snapshot(self, state: dict[str, Any]) -> str:
        """Emit STATE_SNAPSHOT event (full state)."""
        event = StateEvent(
            event=StreamEventType.STATE_SNAPSHOT.value,
            state=state,
        )
        return self._format_sse(StreamEventType.STATE_SNAPSHOT.value, event.to_dict())

    def state_delta(self, delta: list[dict[str, Any]]) -> str:
        """Emit STATE_DELTA event (JSON Patch operations)."""
        event = StateEvent(
            event=StreamEventType.STATE_DELTA.value,
            delta=delta,
        )
        return self._format_sse(StreamEventType.STATE_DELTA.value, event.to_dict())

    def messages_snapshot(self, messages: list[dict[str, Any]]) -> str:
        """Emit MESSAGES_SNAPSHOT event."""
        data = {
            "event": StreamEventType.MESSAGES_SNAPSHOT.value,
            "messages": messages,
        }
        return self._format_sse(StreamEventType.MESSAGES_SNAPSHOT.value, data)

    # =========================================================================
    # Artifact Events
    # =========================================================================

    def artifact_created(
        self,
        artifact_id: str,
        artifact_type: str,
        name: str,
        url: str | None = None,
        content: str | None = None,
        mime_type: str | None = None,
        size: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit ARTIFACT_CREATED event."""
        event = ArtifactEvent(
            event=StreamEventType.ARTIFACT_CREATED.value,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            name=name,
            url=url,
            content=content,
            mime_type=mime_type,
            size=size,
            metadata=metadata,
        )
        return self._format_sse(StreamEventType.ARTIFACT_CREATED.value, event.to_dict())

    def file_creating(
        self,
        file_name: str,
        file_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit FILE_CREATING event."""
        data = {
            "event": StreamEventType.FILE_CREATING.value,
            "file_name": file_name,
            "file_type": file_type,
            "status": "creating",
        }
        if metadata:
            data["metadata"] = metadata
        return self._format_sse(StreamEventType.FILE_CREATING.value, data)

    def file_created(
        self,
        file_id: str,
        file_name: str,
        file_type: str,
        url: str,
        size: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit FILE_CREATED event."""
        data = {
            "event": StreamEventType.FILE_CREATED.value,
            "file_id": file_id,
            "file_name": file_name,
            "file_type": file_type,
            "url": url,
            "status": "completed",
        }
        if size:
            data["size"] = size
        if metadata:
            data["metadata"] = metadata
        return self._format_sse(StreamEventType.FILE_CREATED.value, data)

    # =========================================================================
    # Document Generation Events (Manus-style)
    # =========================================================================

    def outline_ready(
        self,
        title: str,
        sections: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit OUTLINE_READY event."""
        data = {
            "event": StreamEventType.OUTLINE_READY.value,
            "title": title,
            "sections": sections,
        }
        if metadata:
            data["metadata"] = metadata
        return self._format_sse(StreamEventType.OUTLINE_READY.value, data)

    def document_generation_start(
        self,
        doc_type: str,
        title: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit DOCUMENT_GENERATION_START event."""
        data = {
            "event": StreamEventType.DOCUMENT_GENERATION_START.value,
            "doc_type": doc_type,
            "title": title,
            "status": "generating",
        }
        if metadata:
            data["metadata"] = metadata
        return self._format_sse(StreamEventType.DOCUMENT_GENERATION_START.value, data)

    def document_generation_result(
        self,
        doc_type: str,
        title: str,
        url: str,
        file_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit DOCUMENT_GENERATION_RESULT event."""
        data = {
            "event": StreamEventType.DOCUMENT_GENERATION_RESULT.value,
            "doc_type": doc_type,
            "title": title,
            "url": url,
            "status": "completed",
        }
        if file_id:
            data["file_id"] = file_id
        if metadata:
            data["metadata"] = metadata
        return self._format_sse(StreamEventType.DOCUMENT_GENERATION_RESULT.value, data)

    # =========================================================================
    # Search Events
    # =========================================================================

    def search_started(
        self,
        query: str,
        search_type: str = "knowledge_base",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit SEARCH_STARTED event."""
        data = {
            "event": StreamEventType.SEARCH_STARTED.value,
            "query": query,
            "search_type": search_type,
            "status": "searching",
        }
        if metadata:
            data["metadata"] = metadata
        return self._format_sse(StreamEventType.SEARCH_STARTED.value, data)

    def search_progress(
        self,
        query: str,
        progress: float,
        message: str = "",
    ) -> str:
        """Emit SEARCH_PROGRESS event."""
        data = {
            "event": StreamEventType.SEARCH_PROGRESS.value,
            "query": query,
            "progress": progress,
            "message": message,
        }
        return self._format_sse(StreamEventType.SEARCH_PROGRESS.value, data)

    def search_completed(
        self,
        query: str,
        result_count: int,
        results: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit SEARCH_COMPLETED event."""
        data = {
            "event": StreamEventType.SEARCH_COMPLETED.value,
            "query": query,
            "result_count": result_count,
            "status": "completed",
        }
        if results:
            data["results"] = results
        if metadata:
            data["metadata"] = metadata
        return self._format_sse(StreamEventType.SEARCH_COMPLETED.value, data)

    # =========================================================================
    # Code Execution Events
    # =========================================================================

    def code_execution_start(
        self,
        code: str,
        language: str = "python",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit CODE_EXECUTION_START event."""
        data = {
            "event": StreamEventType.CODE_EXECUTION_START.value,
            "code": code,
            "language": language,
            "status": "executing",
        }
        if metadata:
            data["metadata"] = metadata
        return self._format_sse(StreamEventType.CODE_EXECUTION_START.value, data)

    def code_execution_result(
        self,
        output: str,
        success: bool = True,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit CODE_EXECUTION_RESULT event."""
        data = {
            "event": StreamEventType.CODE_EXECUTION_RESULT.value,
            "output": output,
            "success": success,
            "status": "completed" if success else "error",
        }
        if error:
            data["error"] = error
        if metadata:
            data["metadata"] = metadata
        return self._format_sse(StreamEventType.CODE_EXECUTION_RESULT.value, data)

    # =========================================================================
    # Image Generation Events
    # =========================================================================

    def image_generation_start(
        self,
        prompt: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit IMAGE_GENERATION_START event."""
        data = {
            "event": StreamEventType.IMAGE_GENERATION_START.value,
            "prompt": prompt,
            "status": "generating",
        }
        if metadata:
            data["metadata"] = metadata
        return self._format_sse(StreamEventType.IMAGE_GENERATION_START.value, data)

    def image_generation_result(
        self,
        url: str,
        prompt: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit IMAGE_GENERATION_RESULT event."""
        data = {
            "event": StreamEventType.IMAGE_GENERATION_RESULT.value,
            "url": url,
            "prompt": prompt,
            "status": "completed",
        }
        if metadata:
            data["metadata"] = metadata
        return self._format_sse(StreamEventType.IMAGE_GENERATION_RESULT.value, data)

    # =========================================================================
    # Status Events
    # =========================================================================

    def status(
        self,
        status: str,
        message: str,
        phase: str | None = None,
        progress: float | None = None,
    ) -> str:
        """Emit STATUS event (ReAct phases, progress)."""
        event = StatusEvent(
            event=StreamEventType.STATUS.value,
            status=status,
            message=message,
            phase=phase,
            progress=progress,
        )
        return self._format_sse(StreamEventType.STATUS.value, event.to_dict())

    # =========================================================================
    # Special Events
    # =========================================================================

    def custom_event(
        self,
        event_name: str,
        data: dict[str, Any],
    ) -> str:
        """Emit CUSTOM_EVENT."""
        payload = {
            "event": StreamEventType.CUSTOM_EVENT.value,
            "custom_event_name": event_name,
            "data": data,
        }
        return self._format_sse(StreamEventType.CUSTOM_EVENT.value, payload)

    def raw_event(
        self,
        source: str,
        raw_data: dict[str, Any],
    ) -> str:
        """Emit RAW_EVENT (passthrough external events)."""
        payload = {
            "event": StreamEventType.RAW_EVENT.value,
            "source": source,
            "raw_data": raw_data,
        }
        return self._format_sse(StreamEventType.RAW_EVENT.value, payload)

    def stream_end(self) -> str:
        """Emit STREAM_END event."""
        data = {
            "event": StreamEventType.STREAM_END.value,
            "is_final": True,
        }
        return self._format_sse(StreamEventType.STREAM_END.value, data)


# =============================================================================
# Factory Functions
# =============================================================================


def create_agui_emitter(
    request_id: str,
    run_id: str | None = None,
    thread_id: str | None = None,
) -> AGUIEventEmitter:
    """
    Factory function to create an AG-UI event emitter.

    Args:
        request_id: Unique request identifier
        run_id: Optional run identifier
        thread_id: Optional thread/conversation identifier

    Returns:
        Configured AGUIEventEmitter instance
    """
    return AGUIEventEmitter(
        request_id=request_id,
        run_id=run_id,
        thread_id=thread_id,
    )
