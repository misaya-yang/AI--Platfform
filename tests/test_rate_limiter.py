import pytest

from src.core.gateway.rate_limiter import RateLimit, RateLimitConfig, RateLimiter
from src.models.enums import ContentType
from src.models.request import ContentItem, UnifiedRequest
from src.models.service import ServiceDefinition


@pytest.mark.asyncio
async def test_rate_limiter_exceeded():
    cfg = RateLimitConfig(global_limit=RateLimit(requests=1, window=60))
    limiter = RateLimiter(cfg)
    req = UnifiedRequest(
        request_id="r1",
        service_id="svc",
        inputs=[ContentItem(type=ContentType.TEXT, data="hi")],
    )
    svc = ServiceDefinition(service_id="svc", name="svc")
    await limiter.enforce(req, svc)
    with pytest.raises(Exception):
        await limiter.enforce(req, svc)
