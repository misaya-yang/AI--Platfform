"""Explicit, fail-closed FastAPI wiring for the Local Node control plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from .control_plane import (
    InMemoryLocalNodeRepository,
    LocalNodeActionDeliveryProvider,
    LocalNodeControlPlaneService,
    LocalNodeRepository,
)
from .provider_adapter import (
    ControlPlaneLocalNodeToolProvider,
    LocalNodeActionResultWaiter,
    LocalNodePlatformActionSigner,
    LocalNodeRunBindingResolver,
    LocalNodeTrustedApprovalReceiptVerifier,
    LocalNodeTrustedApprovalRegistrar,
)


@dataclass(frozen=True, slots=True)
class LocalNodeWiringResult:
    enabled: bool
    reason: str


def wire_local_node_control_plane(
    app: Any,
    *,
    enabled: bool,
    environment: Literal["development", "test", "staging", "production"],
    repository: LocalNodeRepository | None,
    channel_verifier: Any | None,
    dispatch_authority: Any | None,
    action_provider: LocalNodeActionDeliveryProvider | None,
    device_channel_broker: Any | None = None,
) -> LocalNodeWiringResult:
    """Install dependencies only when the full trusted path is explicit.

    The helper intentionally has no environment-variable auto-discovery and is
    not called from ``main``.  Missing dependencies leave every state member as
    ``None``, which keeps the already-registered API routes at their stable 503
    boundary.  In-memory state is rejected outside explicit dev/test use.
    """

    app.state.local_node_control_service = None
    app.state.local_node_channel_verifier = None
    app.state.local_node_dispatch_authority = None
    app.state.local_node_device_channel_broker = None
    if not enabled:
        return LocalNodeWiringResult(False, "disabled")
    if any(
        value is None
        for value in (
            repository,
            channel_verifier,
            dispatch_authority,
            action_provider,
        )
    ):
        return LocalNodeWiringResult(False, "trusted_dependency_missing")
    if getattr(action_provider, "idempotent_enqueue", False) is not True:
        return LocalNodeWiringResult(False, "idempotent_delivery_required")
    supported_environments = getattr(repository, "supported_environments", None)
    if supported_environments is not None and environment not in supported_environments:
        return LocalNodeWiringResult(False, "repository_environment_unsupported")
    if isinstance(repository, InMemoryLocalNodeRepository) and environment not in {
        "development",
        "test",
    }:
        return LocalNodeWiringResult(False, "non_durable_repository_rejected")
    if (
        environment in {"staging", "production"}
        and getattr(repository, "durable_dispatch_fence", False) is not True
    ):
        return LocalNodeWiringResult(False, "durable_dispatch_fence_required")

    service = LocalNodeControlPlaneService(
        repository=cast(LocalNodeRepository, repository),
        action_provider=cast(LocalNodeActionDeliveryProvider, action_provider),
    )
    app.state.local_node_control_service = service
    app.state.local_node_channel_verifier = channel_verifier
    app.state.local_node_dispatch_authority = dispatch_authority
    if device_channel_broker is not None:
        if getattr(device_channel_broker, "control_service", None) is not service:
            return LocalNodeWiringResult(False, "device_channel_service_mismatch")
        cast(LocalNodeControlPlaneService, service).set_pairing_challenge_observer(
            cast(Any, device_channel_broker).register_challenge
        )
        app.state.local_node_device_channel_broker = device_channel_broker
    return LocalNodeWiringResult(True, "enabled")


def build_local_node_tool_provider(
    *,
    enabled: bool,
    control_plane: LocalNodeControlPlaneService | None,
    repository: LocalNodeRepository | None,
    binding_resolver: LocalNodeRunBindingResolver | None,
    action_signer: LocalNodePlatformActionSigner | None,
    approval_registrar: LocalNodeTrustedApprovalRegistrar | None,
    approval_receipt_verifier: LocalNodeTrustedApprovalReceiptVerifier | None,
    result_waiter: LocalNodeActionResultWaiter | None = None,
) -> ControlPlaneLocalNodeToolProvider | None:
    """Build the canonical tool provider only from explicit trusted seams.

    This helper performs no environment discovery and is not called from the
    default service startup.  Side-effect tools require a trusted-local
    approval registrar, so it is part of the all-or-nothing composition.
    """

    if not enabled or any(
        value is None
        for value in (
            control_plane,
            repository,
            binding_resolver,
            action_signer,
            approval_registrar,
            approval_receipt_verifier,
        )
    ):
        return None
    return ControlPlaneLocalNodeToolProvider(
        control_plane=cast(LocalNodeControlPlaneService, control_plane),
        repository=cast(LocalNodeRepository, repository),
        binding_resolver=cast(LocalNodeRunBindingResolver, binding_resolver),
        action_signer=cast(LocalNodePlatformActionSigner, action_signer),
        approval_registrar=cast(LocalNodeTrustedApprovalRegistrar, approval_registrar),
        approval_receipt_verifier=cast(
            LocalNodeTrustedApprovalReceiptVerifier,
            approval_receipt_verifier,
        ),
        result_waiter=result_waiter,
    )
