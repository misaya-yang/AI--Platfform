"""Deny-wins policy lattice for tool execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class PolicyLayerOutcome:
    """Single layer outcome in policy lattice evaluation."""

    layer: str
    effect: str
    reason: str


@dataclass
class ToolLatticeDecision:
    """Final policy decision after evaluating all layers."""

    allowed: bool
    requires_approval: bool
    reason: str
    outcomes: list[PolicyLayerOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "reason": self.reason,
            "outcomes": [asdict(outcome) for outcome in self.outcomes],
        }


class ToolPolicyLattice:
    """Evaluate layered allow/deny/approval rules with deny precedence."""

    def evaluate(
        self,
        *,
        tool_name: str,
        base_allowed: bool,
        base_requires_approval: bool,
        base_reason: str,
        layers: dict[str, dict[str, Any]] | None = None,
    ) -> ToolLatticeDecision:
        layers = layers or {}
        outcomes: list[PolicyLayerOutcome] = []

        allowed = bool(base_allowed)
        requires_approval = bool(base_requires_approval)
        reason = base_reason or "Allowed by base policy"

        for layer_name, rules in layers.items():
            if not isinstance(rules, dict):
                continue

            deny_set = {str(item) for item in rules.get("deny", []) or []}
            allow_set = {str(item) for item in rules.get("allow", []) or []}
            approval_set = {str(item) for item in rules.get("require_approval", []) or []}

            if tool_name in deny_set:
                outcomes.append(
                    PolicyLayerOutcome(
                        layer=layer_name,
                        effect="deny",
                        reason=f"Tool denied by {layer_name}",
                    )
                )
                return ToolLatticeDecision(
                    allowed=False,
                    requires_approval=False,
                    reason=f"Denied by layer: {layer_name}",
                    outcomes=outcomes,
                )

            if allow_set and tool_name not in allow_set:
                outcomes.append(
                    PolicyLayerOutcome(
                        layer=layer_name,
                        effect="deny",
                        reason=f"Tool not in allow-list for {layer_name}",
                    )
                )
                return ToolLatticeDecision(
                    allowed=False,
                    requires_approval=False,
                    reason=f"Not allowed by layer: {layer_name}",
                    outcomes=outcomes,
                )

            if tool_name in approval_set:
                requires_approval = True
                reason = f"Approval required by layer: {layer_name}"
                outcomes.append(
                    PolicyLayerOutcome(
                        layer=layer_name,
                        effect="approval",
                        reason=reason,
                    )
                )
            else:
                outcomes.append(
                    PolicyLayerOutcome(
                        layer=layer_name,
                        effect="allow",
                        reason=f"No additional restrictions in {layer_name}",
                    )
                )

        if not allowed:
            return ToolLatticeDecision(
                allowed=False,
                requires_approval=False,
                reason=reason,
                outcomes=outcomes,
            )

        return ToolLatticeDecision(
            allowed=True,
            requires_approval=requires_approval,
            reason=reason,
            outcomes=outcomes,
        )
