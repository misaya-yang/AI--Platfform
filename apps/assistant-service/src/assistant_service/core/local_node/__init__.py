"""Trusted Local Node control-plane primitives.

Nothing in this package is enabled by importing it.  The composition root must
explicitly provide a repository, device-channel verifier, canonical dispatch
authority, and action-delivery provider through :func:`wire_local_node_control_plane`.
"""

from .control_plane import (
    InMemoryLocalNodeRepository,
    LocalNodeActionDeliveryProvider,
    LocalNodeControlPlaneService,
    LocalNodeRepository,
    canonical_digest,
    derive_action_id,
)
from .device_channel import SQLiteDeviceChannelBroker
from .device_delivery import SQLiteDeviceDelivery
from .protocol import LOCAL_NODE_PROTOCOL_VERSION
from .provider_adapter import (
    ControlPlaneLocalNodeToolProvider,
    LocalNodeActionResultWaiter,
    LocalNodeDeviceChannelPrincipal,
    LocalNodePlatformActionSigner,
    LocalNodeRunBinding,
    LocalNodeRunBindingResolver,
    LocalNodeTrustedApprovalReceiptVerifier,
    LocalNodeTrustedApprovalRegistrar,
    PinnedLocalNodeRunBindingResolver,
    SelectedLocalNodeRunBindingResolver,
    validate_file_result,
)
from .sqlite_repository import SQLiteLocalNodeRepository
from .tool_bridge import (
    LocalNodeCapabilitySnapshot,
    LocalNodeDispatchEnvelope,
    LocalNodeRunScope,
    LocalNodeToolProvider,
    prepare_local_node_runtime_tools,
)
from .wiring import (
    LocalNodeWiringResult,
    build_local_node_tool_provider,
    wire_local_node_control_plane,
)

__all__ = [
    "InMemoryLocalNodeRepository",
    "SQLiteDeviceChannelBroker",
    "SQLiteDeviceDelivery",
    "LocalNodeActionDeliveryProvider",
    "LocalNodeControlPlaneService",
    "LocalNodeRepository",
    "LocalNodeWiringResult",
    "LocalNodeCapabilitySnapshot",
    "LocalNodeDispatchEnvelope",
    "LocalNodeRunScope",
    "LocalNodeToolProvider",
    "ControlPlaneLocalNodeToolProvider",
    "LocalNodeActionResultWaiter",
    "LocalNodeDeviceChannelPrincipal",
    "LocalNodePlatformActionSigner",
    "LocalNodeRunBinding",
    "LocalNodeRunBindingResolver",
    "LocalNodeTrustedApprovalRegistrar",
    "LocalNodeTrustedApprovalReceiptVerifier",
    "LOCAL_NODE_PROTOCOL_VERSION",
    "PinnedLocalNodeRunBindingResolver",
    "SelectedLocalNodeRunBindingResolver",
    "SQLiteLocalNodeRepository",
    "canonical_digest",
    "derive_action_id",
    "prepare_local_node_runtime_tools",
    "build_local_node_tool_provider",
    "wire_local_node_control_plane",
    "validate_file_result",
]
