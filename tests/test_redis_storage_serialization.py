import json
from datetime import datetime, timezone

from src.persistence.redis import RedisStorage


def test_redis_json_default_serializes_datetime():
    payload = {"created_at": datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)}
    s = json.dumps(payload, default=RedisStorage._json_default, ensure_ascii=False)
    assert "2025-01-02T03:04:05+00:00" in s

