from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Optional

from ..core.exceptions import ValidationFailedError
from ..models.enums import ContentType, ServiceType
from ..models.request import ContentItem, UnifiedRequest
from ..models.response import StreamChunk, UnifiedResponse
from .base import ProtocolAdapter


class OpenAIAdapter(ProtocolAdapter):
    async def invoke(self, request: UnifiedRequest) -> UnifiedResponse:
        prompt = self._extract_text(request.inputs)
        params = request.parameters or {}
        model = (
            params.get("model")
            or (self.service.metadata or {}).get("model")
            or "gpt-4o-mini"
        )

        if self.service.service_type == ServiceType.EMBEDDING:
            payload = {"model": model, "input": prompt}
            payload.update({k: v for k, v in params.items() if k != "model"})
            response = await self.connector.post("/v1/embeddings", json=payload)
            return UnifiedResponse(
                request_id=request.request_id,
                status="success",
                outputs=[ContentItem(type=ContentType.JSON, data=response)],
            )

        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        payload.update({k: v for k, v in params.items() if k != "model"})
        response = await self.connector.post("/v1/chat/completions", json=payload)
        content = self._extract_chat_content(response)
        usage = response.get("usage") if isinstance(response, dict) else None
        return UnifiedResponse(
            request_id=request.request_id,
            status="success",
            outputs=[ContentItem(type=ContentType.TEXT, data=content)],
            usage=usage,
        )

    async def stream(self, request: UnifiedRequest) -> AsyncIterator[StreamChunk]:
        if self.service.service_type == ServiceType.EMBEDDING:
            raise ValidationFailedError("stream not supported for embedding")
        prompt = self._extract_text(request.inputs)
        params = request.parameters or {}
        model = (
            params.get("model")
            or (self.service.metadata or {}).get("model")
            or "gpt-4o-mini"
        )
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
        payload.update({k: v for k, v in params.items() if k != "model"})

        client = getattr(self.connector, "_client", None)
        if client is None:
            raise ValidationFailedError("stream requires HTTPConnector")

        chunk_index = 0
        async with client.stream(
            "POST", "/v1/chat/completions", json=payload
        ) as resp:
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    evt = json.loads(data_str)
                except Exception:
                    continue
                delta = (
                    evt.get("choices", [{}])[0]
                    .get("delta", {})
                    .get("content")
                )
                if delta:
                    yield StreamChunk(
                        request_id=request.request_id,
                        chunk_index=chunk_index,
                        content=ContentItem(type=ContentType.TEXT, data=delta),
                        is_final=False,
                    )
                    chunk_index += 1
        yield StreamChunk(
            request_id=request.request_id,
            chunk_index=chunk_index,
            content=ContentItem(type=ContentType.TEXT, data=""),
            is_final=True,
        )

    def _extract_text(self, inputs: List[ContentItem]) -> str:
        texts = [str(i.data) for i in inputs if i.type == ContentType.TEXT and i.data]
        if not texts:
            raise ValidationFailedError("text input is required")
        return "\n".join(texts)

    def _extract_chat_content(self, response: Any) -> str:
        if not isinstance(response, dict):
            return str(response)
        choices = response.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return str(msg.get("content") or "")
