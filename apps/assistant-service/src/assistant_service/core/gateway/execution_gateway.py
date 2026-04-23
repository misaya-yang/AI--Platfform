"""Assistant execution gateway with command queue, approval, and run tracking."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ai_gateway_core.logging import get_logger
from ..openclaw.security.sandbox_resolver import SandboxResolver
from ..openclaw.tools.lane_scheduler import LaneScheduler
from ..openclaw.tools.policy_lattice import ToolPolicyLattice
from ..tool_invoker import ToolInvocationContext, ToolInvoker
from ..tools.tool_registry import ToolCallResult
from .policy_engine import AssistantPolicyEngine
from .request_router import RoutedAssistantRequest

logger = get_logger(__name__)


@dataclass
class ApprovalRecord:
    """In-memory fallback approval record."""

    approval_id: str
    tenant_id: str
    user_id: str
    session_id: str
    run_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: str = "pending"
    reason: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass
class RunRecord:
    """In-memory fallback run record."""

    run_id: str
    tenant_id: str
    user_id: str
    session_id: str
    status: str
    engine: str
    execution_profile: str
    memory_mode: str
    os_agent_enabled: bool
    request_preview: str
    queue_mode: str | None = None
    openclaw_mode: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None


class AssistantExecutionGateway:
    """Gateway wrapper around tool invocation and run lifecycle."""

    def __init__(
        self,
        tool_invoker: ToolInvoker,
        policy_engine: AssistantPolicyEngine | None = None,
        database: Any | None = None,
        enabled: bool = True,
    ) -> None:
        self.tool_invoker = tool_invoker
        self.policy_engine = policy_engine or AssistantPolicyEngine.from_env()
        self.database = database
        self.enabled = enabled

        self._runs: dict[str, RunRecord] = {}
        self._approvals: dict[str, ApprovalRecord] = {}
        self._commands: dict[str, dict[str, Any]] = {}
        self._lane_scheduler = LaneScheduler()
        self._policy_lattice = ToolPolicyLattice()
        self._sandbox_resolver = SandboxResolver()
        self._tool_policy_v2_enabled = (
            os.getenv("ASSISTANT_OPENCLAW_TOOL_POLICY_V2", "false").lower() == "true"
        )

    @staticmethod
    def _safe_uuid(value: str | None) -> str | None:
        if not value:
            return None
        try:
            return str(uuid.UUID(str(value)))
        except Exception:
            return None

    # ---------------------------------------------------------------------
    # Public API - policies / runs / approvals
    # ---------------------------------------------------------------------

    def get_policies(self) -> dict[str, Any]:
        return {
            **self.policy_engine.get_public_policies(),
            "gateway_enabled": self.enabled,
        }

    async def start_run(
        self,
        run_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        engine: str,
        execution_profile: str,
        memory_mode: str,
        os_agent_enabled: bool,
        request_preview: str,
        queue_mode: str | None = None,
        openclaw_mode: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self._runs[run_id] = RunRecord(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            status="running",
            engine=engine,
            execution_profile=execution_profile,
            memory_mode=memory_mode,
            os_agent_enabled=os_agent_enabled,
            queue_mode=queue_mode,
            openclaw_mode=openclaw_mode,
            request_preview=request_preview,
            started_at=now,
        )

        if not self.database:
            return

        query = """
            INSERT INTO assistant_runs (
                run_id, tenant_id, user_id, session_id, status, engine,
                execution_profile, memory_mode, os_agent_enabled,
                request_preview, started_at, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW(), NOW())
            ON CONFLICT (run_id)
            DO UPDATE SET
                status = EXCLUDED.status,
                engine = EXCLUDED.engine,
                execution_profile = EXCLUDED.execution_profile,
                memory_mode = EXCLUDED.memory_mode,
                os_agent_enabled = EXCLUDED.os_agent_enabled,
                request_preview = EXCLUDED.request_preview,
                updated_at = NOW();
        """
        try:
            await self.database.execute(
                query,
                run_id,
                tenant_id,
                user_id,
                session_id,
                "running",
                engine,
                execution_profile,
                memory_mode,
                os_agent_enabled,
                request_preview,
                now,
            )
        except Exception as exc:
            logger.warning("Failed to persist assistant run start: %s", exc)

    async def finish_run(
        self,
        run_id: str,
        status: str,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        run = self._runs.get(run_id)
        if run:
            run.status = status
            run.usage = usage or {}
            run.error = error
            run.finished_at = now

        if not self.database:
            return

        query = """
            UPDATE assistant_runs
            SET status = $2,
                usage = $3,
                error = $4,
                finished_at = $5,
                updated_at = NOW()
            WHERE run_id = $1;
        """
        try:
            await self.database.execute(
                query,
                run_id,
                status,
                json.dumps(usage or {}),
                error,
                now,
            )
        except Exception as exc:
            logger.warning("Failed to persist assistant run finish: %s", exc)

    async def get_run(
        self,
        run_id: str,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        run = self._runs.get(run_id)
        if run and run.tenant_id == tenant_id and run.user_id == user_id:
            return {
                "run_id": run.run_id,
                "tenant_id": run.tenant_id,
                "user_id": run.user_id,
                "session_id": run.session_id,
                "status": run.status,
                "engine": run.engine,
                "execution_profile": run.execution_profile,
                "memory_mode": run.memory_mode,
                "os_agent_enabled": run.os_agent_enabled,
                "queue_mode": run.queue_mode,
                "openclaw_mode": run.openclaw_mode,
                "request_preview": run.request_preview,
                "usage": run.usage,
                "error": run.error,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            }

        if not self.database:
            return None

        query = """
            SELECT run_id, tenant_id, user_id, session_id, status, engine,
                   execution_profile, memory_mode, os_agent_enabled,
                   request_preview, usage, error, started_at, finished_at
            FROM assistant_runs
            WHERE run_id = $1 AND tenant_id = $2 AND user_id = $3
            LIMIT 1;
        """
        row = await self.database.fetchrow(query, run_id, tenant_id, user_id)
        if not row:
            return None

        usage = row.get("usage")
        if isinstance(usage, str):
            try:
                usage = json.loads(usage)
            except Exception:
                usage = {}

        return {
            "run_id": row.get("run_id"),
            "tenant_id": row.get("tenant_id"),
            "user_id": row.get("user_id"),
            "session_id": row.get("session_id"),
            "status": row.get("status"),
            "engine": row.get("engine"),
            "execution_profile": row.get("execution_profile"),
            "memory_mode": row.get("memory_mode"),
            "os_agent_enabled": bool(row.get("os_agent_enabled")),
            "queue_mode": row.get("queue_mode"),
            "openclaw_mode": row.get("openclaw_mode"),
            "request_preview": row.get("request_preview"),
            "usage": usage or {},
            "error": row.get("error"),
            "started_at": row.get("started_at").isoformat() if row.get("started_at") else None,
            "finished_at": row.get("finished_at").isoformat() if row.get("finished_at") else None,
        }

    async def approve(
        self,
        approval_id: str,
        tenant_id: str,
        user_id: str,
        approved: bool,
        approver_user_id: str,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        status = "approved" if approved else "rejected"
        now = datetime.now(timezone.utc)

        record = self._approvals.get(approval_id)
        if record and record.tenant_id == tenant_id and record.user_id == user_id:
            record.status = status
            record.approved_by = approver_user_id
            record.approved_at = now
            record.reason = reason

        if self.database:
            query = """
                UPDATE assistant_tool_approvals
                SET status = $4,
                    approved_by = $5,
                    approved_at = $6,
                    reason = COALESCE($7, reason),
                    updated_at = NOW()
                WHERE approval_id = $1
                  AND tenant_id = $2
                  AND user_id = $3
                RETURNING approval_id, tenant_id, user_id, session_id, run_id,
                          tool_name, arguments, status, reason, approved_by,
                          approved_at, expires_at, created_at;
            """
            row = await self.database.fetchrow(
                query,
                approval_id,
                tenant_id,
                user_id,
                status,
                approver_user_id,
                now,
                reason,
            )
            if row:
                args = row.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                return {
                    "approval_id": row.get("approval_id"),
                    "tenant_id": row.get("tenant_id"),
                    "user_id": row.get("user_id"),
                    "session_id": row.get("session_id"),
                    "run_id": row.get("run_id"),
                    "tool_name": row.get("tool_name"),
                    "arguments": args or {},
                    "status": row.get("status"),
                    "reason": row.get("reason"),
                    "approved_by": row.get("approved_by"),
                    "approved_at": row.get("approved_at").isoformat()
                    if row.get("approved_at")
                    else None,
                    "expires_at": row.get("expires_at").isoformat()
                    if row.get("expires_at")
                    else None,
                    "created_at": row.get("created_at").isoformat()
                    if row.get("created_at")
                    else None,
                }

        if not record:
            return None

        return {
            "approval_id": record.approval_id,
            "tenant_id": record.tenant_id,
            "user_id": record.user_id,
            "session_id": record.session_id,
            "run_id": record.run_id,
            "tool_name": record.tool_name,
            "arguments": record.arguments,
            "status": record.status,
            "reason": record.reason,
            "approved_by": record.approved_by,
            "approved_at": record.approved_at.isoformat() if record.approved_at else None,
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
            "created_at": None,
        }

    async def invoke_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        routed_request: RoutedAssistantRequest | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> ToolCallResult:
        """Invoke tool through queue + policy checks."""
        started = time.time()
        profile = (routed_request.execution_profile if routed_request else None) or getattr(
            context, "policy_profile", "safe"
        )
        queue_mode = (
            routed_request.queue_mode
            if routed_request is not None
            else str((getattr(context, "metadata", {}) or {}).get("queue_mode") or "collect")
        )
        lane = self._resolve_lane(queue_mode, tool_name)
        priority = self._resolve_priority(queue_mode)
        steer_payload = arguments.get("_steer_payload")
        os_agent_enabled = (
            bool(routed_request.os_agent_enabled)
            if routed_request is not None
            else bool(getattr(context, "os_agent_enabled", False))
        )

        decision = self.policy_engine.evaluate_tool(
            tool_name=tool_name,
            context=context,
            execution_profile=profile,
            os_agent_enabled=os_agent_enabled,
        )

        if self._tool_policy_v2_enabled:
            lattice_layers = {
                "profile": {
                    "require_approval": sorted(
                        self.policy_engine.MEDIUM_RISK_TOOLS | self.policy_engine.HIGH_RISK_TOOLS
                    )
                    if profile == "safe"
                    else []
                },
                "queue_mode": {
                    "require_approval": sorted(self.policy_engine.HIGH_RISK_TOOLS)
                    if queue_mode in {"steer", "interrupt"}
                    else []
                },
            }
            lattice = self._policy_lattice.evaluate(
                tool_name=tool_name,
                base_allowed=decision.allowed,
                base_requires_approval=decision.requires_approval,
                base_reason=decision.reason or "Allowed by base policy",
                layers=lattice_layers,
            )
            decision.allowed = lattice.allowed
            decision.requires_approval = lattice.requires_approval
            decision.reason = lattice.reason
            lattice_payload = lattice.to_dict()
        else:
            lattice_payload = None

        sandbox_decision = self._sandbox_resolver.resolve(
            tool_name=tool_name,
            execution_profile=profile,
            os_agent_enabled=os_agent_enabled,
        )
        if not sandbox_decision.allowed:
            decision.allowed = False
            decision.requires_approval = False
            decision.reason = sandbox_decision.reason
        elif sandbox_decision.requires_approval:
            decision.requires_approval = True

        decision_payload = {
            "allowed": decision.allowed,
            "requires_approval": decision.requires_approval,
            "reason": decision.reason,
            "policy_profile": decision.policy_profile,
            "queue_mode": queue_mode,
            "lane": lane,
            "lattice": lattice_payload,
        }
        sandbox_payload = sandbox_decision.to_dict()

        if not decision.allowed:
            return ToolCallResult(
                call_id=str(uuid.uuid4()),
                tool_name=tool_name,
                success=False,
                error=decision.reason or "Tool denied by policy",
                duration_ms=(time.time() - started) * 1000,
                metadata={
                    "gateway_decision": decision_payload,
                    "sandbox_decision": sandbox_payload,
                    "queue_mode": queue_mode,
                    "lane": lane,
                },
            )

        command_key = self._build_command_key(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=context.session_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        existing_command_id = await self._find_active_command(command_key)
        if existing_command_id:
            return ToolCallResult(
                call_id=str(uuid.uuid4()),
                tool_name=tool_name,
                success=False,
                error="COMMAND_DEDUPED",
                duration_ms=(time.time() - started) * 1000,
                metadata={
                    "queue_state": "deduped",
                    "command_id": existing_command_id,
                    "gateway_decision": decision_payload,
                    "sandbox_decision": sandbox_payload,
                    "queue_mode": queue_mode,
                    "lane": lane,
                },
            )

        command_id = str(uuid.uuid4())
        await self._create_command(
            command_id=command_id,
            context=context,
            tool_name=tool_name,
            arguments=arguments,
            status="queued",
            lane=lane,
            queue_mode=queue_mode,
            priority=priority,
            steer_payload=steer_payload if isinstance(steer_payload, dict) else None,
        )

        approval_id = arguments.get("_approval_id")
        if decision.requires_approval and not await self._approval_granted(
            approval_id=approval_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            tool_name=tool_name,
        ):
            pending_approval_id = await self._create_approval(
                context=context,
                tool_name=tool_name,
                arguments=arguments,
                reason=decision.reason or "Approval required by policy",
            )
            await self._update_command(
                command_id=command_id,
                status="awaiting_approval",
                error="APPROVAL_REQUIRED",
            )
            return ToolCallResult(
                call_id=str(uuid.uuid4()),
                tool_name=tool_name,
                success=False,
                error="APPROVAL_REQUIRED",
                duration_ms=(time.time() - started) * 1000,
                metadata={
                    "approval_required": True,
                    "approval_id": pending_approval_id,
                    "queue_state": "awaiting_approval",
                    "command_id": command_id,
                    "gateway_decision": decision_payload,
                    "sandbox_decision": sandbox_payload,
                    "queue_mode": queue_mode,
                    "lane": lane,
                },
            )

        await self._update_command(command_id=command_id, status="running")

        # Remove control-only args before tool call
        invoke_args = {
            k: v for k, v in arguments.items() if k not in {"_approval_id", "_steer_payload"}
        }

        async def _invoke() -> ToolCallResult:
            return await self.tool_invoker.invoke(
                tool_name=tool_name,
                arguments=invoke_args,
                context=context,
                cancel_event=cancel_event,
            )

        result = await self._lane_scheduler.run_in_lane(lane, _invoke)

        final_state = "succeeded" if result.success else "failed"
        await self._update_command(
            command_id=command_id,
            status=final_state,
            result=result.result,
            error=result.error,
        )

        metadata = dict(result.metadata or {})
        metadata.update(
            {
                "queue_state": final_state,
                "command_id": command_id,
                "gateway_decision": decision_payload,
                "sandbox_decision": sandbox_payload,
                "queue_mode": queue_mode,
                "lane": lane,
            }
        )
        result.metadata = metadata
        return result

    # ---------------------------------------------------------------------
    # Internal helpers - queue / approval storage
    # ---------------------------------------------------------------------

    async def _create_command(
        self,
        command_id: str,
        context: ToolInvocationContext,
        tool_name: str,
        arguments: dict[str, Any],
        status: str,
        lane: str,
        queue_mode: str,
        priority: int,
        steer_payload: dict[str, Any] | None,
    ) -> None:
        command_key = self._build_command_key(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=context.session_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        self._commands[command_id] = {
            "command_id": command_id,
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "session_id": context.session_id,
            "run_id": self._safe_uuid(context.run_id),
            "tool_name": tool_name,
            "arguments": arguments,
            "status": status,
            "command_key": command_key,
            "lane": lane,
            "queue_mode": queue_mode,
            "priority": priority,
            "steer_payload": steer_payload,
            "created_at": datetime.now(timezone.utc),
        }

        if not self.database:
            return

        query = """
            INSERT INTO assistant_command_queue (
                command_id, tenant_id, user_id, session_id, run_id,
                command_key, tool_name, arguments, status, lane,
                queue_mode, priority, steer_payload, retry_count,
                max_retries, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 0, 2, NOW(), NOW())
            ON CONFLICT (command_id) DO NOTHING;
        """
        try:
            await self.database.execute(
                query,
                command_id,
                context.tenant_id,
                context.user_id,
                context.session_id,
                self._safe_uuid(context.run_id),
                command_key,
                tool_name,
                json.dumps(arguments or {}),
                status,
                lane,
                queue_mode,
                priority,
                json.dumps(steer_payload or {}),
            )
        except Exception as exc:
            legacy_query = """
                INSERT INTO assistant_command_queue (
                    command_id, tenant_id, user_id, session_id, run_id,
                    command_key, tool_name, arguments, status, retry_count,
                    max_retries, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 0, 2, NOW(), NOW())
                ON CONFLICT (command_id) DO NOTHING;
            """
            try:
                await self.database.execute(
                    legacy_query,
                    command_id,
                    context.tenant_id,
                    context.user_id,
                    context.session_id,
                    self._safe_uuid(context.run_id),
                    command_key,
                    tool_name,
                    json.dumps(arguments or {}),
                    status,
                )
            except Exception:
                logger.warning("Failed to persist command queue item: %s", exc)

    async def _update_command(
        self,
        command_id: str,
        status: str,
        result: Any | None = None,
        error: str | None = None,
    ) -> None:
        item = self._commands.get(command_id)
        if item:
            item["status"] = status
            item["result"] = result
            item["error"] = error
            item["updated_at"] = datetime.now(timezone.utc)

        if not self.database:
            return

        lease_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=45) if status == "running" else None
        )

        query = """
            UPDATE assistant_command_queue
            SET status = $2,
                result = $3,
                error = $4,
                lease_expires_at = $5,
                updated_at = NOW()
            WHERE command_id = $1;
        """
        try:
            await self.database.execute(
                query,
                command_id,
                status,
                json.dumps(result) if result is not None else None,
                error,
                lease_expires_at,
            )
        except Exception as exc:
            logger.warning("Failed to update command queue item: %s", exc)

    async def _create_approval(
        self,
        context: ToolInvocationContext,
        tool_name: str,
        arguments: dict[str, Any],
        reason: str,
    ) -> str:
        approval_id = str(uuid.uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        record = ApprovalRecord(
            approval_id=approval_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=context.session_id,
            run_id=self._safe_uuid(context.run_id) or self._safe_uuid(context.request_id) or "",
            tool_name=tool_name,
            arguments=arguments,
            reason=reason,
            expires_at=expires_at,
        )
        self._approvals[approval_id] = record

        if not self.database:
            return approval_id

        query = """
            INSERT INTO assistant_tool_approvals (
                approval_id, tenant_id, user_id, session_id, run_id,
                tool_name, arguments, status, reason, expires_at,
                created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8, $9, NOW(), NOW())
            ON CONFLICT (approval_id) DO NOTHING;
        """
        try:
            await self.database.execute(
                query,
                approval_id,
                context.tenant_id,
                context.user_id,
                context.session_id,
                self._safe_uuid(context.run_id),
                tool_name,
                json.dumps(arguments or {}),
                reason,
                expires_at,
            )
        except Exception as exc:
            logger.warning("Failed to persist approval: %s", exc)

        return approval_id

    async def _approval_granted(
        self,
        approval_id: str | None,
        tenant_id: str,
        user_id: str,
        tool_name: str,
    ) -> bool:
        if not approval_id:
            return False

        record = self._approvals.get(approval_id)
        if record:
            if record.tenant_id != tenant_id or record.user_id != user_id:
                return False
            if record.tool_name != tool_name:
                return False
            if record.expires_at and record.expires_at < datetime.now(timezone.utc):
                return False
            return record.status == "approved"

        if not self.database:
            return False

        query = """
            SELECT status, expires_at, tool_name
            FROM assistant_tool_approvals
            WHERE approval_id = $1 AND tenant_id = $2 AND user_id = $3
            LIMIT 1;
        """
        row = await self.database.fetchrow(query, approval_id, tenant_id, user_id)
        if not row:
            return False

        if row.get("tool_name") != tool_name:
            return False
        expires_at = row.get("expires_at")
        if expires_at and expires_at < datetime.now(timezone.utc):
            return False
        return row.get("status") == "approved"

    async def _find_active_command(self, command_key: str) -> str | None:
        """Return the command_id of an active dedup match, or None.

        ADR-004 §B: when ``self.database`` is available this reads against
        ``assistant_command_queue`` via the partial index
        ``idx_assistant_command_queue_active_by_key`` (migration 056).
        The in-memory dict scan is kept as a fallback **only** for code
        paths that construct an ``AssistantExecutionGateway`` without a
        database (primarily tests and the legacy no-DB dev loop); it is
        scheduled for removal once Phase 5c migrates every deployment to
        the DB-required init path.
        """
        if self.database:
            try:
                row = await self.database.fetchrow(
                    """
                    SELECT command_id
                      FROM assistant_command_queue
                     WHERE command_key = $1
                       AND status IN ('queued', 'running', 'awaiting_approval')
                     ORDER BY created_at DESC
                     LIMIT 1
                    """,
                    command_key,
                )
                if row:
                    return row["command_id"]
                return None
            except Exception as exc:  # noqa: BLE001 — keep the fallback open
                logger.warning(
                    "_find_active_command DB query failed, falling back to "
                    "in-memory scan: %s",
                    exc,
                )
        # In-memory fallback — tests and DB-less dev only.
        for command_id, item in self._commands.items():
            if item.get("command_key") != command_key:
                continue
            if item.get("status") in {"queued", "running", "awaiting_approval"}:
                return command_id
        return None

    @staticmethod
    def _resolve_priority(queue_mode: str) -> int:
        mapping = {
            "collect": 0,
            "followup": 1,
            "steer": 2,
            "interrupt": 3,
        }
        return mapping.get(str(queue_mode or "collect"), 0)

    def _resolve_lane(self, queue_mode: str, tool_name: str) -> str:
        mode = str(queue_mode or "collect")
        if mode == "interrupt":
            return "main"
        if mode == "followup":
            return "subagent"
        if mode == "steer":
            return "main"
        if tool_name in {"system_run_lite", "browser_action_lite"}:
            return "subagent"
        return "main"

    @staticmethod
    def _build_command_key(
        tenant_id: str,
        user_id: str,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        payload = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "session_id": session_id,
            "tool_name": tool_name,
            "arguments": arguments,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, encoded))
