"""
Confluence Polling Scheduler.

Provides scheduled polling for Confluence space synchronization.
Uses asyncio for lightweight scheduling without external dependencies.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Set

if TYPE_CHECKING:
    from .sync_service import ConfluenceSyncService

logger = logging.getLogger(__name__)


class PollingTask:
    """单个轮询任务"""

    def __init__(
        self,
        binding_id: str,
        interval_minutes: int,
        callback: Callable,
    ):
        self.binding_id = binding_id
        self.interval_minutes = interval_minutes
        self.callback = callback
        self.last_run: Optional[datetime] = None
        self.next_run: Optional[datetime] = None
        self.is_running: bool = False
        self.run_count: int = 0
        self.error_count: int = 0
        self.last_error: Optional[str] = None
        self._task: Optional[asyncio.Task] = None

    @property
    def interval_seconds(self) -> int:
        return self.interval_minutes * 60

    def schedule_next(self) -> None:
        """计算下次运行时间"""
        self.next_run = datetime.utcnow() + timedelta(minutes=self.interval_minutes)

    def should_run(self) -> bool:
        """检查是否应该运行"""
        if self.is_running:
            return False
        if self.next_run is None:
            return True
        return datetime.utcnow() >= self.next_run


class ConfluenceScheduler:
    """
    Confluence 轮询调度器

    管理多个空间绑定的定时同步任务。
    使用 asyncio 实现轻量级调度。

    Features:
    - 动态添加/移除轮询任务
    - 独立的轮询间隔
    - 错误重试和回退
    - 并发控制
    """

    def __init__(
        self,
        sync_service: "ConfluenceSyncService",
        max_concurrent: int = 3,
        error_backoff_minutes: int = 5,
        max_error_backoff_minutes: int = 60,
    ):
        """
        初始化调度器

        Args:
            sync_service: Confluence 同步服务
            max_concurrent: 最大并发同步数
            error_backoff_minutes: 错误后初始回退时间
            max_error_backoff_minutes: 最大回退时间
        """
        self.sync_service = sync_service
        self.max_concurrent = max_concurrent
        self.error_backoff_minutes = error_backoff_minutes
        self.max_error_backoff_minutes = max_error_backoff_minutes

        self._tasks: Dict[str, PollingTask] = {}
        self._running: bool = False
        self._scheduler_task: Optional[asyncio.Task] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._active_syncs: Set[str] = set()

    @property
    def is_running(self) -> bool:
        """调度器是否正在运行"""
        return self._running

    @property
    def task_count(self) -> int:
        """当前任务数量"""
        return len(self._tasks)

    @property
    def active_sync_count(self) -> int:
        """当前正在同步的数量"""
        return len(self._active_syncs)

    async def start(self) -> None:
        """启动调度器"""
        if self._running:
            logger.warning("Scheduler is already running")
            return

        self._running = True
        self._semaphore = asyncio.Semaphore(self.max_concurrent)

        # 加载已启用轮询的绑定
        await self._load_polling_bindings()

        # 启动调度循环
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info(f"Confluence scheduler started with {self.task_count} tasks")

    async def stop(self) -> None:
        """停止调度器"""
        if not self._running:
            return

        self._running = False

        # 取消调度任务
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass

        # 等待所有活动同步完成
        if self._active_syncs:
            logger.info(f"Waiting for {len(self._active_syncs)} active syncs to complete")
            # 给一些时间让同步完成
            await asyncio.sleep(2)

        self._tasks.clear()
        logger.info("Confluence scheduler stopped")

    async def add_task(
        self,
        binding_id: str,
        interval_minutes: int,
        run_immediately: bool = False,
    ) -> None:
        """
        添加轮询任务

        Args:
            binding_id: 空间绑定 ID
            interval_minutes: 轮询间隔（分钟）
            run_immediately: 是否立即运行一次
        """
        if binding_id in self._tasks:
            # 更新现有任务的间隔
            self._tasks[binding_id].interval_minutes = interval_minutes
            logger.info(f"Updated polling interval for binding {binding_id}")
            return

        task = PollingTask(
            binding_id=binding_id,
            interval_minutes=interval_minutes,
            callback=self._sync_binding,
        )

        if run_immediately:
            task.next_run = datetime.utcnow()
        else:
            task.schedule_next()

        self._tasks[binding_id] = task
        logger.info(f"Added polling task for binding {binding_id}, interval={interval_minutes}min")

    async def remove_task(self, binding_id: str) -> None:
        """
        移除轮询任务

        Args:
            binding_id: 空间绑定 ID
        """
        if binding_id in self._tasks:
            del self._tasks[binding_id]
            logger.info(f"Removed polling task for binding {binding_id}")

    async def trigger_now(self, binding_id: str) -> bool:
        """
        立即触发同步

        Args:
            binding_id: 空间绑定 ID

        Returns:
            是否成功触发
        """
        if binding_id in self._active_syncs:
            logger.warning(f"Binding {binding_id} is already syncing")
            return False

        # 创建临时任务如果不存在
        if binding_id not in self._tasks:
            task = PollingTask(
                binding_id=binding_id,
                interval_minutes=0,  # 一次性任务
                callback=self._sync_binding,
            )
            self._tasks[binding_id] = task

        # 设置立即运行
        self._tasks[binding_id].next_run = datetime.utcnow()
        return True

    async def get_status(self) -> Dict:
        """获取调度器状态"""
        tasks_status = []
        for binding_id, task in self._tasks.items():
            tasks_status.append({
                "binding_id": binding_id,
                "interval_minutes": task.interval_minutes,
                "is_running": task.is_running,
                "last_run": task.last_run.isoformat() if task.last_run else None,
                "next_run": task.next_run.isoformat() if task.next_run else None,
                "run_count": task.run_count,
                "error_count": task.error_count,
                "last_error": task.last_error,
            })

        return {
            "is_running": self._running,
            "task_count": self.task_count,
            "active_sync_count": self.active_sync_count,
            "max_concurrent": self.max_concurrent,
            "tasks": tasks_status,
        }

    async def _load_polling_bindings(self) -> None:
        """从数据库加载启用轮询的绑定"""
        try:
            # 获取所有启用轮询的连接
            connections = await self.sync_service.db.get_confluence_connections_with_polling()

            for conn in connections:
                # 获取该连接下的所有绑定
                bindings = await self.sync_service.db.get_confluence_bindings_by_connection(
                    conn["connection_id"]
                )

                for binding in bindings:
                    if binding.get("status") in ("completed", "pending"):
                        await self.add_task(
                            binding_id=binding["binding_id"],
                            interval_minutes=conn.get("polling_interval_minutes", 60),
                        )

            logger.info(f"Loaded {self.task_count} polling tasks from database")

        except Exception as e:
            logger.error(f"Failed to load polling bindings: {e}")

    async def _scheduler_loop(self) -> None:
        """调度主循环"""
        logger.debug("Scheduler loop started")

        while self._running:
            try:
                # 检查所有任务
                tasks_to_run = []
                for binding_id, task in list(self._tasks.items()):
                    if task.should_run():
                        tasks_to_run.append(binding_id)

                # 并发执行待运行的任务
                if tasks_to_run:
                    await asyncio.gather(
                        *[self._run_task(bid) for bid in tasks_to_run],
                        return_exceptions=True,
                    )

                # 等待一段时间再检查
                await asyncio.sleep(30)  # 每 30 秒检查一次

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}")
                await asyncio.sleep(60)

        logger.debug("Scheduler loop stopped")

    async def _run_task(self, binding_id: str) -> None:
        """运行单个任务"""
        if binding_id not in self._tasks:
            return

        task = self._tasks[binding_id]
        if task.is_running or binding_id in self._active_syncs:
            return

        # 获取信号量
        if self._semaphore is None:
            return

        async with self._semaphore:
            task.is_running = True
            self._active_syncs.add(binding_id)

            try:
                logger.info(f"Starting polling sync for binding {binding_id}")
                await task.callback(binding_id)

                # 成功后重置错误计数
                task.run_count += 1
                task.error_count = 0
                task.last_error = None
                task.last_run = datetime.utcnow()
                task.schedule_next()

                logger.info(f"Polling sync completed for binding {binding_id}")

            except Exception as e:
                logger.error(f"Polling sync failed for binding {binding_id}: {e}")
                task.error_count += 1
                task.last_error = str(e)

                # 错误回退
                backoff = min(
                    self.error_backoff_minutes * (2 ** (task.error_count - 1)),
                    self.max_error_backoff_minutes,
                )
                task.next_run = datetime.utcnow() + timedelta(minutes=backoff)
                logger.info(f"Next retry for binding {binding_id} in {backoff} minutes")

            finally:
                task.is_running = False
                self._active_syncs.discard(binding_id)

    async def _sync_binding(self, binding_id: str) -> None:
        """执行绑定同步（使用增量同步）"""
        try:
            # 优先使用增量同步
            await self.sync_service.incremental_sync(binding_id)
        except Exception as e:
            logger.warning(f"Incremental sync failed for {binding_id}, error: {e}")
            # 如果增量同步失败（可能是首次同步），回退到全量同步
            await self.sync_service.trigger_sync(binding_id)

    async def reschedule_binding(self, binding_id: str, interval_minutes: int) -> None:
        """
        重新调度绑定（当配置变更时调用）

        Args:
            binding_id: 绑定 ID
            interval_minutes: 新的轮询间隔（分钟）
        """
        if binding_id in self._tasks:
            task = self._tasks[binding_id]
            old_interval = task.interval_minutes
            task.interval_minutes = interval_minutes
            task.schedule_next()
            logger.info(
                f"Rescheduled binding {binding_id}: "
                f"interval changed from {old_interval}min to {interval_minutes}min"
            )
        else:
            # 如果任务不存在，添加新任务
            await self.add_task(binding_id, interval_minutes)

    async def disable_binding(self, binding_id: str) -> None:
        """
        禁用绑定的轮询

        Args:
            binding_id: 绑定 ID
        """
        await self.remove_task(binding_id)
        logger.info(f"Disabled polling for binding {binding_id}")

    async def reload_bindings(self) -> None:
        """
        重新加载所有绑定配置

        在配置变更后调用以刷新调度
        """
        logger.info("Reloading polling bindings...")

        # 获取当前应该轮询的绑定
        polling_bindings = await self.sync_service.list_bindings_for_polling()
        polling_binding_ids = {b["binding_id"] for b in polling_bindings}

        # 移除不再需要轮询的任务
        for binding_id in list(self._tasks.keys()):
            if binding_id not in polling_binding_ids:
                await self.remove_task(binding_id)

        # 添加或更新轮询任务
        for binding in polling_bindings:
            binding_id = binding["binding_id"]
            interval = binding.get("polling_interval_minutes", 60)

            if binding_id in self._tasks:
                # 更新间隔
                if self._tasks[binding_id].interval_minutes != interval:
                    await self.reschedule_binding(binding_id, interval)
            else:
                # 添加新任务
                await self.add_task(binding_id, interval)

        logger.info(f"Reloaded {self.task_count} polling tasks")


class SchedulerManager:
    """
    调度器管理器

    提供全局单例访问和生命周期管理。
    """

    _instance: Optional[ConfluenceScheduler] = None

    @classmethod
    def get_instance(cls) -> Optional[ConfluenceScheduler]:
        """获取调度器实例"""
        return cls._instance

    @classmethod
    def initialize(
        cls,
        sync_service: "ConfluenceSyncService",
        **kwargs,
    ) -> ConfluenceScheduler:
        """
        初始化调度器

        Args:
            sync_service: 同步服务实例
            **kwargs: 传递给 ConfluenceScheduler 的参数

        Returns:
            调度器实例
        """
        if cls._instance is not None:
            logger.warning("Scheduler already initialized, returning existing instance")
            return cls._instance

        cls._instance = ConfluenceScheduler(sync_service, **kwargs)
        return cls._instance

    @classmethod
    async def start(cls) -> None:
        """启动调度器"""
        if cls._instance:
            await cls._instance.start()

    @classmethod
    async def stop(cls) -> None:
        """停止调度器"""
        if cls._instance:
            await cls._instance.stop()
            cls._instance = None
