"""
统一限流器

整合多种限流策略和维度，提供统一的限流接口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..errors import RateLimitError
from ai_gateway_core.logging import get_logger
from ..observability.metrics import get_metrics
from .storage import MemoryRateLimitStorage, RedisRateLimitStorage
from .strategy import RateLimitStrategy, get_strategy

logger = get_logger(__name__)


class RateLimitDimension(str, Enum):
    """限流维度"""

    GLOBAL = "global"
    TENANT = "tenant"
    USER = "user"
    IP = "ip"
    SERVICE = "service"
    API_KEY = "api_key"
    OPERATION = "operation"


@dataclass
class RateLimitRule:
    """限流规则"""

    dimension: RateLimitDimension
    limit: int  # 限制次数
    window: int  # 时间窗口（秒）
    burst: int = 0  # 突发容量
    strategy: str = "sliding_window"  # 限流策略
    enabled: bool = True

    # 维度特定配置
    key_template: str = ""  # 键模板，如 "user:{user_id}"

    def make_key(self, context: dict[str, Any]) -> str:
        """生成限流键"""
        if self.key_template:
            return self.key_template.format(**context)

        prefix = f"ratelimit:{self.dimension.value}"

        if self.dimension == RateLimitDimension.GLOBAL:
            return prefix
        elif self.dimension == RateLimitDimension.TENANT:
            return f"{prefix}:{context.get('tenant_id', 'default')}"
        elif self.dimension == RateLimitDimension.USER:
            return f"{prefix}:{context.get('user_id', 'anonymous')}"
        elif self.dimension == RateLimitDimension.IP:
            return f"{prefix}:{context.get('client_ip', 'unknown')}"
        elif self.dimension == RateLimitDimension.SERVICE:
            return f"{prefix}:{context.get('service_id', 'unknown')}"
        elif self.dimension == RateLimitDimension.API_KEY:
            return f"{prefix}:{context.get('api_key', 'unknown')}"
        elif self.dimension == RateLimitDimension.OPERATION:
            return f"{prefix}:{context.get('operation', 'unknown')}"
        else:
            return prefix


@dataclass
class RateLimitResult:
    """限流结果"""

    allowed: bool
    dimension: str = ""
    limit: int = 0
    remaining: int = 0
    reset_at: int = 0  # Unix 时间戳
    retry_after: int = 0  # 建议等待秒数

    def to_headers(self) -> dict[str, str]:
        """转换为 HTTP 响应头"""
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_at),
        }

        if self.dimension:
            headers["X-RateLimit-Dimension"] = self.dimension

        if not self.allowed and self.retry_after > 0:
            headers["Retry-After"] = str(self.retry_after)

        return headers


@dataclass
class RateLimitConfig:
    """限流配置"""

    # 默认规则
    rules: list[RateLimitRule] = field(default_factory=list)

    # 用户层级配置
    tier_limits: dict[str, dict[str, int]] = field(
        default_factory=lambda: {
            "anonymous": {"requests": 10, "window": 60},
            "normal": {"requests": 60, "window": 60},
            "premium": {"requests": 300, "window": 60},
            "enterprise": {"requests": 1000, "window": 60},
        }
    )

    # 服务特定配置
    service_limits: dict[str, dict[str, int]] = field(default_factory=dict)

    # 操作类型配置
    operation_limits: dict[str, dict[str, int]] = field(default_factory=dict)

    @classmethod
    def default(cls) -> RateLimitConfig:
        """创建默认配置"""
        return cls(
            rules=[
                RateLimitRule(
                    dimension=RateLimitDimension.GLOBAL,
                    limit=10000,
                    window=60,
                ),
                RateLimitRule(
                    dimension=RateLimitDimension.IP,
                    limit=100,
                    window=60,
                ),
                RateLimitRule(
                    dimension=RateLimitDimension.USER,
                    limit=60,
                    window=60,
                ),
            ]
        )


class UnifiedRateLimiter:
    """
    统一限流器

    整合所有限流维度和策略，提供统一的限流检查接口。

    Features:
    - 多维度限流（全局、租户、用户、IP、服务、API Key）
    - 多种策略（滑动窗口、令牌桶、漏桶）
    - 用户分层限流
    - 服务级别自定义
    - 热更新配置
    """

    def __init__(
        self,
        config: RateLimitConfig | None = None,
        redis_client: Any | None = None,
    ):
        """
        Args:
            config: 限流配置
            redis_client: Redis 客户端（可选，用于分布式限流）
        """
        self.config = config or RateLimitConfig.default()

        # 初始化存储
        if redis_client:
            self._storage = RedisRateLimitStorage(redis_client)
        else:
            self._storage = MemoryRateLimitStorage()

        # 策略缓存
        self._strategies: dict[str, RateLimitStrategy] = {}

    def _get_strategy(self, name: str) -> RateLimitStrategy:
        """获取限流策略（带缓存）"""
        if name not in self._strategies:
            self._strategies[name] = get_strategy(name)
        return self._strategies[name]

    async def check(
        self,
        user_id: str | None = None,
        tenant_id: str | None = None,
        client_ip: str | None = None,
        service_id: str | None = None,
        api_key: str | None = None,
        operation: str | None = None,
        user_tier: str = "anonymous",
    ) -> RateLimitResult:
        """
        检查限流

        按配置的规则顺序检查，任一规则超限则拒绝。

        Args:
            user_id: 用户 ID
            tenant_id: 租户 ID
            client_ip: 客户端 IP
            service_id: 服务 ID
            api_key: API Key
            operation: 操作类型
            user_tier: 用户层级

        Returns:
            限流结果
        """
        context = {
            "user_id": user_id or "anonymous",
            "tenant_id": tenant_id or "default",
            "client_ip": client_ip or "unknown",
            "service_id": service_id or "unknown",
            "api_key": api_key or "unknown",
            "operation": operation or "default",
            "user_tier": user_tier,
        }

        # 收集要检查的规则
        rules_to_check = self._collect_rules(context)

        # 按顺序检查每条规则
        for rule in rules_to_check:
            if not rule.enabled:
                continue

            key = rule.make_key(context)
            strategy = self._get_strategy(rule.strategy)

            # 获取实际限制（考虑用户层级）
            limit, burst = self._get_effective_limit(rule, context)

            result = await strategy.check(
                key=key,
                limit=limit,
                window=rule.window,
                storage=self._storage,
                burst=burst,
            )

            if not result.allowed:
                logger.warning(
                    f"Rate limit exceeded for {rule.dimension.value}",
                    extra={
                        "dimension": rule.dimension.value,
                        "key": key,
                        "limit": limit,
                        **context,
                    },
                )

                # 记录指标
                metrics = get_metrics()
                metrics.request_metrics.record_rate_limit(
                    dimension=rule.dimension.value,
                    service_id=service_id or "",
                )

                return RateLimitResult(
                    allowed=False,
                    dimension=rule.dimension.value,
                    limit=result.limit,
                    remaining=0,
                    reset_at=int(result.reset_at),
                    retry_after=int(result.retry_after or 0),
                )

        # 所有规则都通过
        return RateLimitResult(allowed=True)

    def _collect_rules(self, context: dict[str, Any]) -> list[RateLimitRule]:
        """收集要检查的规则"""
        rules = []

        # 添加配置的规则
        rules.extend(self.config.rules)

        # 添加服务特定规则
        service_id = context.get("service_id")
        if service_id and service_id in self.config.service_limits:
            service_config = self.config.service_limits[service_id]
            rules.append(
                RateLimitRule(
                    dimension=RateLimitDimension.SERVICE,
                    limit=service_config.get("requests", 100),
                    window=service_config.get("window", 60),
                    burst=service_config.get("burst", 0),
                )
            )

        # 添加操作类型规则
        operation = context.get("operation")
        if operation and operation in self.config.operation_limits:
            op_config = self.config.operation_limits[operation]
            rules.append(
                RateLimitRule(
                    dimension=RateLimitDimension.OPERATION,
                    limit=op_config.get("requests", 100),
                    window=op_config.get("window", 60),
                    burst=op_config.get("burst", 0),
                )
            )

        return rules

    def _get_effective_limit(
        self,
        rule: RateLimitRule,
        context: dict[str, Any],
    ) -> tuple[int, int]:
        """
        获取有效的限制值

        对于用户维度，根据用户层级调整限制。
        """
        limit = rule.limit
        burst = rule.burst

        # 用户维度：根据层级调整
        if rule.dimension == RateLimitDimension.USER:
            user_tier = context.get("user_tier", "anonymous")
            if user_tier in self.config.tier_limits:
                tier_config = self.config.tier_limits[user_tier]
                limit = tier_config.get("requests", limit)
                burst = tier_config.get("burst", burst)

        return limit, burst

    async def enforce(
        self,
        user_id: str | None = None,
        tenant_id: str | None = None,
        client_ip: str | None = None,
        service_id: str | None = None,
        api_key: str | None = None,
        operation: str | None = None,
        user_tier: str = "anonymous",
    ) -> RateLimitResult:
        """
        强制执行限流

        如果超限，抛出 RateLimitError。

        Returns:
            限流结果（如果通过）

        Raises:
            RateLimitError: 如果超限
        """
        result = await self.check(
            user_id=user_id,
            tenant_id=tenant_id,
            client_ip=client_ip,
            service_id=service_id,
            api_key=api_key,
            operation=operation,
            user_tier=user_tier,
        )

        if not result.allowed:
            raise RateLimitError(
                dimension=result.dimension,
                limit=result.limit,
                remaining=result.remaining,
                reset_at=result.reset_at,
                retry_after=result.retry_after,
            )

        return result

    def update_config(self, config: RateLimitConfig) -> None:
        """热更新配置"""
        self.config = config
        logger.info("Rate limit config updated")

    def add_rule(self, rule: RateLimitRule) -> None:
        """添加规则"""
        self.config.rules.append(rule)

    def remove_rule(self, dimension: RateLimitDimension) -> bool:
        """移除指定维度的规则"""
        for i, rule in enumerate(self.config.rules):
            if rule.dimension == dimension:
                self.config.rules.pop(i)
                return True
        return False
