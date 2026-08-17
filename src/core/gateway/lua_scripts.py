"""Atomic Redis admission/rate-limit Lua scripts (SPO-02 / GW2).

Before this module, the warm path paid one round trip per Redis command
(``ZREMRANGEBYSCORE`` → ``ZCARD`` → ``ZADD``) and multiple workers could
observe the same pre-add count (TOCTOU over-sell). Each script below performs
cleanup + count + conditional add + expire in a single ``EVAL``:

- ``capacity_acquire_lua``: shared capacity lease admission.
- ``sliding_window_check_lua``: N-key sliding window with per-key limits;
  returns the rejected dimension (or -1) and the earliest score for retry.

Both keep the existing semantics: a request that exceeds a limit is NOT
recorded (the add only happens when the pre-add count is under the limit),
and rejected dimensions return enough state for ``Retry-After`` headers.
"""

from __future__ import annotations

from typing import Any

# KEYS[1] = shared capacity key, KEYS[2] = tenant capacity key
# ARGV[1] = now_ms, ARGV[2] = expires_at_ms, ARGV[3] = member,
# ARGV[4] = lease ttl ms, ARGV[5] = shared limit, ARGV[6] = tenant limit
# Returns {shared_pre_add_count, tenant_pre_add_count}. Shared is admitted
# first; a later tenant reject must ZREM the shared member so a busy tenant
# cannot leak cluster-wide slots until TTL.
CAPACITY_ACQUIRE_PAIR_LUA = """
local function admit(key, limit)
  if limit <= 0 then
    return -1
  end
  redis.call('ZREMRANGEBYSCORE', key, 0, ARGV[1])
  local count = redis.call('ZCARD', key)
  if count < limit then
    redis.call('ZADD', key, ARGV[2], ARGV[3])
    redis.call('PEXPIRE', key, ARGV[4])
  end
  return count
end
local shared_limit = tonumber(ARGV[5])
local tenant_limit = tonumber(ARGV[6])
local shared_count = admit(KEYS[1], shared_limit)
local tenant_count = -1
if shared_count < shared_limit then
  tenant_count = admit(KEYS[2], tenant_limit)
  if tenant_count >= tenant_limit then
    redis.call('ZREM', KEYS[1], ARGV[3])
  end
end
return {shared_count, tenant_count}
"""

# KEYS[1..N] = capacity keys to release, ARGV[1] = member
CAPACITY_RELEASE_LUA = """
for i = 1, #KEYS do
  redis.call('ZREM', KEYS[i], ARGV[1])
end
return #KEYS
"""

# KEYS[1..N] = sliding window keys (score unit: seconds)
# ARGV[1] = now_s, ARGV[2] = window_start_s, ARGV[3] = expire_s,
# ARGV[4] = unique member, ARGV[5..4+N] = per-key limits
# Returns {rejected_dimension_index, earliest_score_s, min_remaining}.
SLIDING_WINDOW_CHECK_LUA = """
local min_remaining = nil
for i = 1, #KEYS do
  redis.call('ZREMRANGEBYSCORE', KEYS[i], 0, ARGV[2])
  local count = redis.call('ZCARD', KEYS[i])
  if count >= tonumber(ARGV[4 + i]) then
    local earliest = redis.call('ZRANGE', KEYS[i], 0, 0, 'WITHSCORES')
    local earliest_score = tonumber(ARGV[2])
    if earliest[2] then
      earliest_score = tonumber(earliest[2])
    end
    return {i - 1, earliest_score, 0}
  end
  redis.call('ZADD', KEYS[i], ARGV[1], ARGV[4])
  redis.call('EXPIRE', KEYS[i], ARGV[3])
  local remaining = tonumber(ARGV[4 + i]) - count - 1
  if min_remaining == nil or remaining < min_remaining then
    min_remaining = remaining
  end
end
return {-1, 0, min_remaining or 0}
"""


async def eval_script(
    redis: Any,
    script: str,
    *,
    keys: list[str],
    args: list[Any],
) -> Any:
    """Evaluate a Lua script in one Redis round trip."""
    return await redis.eval(script, len(keys), *keys, *args)
