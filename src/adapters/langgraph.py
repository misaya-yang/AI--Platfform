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
            # 认证配置：如果 Agent 启用了 auth，网关需要传递用户信息
            # 支持配置静态用户 ID 或动态传递请求中的用户信息
            self.auth_user_id = config.get("auth_user_id")  # 静态用户 ID（可选）
            self.auth_user_type = config.get("auth_user_type", "user")  # 用户类型
            self.forward_auth = config.get("forward_auth", True)  # 是否转发认证信息

    def _build_auth_headers(self, request: UnifiedRequest) -> Dict[str, str]:
        """
        构建认证头部，用于转发给 LangGraph Agent。
        
        LangGraph Agent 的 auth 模块支持两种认证方式：
        1. X-User-Id 头部（网关转发）
        2. Authorization Bearer token（直接访问）
        
        为确保兼容性，同时发送两种认证方式。
        """
        if not self.forward_auth:
            return {}
        
        # 使用配置的静态用户 ID 或请求中的用户 ID
        user_id = self.auth_user_id or request.user_id or "gateway-user"
        user_type = self.auth_user_type
        user_name = f"User-{request.user_id}" if request.user_id else "Gateway User"
        
        headers: Dict[str, str] = {
            # 方式1: X-User-Id 头部（LangGraph SDK 会自动映射到 x_user_id 参数）
            "x-user-id": str(user_id),
            "x-user-type": user_type,
            "x-user-name": user_name,
            "x-user-permissions": "read,write",
        }
        
        # 方式2: 同时发送 Bearer token 作为备用认证
        # 使用 user_id 作为简单 token，LangGraph auth 会在 DEV_MODE 下接受任何 token
        headers["Authorization"] = f"Bearer gateway-{user_id}"
        
        # 如果有租户 ID，也传递
        if request.tenant_id:
            headers["x-tenant-id"] = str(request.tenant_id)
        
        return headers

    def _build_run_config(self, request: UnifiedRequest, thread_id: Optional[str] = None) -> Dict[str, Any]:
        """Build LangGraph run config with gateway-injected `configurable` values."""
        params = request.parameters or {}
        run_config: Dict[str, Any] = {}
        if isinstance(params.get("config"), dict):
            run_config = dict(params["config"])

        configurable = run_config.get("configurable") or {}
        if not isinstance(configurable, dict):
            configurable = {}
        configurable = dict(configurable)

        # Merge in caller-provided configurable from request.context / request.parameters.
        for source in (request.context, request.parameters):
            if not isinstance(source, dict):
                continue
            src_cfg = source.get("configurable")
            if isinstance(src_cfg, dict):
                for k, v in src_cfg.items():
                    if k not in configurable and v is not None:
                        configurable[k] = v
            for k in ("dataset_id", "gateway_token"):
                v = source.get(k)
                if k not in configurable and v is not None:
                    configurable[k] = v

        # Enforce gateway scoping fields.
        configurable["user_id"] = request.user_id
        configurable["tenant_id"] = request.tenant_id
        configurable["checkpoint_ns"] = request.tenant_id or ""
        configurable["thread_id"] = thread_id or (request.session_id or request.request_id)

        run_config["configurable"] = configurable
        return run_config

    async def invoke(self, request: UnifiedRequest) -> UnifiedResponse:
        messages = self._build_messages(request.inputs)
        if not self.remote:
            run_config = self._build_run_config(request)
            result = await self.graph.ainvoke({"messages": messages}, run_config)
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
            run_config = self._build_run_config(request)
            # 追踪已处理的内容，用于去重
            seen_content_ids: set = set()

            async for event in self.graph.astream_events({"messages": messages}, run_config, version="v2"):
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
        # 为远程健康检查构建认证头部
        # 使用虚拟请求构建头部（因为健康检查没有实际请求上下文）
        from ..models.request import UnifiedRequest
        dummy_request = UnifiedRequest(
            request_id="health-check",
            service_id=self.service.service_id,
            inputs=[],
            user_id="gateway-health-check",
            tenant_id="gateway",
        )
        auth_headers = self._build_auth_headers(dummy_request)
        return await self.connector.health_check(headers=auth_headers)

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
        if self.service.session_enabled and request.session_id:
            # 确保 thread 存在并获取有效的 UUID thread_id
            valid_thread_id = await self._ensure_thread(request.session_id, request)
            endpoint = self.thread_invoke_endpoint.format(thread_id=valid_thread_id)
            payload["config"] = self._build_run_config(request, thread_id=valid_thread_id)
        else:
            endpoint = self.invoke_endpoint
            payload["config"] = self._build_run_config(request)
        try:
            # 构建认证头部
            auth_headers = self._build_auth_headers(request)
            return await self.connector.post(endpoint, json=payload, headers=auth_headers)
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
        if self.service.session_enabled and request.session_id:
            # 确保 thread 存在并获取有效的 UUID thread_id
            valid_thread_id = await self._ensure_thread(request.session_id, request)
            endpoint = self.thread_stream_endpoint.format(thread_id=valid_thread_id)
            payload["config"] = self._build_run_config(request, thread_id=valid_thread_id)
        else:
            endpoint = self.stream_endpoint
            payload["config"] = self._build_run_config(request)

        client = getattr(self.connector, "_client", None)
        if client is None:
            raise ValidationFailedError("remote streaming requires HTTPConnector")

        import httpx
        import time
        import logging
        logger = logging.getLogger(__name__)
        
        # Use shorter connect timeout, longer read timeout for streaming
        stream_timeout = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=10.0)
        auth_headers = self._build_auth_headers(request)
        
        t_start = time.perf_counter()
        first_data_time = None
        first_yield_time = None
        
        try:
            async with client.stream(
                "POST", endpoint, json=payload, timeout=stream_timeout, headers=auth_headers
            ) as resp:
                t_response = time.perf_counter()
                logger.info(f"[TIMING] LangGraph HTTP response: {(t_response - t_start)*1000:.2f}ms, status={resp.status_code}")
                
                if resp.status_code != 200:
                    error_text = await resp.aread()
                    logger.error(f"LangGraph stream error: {resp.status_code} - {error_text}")
                    raise ValidationFailedError(
                        f"LangGraph stream failed ({resp.status_code}) at {endpoint}: {error_text!r}"
                    )

                current_event_type = ""
                last_content_length = 0
                last_tool_args_length: Dict[str, int] = {}
                current_tool_call_id = ""
                current_tool_name = ""
                
                # Use aiter_lines for SSE parsing
                line_count = 0
                async for line in resp.aiter_lines():
                    line_count += 1
                    if first_data_time is None and line:
                        first_data_time = time.perf_counter()
                        logger.info(f"[TIMING] LangGraph first line: {(first_data_time - t_start)*1000:.2f}ms")
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
                    
                    # Debug: log received event type and content preview
                    if current_event_type:
                        content_preview = ""
                        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
                            c = data[0].get("content", "")
                            if isinstance(c, str):
                                content_preview = f", content_len={len(c)}"
                        logger.debug(f"[SSE] event={current_event_type}{content_preview}")
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
                        if first_yield_time is None:
                            first_yield_time = time.perf_counter()
                            logger.info(f"[TIMING] LangGraph first yield: {(first_yield_time - t_start)*1000:.2f}ms, event_type={stream_event.event_type}, text_len={len(stream_event.text) if stream_event.text else 0}")
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
        
        注意：此方法会在流式响应前执行 HTTP 调用，可能影响首 token 延迟。
        使用缓存来减少后续请求的开销。
        """
        import uuid
        import time
        import logging
        import asyncio
        logger = logging.getLogger(__name__)
        
        t_start = time.perf_counter()
        
        # 检查缓存中是否已有映射 - 快速路径
        if session_id in self._session_to_thread_map:
            return self._session_to_thread_map[session_id]
        
        # 验证是否为有效 UUID
        try:
            uuid.UUID(session_id)
            valid_thread_id = session_id
        except ValueError:
            # 不是有效 UUID，生成一个新的
            valid_thread_id = str(uuid.uuid4())
        
        # 缓存映射关系（先缓存，后台创建 thread）
        self._session_to_thread_map[session_id] = valid_thread_id
        
        # 异步创建 thread，不阻塞主流程
        # LangGraph 的 /runs/stream 会自动创建 thread 如果不存在
        async def create_thread_background():
            try:
                auth_headers = self._build_auth_headers(request)
                await self.connector.post(
                    "/threads",
                    json={
                        "thread_id": valid_thread_id,
                        "if_exists": "do_nothing",
                        "metadata": {
                            "user_id": request.user_id,
                            "tenant_id": request.tenant_id,
                            "original_session_id": session_id,
                        },
                    },
                    headers=auth_headers,
                )
            except Exception:
                pass  # Thread creation is best-effort
        
        # Fire and forget - don't wait for thread creation
        asyncio.create_task(create_thread_background())
        
        t_end = time.perf_counter()
        logger.debug(f"[TIMING] _ensure_thread: {(t_end - t_start)*1000:.2f}ms (cached: False)")
        
        return valid_thread_id
