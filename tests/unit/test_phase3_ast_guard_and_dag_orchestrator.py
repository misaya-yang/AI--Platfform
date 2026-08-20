"""Unit tests for Phase 3: ASTSecurityGuard, DAGSwarmOrchestrator, and AaaS Protocol."""

from __future__ import annotations

import pytest
from ai_gateway_core.agents.aaas_protocol import (
    AaaSRunRequest,
    AaaSRunResponse,
    AaaSTaskStep,
)
from assistant_service.core.agent.dag_swarm_orchestrator import (
    CyclicDependencyError,
    DAGGraph,
    DAGSwarmOrchestrator,
    DAGTaskNode,
    MissingDependencyError,
)
from assistant_service.core.security.ast_guard import ASTSecurityGuard

# ============================================================================
# 1. AST Security Guard Tests
# ============================================================================


def test_ast_guard_safe_code() -> None:
    safe_code = """
def calculate_growth(revenue_prev: float, revenue_curr: float) -> float:
    if revenue_prev == 0:
        return 0.0
    return ((revenue_curr - revenue_prev) / revenue_prev) * 100.0

result = calculate_growth(100.0, 150.0)
"""
    report = ASTSecurityGuard.audit_python_code(safe_code)
    assert report.is_safe is True
    assert len(report.violations) == 0


def test_ast_guard_blocks_forbidden_imports() -> None:
    malicious_code = """
import subprocess
subprocess.run(["ls", "-la"])
"""
    report = ASTSecurityGuard.audit_python_code(malicious_code)
    assert report.is_safe is False
    assert any(v.rule_id == "SEC_FORBIDDEN_MODULE" for v in report.violations)


def test_ast_guard_blocks_eval_exec() -> None:
    eval_code = """
payload = "__import__('os').system('echo pwned')"
eval(payload)
"""
    report = ASTSecurityGuard.audit_python_code(eval_code)
    assert report.is_safe is False
    assert any(v.rule_id == "SEC_DANGEROUS_BUILTIN" for v in report.violations)


def test_ast_guard_blocks_destructive_shutil() -> None:
    shutil_code = """
import shutil
shutil.rmtree("/tmp/target_dir")
"""
    report = ASTSecurityGuard.audit_python_code(shutil_code)
    assert report.is_safe is False
    assert any(v.rule_id == "SEC_DESTRUCTIVE_SHUTIL" for v in report.violations)


def test_ast_guard_handles_syntax_error() -> None:
    broken_code = "def invalid_syntax(:"
    report = ASTSecurityGuard.audit_python_code(broken_code)
    assert report.is_safe is False
    assert report.violations[0].rule_id == "SEC_SYNTAX_ERROR"


def test_ast_guard_blocks_dunder_traversal() -> None:
    exploit_code = """
# Sandbox escape via object class traversal
subclasses = ().__class__.__bases__[0].__subclasses__()
"""
    report = ASTSecurityGuard.audit_python_code(exploit_code)
    assert report.is_safe is False
    assert any(v.rule_id == "SEC_DUNDER_TRAVERSAL" for v in report.violations)


@pytest.mark.parametrize(
    "exploit_code",
    [
        "from os import system\nsystem('id')",
        "import importlib\nimportlib.import_module('subprocess')",
        "getattr(__builtins__, '__import__')('os')",
        "os.execv('/bin/sh', ['sh'])",
    ],
)
def test_ast_guard_blocks_import_and_reflection_bypasses(exploit_code: str) -> None:
    report = ASTSecurityGuard.audit_python_code(exploit_code)

    assert report.is_safe is False
    assert report.violations


# ============================================================================
# 2. DAG Swarm Orchestrator Tests
# ============================================================================


@pytest.mark.asyncio
async def test_dag_orchestrator_linear_chain() -> None:
    dag = DAGGraph()
    dag.add_node(DAGTaskNode(node_id="A", name="DataFetcher", agent_role="Fetcher", prompt="Fetch data"))
    dag.add_node(DAGTaskNode(node_id="B", name="DataAnalyzer", agent_role="Analyzer", prompt="Analyze data", dependencies=["A"]))
    dag.add_node(DAGTaskNode(node_id="C", name="ReportWriter", agent_role="Writer", prompt="Write report", dependencies=["B"]))

    execution_order = []

    async def mock_agent_executor(node: DAGTaskNode, parent_context: dict) -> str:
        execution_order.append(node.node_id)
        return f"Output of {node.node_id} (parents: {list(parent_context.keys())})"

    orchestrator = DAGSwarmOrchestrator()
    results = await orchestrator.execute_dag(dag, mock_agent_executor)

    assert execution_order == ["A", "B", "C"]
    assert results["A"]["status"] == "completed"
    assert results["B"]["status"] == "completed"
    assert results["C"]["status"] == "completed"
    assert "Output of A" in results["A"]["output"]
    assert "A" in results["B"]["output"]


@pytest.mark.asyncio
async def test_dag_orchestrator_diamond_parallel() -> None:
    # A -> B, A -> C, (B, C) -> D
    dag = DAGGraph()
    dag.add_node(DAGTaskNode(node_id="A", name="Root", agent_role="Planner", prompt="Plan"))
    dag.add_node(DAGTaskNode(node_id="B", name="Worker1", agent_role="Coder", prompt="Code", dependencies=["A"]))
    dag.add_node(DAGTaskNode(node_id="C", name="Worker2", agent_role="Reviewer", prompt="Review", dependencies=["A"]))
    dag.add_node(DAGTaskNode(node_id="D", name="Sink", agent_role="Deployer", prompt="Deploy", dependencies=["B", "C"]))

    tiers = dag.topological_tiers()
    assert tiers == [["A"], ["B", "C"], ["D"]]

    async def mock_agent_executor(node: DAGTaskNode, parent_context: dict) -> str:
        return f"Done {node.node_id}"

    orchestrator = DAGSwarmOrchestrator()
    results = await orchestrator.execute_dag(dag, mock_agent_executor)

    assert all(res["status"] == "completed" for res in results.values())


def test_dag_detects_cyclic_dependency() -> None:
    dag = DAGGraph()
    dag.add_node(DAGTaskNode(node_id="A", name="A", agent_role="A", prompt="", dependencies=["B"]))
    dag.add_node(DAGTaskNode(node_id="B", name="B", agent_role="B", prompt="", dependencies=["A"]))

    with pytest.raises(CyclicDependencyError, match="Circular dependency detected"):
        dag.topological_tiers()


def test_dag_rejects_missing_dependency_explicitly() -> None:
    dag = DAGGraph()
    dag.add_node(
        DAGTaskNode(
            node_id="A",
            name="A",
            agent_role="A",
            prompt="",
            dependencies=["missing"],
        )
    )

    with pytest.raises(MissingDependencyError, match="missing"):
        dag.topological_tiers()


# ============================================================================
# 3. AaaS Protocol Serialization Tests
# ============================================================================


def test_aaas_protocol_models_roundtrip() -> None:
    req = AaaSRunRequest(
        agent_id="agent_enterprise_01",
        input_prompt="Audit system security",
        session_id="sess_live",
        stream=True,
    )
    req_dict = req.to_dict()
    assert req_dict["agent_id"] == "agent_enterprise_01"
    assert req_dict["stream"] is True

    step1 = AaaSTaskStep(
        step_id="step_01",
        turn_index=1,
        status="completed",
        thought="Need to check network ports",
        tool_call={"tool": "port_scanner", "args": {"target": "127.0.0.1"}},
        tool_result={"open_ports": [80, 443]},
    )

    resp = AaaSRunResponse(
        run_id="run_999",
        agent_id="agent_enterprise_01",
        session_id="sess_live",
        status="completed",
        final_output="Audit completed with 0 vulnerabilities",
        steps=[step1],
        usage={"input_tokens": 1500, "output_tokens": 420},
    )
    resp_dict = resp.to_dict()

    assert resp_dict["run_id"] == "run_999"
    assert resp_dict["status"] == "completed"
    assert len(resp_dict["steps"]) == 1
    assert resp_dict["steps"][0]["thought"] == "Need to check network ports"
    assert resp_dict["usage"]["output_tokens"] == 420
