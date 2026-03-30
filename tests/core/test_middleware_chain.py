"""Tests for middleware chain (InvocationContext and MiddlewareChain)."""

import pytest

from src.core.middleware.base import InvocationContext, MiddlewareChain, InvocationMiddleware


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class PassthroughMiddleware(InvocationMiddleware):
    """Middleware that records it was called and passes through."""

    def __init__(self, name: str = "passthrough"):
        self.name = name
        self.enabled = True
        self.called = False

    async def process(self, context, next_middleware):
        self.called = True
        return await next_middleware(context)


class RecordingMiddleware(InvocationMiddleware):
    """Middleware that appends its name to context['order']."""

    def __init__(self, name: str):
        self.name = name
        self.enabled = True

    async def process(self, context, next_middleware):
        context.get("order", []).append(self.name)
        return await next_middleware(context)


class BlockingMiddleware(InvocationMiddleware):
    """Middleware that short-circuits with a fixed response."""

    def __init__(self, name: str = "blocker", response: str = "blocked"):
        self.name = name
        self.enabled = True
        self.response = response

    async def process(self, context, next_middleware):
        return self.response


# ---------------------------------------------------------------------------
# InvocationContext Tests
# ---------------------------------------------------------------------------

class TestInvocationContext:
    def test_set_and_get(self):
        ctx = InvocationContext(request=None, service=None)
        ctx.set("key", "value")
        assert ctx.get("key") == "value"

    def test_get_default(self):
        ctx = InvocationContext(request=None, service=None)
        assert ctx.get("missing") is None
        assert ctx.get("missing", 42) == 42

    def test_record_timing(self):
        ctx = InvocationContext(request=None, service=None)
        ctx.record_timing("step1", 100.5)
        assert ctx.timings["step1"] == 100.5


# ---------------------------------------------------------------------------
# MiddlewareChain Tests
# ---------------------------------------------------------------------------

class TestMiddlewareChain:
    @pytest.fixture
    def chain(self):
        return MiddlewareChain()

    async def test_empty_chain_calls_handler(self, chain):
        async def handler(ctx):
            return "ok"

        ctx = InvocationContext(request=None, service=None)
        result = await chain.invoke(ctx, handler)
        assert result == "ok"

    async def test_single_middleware_called(self, chain):
        mw = PassthroughMiddleware()
        chain.add(mw)

        async def handler(ctx):
            return "done"

        ctx = InvocationContext(request=None, service=None)
        result = await chain.invoke(ctx, handler)
        assert result == "done"
        assert mw.called

    async def test_execution_order(self, chain):
        chain.add(RecordingMiddleware("first"))
        chain.add(RecordingMiddleware("second"))
        chain.add(RecordingMiddleware("third"))

        order = []

        async def handler(ctx):
            return ctx.get("order")

        ctx = InvocationContext(request=None, service=None)
        ctx.set("order", order)
        await chain.invoke(ctx, handler)
        assert order == ["first", "second", "third"]

    async def test_blocking_middleware_short_circuits(self, chain):
        chain.add(BlockingMiddleware())
        chain.add(PassthroughMiddleware("after_blocker"))

        async def handler(ctx):
            return "should_not_reach"

        ctx = InvocationContext(request=None, service=None)
        result = await chain.invoke(ctx, handler)
        assert result == "blocked"

    def test_add_and_get(self, chain):
        mw = PassthroughMiddleware("test_mw")
        chain.add(mw)
        assert chain.get("test_mw") is mw

    def test_remove(self, chain):
        chain.add(PassthroughMiddleware("removable"))
        chain.remove("removable")
        assert chain.get("removable") is None

    def test_enable_disable(self, chain):
        mw = PassthroughMiddleware("toggleable")
        chain.add(mw)
        chain.disable("toggleable")
        assert not mw.enabled
        chain.enable("toggleable")
        assert mw.enabled

    def test_list_middlewares(self, chain):
        chain.add(PassthroughMiddleware("a"))
        chain.add(PassthroughMiddleware("b"))
        names = [m["name"] for m in chain.list_middlewares()]
        assert names == ["a", "b"]

    def test_insert_at_position(self, chain):
        chain.add(PassthroughMiddleware("first"))
        chain.add(PassthroughMiddleware("third"))
        chain.insert(1, PassthroughMiddleware("second"))
        names = [m["name"] for m in chain.list_middlewares()]
        assert names == ["first", "second", "third"]

    async def test_disabled_middleware_skipped(self, chain):
        mw = PassthroughMiddleware("disabled_mw")
        chain.add(mw)
        chain.disable("disabled_mw")

        async def handler(ctx):
            return "ok"

        ctx = InvocationContext(request=None, service=None)
        result = await chain.invoke(ctx, handler)
        assert result == "ok"
        assert not mw.called
