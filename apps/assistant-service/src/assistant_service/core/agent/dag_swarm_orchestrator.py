"""2026 SOTA Dynamic DAG Multi-Agent Swarm Orchestration Engine.

Manages topological dependency resolution, concurrent execution tiers,
intermediate output routing, and fault isolation across multi-agent swarms.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DAGTaskNode:
    """A discrete unit of work executed by a specialized agent role."""

    node_id: str
    name: str
    agent_role: str
    prompt: str
    dependencies: list[str] = field(default_factory=list)
    status: str = "pending"  # "pending", "running", "completed", "failed", "skipped"
    output: Any | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class CyclicDependencyError(ValueError):
    """Raised when a circular dependency is detected in the agent DAG."""


class DAGGraph:
    """Represents an acyclic dependency graph of agent task nodes."""

    def __init__(self) -> None:
        self.nodes: dict[str, DAGTaskNode] = {}

    def add_node(self, node: DAGTaskNode) -> None:
        self.nodes[node.node_id] = node

    def get_ready_nodes(self) -> list[DAGTaskNode]:
        """Return all nodes whose dependencies have successfully completed."""
        ready: list[DAGTaskNode] = []
        for node in self.nodes.values():
            if node.status != "pending":
                continue
            deps_satisfied = all(
                dep_id in self.nodes and self.nodes[dep_id].status == "completed"
                for dep_id in node.dependencies
            )
            if deps_satisfied:
                ready.append(node)
        return ready

    def topological_tiers(self) -> list[list[str]]:
        """Compute execution tiers (parallel waves) using Kahn's algorithm."""
        in_degree: dict[str, int] = defaultdict(int)
        adj: dict[str, list[str]] = defaultdict(list)

        for node_id, node in self.nodes.items():
            in_degree[node_id] = len(node.dependencies)
            for dep_id in node.dependencies:
                adj[dep_id].append(node_id)

        queue = deque([nid for nid, deg in in_degree.items() if deg == 0])
        tiers: list[list[str]] = []
        visited_count = 0

        while queue:
            current_tier = list(queue)
            tiers.append(current_tier)
            next_queue = deque()
            for u in current_tier:
                visited_count += 1
                for v in adj[u]:
                    in_degree[v] -= 1
                    if in_degree[v] == 0:
                        next_queue.append(v)
            queue = next_queue

        if visited_count != len(self.nodes):
            raise CyclicDependencyError("Circular dependency detected in multi-agent DAG")

        return tiers


class DAGSwarmOrchestrator:
    """Executes a multi-agent DAG in parallel topological waves."""

    async def execute_dag(
        self,
        dag: DAGGraph,
        agent_executor: Callable[[DAGTaskNode, dict[str, Any]], Awaitable[Any]],
        max_parallel_nodes: int = 5,
    ) -> dict[str, Any]:
        """Execute the DAG graph until completion, propagating dependency outputs."""
        # Verify acyclic topology first
        dag.topological_tiers()

        semaphore = asyncio.Semaphore(max_parallel_nodes)

        async def _run_single_node(node: DAGTaskNode) -> None:
            async with semaphore:
                node.status = "running"
                # Gather outputs from upstream dependencies
                parent_context = {
                    dep_id: self._format_parent_output(dag.nodes[dep_id])
                    for dep_id in node.dependencies
                    if dep_id in dag.nodes
                }
                try:
                    res = await agent_executor(node, parent_context)
                    node.output = res
                    node.status = "completed"
                except Exception as exc:
                    node.error = str(exc)
                    node.status = "failed"

        while True:
            ready_nodes = dag.get_ready_nodes()
            if not ready_nodes:
                # Check if everything is done or if failed nodes blocked remaining
                pending_nodes = [n for n in dag.nodes.values() if n.status == "pending"]
                if pending_nodes:
                    for n in pending_nodes:
                        n.status = "skipped"
                        n.error = "Upstream dependency failed or skipped"
                break

            tasks = [_run_single_node(node) for node in ready_nodes]
            await asyncio.gather(*tasks)

        return {
            node_id: {
                "status": node.status,
                "output": node.output,
                "error": node.error,
                "role": node.agent_role,
            }
            for node_id, node in dag.nodes.items()
        }

    @staticmethod
    def _format_parent_output(node: DAGTaskNode) -> Any:
        return node.output if node.output is not None else f"[No output from {node.node_id}]"
