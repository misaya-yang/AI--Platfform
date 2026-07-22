"""Focused UAO-04 checkpoint, approval, and recovery safety contracts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway
from assistant_service.core.tool_invoker import ToolInvocationContext
from assistant_service.core.tools.tool_registry import ToolCallResult

RUN_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID_2 = "22222222-2222-4222-8222-222222222222"
RUN_ID_3 = "33333333-3333-4333-8333-333333333333"


class _CountingInvoker:
    def __init__(self) -> None:
        self.count = 0

    async def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        cancel_event: Any = None,
    ) -> ToolCallResult:
        del arguments, context, cancel_event
        self.count += 1
        return ToolCallResult(
            call_id=f"call-{self.count}",
            tool_name=tool_name,
            success=True,
            result={"ok": True},
        )


class _UnknownResultInvoker(_CountingInvoker):
    async def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        cancel_event: Any = None,
    ) -> ToolCallResult:
        del arguments, context, cancel_event
        self.count += 1
        return ToolCallResult(
            call_id=f"call-{self.count}",
            tool_name=tool_name,
            success=False,
            error="provider outcome unavailable",
            metadata={
                "tool_failure": {
                    "failure_kind": "side_effect_unknown",
                    "side_effect_state": "unknown",
                }
            },
        )


class _ArtifactResultInvoker(_CountingInvoker):
    async def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        cancel_event: Any = None,
    ) -> ToolCallResult:
        del arguments, context, cancel_event
        self.count += 1
        return ToolCallResult(
            call_id=f"call-{self.count}",
            tool_name=tool_name,
            success=True,
            result={"ok": True},
            output_files=[
                {
                    "filename": "receipt.txt",
                    "mime_type": "text/plain",
                    "content_base64": "b2s=",
                    "size_bytes": 2,
                }
            ],
        )


class _AsyncContext:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _SharedCommandConnection:
    def __init__(self, database: _SharedCommandDatabase) -> None:
        self.database = database

    def transaction(self, **_kwargs: Any) -> _AsyncContext:
        return _AsyncContext(self)

    async def execute(self, _query: str, *_args: Any) -> str:
        return "SELECT 1"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        database = self.database
        if "RETURNING command.status, command.steer_payload" in query:
            command = database.commands.get(str(args[0]))
            checkpoint = database.find_completion_checkpoint(
                command_id=str(args[0]),
                tenant_id=str(args[1]),
                user_id=str(args[2]),
                session_id=str(args[3]),
            )
            if not database.completion_matches_command(checkpoint, command):
                return None
            database.settle_command(command, checkpoint)
            if database.drop_reconcile_after_commit_once:
                database.drop_reconcile_after_commit_once = False
                raise ConnectionError("ack lost after reconcile commit")
            return {
                "status": command["status"],
                "steer_payload": dict(command["steer_payload"]),
            }
        if "SELECT runs.run_id" in query:
            if database.run["status"] != "running" or database.hard_checkpoint:
                return None
            return {"run_id": database.run["run_id"]}
        if "SELECT command_id, run_id, tool_name" in query:
            database.command_select_query = query
            command_key = str(args[0])
            execution_intent_id = str(args[4])
            tool_name = str(args[5])
            effective_arguments = json.loads(str(args[6]))
            approval_id = str(args[7])
            active = {
                "queued",
                "running",
                "awaiting_approval",
                "approval_claimed",
                "side_effect_unknown",
            }
            candidates = [
                item
                for item in database.commands.values()
                if (
                    item["command_key"] == command_key
                    or (
                        item["tool_name"] == tool_name
                        and AssistantExecutionGateway._without_control_args(item["arguments"])
                        == effective_arguments
                    )
                )
                and (
                    item["status"] in active
                    or item["status"] in {"result_recorded_succeeded", "result_recorded_failed"}
                    or (
                        item["status"] in {"succeeded", "failed"}
                        and (
                            item.get("steer_payload", {}).get("_execution_intent_id")
                            == execution_intent_id
                            or (
                                approval_id
                                and item.get("arguments", {}).get("_approval_id") == approval_id
                            )
                        )
                        and item.get("steer_payload", {}).get("_result_receipt_recorded") is True
                    )
                )
            ]
            safety_priority = {
                "side_effect_unknown": 0,
                "approval_claimed": 0,
                "running": 0,
                "result_recorded_succeeded": 1,
                "result_recorded_failed": 1,
                "succeeded": 1,
                "failed": 1,
                "queued": 2,
                "awaiting_approval": 3,
            }
            candidates.sort(
                key=lambda item: (
                    (
                        0
                        if item["status"] == "queued" and item.get("lease_expires_at") is None
                        else safety_priority.get(item["status"], 4)
                    ),
                    -item["created_at"].timestamp(),
                )
            )
            return dict(candidates[0]) if candidates else None
        if "WITH exact_approval AS MATERIALIZED" in query:
            command = database.commands.get(str(args[0]))
            effective_arguments = json.loads(str(args[7]))
            approval = next(
                (
                    item
                    for item in database.approvals
                    if item.get("approval_id") == str(args[1])
                    and item["tenant_id"] == args[2]
                    and item["user_id"] == args[3]
                    and item["session_id"] == args[4]
                    and item["run_id"] == args[5]
                    and item["tool_name"] == args[6]
                    and item["status"] == "approved"
                    and AssistantExecutionGateway._without_control_args(item["arguments"])
                    == effective_arguments
                ),
                None,
            )
            if not (
                command
                and approval
                and command["status"] == "awaiting_approval"
                and command["tenant_id"] == args[2]
                and command["user_id"] == args[3]
                and command["session_id"] == args[4]
                and command["run_id"] == args[5]
                and command["tool_name"] == args[6]
                and AssistantExecutionGateway._without_control_args(command["arguments"])
                == effective_arguments
            ):
                return None
            command.update(
                status="cancelled",
                error="APPROVAL_COMMAND_SUPERSEDED",
                lease_expires_at=None,
            )
            return {"command_id": command["command_id"]}
        if "FROM assistant_tool_approvals" in query:
            expected_arguments = json.loads(str(args[5]))
            for approval in reversed(database.approvals):
                if (
                    approval["tenant_id"] == args[0]
                    and approval["user_id"] == args[1]
                    and approval["session_id"] == args[2]
                    and approval["run_id"] == args[3]
                    and approval["tool_name"] == args[4]
                    and approval["arguments"] == expected_arguments
                ):
                    return dict(approval)
            return None
        if "SET status = 'side_effect_unknown'" in query:
            command = database.commands[str(args[0])]
            command.update(
                status="side_effect_unknown",
                error="SIDE_EFFECT_UNKNOWN",
                lease_expires_at=None,
            )
            return {"command_id": command["command_id"]}
        if "SET status = 'failed'" in query:
            command = database.commands[str(args[0])]
            command.update(
                status="failed",
                error="COMMAND_EXPIRED_BEFORE_DISPATCH",
                lease_expires_at=None,
            )
            return {"command_id": command["command_id"]}
        if "AND status = $3" in query and "UPDATE assistant_command_queue" in query:
            command = database.commands[str(args[0])]
            if command["status"] != str(args[2]):
                return None
            command.update(status=str(args[1]), lease_expires_at=None)
            return {"command_id": command["command_id"]}
        if "INSERT INTO assistant_command_queue" in query:
            command = {
                "command_id": str(args[0]),
                "tenant_id": str(args[1]),
                "user_id": str(args[2]),
                "session_id": str(args[3]),
                "run_id": str(args[4]),
                "command_key": str(args[5]),
                "tool_name": str(args[6]),
                "arguments": json.loads(str(args[7])),
                "status": str(args[8]),
                "result": None,
                "error": None,
                "steer_payload": json.loads(str(args[12])),
                "lease_expires_at": args[13],
                "created_at": datetime.now(timezone.utc),
            }
            database.commands[command["command_id"]] = command
            return {"command_id": command["command_id"]}
        return None


class _SharedCommandPool:
    def __init__(self, database: _SharedCommandDatabase) -> None:
        self.connection = _SharedCommandConnection(database)

    def acquire(self) -> _AsyncContext:
        return _AsyncContext(self.connection)


class _SharedCommandDatabase:
    def __init__(self) -> None:
        self.run = {
            "run_id": RUN_ID,
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "session_id": "session-a",
            "status": "running",
            "error": None,
        }
        self.commands: dict[str, dict[str, Any]] = {}
        self.checkpoints: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []
        self.hard_checkpoint = False
        self.inject_hard_before_authorize = False
        self.drop_ack_after_commit_once = False
        self.drop_reconcile_after_commit_once = False
        self.ack_query = ""
        self.command_select_query = ""
        self._pool = _SharedCommandPool(self)

    @staticmethod
    def _json_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except Exception:
                return {}
            return dict(decoded) if isinstance(decoded, dict) else {}
        return {}

    def find_completion_checkpoint(
        self,
        *,
        command_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        checkpoint_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        for checkpoint in reversed(self.checkpoints):
            keys = self._json_dict(checkpoint.get("idempotency_keys"))
            receipt = self._json_dict(checkpoint.get("resume_payload"))
            durable_receipt = self._json_dict(receipt.get("_checkpoint_receipt"))
            if (
                (checkpoint_id is None or str(checkpoint.get("checkpoint_id")) == checkpoint_id)
                and (run_id is None or str(checkpoint.get("run_id")) == run_id)
                and checkpoint.get("tenant_id") == tenant_id
                and checkpoint.get("user_id") == user_id
                and checkpoint.get("session_id") == session_id
                and checkpoint.get("phase") == "tool_call_completed"
                and str(keys.get("command_id") or "") == command_id
                and keys.get("command_result_acknowledgeable") is True
                and durable_receipt.get("committed") is True
                and durable_receipt.get("durability") == "database"
            ):
                return checkpoint
        return None

    def completion_matches_command(
        self,
        checkpoint: dict[str, Any] | None,
        command: dict[str, Any] | None,
    ) -> bool:
        pending_tool = self._json_dict((checkpoint or {}).get("pending_tool"))
        steer_payload = self._json_dict((command or {}).get("steer_payload"))
        return bool(
            checkpoint
            and command
            and command.get("tool_name") == pending_tool.get("tool_name")
            and steer_payload.get("_arguments_hash")
            and steer_payload.get("_arguments_hash") == pending_tool.get("arguments_hash")
            and command.get("status") in {"result_recorded_succeeded", "result_recorded_failed"}
            and steer_payload.get("_result_receipt_recorded") is True
        )

    def settle_command(
        self,
        command: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> None:
        steer_payload = self._json_dict(command.get("steer_payload"))
        resume_payload = self._json_dict(checkpoint.get("resume_payload"))
        command["status"] = (
            "succeeded" if steer_payload.get("_result_success") is True else "failed"
        )
        command["lease_expires_at"] = None
        command["steer_payload"] = {
            **steer_payload,
            "_result_receipt_complete": True,
            "_result_artifact_ids": list(resume_payload.get("output_artifact_ids") or []),
            "_result_acknowledged_checkpoint_id": str(checkpoint["checkpoint_id"]),
        }

    def add_completion_checkpoint(
        self,
        *,
        command_id: str,
        run_id: str,
        checkpoint_id: str,
        acknowledgeable: bool = True,
        artifact_ids: list[str] | None = None,
    ) -> None:
        command = self.commands[command_id]
        self.checkpoints.append(
            {
                "checkpoint_id": checkpoint_id,
                "run_id": run_id,
                "tenant_id": command["tenant_id"],
                "user_id": command["user_id"],
                "session_id": command["session_id"],
                "phase": "tool_call_completed",
                "pending_tool": {
                    "tool_name": command["tool_name"],
                    "arguments_hash": command["steer_payload"]["_arguments_hash"],
                },
                "idempotency_keys": {
                    "command_id": command_id,
                    "command_result_acknowledgeable": acknowledgeable,
                },
                "resume_payload": {
                    "output_artifact_ids": list(artifact_ids or []),
                    "_checkpoint_receipt": {
                        "committed": True,
                        "durability": "database",
                    },
                },
            }
        )

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "claimed_approval AS" in query:
            effective_arguments = json.loads(str(args[6])) if len(args) > 6 else None
            approval = next(
                (
                    item
                    for item in self.approvals
                    if item.get("approval_id") == str(args[0])
                    and item["tenant_id"] == args[1]
                    and item["user_id"] == args[2]
                    and item["session_id"] == args[3]
                    and item["run_id"] == args[4]
                    and item["tool_name"] == args[5]
                    and item["status"] == "approved"
                    and (
                        effective_arguments is None
                        or AssistantExecutionGateway._without_control_args(item["arguments"])
                        == effective_arguments
                    )
                ),
                None,
            )
            if not approval:
                return None
            approval["status"] = "consumed"
            return {"approval_id": approval["approval_id"], "retired_command_count": 0}
        if "RETURNING command.command_id, command.status" in query:
            self.ack_query = query
            command = self.commands.get(str(args[0]))
            checkpoint = self.find_completion_checkpoint(
                command_id=str(args[0]),
                checkpoint_id=str(args[1]),
                run_id=str(args[2]),
                tenant_id=str(args[3]),
                user_id=str(args[4]),
                session_id=str(args[5]),
            )
            if not self.completion_matches_command(checkpoint, command):
                return None
            self.settle_command(command, checkpoint)
            if self.drop_ack_after_commit_once:
                self.drop_ack_after_commit_once = False
                raise ConnectionError("ack lost after completion commit")
            return {"command_id": command["command_id"], "status": command["status"]}
        if "UPDATE assistant_command_queue AS queued" in query:
            if self.inject_hard_before_authorize:
                self.hard_checkpoint = True
            command = self.commands.get(str(args[0]))
            if (
                not command
                or command["status"] not in {"queued", "awaiting_approval"}
                or self.run["status"] != "running"
                or self.hard_checkpoint
            ):
                return None
            command["status"] = "running"
            command["lease_expires_at"] = args[1]
            return {"command_id": command["command_id"]}
        return None

    async def execute(self, query: str, *args: Any) -> str:
        if "UPDATE assistant_command_queue" in query:
            command = self.commands.get(str(args[0]))
            if not command:
                return "UPDATE 0"
            command.update(
                status=str(args[1]),
                result=args[2],
                error=args[3],
                lease_expires_at=args[4],
            )
            if len(args) > 5:
                command["steer_payload"].update(json.loads(str(args[5])))
            return "UPDATE 1"
        return "OK"

    def add_command(
        self,
        *,
        command_id: str,
        status: str,
        lease_expires_at: datetime | None,
        arguments: dict[str, Any] | None = None,
        legacy_key: bool = False,
    ) -> None:
        context = _context()
        resolved_arguments = arguments or {"value": "x"}
        self.commands[command_id] = {
            "command_id": command_id,
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "session_id": context.session_id,
            "run_id": context.run_id,
            "command_key": (
                AssistantExecutionGateway._build_legacy_command_key
                if legacy_key
                else AssistantExecutionGateway._build_command_key
            )(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                session_id=context.session_id,
                tool_name="external_write",
                arguments=resolved_arguments,
            ),
            "tool_name": "external_write",
            "arguments": resolved_arguments,
            "status": status,
            "result": None,
            "error": None,
            "steer_payload": {
                "_execution_intent_id": AssistantExecutionGateway._execution_intent_id(context),
                "_arguments_hash": AssistantExecutionGateway._hash_value(
                    AssistantExecutionGateway._without_control_args(resolved_arguments)
                ),
            },
            "lease_expires_at": lease_expires_at,
            "created_at": datetime.now(timezone.utc),
        }


class _FailingDatabase:
    async def fetchrow(self, _query: str, *_args: Any) -> dict[str, Any] | None:
        raise RuntimeError("database unavailable")

    async def fetch(self, _query: str, *_args: Any) -> list[dict[str, Any]]:
        raise RuntimeError("database unavailable")


class _ResultReceiptCommitThenAckLossDatabase(_SharedCommandDatabase):
    def __init__(self) -> None:
        super().__init__()
        self.drop_result_receipt_ack_once = True

    async def execute(self, query: str, *args: Any) -> str:
        if (
            "UPDATE assistant_command_queue" in query
            and len(args) > 1
            and str(args[1]).startswith("result_recorded_")
            and self.drop_result_receipt_ack_once
        ):
            self.drop_result_receipt_ack_once = False
            await super().execute(query, *args)
            raise ConnectionError("ack lost after result receipt commit")
        return await super().execute(query, *args)


class _ConfirmingDatabase:
    def __init__(self) -> None:
        self.fail_approval_update = False
        self.approval_update_queries: list[str] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "INSERT INTO assistant_run_checkpoints" in query:
            return {"checkpoint_id": args[0]}
        if "INSERT INTO assistant_tool_approvals" in query:
            return {"approval_id": args[0]}
        if "UPDATE assistant_tool_approvals" in query:
            self.approval_update_queries.append(query)
            if self.fail_approval_update:
                raise RuntimeError("approval update unavailable")
            return {
                "approval_id": args[0],
                "tenant_id": args[1],
                "user_id": args[2],
                "session_id": "session-a",
                "run_id": RUN_ID,
                "tool_name": "confirmation_tool",
                "arguments": {"value": "x"},
                "status": args[3],
                "reason": args[6],
                "approved_by": args[4],
                "approved_at": args[5],
                "expires_at": None,
                "created_at": None,
            }
        return None


class _DurableResumeDatabase:
    """Small shared durable-store double for cross-instance crash recovery."""

    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.run = {
            "run_id": RUN_ID,
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "session_id": "session-a",
            "status": "running",
            "engine": "agent_loop",
            "execution_profile": "safe",
            "memory_mode": "auto",
            "os_agent_enabled": False,
            "queue_mode": "collect",
            "runtime_mode": "compat",
            "agent_id": None,
            "agent_version_id": None,
            "agent_draft_revision": None,
            "publication_id": None,
            "channel": None,
            "runtime_fingerprint": None,
            "agent_spec_hash": None,
            "request_preview": "redacted",
            "usage": {},
            "error": None,
            "started_at": now,
            "finished_at": None,
        }
        self.approval = {
            "approval_id": "22222222-2222-4222-8222-222222222222",
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "session_id": "session-a",
            "run_id": RUN_ID,
            "tool_name": "confirmation_tool",
            "arguments": {"value": "x"},
            "status": "approved",
            "reason": "test",
            "expires_at": None,
        }
        self.checkpoints: list[dict[str, Any]] = []
        self.approval_claim_query = ""
        self.approval_resume_query = ""
        self.unsafe_resume_command = False

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "WITH eligible_checkpoint AS MATERIALIZED" in query:
            self.approval_resume_query = query
            if self.unsafe_resume_command:
                return None
            checkpoint_id = str(args[4])
            approval_id = str(args[5])
            arguments_hash = str(args[6])
            checkpoint = next(
                (
                    item
                    for item in reversed(self.checkpoints)
                    if str(item["checkpoint_id"]) == checkpoint_id
                ),
                None,
            )
            pending_tool = json.loads(str((checkpoint or {}).get("pending_tool") or "{}"))
            resume_payload = json.loads(str((checkpoint or {}).get("resume_payload") or "{}"))
            existing_marker = resume_payload.get("approval_resume") or {}
            existing_lease_raw = str(existing_marker.get("lease_expires_at") or "")
            try:
                existing_lease = datetime.fromisoformat(existing_lease_raw)
            except ValueError:
                existing_lease = None
            marker_reclaimable = bool(
                checkpoint
                and (
                    checkpoint["phase"] == "approval_pending"
                    or str(existing_marker.get("attempt_id") or "") == str(args[7])
                    or (
                        isinstance(existing_lease, datetime)
                        and existing_lease <= datetime.now(timezone.utc)
                    )
                )
            )
            if not (
                checkpoint
                and checkpoint is self.checkpoints[-1]
                and checkpoint["phase"] in {"approval_pending", "approval_resume_started"}
                and marker_reclaimable
                and str(checkpoint.get("approval_id") or "") == approval_id
                and str(pending_tool.get("arguments_hash") or "") == arguments_hash
                and self.approval["status"] == "approved"
                and self.run["status"] in {"running", "blocked"}
                and not self.run["error"]
            ):
                return None
            self.run.update(status="running", error=None, finished_at=None)
            checkpoint["phase"] = "approval_resume_started"
            checkpoint["status"] = "running"
            checkpoint["error"] = None
            resume_payload["approval_resume"] = {
                "state": "pre_dispatch",
                "checkpoint_id": checkpoint_id,
                "approval_id": approval_id,
                "attempt_id": str(args[7]),
                "lease_expires_at": args[15].isoformat(),
                "blind_replay_allowed": False,
            }
            checkpoint["resume_payload"] = json.dumps(resume_payload)
            return {"checkpoint_id": checkpoint_id, "phase": checkpoint["phase"]}
        if "INSERT INTO assistant_run_checkpoints" in query:
            row = {
                "checkpoint_id": args[0],
                "run_id": args[1],
                "tenant_id": args[2],
                "user_id": args[3],
                "session_id": args[4],
                "phase": args[5],
                "iteration": args[6],
                "message_state_hash": args[7],
                "pending_tool": args[8],
                "approval_id": args[9],
                "idempotency_keys": args[10],
                "resume_payload": args[11],
                "status": args[12],
                "error": args[13],
                "agent_id": args[14],
                "agent_version_id": args[15],
                "agent_draft_revision": args[16],
                "publication_id": args[17],
                "channel": args[18],
                "runtime_fingerprint": args[19],
                "agent_spec_hash": args[20],
                "created_at": args[21],
            }
            self.checkpoints.append(row)
            return {"checkpoint_id": args[0]}
        if "UPDATE assistant_tool_approvals" in query and "status = 'consumed'" in query:
            self.approval_claim_query = query
            if (
                self.approval["approval_id"] == args[0]
                and self.approval["tenant_id"] == args[1]
                and self.approval["user_id"] == args[2]
                and self.approval["session_id"] == args[3]
                and self.approval["run_id"] == args[4]
                and self.approval["tool_name"] == args[5]
                and self.approval["status"] == "approved"
                and (len(args) == 6 or self.approval["arguments"] == json.loads(str(args[6])))
            ):
                self.approval["status"] = "consumed"
                return {"approval_id": args[0]}
            return None
        if "FROM assistant_runs" in query:
            if tuple(args[:3]) == (RUN_ID, "tenant-a", "user-a"):
                return dict(self.run)
            return None
        if "FROM assistant_run_checkpoints" in query:
            return dict(self.checkpoints[-1]) if self.checkpoints else None
        if "FROM assistant_tool_approvals" in query:
            if tuple(args[:3]) == (
                self.approval["approval_id"],
                "tenant-a",
                "user-a",
            ):
                return dict(self.approval)
            return None
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM assistant_run_checkpoints" not in query:
            return []
        limit = int(args[3])
        return [dict(row) for row in reversed(self.checkpoints[-limit:])]


class _FlappingRunReadDatabase(_DurableResumeDatabase):
    """Fail the eligibility read while later recovery reads remain available."""

    def __init__(self) -> None:
        super().__init__()
        self.run_read_failures = 1
        self.checkpoint_reads = 0
        self.approval_reads = 0

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM assistant_runs" in query and self.run_read_failures:
            self.run_read_failures -= 1
            raise RuntimeError("transient run read unavailable")
        if "FROM assistant_run_checkpoints" in query:
            self.checkpoint_reads += 1
        if "FROM assistant_tool_approvals" in query:
            self.approval_reads += 1
        return await super().fetchrow(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM assistant_run_checkpoints" in query:
            self.checkpoint_reads += 1
        return await super().fetch(query, *args)


def _context(
    *,
    run_id: str = RUN_ID,
    request_id: str = "request-a",
) -> ToolInvocationContext:
    return ToolInvocationContext(
        session_id="session-a",
        user_id="user-a",
        tenant_id="tenant-a",
        request_id=request_id,
        run_id=run_id,
        policy_profile="power",
    )


async def _start_run(gateway: AssistantExecutionGateway) -> None:
    context = _context()
    await gateway.start_run(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        engine="agent_loop",
        execution_profile="safe",
        memory_mode="auto",
        os_agent_enabled=False,
        request_preview="redacted",
    )


@pytest.mark.asyncio
async def test_checkpoint_digest_binds_same_length_content_and_discloses_saved_scope() -> None:
    gateway = AssistantExecutionGateway(tool_invoker=_CountingInvoker(), database=None)
    context = _context()

    first = await gateway.save_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        phase="tool_call_pending",
        messages=[{"role": "user", "content": "approve A"}],
    )
    second = await gateway.save_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        phase="tool_call_pending",
        messages=[{"role": "user", "content": "approve B"}],
    )

    assert first["message_state_hash"] != second["message_state_hash"]
    receipt = first["checkpoint_receipt"]
    assert receipt["committed"] is True
    assert receipt["durability"] == "process"
    assert receipt["message_state"] == {
        "storage": "digest_only",
        "content_saved": False,
        "input_message_count": 1,
        "digested_message_count": 1,
        "window": "last_50",
        "digest_algorithm": "sha256_canonical_json",
        "restorable_from_checkpoint": False,
    }


@pytest.mark.asyncio
async def test_database_checkpoint_failure_returns_no_receipt_or_mirror() -> None:
    gateway = AssistantExecutionGateway(
        tool_invoker=_CountingInvoker(),
        database=_FailingDatabase(),
    )
    context = _context()

    with pytest.raises(RuntimeError, match="checkpoint was not persisted"):
        await gateway.save_run_checkpoint(
            run_id=context.run_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=context.session_id,
            phase="tool_call_pending",
            messages=[{"role": "user", "content": "do not duplicate"}],
        )

    assert gateway._checkpoints == {}


@pytest.mark.asyncio
async def test_database_checkpoint_receipt_is_returned_only_after_insert_confirmation() -> None:
    gateway = AssistantExecutionGateway(
        tool_invoker=_CountingInvoker(),
        database=_ConfirmingDatabase(),
    )
    context = _context()

    checkpoint = await gateway.save_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        phase="tool_call_pending",
    )

    assert checkpoint["checkpoint_receipt"]["committed"] is True
    assert checkpoint["checkpoint_receipt"]["durability"] == "database"
    assert gateway._checkpoints[context.run_id][0].checkpoint_id == checkpoint["checkpoint_id"]


@pytest.mark.asyncio
async def test_checkpoint_persistence_redacts_and_bounds_flattened_exception_chain() -> None:
    database = _DurableResumeDatabase()
    gateway = AssistantExecutionGateway(
        tool_invoker=_CountingInvoker(),
        database=database,
    )
    sentinel = "checkpoint-provider-sentinel-123456"
    flattened_chain = (
        f"ProviderError: password={sentinel}; caused by "
        f"DatabaseError: postgresql://service:{sentinel}@db.internal/runtime" + ("x" * 700)
    )

    checkpoint = await gateway.save_run_checkpoint(
        run_id=RUN_ID,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        phase="side_effect_unknown",
        pending_tool={
            "tool_id": f"Authorization: Bearer {sentinel}",
            "tool_name": f"provider password={sentinel}",
            "arguments": {
                f"Authorization: Bearer {sentinel}": "first",
                f"password={sentinel}": "second",
            },
        },
        resume_payload={
            "reason": flattened_chain,
            "nested": {"error": f"Authorization: Bearer {sentinel}"},
            "collision_map": {
                f"Authorization: Bearer {sentinel}": "first",
                f"password={sentinel}": "second",
            },
            "long_key_map": {"k" * 140: "bounded"},
        },
        status="blocked",
        error=flattened_chain,
    )
    readback = await gateway.get_run_checkpoint(
        run_id=RUN_ID,
        tenant_id="tenant-a",
        user_id="user-a",
    )

    stored = database.checkpoints[-1]
    stored_pending_tool = json.loads(str(stored["pending_tool"]))
    stored_payload = json.loads(str(stored["resume_payload"]))
    assert sentinel not in json.dumps(stored, default=str)
    assert sentinel not in json.dumps(checkpoint, default=str)
    assert sentinel not in json.dumps(readback, default=str)
    assert len(str(stored["error"])) <= 500
    assert len(str(stored_payload["reason"])) <= 500
    assert stored_pending_tool["tool_id"] == "[redacted]"
    assert stored_pending_tool["tool_name"] == "[redacted]"
    assert "arguments_hash" not in stored_pending_tool
    assert stored_payload["collision_map"] == {"_checkpoint_sanitization_collision": True}
    assert max(map(len, stored_payload["long_key_map"])) == 100
    assert "[redacted]" in str(stored["error"])
    assert "[redacted]" in str(stored_payload["nested"]["error"])
    assert readback is not None
    assert readback["pending_tool"]["tool_id"] == "[redacted]"
    assert readback["pending_tool"]["tool_name"] == "[redacted]"


@pytest.mark.asyncio
async def test_checkpoint_database_read_redacts_legacy_error_and_resume_reason() -> None:
    database = _DurableResumeDatabase()
    sentinel = "legacy-checkpoint-sentinel-987654"
    database.checkpoints.append(
        {
            "checkpoint_id": "33333333-3333-4333-8333-333333333333",
            "run_id": RUN_ID,
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "session_id": "session-a",
            "phase": "side_effect_unknown",
            "iteration": 1,
            "message_state_hash": "digest",
            "pending_tool": json.dumps(
                {
                    "tool_id": f"Bearer {sentinel}",
                    "tool_name": f"password={sentinel}",
                    "arguments_hash": "a" * 64,
                    "has_arguments": True,
                }
            ),
            "approval_id": None,
            "idempotency_keys": json.dumps({"operation_id": "legacy-operation"}),
            "resume_payload": json.dumps(
                {
                    "reason": f"provider api_key={sentinel}",
                    "nested": {"error": f"Bearer {sentinel}"},
                    "collision_map": {
                        f"Authorization: Bearer {sentinel}": "first",
                        f"password={sentinel}": "second",
                    },
                    "_checkpoint_receipt": {
                        "version": 1,
                        "committed": True,
                        "durability": "database",
                    },
                }
            ),
            "status": "blocked",
            "error": f"database redis://service:{sentinel}@cache.internal/0",
            "agent_id": None,
            "agent_version_id": None,
            "agent_draft_revision": None,
            "publication_id": None,
            "channel": None,
            "runtime_fingerprint": None,
            "agent_spec_hash": None,
            "created_at": datetime.now(timezone.utc),
        }
    )
    gateway = AssistantExecutionGateway(
        tool_invoker=_CountingInvoker(),
        database=database,
    )

    checkpoint = await gateway.get_run_checkpoint(
        run_id=RUN_ID,
        tenant_id="tenant-a",
        user_id="user-a",
    )
    recovery = await gateway.prepare_run_resume(
        run_id=RUN_ID,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
    )

    assert checkpoint is not None
    assert recovery is not None
    assert recovery["reason"] == "side_effect_state_unknown"
    assert sentinel not in json.dumps(checkpoint, default=str)
    assert sentinel not in json.dumps(recovery, default=str)
    assert len(str(checkpoint["error"])) <= 500
    assert len(str(checkpoint["resume_payload"]["reason"])) <= 500
    assert checkpoint["pending_tool"]["tool_id"] == "[redacted]"
    assert checkpoint["pending_tool"]["tool_name"] == "[redacted]"
    assert checkpoint["resume_payload"]["collision_map"] == {
        "_checkpoint_sanitization_collision": True
    }
    assert "[redacted]" in str(checkpoint["error"])
    assert "[redacted]" in str(checkpoint["resume_payload"]["nested"]["error"])


@pytest.mark.asyncio
async def test_database_approval_insert_failure_returns_no_phantom_approval() -> None:
    gateway = AssistantExecutionGateway(
        tool_invoker=_CountingInvoker(),
        database=_FailingDatabase(),
    )

    with pytest.raises(RuntimeError, match="approval was not persisted"):
        await gateway.request_tool_approval(
            context=_context(),
            tool_name="confirmation_tool",
            arguments={"value": "x"},
            reason="test",
        )

    assert gateway._approvals == {}


@pytest.mark.asyncio
async def test_database_approval_update_failure_does_not_mutate_mirror() -> None:
    database = _ConfirmingDatabase()
    gateway = AssistantExecutionGateway(
        tool_invoker=_CountingInvoker(),
        database=database,
    )
    context = _context()
    approval_id = await gateway.request_tool_approval(
        context=context,
        tool_name="confirmation_tool",
        arguments={"value": "x"},
        reason="test",
    )
    assert gateway._approvals[approval_id].status == "pending"

    database.fail_approval_update = True
    result = await gateway.approve(
        approval_id=approval_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        approved=True,
        approver_user_id=context.user_id,
    )

    assert result is None
    assert gateway._approvals[approval_id].status == "pending"
    assert gateway._approvals[approval_id].approved_at is None
    assert "current_checkpoint AS MATERIALIZED" in database.approval_update_queries[-1]
    assert "ORDER BY checkpoint.created_at DESC" in database.approval_update_queries[-1]
    assert "checkpoint.checkpoint_id DESC" in database.approval_update_queries[-1]
    assert "checkpoint.phase = 'approval_pending'" in database.approval_update_queries[-1]
    assert "checkpoint.resume_payload->>'attempt_id'" in database.approval_update_queries[-1]
    assert "checkpoint.pending_tool->>'arguments_hash'" in database.approval_update_queries[-1]
    assert "command.steer_payload->>'_arguments_hash'" in database.approval_update_queries[-1]
    assert "command.arguments" in database.approval_update_queries[-1]
    assert "- '_middleware_approval_required'" in database.approval_update_queries[-1]


@pytest.mark.asyncio
async def test_approval_requires_the_current_attempt_checkpoint() -> None:
    gateway = AssistantExecutionGateway(tool_invoker=_CountingInvoker(), database=None)
    context = _context()
    await _start_run(gateway)
    approval_id = await gateway.request_tool_approval(
        context=context,
        tool_name="confirmation_tool",
        arguments={"value": "current"},
        reason="test",
    )
    await gateway.save_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        phase="approval_pending",
        pending_tool={
            "tool_id": "tool-current",
            "tool_name": "confirmation_tool",
            "arguments": {"value": "current"},
        },
        approval_id=approval_id,
        resume_payload={"attempt_id": "attempt-current"},
        status="blocked",
    )

    approved = await gateway.approve(
        approval_id=approval_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        approved=True,
        approver_user_id=context.user_id,
    )

    assert approved is not None
    assert approved["status"] == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("newer_phase", "newer_arguments", "newer_attempt_id"),
    [
        ("model_completed", {"value": "current"}, "attempt-newer"),
        ("approval_pending", {"value": "different"}, "attempt-newer"),
        ("approval_pending", {"value": "current"}, None),
    ],
)
async def test_stale_approval_action_fails_closed_before_mutation(
    newer_phase: str,
    newer_arguments: dict[str, str],
    newer_attempt_id: str | None,
) -> None:
    gateway = AssistantExecutionGateway(tool_invoker=_CountingInvoker(), database=None)
    context = _context()
    await _start_run(gateway)
    approval_id = await gateway.request_tool_approval(
        context=context,
        tool_name="confirmation_tool",
        arguments={"value": "current"},
        reason="test",
    )
    await gateway.save_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        phase="approval_pending",
        pending_tool={
            "tool_id": "tool-original",
            "tool_name": "confirmation_tool",
            "arguments": {"value": "current"},
        },
        approval_id=approval_id,
        resume_payload={"attempt_id": "attempt-original"},
        status="blocked",
    )
    await gateway.save_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        phase=newer_phase,
        pending_tool={
            "tool_id": "tool-newer",
            "tool_name": "confirmation_tool",
            "arguments": newer_arguments,
        },
        approval_id=approval_id,
        resume_payload={"attempt_id": newer_attempt_id},
        status="blocked",
    )

    result = await gateway.approve(
        approval_id=approval_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        approved=True,
        approver_user_id=context.user_id,
    )

    assert result is None
    assert gateway._approvals[approval_id].status == "pending"
    assert gateway._approvals[approval_id].approved_at is None


@pytest.mark.asyncio
async def test_duplicate_resume_of_succeeded_run_is_read_only() -> None:
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    context = _context()
    await _start_run(gateway)
    approval_id = await gateway.request_tool_approval(
        context=context,
        tool_name="confirmation_tool",
        arguments={"value": "x"},
        reason="test",
    )
    await gateway.save_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        phase="approval_pending",
        pending_tool={
            "tool_id": "tool-1",
            "tool_name": "confirmation_tool",
            "arguments": {"value": "x"},
        },
        approval_id=approval_id,
        resume_payload={"attempt_id": "attempt-approval"},
        status="blocked",
    )
    await gateway.approve(
        approval_id=approval_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        approved=True,
        approver_user_id=context.user_id,
    )
    await gateway.consume_tool_approval(
        approval_id=approval_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        tool_name="confirmation_tool",
        session_id=context.session_id,
        run_id=context.run_id,
    )
    await gateway.finish_run(
        run_id=context.run_id,
        status="succeeded",
        usage={"output_tokens": 7},
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
    )
    before = await gateway.get_run(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
    )
    checkpoint_count = len(gateway._checkpoints[context.run_id])

    duplicate = await gateway.prepare_run_resume(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        approval_id=approval_id,
    )
    after = await gateway.get_run(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
    )

    assert duplicate is not None
    assert duplicate["status"] == "blocked"
    assert duplicate["reason"] == "run_already_terminal"
    assert before == after
    assert after is not None and after["status"] == "succeeded"
    assert len(gateway._checkpoints[context.run_id]) == checkpoint_count
    assert invoker.count == 0


@pytest.mark.asyncio
async def test_resume_run_read_failure_never_falls_back_to_stale_running_mirror() -> None:
    database = _FlappingRunReadDatabase()
    database.run["status"] = "succeeded"
    approval_id = str(database.approval["approval_id"])
    durable_gateway = AssistantExecutionGateway(
        tool_invoker=_CountingInvoker(),
        database=database,
    )
    await durable_gateway.save_run_checkpoint(
        run_id=RUN_ID,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        phase="approval_pending",
        pending_tool={
            "tool_id": "durable-tool-1",
            "tool_name": "confirmation_tool",
            "arguments": {"value": "x"},
        },
        approval_id=approval_id,
        status="blocked",
    )

    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    await _start_run(gateway)
    gateway.database = database

    resume = await gateway.prepare_run_resume(
        run_id=RUN_ID,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        approval_id=approval_id,
    )

    assert resume is not None
    assert resume["status"] == "blocked"
    assert resume["reason"] == "run_state_unavailable"
    assert resume["execution_authorized"] is False
    assert database.checkpoint_reads == 0
    assert database.approval_reads == 0
    assert invoker.count == 0


@pytest.mark.asyncio
async def test_side_effect_unknown_returns_non_executing_structured_recovery_plan() -> None:
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    context = _context()
    await _start_run(gateway)
    checkpoint = await gateway.save_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        phase="side_effect_unknown",
        pending_tool={
            "tool_id": "tool-1",
            "tool_name": "confirmation_tool",
            "arguments": {"value": "x"},
        },
        idempotency_keys={
            "operation_id": "operation-1",
            "idempotency_supported": True,
            "idempotency_key_present": True,
        },
        resume_payload={
            "operation_id": "operation-1",
            "read_back_available": True,
            "idempotency_supported": True,
            "compensation_available": True,
            "recovery_action": "resume",
        },
        status="blocked",
        error="SIDE_EFFECT_UNKNOWN",
    )

    recovery = await gateway.prepare_run_resume(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
    )
    run = await gateway.get_run(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
    )

    assert recovery is not None
    assert recovery["status"] == "blocked"
    assert recovery["reason"] == "side_effect_state_unknown"
    assert recovery["recoverable"] is True
    assert recovery["resume_mode"] == "side_effect_recovery_plan"
    assert recovery["execution_authorized"] is False
    plan = recovery["recovery_plan"]
    assert plan["automatic_execution"] is False
    assert plan["blind_replay_allowed"] is False
    assert plan["exactly_once_guaranteed"] is False
    actions = {action["kind"]: action for action in plan["actions"]}
    assert actions["read_back"]["available"] is True
    # A boolean "key present" marker cannot reconstruct the opaque key or
    # identify the component that owns it, so retry must remain unavailable.
    assert actions["idempotent_retry"]["available"] is False
    assert actions["idempotent_retry"]["precondition"] == "read_back_confirms_absent"
    assert actions["compensation"]["available"] is True
    assert actions["compensation"]["requires_explicit_approval"] is True
    assert actions["compensation"]["semantics"] == "compensation_not_rollback"
    assert actions["manual_pause"]["state"] == "active"
    assert run is not None and run["status"] == "running"
    assert run["checkpoint"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert invoker.count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    ["tool_call_pending", "model_turn_started", "provider_pause_turn"],
)
async def test_digest_only_checkpoint_never_authorizes_resume(phase: str) -> None:
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    context = _context()
    await _start_run(gateway)
    checkpoint = await gateway.save_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        phase=phase,
        messages=[{"role": "user", "content": "do not replay this turn"}],
        pending_tool={
            "tool_id": "tool-1",
            "tool_name": "confirmation_tool",
            "arguments": {"value": "x"},
        },
    )

    blocked = await gateway.prepare_run_resume(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
    )

    assert checkpoint["checkpoint_receipt"]["message_state"]["content_saved"] is False
    assert blocked is not None
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "checkpoint_not_restorable"
    assert blocked["recoverable"] is False
    assert blocked["execution_authorized"] is False
    assert invoker.count == 0


@pytest.mark.asyncio
async def test_consumed_approval_without_terminal_receipt_pauses_as_unknown() -> None:
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    context = _context()
    await _start_run(gateway)
    approval_id = await gateway.request_tool_approval(
        context=context,
        tool_name="confirmation_tool",
        arguments={"value": "x"},
        reason="test",
    )
    await gateway.save_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        phase="approval_pending",
        pending_tool={
            "tool_id": "tool-approval-1",
            "tool_name": "confirmation_tool",
            "arguments": {"value": "x"},
        },
        approval_id=approval_id,
        idempotency_keys={"operation_id": "operation-original"},
        resume_payload={
            "operation_id": "operation-original",
            "attempt_id": "attempt-approval",
        },
        status="blocked",
    )
    await gateway.approve(
        approval_id=approval_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        approved=True,
        approver_user_id=context.user_id,
    )
    await gateway.consume_tool_approval(
        approval_id=approval_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        tool_name="confirmation_tool",
        session_id=context.session_id,
        run_id=context.run_id,
    )

    recovery = await gateway.prepare_run_resume(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        approval_id=approval_id,
    )

    assert recovery is not None
    assert recovery["reason"] == "side_effect_state_unknown"
    assert recovery["execution_authorized"] is False
    assert recovery["approval_id"] == approval_id
    assert recovery["recovery_plan"]["operation_id"] == "operation-original"
    assert recovery["recovery_plan"]["blind_replay_allowed"] is False
    assert recovery["recovery_plan"]["exactly_once_guaranteed"] is False
    assert recovery["recovery_plan"]["actions"][-1]["kind"] == "manual_pause"
    assert invoker.count == 0


@pytest.mark.asyncio
async def test_new_gateway_recovers_durable_claim_crash_as_non_executing_unknown() -> None:
    database = _DurableResumeDatabase()
    first_invoker = _CountingInvoker()
    first_gateway = AssistantExecutionGateway(
        tool_invoker=first_invoker,
        database=database,
    )
    approval_id = str(database.approval["approval_id"])
    await first_gateway.save_run_checkpoint(
        run_id=RUN_ID,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        phase="approval_pending",
        pending_tool={
            "tool_id": "durable-tool-1",
            "tool_name": "confirmation_tool",
            "arguments": {"value": "x"},
        },
        approval_id=approval_id,
        idempotency_keys={"operation_id": "durable-operation-1"},
        resume_payload={"operation_id": "durable-operation-1"},
        status="blocked",
    )
    claimed = await first_gateway._claim_approval(
        approval_id=approval_id,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        run_id=RUN_ID,
        tool_name="confirmation_tool",
        arguments={"value": "x"},
    )
    assert claimed is True
    assert first_invoker.count == 0
    assert "command.arguments" in database.approval_claim_query
    assert "- '_middleware_approval_required'" in database.approval_claim_query

    # Simulate a process crash: the new gateway has no in-memory mirrors and
    # must decide solely from the shared durable rows.
    resumed_invoker = _CountingInvoker()
    resumed_gateway = AssistantExecutionGateway(
        tool_invoker=resumed_invoker,
        database=database,
    )
    recovery = await resumed_gateway.prepare_run_resume(
        run_id=RUN_ID,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        approval_id=approval_id,
    )

    assert recovery is not None
    assert recovery["reason"] == "side_effect_state_unknown"
    assert recovery["execution_authorized"] is False
    assert recovery["recovery_plan"]["operation_id"] == "durable-operation-1"
    assert recovery["recovery_plan"]["blind_replay_allowed"] is False
    assert database.approval["status"] == "consumed"
    assert resumed_invoker.count == 0


@pytest.mark.asyncio
async def test_approval_resume_marker_recovers_both_pre_dispatch_crash_windows() -> None:
    database = _DurableResumeDatabase()
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)
    approval_id = str(database.approval["approval_id"])
    checkpoint = await gateway.save_run_checkpoint(
        run_id=RUN_ID,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        phase="approval_pending",
        pending_tool={
            "tool_id": "resume-crash-window",
            "tool_name": "confirmation_tool",
            "arguments": {"value": "x"},
        },
        approval_id=approval_id,
        status="blocked",
    )

    # Window one: the approval checkpoint committed, but the pausing process
    # died before changing assistant_runs from running to blocked.
    assert database.run["status"] == "running"
    first_preflight = await gateway.prepare_run_resume(
        run_id=RUN_ID,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        approval_id=approval_id,
    )
    assert first_preflight is not None
    assert first_preflight["status"] == "ready"

    receipt = await gateway.start_approval_resume(
        run_id=RUN_ID,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        checkpoint_id=str(checkpoint["checkpoint_id"]),
        approval_id=approval_id,
        arguments_hash=str(checkpoint["pending_tool"]["arguments_hash"]),
        attempt_id="attempt-before-dispatch",
    )
    assert receipt["committed"] is True
    assert database.checkpoints[-1]["phase"] == "approval_resume_started"
    assert "FOR UPDATE OF checkpoint, runs" in database.approval_resume_query
    assert database.approval_resume_query.count("'{approval_resume,attempt_id}' = $8") == 2

    with pytest.raises(PermissionError, match="start fence was not eligible"):
        await gateway.start_approval_resume(
            run_id=RUN_ID,
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
            checkpoint_id=str(checkpoint["checkpoint_id"]),
            approval_id=approval_id,
            arguments_hash=str(checkpoint["pending_tool"]["arguments_hash"]),
            attempt_id="competing-live-attempt",
        )
    active_preflight = await gateway.prepare_run_resume(
        run_id=RUN_ID,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        approval_id=approval_id,
    )
    assert active_preflight is not None
    assert active_preflight["status"] == "blocked"
    assert active_preflight["reason"] == "approval_resume_in_progress"

    # Window two: the process died immediately after the atomic start marker,
    # before a tool_call_pending fence or approval claim existed. Once its
    # bounded claim lease expires, a new process may reclaim the exact marker.
    marker_payload = json.loads(database.checkpoints[-1]["resume_payload"])
    marker_payload["approval_resume"]["lease_expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    database.checkpoints[-1]["resume_payload"] = json.dumps(marker_payload)
    restarted = AssistantExecutionGateway(tool_invoker=invoker, database=database)
    restart_preflight = await restarted.prepare_run_resume(
        run_id=RUN_ID,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        approval_id=approval_id,
    )
    assert restart_preflight is not None
    assert restart_preflight["status"] == "ready"
    assert restart_preflight["checkpoint"]["phase"] == "approval_resume_started"
    assert database.run["status"] == "running"
    assert database.approval["status"] == "approved"
    assert invoker.count == 0


@pytest.mark.asyncio
async def test_approval_resume_start_cas_rejects_active_unsafe_command() -> None:
    database = _DurableResumeDatabase()
    gateway = AssistantExecutionGateway(tool_invoker=_CountingInvoker(), database=database)
    approval_id = str(database.approval["approval_id"])
    checkpoint = await gateway.save_run_checkpoint(
        run_id=RUN_ID,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        phase="approval_pending",
        pending_tool={
            "tool_id": "resume-active-command",
            "tool_name": "confirmation_tool",
            "arguments": {"value": "x"},
        },
        approval_id=approval_id,
        status="blocked",
    )
    database.unsafe_resume_command = True

    with pytest.raises(PermissionError, match="start fence was not eligible"):
        await gateway.start_approval_resume(
            run_id=RUN_ID,
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
            checkpoint_id=str(checkpoint["checkpoint_id"]),
            approval_id=approval_id,
            arguments_hash=str(checkpoint["pending_tool"]["arguments_hash"]),
            attempt_id="attempt-must-not-start",
        )

    assert database.checkpoints[-1]["phase"] == "approval_pending"
    assert database.approval["status"] == "approved"


@pytest.mark.asyncio
async def test_unknown_durable_result_is_never_replayable_after_gateway_restart() -> None:
    database = _SharedCommandDatabase()
    invoker = _UnknownResultInvoker()
    first_gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)

    first = await first_gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(),
    )

    assert first.error == "SIDE_EFFECT_UNKNOWN"
    assert invoker.count == 1
    assert [item["status"] for item in database.commands.values()] == ["side_effect_unknown"]
    assert all(item["status"] != "failed" for item in database.commands.values())

    restarted_gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)
    restarted = await restarted_gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(),
    )

    assert invoker.count == 1
    assert restarted.error == "SIDE_EFFECT_UNKNOWN"
    assert restarted.metadata["queue_state"] == "side_effect_unknown"
    assert restarted.metadata["execution_authorized"] is False
    assert restarted.metadata["blind_replay_allowed"] is False


@pytest.mark.asyncio
async def test_process_unknown_result_and_control_variant_are_never_replayed() -> None:
    invoker = _UnknownResultInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)

    first = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(request_id="process-unknown-first"),
    )
    retried = await gateway.invoke_tool(
        "external_write",
        {"value": "x", "_steer_payload": {"source": "retry"}},
        _context(request_id="process-unknown-retry"),
    )

    assert first.error == "SIDE_EFFECT_UNKNOWN"
    assert retried.error == "SIDE_EFFECT_UNKNOWN"
    assert retried.metadata["execution_authorized"] is False
    assert retried.metadata["blind_replay_allowed"] is False
    assert invoker.count == 1
    assert len(gateway._commands) == 1


@pytest.mark.asyncio
async def test_expired_queued_command_is_safely_reclaimed_before_dispatch() -> None:
    database = _SharedCommandDatabase()
    database.add_command(
        command_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        status="queued",
        lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)

    result = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(),
    )

    assert result.success is True
    assert invoker.count == 1
    assert database.commands["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]["status"] == "failed"
    assert len(database.commands) == 2
    assert (
        sum(item["status"] == "result_recorded_succeeded" for item in database.commands.values())
        == 1
    )


@pytest.mark.asyncio
async def test_expired_running_command_becomes_manual_unknown_and_never_replays() -> None:
    database = _SharedCommandDatabase()
    database.add_command(
        command_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        status="running",
        lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)

    result = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(),
    )

    assert invoker.count == 0
    assert result.error == "SIDE_EFFECT_UNKNOWN"
    assert result.metadata["execution_authorized"] is False
    assert database.commands["bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"]["status"] == (
        "side_effect_unknown"
    )


@pytest.mark.asyncio
async def test_awaiting_approval_dedupes_only_while_exact_approval_is_actionable() -> None:
    database = _SharedCommandDatabase()
    database.add_command(
        command_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        status="awaiting_approval",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    database.approvals.append(
        {
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "session_id": "session-a",
            "run_id": RUN_ID,
            "tool_name": "external_write",
            "arguments": {"value": "different"},
            "status": "approved",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        }
    )
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)

    result = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(),
    )

    assert result.success is True
    assert invoker.count == 1
    assert database.commands["cccccccc-cccc-4ccc-8ccc-cccccccccccc"]["status"] == "failed"


@pytest.mark.asyncio
async def test_result_stays_sticky_until_durable_completion_ack() -> None:
    database = _SharedCommandDatabase()
    invoker = _CountingInvoker()
    first_gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)

    first = await first_gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(),
    )
    persisted = list(database.commands.values())

    restarted_gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)
    recovered = await restarted_gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(),
    )

    assert first.success is True
    assert first.metadata["result_receipt_recorded"] is True
    assert first.metadata["finalization_acknowledged"] is False
    assert first.metadata["result_acknowledgement_required"] is True
    assert len(persisted) == 1
    assert persisted[0]["status"] == "result_recorded_succeeded"
    assert recovered.success is True
    assert recovered.result == {"ok": True}
    assert recovered.metadata["result_receipt_recovered"] is True
    assert recovered.metadata["result_acknowledgement_required"] is True
    assert recovered.metadata["execution_authorized"] is False
    assert recovered.metadata["blind_replay_allowed"] is False
    assert invoker.count == 1
    assert len(database.commands) == 1


@pytest.mark.parametrize(
    "control_arguments",
    [
        {"_steer_payload": {"source": "retry"}},
        {"_approval_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        {"_middleware_approval_required": True},
    ],
)
@pytest.mark.asyncio
async def test_control_only_argument_variants_cannot_bypass_sticky_fence(
    control_arguments: dict[str, Any],
) -> None:
    database = _SharedCommandDatabase()
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)

    first = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(request_id="control-base"),
    )
    retried = await gateway.invoke_tool(
        "external_write",
        {"value": "x", **control_arguments},
        _context(request_id="control-retry"),
    )

    assert first.success is True
    assert retried.success is True
    assert retried.metadata["result_receipt_recovered"] is True
    assert retried.metadata["execution_authorized"] is False
    assert invoker.count == 1
    assert len(database.commands) == 1
    assert gateway._build_command_key(
        "tenant-a",
        "user-a",
        "session-a",
        "external_write",
        {"value": "x"},
    ) == gateway._build_command_key(
        "tenant-a",
        "user-a",
        "session-a",
        "external_write",
        {"value": "x", **control_arguments},
    )


@pytest.mark.asyncio
async def test_legacy_raw_control_key_row_is_protected_by_effective_argument_fallback() -> None:
    database = _SharedCommandDatabase()
    legacy_arguments = {"value": "x", "_steer_payload": {"source": "old-version"}}
    database.add_command(
        command_id="abababab-abab-4bab-8bab-abababababab",
        status="result_recorded_succeeded",
        lease_expires_at=None,
        arguments=legacy_arguments,
        legacy_key=True,
    )
    command = database.commands["abababab-abab-4bab-8bab-abababababab"]
    command["result"] = json.dumps({"ok": True})
    command["steer_payload"].update(
        {
            "_result_receipt_recorded": True,
            "_result_success": True,
            "_result_output_file_count": 0,
            "_result_receipt_complete": True,
        }
    )
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)

    recovered = await gateway.invoke_tool(
        "external_write",
        {"value": "x", "_steer_payload": {"source": "new-version"}},
        _context(request_id="legacy-fallback"),
    )

    assert recovered.success is True
    assert recovered.metadata["result_receipt_recovered"] is True
    assert invoker.count == 0
    assert len(database.commands) == 1
    assert "arguments" in database.command_select_query
    assert "- '_approval_id'" in database.command_select_query


@pytest.mark.asyncio
async def test_legacy_unknown_row_preempts_newer_exact_approved_awaiting_row() -> None:
    database = _SharedCommandDatabase()
    unknown_command_id = "a1a1a1a1-a1a1-41a1-81a1-a1a1a1a1a1a1"
    awaiting_command_id = "a2a2a2a2-a2a2-42a2-82a2-a2a2a2a2a2a2"
    approval_id = "a3a3a3a3-a3a3-43a3-83a3-a3a3a3a3a3a3"
    database.add_command(
        command_id=unknown_command_id,
        status="side_effect_unknown",
        lease_expires_at=None,
        arguments={"value": "x", "_steer_payload": {"writer": "legacy-a"}},
        legacy_key=True,
    )
    database.add_command(
        command_id=awaiting_command_id,
        status="awaiting_approval",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        arguments={"value": "x", "_steer_payload": {"writer": "legacy-b"}},
        legacy_key=True,
    )
    database.approvals.append(
        {
            "approval_id": approval_id,
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "session_id": "session-a",
            "run_id": RUN_ID,
            "tool_name": "external_write",
            "arguments": {"value": "x"},
            "status": "approved",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        }
    )
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)

    held = await gateway.invoke_tool(
        "external_write",
        {
            "value": "x",
            "_approval_id": approval_id,
            "_middleware_approval_required": True,
            "_steer_payload": {"writer": "current"},
        },
        _context(request_id="legacy-unknown-wins"),
    )

    assert held.error == "SIDE_EFFECT_UNKNOWN"
    assert held.metadata["execution_authorized"] is False
    assert held.metadata["blind_replay_allowed"] is False
    assert invoker.count == 0
    assert database.commands[unknown_command_id]["status"] == "side_effect_unknown"
    assert database.commands[awaiting_command_id]["status"] == "awaiting_approval"
    assert len(database.commands) == 2
    assert "WHEN status IN" in database.command_select_query


@pytest.mark.asyncio
async def test_exact_approved_resume_atomically_supersedes_awaiting_command() -> None:
    database = _SharedCommandDatabase()
    approval_id = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd"
    original_command_id = "bcbcbcbc-bcbc-4cbc-8cbc-bcbcbcbcbcbc"
    database.add_command(
        command_id=original_command_id,
        status="awaiting_approval",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    database.approvals.append(
        {
            "approval_id": approval_id,
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "session_id": "session-a",
            "run_id": RUN_ID,
            "tool_name": "external_write",
            "arguments": {"value": "x"},
            "status": "approved",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        }
    )
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)

    resumed = await gateway.invoke_tool(
        "external_write",
        {
            "value": "x",
            "_approval_id": approval_id,
            "_middleware_approval_required": True,
        },
        _context(request_id="approved-resume"),
    )

    assert resumed.success is True
    assert invoker.count == 1
    assert database.approvals[0]["status"] == "consumed"
    assert database.commands[original_command_id]["status"] == "cancelled"
    assert database.commands[original_command_id]["error"] == "APPROVAL_COMMAND_SUPERSEDED"
    assert len(database.commands) == 2
    new_command = next(
        item for key, item in database.commands.items() if key != original_command_id
    )
    assert new_command["status"] == "result_recorded_succeeded"


@pytest.mark.parametrize(
    "supplied_approval_id",
    [None, "dededede-dede-4ede-8ede-dededededede"],
)
@pytest.mark.asyncio
async def test_missing_or_wrong_approval_id_keeps_awaiting_command_deduped(
    supplied_approval_id: str | None,
) -> None:
    database = _SharedCommandDatabase()
    original_command_id = "efefefef-efef-4fef-8fef-efefefefefef"
    database.add_command(
        command_id=original_command_id,
        status="awaiting_approval",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    database.approvals.append(
        {
            "approval_id": "f0f0f0f0-f0f0-40f0-80f0-f0f0f0f0f0f0",
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "session_id": "session-a",
            "run_id": RUN_ID,
            "tool_name": "external_write",
            "arguments": {"value": "x"},
            "status": "approved",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        }
    )
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)
    arguments: dict[str, Any] = {
        "value": "x",
        "_middleware_approval_required": True,
    }
    if supplied_approval_id:
        arguments["_approval_id"] = supplied_approval_id

    held = await gateway.invoke_tool(
        "external_write",
        arguments,
        _context(request_id="invalid-approval-resume"),
    )

    assert held.error == "COMMAND_DEDUPED"
    assert invoker.count == 0
    assert len(database.commands) == 1
    assert database.commands[original_command_id]["status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_result_receipt_ack_loss_fences_every_new_intent_until_acknowledged() -> None:
    database = _ResultReceiptCommitThenAckLossDatabase()
    invoker = _CountingInvoker()
    first_gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)

    first = await first_gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(),
    )
    assert first.error == "SIDE_EFFECT_UNKNOWN"
    assert list(database.commands.values())[0]["status"] == "result_recorded_succeeded"

    fresh_context = _context()
    fresh_context.request_id = "fresh-request-after-unknown"
    restarted_gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)
    recovered = await restarted_gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        fresh_context,
    )

    assert recovered.success is True
    assert recovered.result == {"ok": True}
    assert recovered.metadata["result_receipt_recovered"] is True
    assert recovered.metadata["result_acknowledgement_required"] is True
    assert recovered.metadata["execution_authorized"] is False
    assert invoker.count == 1
    assert list(database.commands.values())[0]["status"] == "result_recorded_succeeded"

    legitimate_new_intent = _context()
    legitimate_new_intent.request_id = "legitimate-new-intent"
    held_result = await restarted_gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        legitimate_new_intent,
    )

    assert held_result.success is True
    assert held_result.metadata["result_receipt_recovered"] is True
    assert held_result.metadata["result_acknowledgement_required"] is True
    assert invoker.count == 1
    assert len(database.commands) == 1


@pytest.mark.asyncio
async def test_fresh_run_completion_ack_releases_sticky_result_for_third_intent() -> None:
    database = _SharedCommandDatabase()
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)

    first = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(request_id="original-intent"),
    )
    command_id = str(first.metadata["command_id"])
    assert database.commands[command_id]["status"] == "result_recorded_succeeded"

    database.run["run_id"] = RUN_ID_2
    recovered = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(run_id=RUN_ID_2, request_id="fresh-recovery-intent"),
    )
    assert recovered.success is True
    assert recovered.metadata["result_acknowledgement_required"] is True
    assert invoker.count == 1

    checkpoint_id = "44444444-4444-4444-8444-444444444444"
    database.add_completion_checkpoint(
        command_id=command_id,
        run_id=RUN_ID_2,
        checkpoint_id=checkpoint_id,
    )
    ack = await gateway.acknowledge_command_result(
        command_id=command_id,
        checkpoint_id=checkpoint_id,
        run_id=RUN_ID_2,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
    )
    assert ack["committed"] is True
    assert database.commands[command_id]["status"] == "succeeded"
    assert "command.run_id" not in database.ack_query
    assert "completion.pending_tool->>'tool_name'" in database.ack_query
    assert "completion.pending_tool->>'arguments_hash'" in database.ack_query

    database.run["run_id"] = RUN_ID_3
    third = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(run_id=RUN_ID_3, request_id="legitimate-third-intent"),
    )
    assert third.success is True
    assert invoker.count == 2
    assert len(database.commands) == 2
    assert (
        sum(item["status"] == "result_recorded_succeeded" for item in database.commands.values())
        == 1
    )


@pytest.mark.asyncio
async def test_completion_ack_commit_then_ack_loss_never_replays_exact_intent() -> None:
    database = _SharedCommandDatabase()
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)

    first = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(request_id="ack-loss-intent"),
    )
    command_id = str(first.metadata["command_id"])
    checkpoint_id = "55555555-5555-4555-8555-555555555555"
    database.add_completion_checkpoint(
        command_id=command_id,
        run_id=RUN_ID_2,
        checkpoint_id=checkpoint_id,
    )
    database.drop_ack_after_commit_once = True

    ack = await gateway.acknowledge_command_result(
        command_id=command_id,
        checkpoint_id=checkpoint_id,
        run_id=RUN_ID_2,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
    )
    assert ack["committed"] is False
    assert database.commands[command_id]["status"] == "succeeded"

    database.run["run_id"] = RUN_ID_3
    exact = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(run_id=RUN_ID_3, request_id="ack-loss-intent"),
    )
    assert exact.success is True
    assert exact.metadata["result_receipt_recovered"] is True
    assert invoker.count == 1
    assert len(database.commands) == 1


@pytest.mark.asyncio
async def test_completion_reconcile_commit_then_ack_loss_never_replays() -> None:
    database = _SharedCommandDatabase()
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)

    first = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(request_id="reconcile-original"),
    )
    command_id = str(first.metadata["command_id"])
    database.add_completion_checkpoint(
        command_id=command_id,
        run_id=RUN_ID_2,
        checkpoint_id="66666666-6666-4666-8666-666666666666",
    )
    database.run["run_id"] = RUN_ID_3
    database.drop_reconcile_after_commit_once = True

    uncertain = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(run_id=RUN_ID_3, request_id="new-intent-during-reconcile"),
    )
    assert uncertain.error == "COMMAND_PERSISTENCE_UNAVAILABLE"
    assert database.commands[command_id]["status"] == "succeeded"
    assert invoker.count == 1

    exact = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(run_id=RUN_ID_3, request_id="reconcile-original"),
    )
    assert exact.success is True
    assert exact.metadata["result_receipt_recovered"] is True
    assert invoker.count == 1


@pytest.mark.asyncio
async def test_incomplete_artifact_receipt_never_acks_or_reports_plain_success() -> None:
    database = _ResultReceiptCommitThenAckLossDatabase()
    invoker = _ArtifactResultInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)

    first = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(request_id="artifact-intent"),
    )
    command_id = next(iter(database.commands))
    assert first.error == "SIDE_EFFECT_UNKNOWN"

    database.run["run_id"] = RUN_ID_2
    recovered = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(run_id=RUN_ID_2, request_id="artifact-recovery"),
    )
    assert recovered.success is False
    assert recovered.error == "RESULT_RECEIPT_INCOMPLETE"
    assert recovered.result is None
    assert recovered.output_files == []
    assert recovered.metadata["result_receipt_incomplete"] is True
    assert recovered.metadata["manual_recovery_required"] is True
    assert recovered.metadata["blind_replay_allowed"] is False
    assert invoker.count == 1

    checkpoint_id = "77777777-7777-4777-8777-777777777777"
    database.add_completion_checkpoint(
        command_id=command_id,
        run_id=RUN_ID_2,
        checkpoint_id=checkpoint_id,
        acknowledgeable=False,
    )
    ack = await gateway.acknowledge_command_result(
        command_id=command_id,
        checkpoint_id=checkpoint_id,
        run_id=RUN_ID_2,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
    )
    assert ack["committed"] is False
    assert database.commands[command_id]["status"] == "result_recorded_succeeded"


@pytest.mark.asyncio
async def test_final_dispatch_cas_blocks_hard_terminal_race_before_invocation() -> None:
    database = _SharedCommandDatabase()
    database.inject_hard_before_authorize = True
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=database)

    result = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        _context(),
    )

    assert result.error == "COMMAND_PERSISTENCE_UNAVAILABLE"
    assert result.metadata["execution_authorized"] is False
    assert result.metadata["side_effect_state"] == "not_started"
    assert invoker.count == 0


@pytest.mark.asyncio
async def test_process_dispatch_gate_honors_hard_fence_for_legacy_run_id() -> None:
    invoker = _CountingInvoker()
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    context = _context()
    context.run_id = "legacy-non-uuid-run"
    await gateway.start_run(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        engine="agent_loop",
        execution_profile="safe",
        memory_mode="auto",
        os_agent_enabled=False,
        request_preview="redacted",
    )
    await gateway.save_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        phase="terminal_persistence_unknown",
        status="blocked",
        error="terminal_persistence_unknown",
    )

    result = await gateway.invoke_tool(
        "external_write",
        {"value": "x"},
        context,
    )

    assert result.error == "COMMAND_PERSISTENCE_UNAVAILABLE"
    assert result.metadata["execution_authorized"] is False
    assert result.metadata["side_effect_state"] == "not_started"
    assert invoker.count == 0


class _TerminalFenceDatabase:
    def __init__(self, *, run_status: str, checkpoint_phase: str) -> None:
        self.run_status = run_status
        self.checkpoint_phase = checkpoint_phase
        self.execute_queries: list[str] = []

    async def execute(self, query: str, *_args: Any) -> str:
        self.execute_queries.append(query)
        return "UPDATE 0"

    async def fetchrow(self, query: str, *_args: Any) -> dict[str, Any] | None:
        if "FROM assistant_runs" in query:
            return {
                "run_id": RUN_ID,
                "tenant_id": "tenant-a",
                "user_id": "user-a",
                "session_id": "session-a",
                "status": self.run_status,
                "usage": {},
            }
        if "FROM assistant_run_checkpoints" in query:
            return {
                "checkpoint_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                "phase": self.checkpoint_phase,
                "status": self.run_status,
                "created_at": datetime.now(timezone.utc),
            }
        return None


class _HardCheckpointGateDatabase:
    def __init__(self) -> None:
        self.run = {
            "run_id": RUN_ID,
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "session_id": "session-a",
            "status": "running",
            "usage": {},
            "error": None,
        }
        self.hard_checkpoint: dict[str, Any] | None = None
        self.checkpoint_query = ""

    async def execute(self, query: str, *args: Any) -> str:
        if "UPDATE assistant_runs" not in query:
            return "OK"
        if self.run["status"] != "running" or self.hard_checkpoint:
            return "UPDATE 0"
        self.run.update(status=str(args[1]), usage=json.loads(args[2]), error=args[3])
        return "UPDATE 1"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "INSERT INTO assistant_run_checkpoints" in query:
            self.checkpoint_query = query
            if self.hard_checkpoint:
                return None
            phase = str(args[5])
            if phase != "terminal_persistence_unknown" or self.run["status"] not in {
                "running",
                "blocked",
            }:
                return None
            self.run.update(
                status="blocked",
                error=str(args[13] or "terminal_persistence_unknown"),
            )
            self.hard_checkpoint = {
                "checkpoint_id": str(args[0]),
                "phase": phase,
                "status": "blocked",
                "error": self.run["error"],
                "created_at": datetime.now(timezone.utc),
            }
            return {"checkpoint_id": args[0]}
        if "FROM assistant_runs" in query:
            return dict(self.run)
        if "FROM assistant_run_checkpoints" in query:
            return dict(self.hard_checkpoint) if self.hard_checkpoint else None
        return None


class _PredispatchFinishFenceDatabase:
    def __init__(self, *, command_status: str = "queued") -> None:
        self.finish_query = ""
        self.command_status = command_status

    async def execute(self, query: str, *_args: Any) -> str:
        self.finish_query = query
        return "UPDATE 0"

    async def fetchrow(self, query: str, *_args: Any) -> dict[str, Any] | None:
        if "FROM assistant_command_queue" in query:
            return {
                "command_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                "status": self.command_status,
            }
        if "FROM assistant_runs" in query:
            return {
                "run_id": RUN_ID,
                "tenant_id": "tenant-a",
                "user_id": "user-a",
                "session_id": "session-a",
                "status": "running",
                "usage": {},
                "error": None,
            }
        return None


@pytest.mark.asyncio
async def test_finish_run_cas_respects_existing_hard_terminal_checkpoint() -> None:
    database = _TerminalFenceDatabase(
        run_status="succeeded",
        checkpoint_phase="run_succeeded",
    )
    gateway = AssistantExecutionGateway(tool_invoker=_CountingInvoker(), database=database)

    matching = await gateway.finish_run(
        run_id=RUN_ID,
        status="succeeded",
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
    )
    conflicting = await gateway.finish_run(
        run_id=RUN_ID,
        status="failed",
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
    )

    assert matching["committed"] is True
    assert matching["idempotent"] is True
    assert conflicting["committed"] is False
    assert conflicting["authoritative_terminal"] is True
    assert conflicting["status"] == "succeeded"
    assert all("hard_checkpoint.phase IN" in query for query in database.execute_queries)


@pytest.mark.asyncio
async def test_unknown_hard_checkpoint_atomically_blocks_later_finish() -> None:
    database = _HardCheckpointGateDatabase()
    gateway = AssistantExecutionGateway(tool_invoker=_CountingInvoker(), database=database)

    checkpoint = await gateway.save_run_checkpoint(
        run_id=RUN_ID,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        phase="terminal_persistence_unknown",
        status="blocked",
        error="terminal_persistence_unknown",
    )
    finish = await gateway.finish_run(
        run_id=RUN_ID,
        status="succeeded",
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
    )

    assert checkpoint["checkpoint_receipt"]["committed"] is True
    assert database.run["status"] == "blocked"
    assert database.run["error"] == "terminal_persistence_unknown"
    assert finish["committed"] is False
    assert finish["authoritative_terminal"] is True
    assert finish["status"] == "blocked"
    assert finish["hard_checkpoint"]["phase"] == "terminal_persistence_unknown"
    assert "WITH eligible_run AS MATERIALIZED" in database.checkpoint_query
    assert "UPDATE assistant_runs" in database.checkpoint_query
    assert "THEN 'blocked'" in database.checkpoint_query
    assert "AND NOT EXISTS" in database.checkpoint_query


@pytest.mark.asyncio
async def test_stale_pause_finish_is_suppressed_by_queued_resume_command() -> None:
    database = _PredispatchFinishFenceDatabase()
    gateway = AssistantExecutionGateway(tool_invoker=_CountingInvoker(), database=database)

    receipt = await gateway.finish_run(
        run_id=RUN_ID,
        status="blocked",
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
    )

    assert receipt["committed"] is True
    assert receipt["resume_in_progress"] is True
    assert receipt["status"] == "running"
    assert receipt["active_command_id"] == "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    assert "'queued', 'running', 'approval_claimed'" in database.finish_query
    assert "'result_recorded_succeeded', 'result_recorded_failed'" in database.finish_query


@pytest.mark.parametrize(
    "recorded_status",
    ["result_recorded_succeeded", "result_recorded_failed"],
)
@pytest.mark.asyncio
async def test_stale_pause_finish_is_suppressed_by_unacknowledged_result(
    recorded_status: str,
) -> None:
    database = _PredispatchFinishFenceDatabase(command_status=recorded_status)
    gateway = AssistantExecutionGateway(tool_invoker=_CountingInvoker(), database=database)

    receipt = await gateway.finish_run(
        run_id=RUN_ID,
        status="blocked",
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
    )

    assert receipt["committed"] is True
    assert receipt["resume_in_progress"] is True
    assert receipt["status"] == "running"
    assert receipt["active_command_id"] == "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"


@pytest.mark.asyncio
async def test_process_finish_is_suppressed_by_unacknowledged_result() -> None:
    gateway = AssistantExecutionGateway(tool_invoker=_CountingInvoker(), database=None)
    context = _context()
    await _start_run(gateway)
    await gateway._create_command(
        command_id="12121212-1212-4212-8212-121212121212",
        context=context,
        tool_name="external_write",
        arguments={"value": "x"},
        status="result_recorded_succeeded",
        lane="main",
        queue_mode="collect",
        priority=0,
        steer_payload={"_result_receipt_recorded": True},
    )

    receipt = await gateway.finish_run(
        run_id=RUN_ID,
        status="blocked",
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
    )

    assert receipt["committed"] is True
    assert receipt["resume_in_progress"] is True
    assert gateway._runs[RUN_ID].status == "running"
