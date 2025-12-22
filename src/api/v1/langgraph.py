"""
LangGraph API 路由

代理 LangGraph Server 官方 API：
- Assistants: /assistants/*
- Threads: /threads/*  
- Runs: /threads/{thread_id}/runs/*, /runs/*
- Store: /store/*
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ..deps import require_langgraph_proxy, get_user_context, get_rate_limiter
from ...adapters.langgraph_proxy import (
    ForbiddenError,
    LangGraphProxy,
    NoHealthyInstanceError,
    QuotaExceededError,
    ThreadNotFoundError,
)
from ...core.auth.user_resolver import UserContext
from ...core.gateway.multi_dimension_rate_limiter import (
    MultiDimensionRateLimiter,
    RateLimitContext,
    RateLimitHeaders,
)


router = APIRouter(prefix="/langgraph", tags=["LangGraph"])


# ============ Pydantic 模型 ============

class AssistantCreate(BaseModel):
    graph_id: str
    config: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    name: Optional[str] = None


class ThreadCreate(BaseModel):
    metadata: Optional[Dict[str, Any]] = None
    if_exists: str = "do_nothing"


class ThreadUpdate(BaseModel):
    metadata: Optional[Dict[str, Any]] = None


class ThreadStateUpdate(BaseModel):
    values: Dict[str, Any]
    as_node: Optional[str] = None


class RunCreate(BaseModel):
    assistant_id: str
    input: Dict[str, Any] = Field(default_factory=dict)
    config: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    webhook: Optional[str] = None
    interrupt_before: Optional[List[str]] = None
    interrupt_after: Optional[List[str]] = None
    stream_mode: Optional[List[str]] = None


class StoreItem(BaseModel):
    namespace: List[str]
    key: str
    value: Dict[str, Any]


# ============ 异常处理 ============

def handle_proxy_error(e: Exception):
    """处理代理层异常"""
    if isinstance(e, ForbiddenError):
        raise HTTPException(status_code=403, detail=str(e))
    elif isinstance(e, QuotaExceededError):
        raise HTTPException(status_code=429, detail=str(e))
    elif isinstance(e, ThreadNotFoundError):
        raise HTTPException(status_code=404, detail=str(e))
    elif isinstance(e, NoHealthyInstanceError):
        raise HTTPException(status_code=503, detail=str(e))
    else:
        raise HTTPException(status_code=500, detail=str(e))


# ============ 限流检查 ============

async def check_rate_limit(
    user: UserContext,
    rate_limiter: MultiDimensionRateLimiter,
    assistant_id: Optional[str] = None,
    operation: Optional[str] = None,
):
    """检查限流"""
    if not rate_limiter:
        return
    
    context = RateLimitContext.from_user_context(
        user=user,
        assistant_id=assistant_id,
        operation=operation,
    )
    
    result = await rate_limiter.check(context)
    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail=RateLimitHeaders.build_exceeded_response(result),
            headers=RateLimitHeaders.build(result),
        )


# ============ Assistants API ============

@router.get("/assistants")
async def list_assistants(
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """列出可用的 Assistants"""
    try:
        return await proxy.list_assistants(user, limit=limit, offset=offset)
    except Exception as e:
        handle_proxy_error(e)


@router.get("/assistants/{assistant_id}")
async def get_assistant(
    assistant_id: str,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """获取 Assistant 详情"""
    try:
        return await proxy.get_assistant(user, assistant_id)
    except Exception as e:
        handle_proxy_error(e)


@router.post("/assistants")
async def create_assistant(
    data: AssistantCreate,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """创建自定义 Assistant（需要企业版权限）"""
    try:
        return await proxy.create_assistant(
            user=user,
            graph_id=data.graph_id,
            config=data.config,
            metadata=data.metadata,
            name=data.name,
        )
    except Exception as e:
        handle_proxy_error(e)


# ============ Threads API ============

@router.post("/threads")
async def create_thread(
    data: ThreadCreate = None,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
    rate_limiter: MultiDimensionRateLimiter = Depends(get_rate_limiter),
):
    """创建 Thread"""
    await check_rate_limit(user, rate_limiter, operation="thread_create")
    
    try:
        return await proxy.create_thread(
            user=user,
            metadata=data.metadata if data else None,
            if_exists=data.if_exists if data else "do_nothing",
        )
    except Exception as e:
        handle_proxy_error(e)


@router.get("/threads/{thread_id}")
async def get_thread(
    thread_id: str,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """获取 Thread"""
    try:
        return await proxy.get_thread(user, thread_id)
    except Exception as e:
        handle_proxy_error(e)


@router.patch("/threads/{thread_id}")
async def update_thread(
    thread_id: str,
    data: ThreadUpdate,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """更新 Thread"""
    try:
        return await proxy.update_thread(user, thread_id, metadata=data.metadata)
    except Exception as e:
        handle_proxy_error(e)


@router.delete("/threads/{thread_id}")
async def delete_thread(
    thread_id: str,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """删除 Thread"""
    try:
        await proxy.delete_thread(user, thread_id)
        return {"status": "ok"}
    except Exception as e:
        handle_proxy_error(e)


@router.post("/threads/search")
async def search_threads(
    metadata: Optional[Dict[str, Any]] = None,
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """搜索用户的 Threads"""
    try:
        return await proxy.search_threads(
            user=user,
            metadata=metadata,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        handle_proxy_error(e)


@router.get("/threads/{thread_id}/state")
async def get_thread_state(
    thread_id: str,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """获取 Thread 状态"""
    try:
        return await proxy.get_thread_state(user, thread_id)
    except Exception as e:
        handle_proxy_error(e)


@router.post("/threads/{thread_id}/state")
async def update_thread_state(
    thread_id: str,
    data: ThreadStateUpdate,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """更新 Thread 状态"""
    try:
        return await proxy.update_thread_state(
            user=user,
            thread_id=thread_id,
            values=data.values,
            as_node=data.as_node,
        )
    except Exception as e:
        handle_proxy_error(e)


@router.get("/threads/{thread_id}/history")
async def get_thread_history(
    thread_id: str,
    limit: int = Query(10, ge=1, le=100),
    before: Optional[str] = None,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """获取 Thread 历史"""
    try:
        return await proxy.get_thread_history(
            user=user,
            thread_id=thread_id,
            limit=limit,
            before=before,
        )
    except Exception as e:
        handle_proxy_error(e)


# ============ Runs API ============

@router.post("/threads/{thread_id}/runs")
async def create_run(
    thread_id: str,
    data: RunCreate,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
    rate_limiter: MultiDimensionRateLimiter = Depends(get_rate_limiter),
):
    """创建 Run"""
    await check_rate_limit(
        user, rate_limiter,
        assistant_id=data.assistant_id,
        operation="run_create",
    )
    
    try:
        return await proxy.create_run(
            user=user,
            thread_id=thread_id,
            assistant_id=data.assistant_id,
            input_data=data.input,
            config=data.config,
            metadata=data.metadata,
            webhook=data.webhook,
            interrupt_before=data.interrupt_before,
            interrupt_after=data.interrupt_after,
        )
    except Exception as e:
        handle_proxy_error(e)


@router.post("/threads/{thread_id}/runs/wait")
async def create_run_wait(
    thread_id: str,
    data: RunCreate,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
    rate_limiter: MultiDimensionRateLimiter = Depends(get_rate_limiter),
):
    """创建 Run 并等待完成"""
    await check_rate_limit(
        user, rate_limiter,
        assistant_id=data.assistant_id,
        operation="run_create",
    )
    
    try:
        return await proxy.create_run_wait(
            user=user,
            thread_id=thread_id,
            assistant_id=data.assistant_id,
            input_data=data.input,
            config=data.config,
            metadata=data.metadata,
        )
    except Exception as e:
        handle_proxy_error(e)


@router.post("/threads/{thread_id}/runs/stream")
async def stream_run(
    thread_id: str,
    data: RunCreate,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
    rate_limiter: MultiDimensionRateLimiter = Depends(get_rate_limiter),
):
    """流式执行 Run"""
    await check_rate_limit(
        user, rate_limiter,
        assistant_id=data.assistant_id,
        operation="run_create",
    )
    
    async def event_generator():
        try:
            async for chunk in proxy.stream_run(
                user=user,
                thread_id=thread_id,
                assistant_id=data.assistant_id,
                input_data=data.input,
                config=data.config,
                stream_mode=data.stream_mode,
            ):
                yield {
                    "event": chunk.get("event", "message"),
                    "data": json.dumps(chunk.get("data", {})),
                }
        except Exception as e:
            yield {
                "event": "error",
                "data": json.dumps({"error": str(e)}),
            }
    
    return EventSourceResponse(event_generator())


@router.get("/threads/{thread_id}/runs")
async def list_runs(
    thread_id: str,
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """列出 Thread 的 Runs"""
    try:
        return await proxy.list_runs(
            user=user,
            thread_id=thread_id,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        handle_proxy_error(e)


@router.get("/threads/{thread_id}/runs/{run_id}")
async def get_run(
    thread_id: str,
    run_id: str,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """获取 Run 详情"""
    try:
        return await proxy.get_run(user, thread_id, run_id)
    except Exception as e:
        handle_proxy_error(e)


@router.post("/threads/{thread_id}/runs/{run_id}/cancel")
async def cancel_run(
    thread_id: str,
    run_id: str,
    wait: bool = Query(False),
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """取消 Run"""
    try:
        await proxy.cancel_run(user, thread_id, run_id, wait=wait)
        return {"status": "ok"}
    except Exception as e:
        handle_proxy_error(e)


# ============ Threadless Runs ============

@router.post("/runs/stream")
async def stream_stateless_run(
    data: RunCreate,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
    rate_limiter: MultiDimensionRateLimiter = Depends(get_rate_limiter),
):
    """无 Thread 的流式 Run（一次性对话）"""
    await check_rate_limit(
        user, rate_limiter,
        assistant_id=data.assistant_id,
        operation="run_create",
    )
    
    async def event_generator():
        try:
            async for chunk in proxy.stream_run(
                user=user,
                thread_id=None,
                assistant_id=data.assistant_id,
                input_data=data.input,
                config=data.config,
                stream_mode=data.stream_mode,
            ):
                yield {
                    "event": chunk.get("event", "message"),
                    "data": json.dumps(chunk.get("data", {})),
                }
        except Exception as e:
            yield {
                "event": "error",
                "data": json.dumps({"error": str(e)}),
            }
    
    return EventSourceResponse(event_generator())


@router.post("/runs/wait")
async def create_stateless_run(
    data: RunCreate,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
    rate_limiter: MultiDimensionRateLimiter = Depends(get_rate_limiter),
):
    """无状态 Run（一次性对话）"""
    await check_rate_limit(
        user, rate_limiter,
        assistant_id=data.assistant_id,
        operation="run_create",
    )
    
    try:
        return await proxy.create_stateless_run(
            user=user,
            assistant_id=data.assistant_id,
            input_data=data.input,
            config=data.config,
        )
    except Exception as e:
        handle_proxy_error(e)


# ============ Store API ============

@router.post("/store/items")
async def store_put_item(
    data: StoreItem,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
    rate_limiter: MultiDimensionRateLimiter = Depends(get_rate_limiter),
):
    """写入记忆"""
    await check_rate_limit(user, rate_limiter, operation="store_write")
    
    try:
        await proxy.store_put(user, data.namespace, data.key, data.value)
        return {"status": "ok"}
    except Exception as e:
        handle_proxy_error(e)


@router.get("/store/items")
async def store_get_item(
    namespace: str = Query(..., description="JSON encoded namespace list"),
    key: str = Query(...),
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """获取记忆"""
    try:
        ns = json.loads(namespace)
        result = await proxy.store_get(user, ns, key)
        if result is None:
            raise HTTPException(status_code=404, detail="Item not found")
        return result
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid namespace format")
    except Exception as e:
        handle_proxy_error(e)


@router.delete("/store/items")
async def store_delete_item(
    namespace: str = Query(..., description="JSON encoded namespace list"),
    key: str = Query(...),
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """删除记忆"""
    try:
        ns = json.loads(namespace)
        await proxy.store_delete(user, ns, key)
        return {"status": "ok"}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid namespace format")
    except Exception as e:
        handle_proxy_error(e)


@router.get("/store/namespaces")
async def store_list_namespaces(
    prefix: Optional[str] = Query(None, description="JSON encoded prefix list"),
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """列出命名空间"""
    try:
        prefix_list = json.loads(prefix) if prefix else None
        return await proxy.store_list_namespaces(user, prefix=prefix_list)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid prefix format")
    except Exception as e:
        handle_proxy_error(e)


# ============ Passthrough (Full LangGraph API) ============

@router.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def passthrough(
    full_path: str,
    request: Request,
    proxy: LangGraphProxy = Depends(require_langgraph_proxy),
    user: UserContext = Depends(get_user_context),
):
    """
    LangGraph CLI / LangSmith Deployment API is evolving quickly.
    This catch-all route forwards any unimplemented endpoint to the selected
    upstream instance so the gateway stays compatible with upstream `/docs`.
    """
    upstream_path = "/" + full_path.lstrip("/")
    method = request.method.upper()

    # Best-effort ownership checks for thread/assistant scoped paths.
    try:
        parts = [p for p in upstream_path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "threads" and parts[1] not in ("search", "count"):
            await proxy.get_thread(user, parts[1])
        if len(parts) >= 2 and parts[0] == "assistants" and parts[1] not in ("search", "count"):
            await proxy.get_assistant(user, parts[1])
    except Exception:
        # Let upstream decide if our check is not applicable.
        pass

    params = list(request.query_params.multi_items())
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)

    body = await request.body()
    wants_stream = (
        "text/event-stream" in (headers.get("accept", "") or "")
        or upstream_path.endswith("/stream")
        or "/stream" in upstream_path
    )

    instance = await proxy.lb.select_instance()
    client = await proxy._get_client(instance)

    if wants_stream:
        async def gen():
            async with client.stream(
                method,
                upstream_path,
                params=params,
                headers=headers,
                content=body if body else None,
            ) as resp:
                if resp.status_code >= 400:
                    err = await resp.aread()
                    raise HTTPException(
                        status_code=resp.status_code,
                        detail=err.decode("utf-8", errors="ignore"),
                    )
                async for chunk in resp.aiter_raw():
                    yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream")

    resp = await client.request(
        method,
        upstream_path,
        params=params,
        headers=headers,
        content=body if body else None,
    )
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        return resp.json()
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=content_type or None,
    )








