from __future__ import annotations

import importlib
import json
from typing import Any, AsyncIterator, Dict, List, Optional

from ..core.exceptions import ValidationFailedError
from ..models.enums import ContentType, ConnectorType, StreamEventType
from ..models.request import ContentItem, UnifiedRequest
from ..models.response import StreamChunk, ToolCall, UnifiedResponse
from .base import ProtocolAdapter


# 定义流事件的结构
from dataclasses import dataclass


@dataclass
class StreamEvent:
    """流事件封装"""
    event_type: StreamEventType
    text: Optional[str] = None
    tool_call: Optional[ToolCall] = None


class LangGraphAdapter(ProtocolAdapter):
    # 类级别的 session_id -> thread_id (UUID) 映射缓存
    _session_to_thread_map: Dict[str, str] = {}
    
    def __init__(self, service):
        super().__init__(service)
        config = service.connector_config or {}
        self.remote = service.connector_type != ConnectorType.IN_PROCESS
        self.assistant_id = (
            config.get("assistant_id")
            or config.get("graph_id")
            or config.get("graph_name")
            or service.service_id
        )
        if not self.remote:
            module_path = config.get("graph_module")
            graph_name = config.get("graph_name")
            if not module_path or not graph_name:
                raise ValidationFailedError("graph_module and graph_name are required")
            module = importlib.import_module(module_path)
            graph_obj = getattr(module, graph_name)
            if callable(graph_obj) and not hasattr(graph_obj, "ainvoke"):
                graph_obj = graph_obj()
            self.graph = graph_obj
        else:
            self.graph = None
            self.invoke_endpoint = config.get("invoke_endpoint", "/runs/wait")
            self.stream_endpoint = config.get("stream_endpoint", "/runs/stream")
            self.thread_invoke_endpoint = config.get(
                "thread_invoke_endpoint", "/threads/{thread_id}/runs/wait"
            )
            self.thread_stream_endpoint = config.get(
                "thread_stream_endpoint", "/threads/{thread_id}/runs/stream"
            )

    async def invoke(self, request: UnifiedRequest) -> UnifiedResponse:
        messages = self._build_messages(request.inputs)
        if not self.remote:
            config = {
                "configurable": {
                    "thread_id": request.session_id or request.request_id,
                    "checkpoint_ns": request.tenant_id or "",
                }
            }
            result = await self.graph.ainvoke({"messages": messages}, config)
        else:
            result = await self._remote_wait(request, messages)
        content = self._extract_result_text(result)
        return UnifiedResponse(
            request_id=request.request_id,
            status="success",
            outputs=[ContentItem(type=ContentType.TEXT, data=content)],
            session_id=request.session_id,
        )

    async def stream(self, request: UnifiedRequest) -> AsyncIterator[StreamChunk]:
        messages = self._build_messages(request.inputs)
        idx = 0
        if not self.remote:
            if not hasattr(self.graph, "astream_events"):
                raise ValidationFailedError("graph does not support streaming")
            config = {
                "configurable": {
                    "thread_id": request.session_id or request.request_id,
                    "checkpoint_ns": request.tenant_id or "",
                }
            }
            # 追踪已处理的内容，用于去重
            seen_content_ids: set = set()

            async for event in self.graph.astream_events({"messages": messages}, config, version="v2"):
                stream_event = self._extract_stream_event(event, seen_content_ids)
                if stream_event:
                    yield StreamChunk(
                        request_id=request.request_id,
                        chunk_index=idx,
                        content=ContentItem(
                            type=ContentType.TEXT if stream_event.text else ContentType.TOOL_CALL,
                            data=stream_event.text or "",
                        ),
                        event_type=stream_event.event_type,
                        tool_call=stream_event.tool_call,
                    )
                    idx += 1
        else:
            async for stream_event in self._remote_stream(request, messages):
                if stream_event:
                    yield StreamChunk(
                        request_id=request.request_id,
                        chunk_index=idx,
                        content=ContentItem(
                            type=ContentType.TEXT if stream_event.text else ContentType.TOOL_CALL,
                            data=stream_event.text or "",
                        ),
                        event_type=stream_event.event_type,
                        tool_call=stream_event.tool_call,
                    )
                    idx += 1

        yield StreamChunk(
            request_id=request.request_id,
            chunk_index=idx,
            content=ContentItem(type=ContentType.TEXT, data=""),
            is_final=True,
            event_type=StreamEventType.FINAL,
        )

    async def health_check(self) -> bool:
        if not self.remote:
            return self.graph is not None
        return await self.connector.health_check()

    def _build_messages(self, inputs: List[ContentItem]) -> List[Dict[str, Any]]:
        texts = [str(i.data) for i in inputs if i.type == ContentType.TEXT and i.data]
        if not texts:
            raise ValidationFailedError("text input required")
        return [{"role": "user", "content": "\n".join(texts)}]

    def _extract_result_text(self, result: Any) -> str:
        if isinstance(result, dict):
            messages = result.get("messages")
            if not messages and isinstance(result.get("values"), dict):
                messages = result["values"].get("messages")
            if not messages and isinstance(result.get("output"), dict):
                messages = result["output"].get("messages")
            if isinstance(messages, list) and messages:
                # 从后向前遍历，找到最后一条 AI 消息
                for msg in reversed(messages):
                    if isinstance(msg, dict):
                        role = msg.get("role", "") or msg.get("type", "")
                        # 只返回 AI 消息内容
                        if role in ("assistant", "ai", "AIMessage"):
                            return str(msg.get("content") or "")
                    elif hasattr(msg, "type"):
                        msg_type = getattr(msg, "type", "")
                        if msg_type in ("ai", "AIMessage"):
                            return str(getattr(msg, "content", msg))
                # 如果没找到明确的 AI 消息，尝试返回最后一条非用户消息
                last = messages[-1]
                if isinstance(last, dict):
                    role = last.get("role", "") or last.get("type", "")
                    if role not in ("user", "human", "HumanMessage"):
                        return str(last.get("content") or "")
                elif hasattr(last, "type"):
                    msg_type = getattr(last, "type", "")
                    if msg_type not in ("human", "HumanMessage"):
                        return str(getattr(last, "content", last))
        return str(result)

    def _extract_stream_event(self, event: Any, seen_ids: set) -> Optional[StreamEvent]:
        """从本地 LangGraph 的 astream_events 中提取流事件"""
        if not isinstance(event, dict):
            return None

        event_type = event.get("event", "")
        run_id = event.get("run_id", "")
        data = event.get("data") or {}

        # 处理 AI 模型的流式文本输出
        if event_type == "on_chat_model_stream":
            chunk = data.get("chunk")
            if chunk is not None:
                # 生成唯一标识符来去重
                chunk_id = None
                if hasattr(chunk, "id"):
                    chunk_id = chunk.id
                elif isinstance(chunk, dict):
                    chunk_id = chunk.get("id")

                # 如果有 ID 且已处理过，跳过
                if chunk_id and chunk_id in seen_ids:
                    return None
                if chunk_id:
                    seen_ids.add(chunk_id)

                # 检查是否有工具调用
                tool_calls = None
                if hasattr(chunk, "tool_call_chunks"):
                    tool_calls = chunk.tool_call_chunks
                elif hasattr(chunk, "tool_calls"):
                    tool_calls = chunk.tool_calls
                elif isinstance(chunk, dict):
                    tool_calls = chunk.get("tool_call_chunks") or chunk.get("tool_calls")

                # 处理工具调用增量
                if tool_calls and len(tool_calls) > 0:
                    tc = tool_calls[0]
                    if isinstance(tc, dict):
                        tc_id = tc.get("id") or tc.get("tool_call_id") or ""
                        tc_name = tc.get("name") or ""
                        tc_args = tc.get("args") or tc.get("arguments") or ""
                    else:
                        tc_id = getattr(tc, "id", "") or getattr(tc, "tool_call_id", "") or ""
                        tc_name = getattr(tc, "name", "") or ""
                        tc_args = getattr(tc, "args", "") or getattr(tc, "arguments", "") or ""

                    if isinstance(tc_args, dict):
                        tc_args = json.dumps(tc_args)

                    # 只返回有实际内容的工具调用
                    if tc_name or tc_args:
                        return StreamEvent(
                            event_type=StreamEventType.TOOL_CALL_DELTA,
                            tool_call=ToolCall(
                                tool_call_id=tc_id,
                                name=tc_name,
                                arguments=tc_args,
                                status="running",
                            ),
                        )

                # 提取文本内容
                if hasattr(chunk, "content"):
                    content = chunk.content
                    if isinstance(content, str) and content:
                        return StreamEvent(
                            event_type=StreamEventType.TEXT_DELTA,
                            text=content,
                        )
                elif isinstance(chunk, dict):
                    content = chunk.get("content")
                    if isinstance(content, str) and content:
                        return StreamEvent(
                            event_type=StreamEventType.TEXT_DELTA,
                            text=content,
                        )
            return None

        # 处理工具调用开始事件
        if event_type == "on_tool_start":
            # 使用 run_id 去重
            event_key = f"tool_start:{run_id}"
            if event_key in seen_ids:
                return None
            seen_ids.add(event_key)

            tool_name = event.get("name", "")
            tool_input = data.get("input", {})
            if isinstance(tool_input, dict):
                tool_input_str = json.dumps(tool_input, ensure_ascii=False)
            else:
                tool_input_str = str(tool_input)

            return StreamEvent(
                event_type=StreamEventType.TOOL_CALL_START,
                tool_call=ToolCall(
                    tool_call_id=run_id,
                    name=tool_name,
                    arguments=tool_input_str,
                    status="running",
                ),
            )

        # 处理工具调用结束事件
        if event_type == "on_tool_end":
            # 使用 run_id 去重
            event_key = f"tool_end:{run_id}"
            if event_key in seen_ids:
                return None
            seen_ids.add(event_key)

            tool_name = event.get("name", "")
            output = data.get("output", "")
            if isinstance(output, dict):
                output_str = json.dumps(output, ensure_ascii=False)
            elif hasattr(output, "content"):
                output_str = str(getattr(output, "content", output))
            else:
                output_str = str(output)

            return StreamEvent(
                event_type=StreamEventType.TOOL_RESULT,
                text=output_str,
                tool_call=ToolCall(
                    tool_call_id=run_id,
                    name=tool_name,
                    arguments="",
                    status="completed",
                ),
            )

        return None

    async def _remote_wait(
        self, request: UnifiedRequest, messages: List[Dict[str, Any]]
    ) -> Any:
        input_payload: Dict[str, Any] = {"messages": messages}
        params = request.parameters or {}
        extra_input = params.get("input")
        if isinstance(extra_input, dict):
            input_payload.update(extra_input)

        payload: Dict[str, Any] = {
            "assistant_id": self.assistant_id,
            "input": input_payload,
            "metadata": {
                "request_id": request.request_id,
                "user_id": request.user_id,
                "tenant_id": request.tenant_id,
            },
        }
        if request.callback_url:
            payload["webhook"] = request.callback_url
        if isinstance(params.get("config"), dict):
            payload["config"] = params["config"]

        if self.service.session_enabled and request.session_id:
            # 确保 thread 存在并获取有效的 UUID thread_id
            valid_thread_id = await self._ensure_thread(request.session_id, request)
            endpoint = self.thread_invoke_endpoint.format(thread_id=valid_thread_id)
        else:
            endpoint = self.invoke_endpoint
        try:
            return await self.connector.post(endpoint, json=payload)
        except Exception as exc:
            # HTTPConnector uses httpx under the hood; surface readable upstream errors
            # instead of returning a generic 500.
            import httpx

            if isinstance(exc, httpx.HTTPStatusError):
                status = exc.response.status_code
                body = exc.response.text
                raise ValidationFailedError(
                    f"LangGraph invoke failed ({status}) at {endpoint}: {body}"
                ) from exc
            if isinstance(exc, httpx.RequestError):
                raise ValidationFailedError(
                    f"LangGraph invoke request error at {endpoint}: {exc}"
                ) from exc
            raise

    async def _remote_stream(
        self, request: UnifiedRequest, messages: List[Dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        input_payload: Dict[str, Any] = {"messages": messages}
        params = request.parameters or {}
        extra_input = params.get("input")
        if isinstance(extra_input, dict):
            input_payload.update(extra_input)

        payload: Dict[str, Any] = {
            "assistant_id": self.assistant_id,
            "input": input_payload,
            "stream_mode": ["messages", "updates"],  # 请求 messages 和 updates 模式以获取工具调用
            "metadata": {
                "request_id": request.request_id,
                "user_id": request.user_id,
                "tenant_id": request.tenant_id,
            },
        }
        if isinstance(params.get("config"), dict):
            payload["config"] = params["config"]

        if self.service.session_enabled and request.session_id:
            # 确保 thread 存在并获取有效的 UUID thread_id
            valid_thread_id = await self._ensure_thread(request.session_id, request)
            endpoint = self.thread_stream_endpoint.format(thread_id=valid_thread_id)
        else:
            endpoint = self.stream_endpoint

        client = getattr(self.connector, "_client", None)
        if client is None:
            raise ValidationFailedError("remote streaming requires HTTPConnector")

        # 使用更长的超时配置用于流式响应
        import httpx
        stream_timeout = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=60.0)
        
        import logging
        logger = logging.getLogger(__name__)
        
        try:
            async with client.stream(
                "POST", endpoint, json=payload, timeout=stream_timeout
            ) as resp:
                # 检查响应状态
                if resp.status_code != 200:
                    error_text = await resp.aread()
                    logger.error(
                        f"LangGraph stream error: {resp.status_code} - {error_text}"
                    )
                    raise ValidationFailedError(
                        f"LangGraph stream failed ({resp.status_code}) at {endpoint}: {error_text!r}"
                    )

                current_event_type = ""
                # 追踪累积内容长度，用于计算增量
                last_content_length = 0
                last_tool_args_length: Dict[str, int] = {}
                # 追踪当前活动的工具调用（因为后续 chunk 可能没有 id）
                current_tool_call_id = ""
                current_tool_name = ""

                async for line in resp.aiter_lines():
                    if not line:
                        current_event_type = ""
                        continue
                    # 解析 SSE event 类型
                    if line.startswith("event:"):
                        current_event_type = line[6:].strip()
                        logger.debug(f"SSE event type: {current_event_type}")
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str or data_str == "[DONE]":
                        logger.debug("SSE stream done")
                        continue
                    try:
                        data = json.loads(data_str)
                    except Exception as e:
                        logger.warning(f"Failed to parse SSE data: {e}")
                        continue
                    # 构建完整事件对象以便统一处理
                    event = (
                        {"event": current_event_type, "data": data}
                        if current_event_type
                        else data
                    )
                    result = self._extract_remote_stream_event(
                        event,
                        last_content_length,
                        last_tool_args_length,
                        current_tool_call_id,
                        current_tool_name,
                    )
                    (
                        stream_event,
                        last_content_length,
                        last_tool_args_length,
                        current_tool_call_id,
                        current_tool_name,
                    ) = result
                    if stream_event:
                        yield stream_event
        except httpx.RequestError as exc:
            raise ValidationFailedError(
                f"LangGraph stream request error at {endpoint}: {exc}"
            ) from exc
        
        logger.debug("LangGraph stream completed")

    def _extract_remote_stream_event(
        self,
        event: Any,
        last_content_length: int,
        last_tool_args_length: Dict[str, int],
        current_tool_call_id: str,
        current_tool_name: str,
    ) -> tuple[Optional[StreamEvent], int, Dict[str, int], str, str]:
        """从远程 LangGraph API 的流式事件中提取流事件（返回增量）"""
        if not isinstance(event, dict):
            return None, last_content_length, last_tool_args_length, current_tool_call_id, current_tool_name

        event_type = event.get("event", "")
        data = event.get("data") if "event" in event else event

        # 处理 messages/complete 事件 - 重置状态
        if event_type == "messages/complete":
            # 消息完成，重置累积长度以准备下一条消息
            return None, 0, {}, "", ""
        
        # 处理 end 事件
        if event_type == "end":
            return None, last_content_length, last_tool_args_length, current_tool_call_id, current_tool_name
        
        # 处理 error 事件
        if event_type == "error":
            import logging
            logging.getLogger(__name__).error(f"LangGraph stream error event: {data}")
            return None, last_content_length, last_tool_args_length, current_tool_call_id, current_tool_name
        
        # 处理 metadata 事件（忽略）
        if event_type == "metadata":
            return None, last_content_length, last_tool_args_length, current_tool_call_id, current_tool_name
        
        # LangGraph Cloud API 的 messages/partial 事件类型
        if event_type in ("messages", "messages/partial"):
            # 格式: data: [message, metadata] 或 data: message
            if isinstance(data, list) and len(data) >= 1:
                msg = data[0]
            else:
                msg = data

            if isinstance(msg, dict):
                msg_type = msg.get("type", "")

                # 优先处理 tool_call_chunks（增量格式，不需要计算增量）
                tool_call_chunks = msg.get("tool_call_chunks")
                if tool_call_chunks and isinstance(tool_call_chunks, list) and len(tool_call_chunks) > 0:
                    tc = tool_call_chunks[0]
                    # id 和 name 可能只在第一个 chunk 中有值
                    tc_id = tc.get("id") or tc.get("tool_call_id") or ""
                    tc_name = tc.get("name") or ""

                    # 如果有新的 id，更新当前追踪的工具调用
                    if tc_id:
                        current_tool_call_id = tc_id
                    if tc_name:
                        current_tool_name = tc_name

                    # tool_call_chunks 中的 args 本身就是增量，直接使用
                    tc_args = tc.get("args") or ""
                    if isinstance(tc_args, dict):
                        tc_args = json.dumps(tc_args, ensure_ascii=False)

                    # 只有当有实际内容时才返回
                    if tc_name or tc_args:
                        return StreamEvent(
                            event_type=StreamEventType.TOOL_CALL_DELTA,
                            tool_call=ToolCall(
                                tool_call_id=current_tool_call_id,
                                name=current_tool_name,
                                arguments=tc_args,
                                status="running",
                            ),
                        ), last_content_length, last_tool_args_length, current_tool_call_id, current_tool_name

                    return None, last_content_length, last_tool_args_length, current_tool_call_id, current_tool_name

                # 处理完整的 tool_calls（累积格式，需要计算增量）
                tool_calls = msg.get("tool_calls")
                if tool_calls and isinstance(tool_calls, list) and len(tool_calls) > 0:
                    tc = tool_calls[0]
                    tc_id = tc.get("id") or tc.get("tool_call_id") or ""
                    tc_name = tc.get("name") or ""
                    tc_args = tc.get("args") or tc.get("arguments") or ""
                    if isinstance(tc_args, dict):
                        tc_args = json.dumps(tc_args, ensure_ascii=False)

                    # 更新当前追踪
                    if tc_id:
                        current_tool_call_id = tc_id
                    if tc_name:
                        current_tool_name = tc_name

                    # 计算参数增量
                    key = current_tool_call_id or "default"
                    prev_len = last_tool_args_length.get(key, 0)
                    if len(tc_args) > prev_len:
                        delta_args = tc_args[prev_len:]
                        last_tool_args_length[key] = len(tc_args)

                        return StreamEvent(
                            event_type=StreamEventType.TOOL_CALL_DELTA,
                            tool_call=ToolCall(
                                tool_call_id=current_tool_call_id,
                                name=current_tool_name,
                                arguments=delta_args,
                                status="running",
                            ),
                        ), last_content_length, last_tool_args_length, current_tool_call_id, current_tool_name

                    return None, last_content_length, last_tool_args_length, current_tool_call_id, current_tool_name

                # 处理工具结果消息（完整内容，不需要增量）
                if msg_type in ("tool", "ToolMessage"):
                    tool_call_id = msg.get("tool_call_id", "")
                    tool_name = msg.get("name", "")
                    content = msg.get("content", "")
                    if isinstance(content, dict):
                        content = json.dumps(content, ensure_ascii=False)

                    # 重置工具调用追踪
                    current_tool_call_id = ""
                    current_tool_name = ""

                    return StreamEvent(
                        event_type=StreamEventType.TOOL_RESULT,
                        text=str(content),
                        tool_call=ToolCall(
                            tool_call_id=tool_call_id,
                            name=tool_name,
                            arguments="",
                            status="completed",
                        ),
                    ), last_content_length, last_tool_args_length, current_tool_call_id, current_tool_name

                # 处理 AI 消息类型的文本（累积内容，需要计算增量）
                if msg_type in ("ai", "AIMessage", "AIMessageChunk"):
                    content = msg.get("content", "")
                    if isinstance(content, str) and len(content) > last_content_length:
                        # 只返回新增的部分
                        delta = content[last_content_length:]
                        new_length = len(content)
                        return StreamEvent(
                            event_type=StreamEventType.TEXT_DELTA,
                            text=delta,
                        ), new_length, last_tool_args_length, current_tool_call_id, current_tool_name

            return None, last_content_length, last_tool_args_length, current_tool_call_id, current_tool_name

        # 处理 updates 事件（节点更新）- 这些是完整内容，不需要增量计算
        if event_type == "updates":
            if isinstance(data, dict):
                # 检查是否有工具节点的更新
                for node_name, node_data in data.items():
                    if node_name == "tools" and isinstance(node_data, dict):
                        messages = node_data.get("messages", [])
                        if isinstance(messages, list):
                            for msg in messages:
                                if isinstance(msg, dict) and msg.get("type") in ("tool", "ToolMessage"):
                                    tool_call_id = msg.get("tool_call_id", "")
                                    tool_name = msg.get("name", "")
                                    content = msg.get("content", "")
                                    if isinstance(content, dict):
                                        content = json.dumps(content, ensure_ascii=False)

                                    return StreamEvent(
                                        event_type=StreamEventType.TOOL_RESULT,
                                        text=str(content),
                                        tool_call=ToolCall(
                                            tool_call_id=tool_call_id,
                                            name=tool_name,
                                            arguments="",
                                            status="completed",
                                        ),
                                    ), last_content_length, last_tool_args_length, current_tool_call_id, current_tool_name
            return None, last_content_length, last_tool_args_length, current_tool_call_id, current_tool_name

        return None, last_content_length, last_tool_args_length, current_tool_call_id, current_tool_name

    async def _ensure_thread(self, session_id: str, request: UnifiedRequest) -> str:
        """
        确保 Thread 存在。如果 session_id 不是有效的 UUID，会生成一个新的 UUID。
        使用缓存确保同一 session_id 始终映射到同一个 thread_id。
        返回实际使用的 thread_id（UUID 格式）。
        """
        import uuid
        
        # 检查缓存中是否已有映射
        if session_id in self._session_to_thread_map:
            return self._session_to_thread_map[session_id]
        
        # 验证是否为有效 UUID
        try:
            uuid.UUID(session_id)
            valid_thread_id = session_id
        except ValueError:
            # 不是有效 UUID，生成一个新的
            valid_thread_id = str(uuid.uuid4())
        
        # 缓存映射关系
        self._session_to_thread_map[session_id] = valid_thread_id
        
        try:
            await self.connector.post(
                "/threads",
                json={
                    "thread_id": valid_thread_id,
                    "if_exists": "do_nothing",
                    "metadata": {
                        "user_id": request.user_id,
                        "tenant_id": request.tenant_id,
                        "original_session_id": session_id,  # 保留原始 session_id
                    },
                },
            )
        except Exception:
            pass
        
        return valid_thread_id
