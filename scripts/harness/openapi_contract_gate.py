#!/usr/bin/env python3
"""Offline in-process Gateway OpenAPI contract gate (L1, stack-free).

Exports the OpenAPI document directly from the FastAPI app object
(``src.main.create_app().openapi()``) and compares it against the checked-in
published baseline (``sdk/openapi.json`` by default). The live stack is NOT
required and this gate NEVER skips: if the app cannot be constructed or the
baseline is missing, the gate fails closed (exit 2).

Contract preservation rules (baseline → current):
  1. every baseline path must survive
  2. every baseline method on a path must survive
  3. every operationId must survive unchanged (SDK generation depends on it)
  4. every required request-body field must survive (loss of a required field
     breaks existing callers)
  5. every documented response status code must survive
  6. every securityScheme name must survive

Adding paths / methods / fields / status codes is allowed (compatible growth).

Usage:
  python scripts/harness/openapi_contract_gate.py                # real gate
  python scripts/harness/openapi_contract_gate.py --baseline X   # custom baseline
  python scripts/harness/openapi_contract_gate.py --selftest     # synthetic drift self-test

Exit codes: 0 = contract preserved, 1 = contract drift, 2 = gate error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = ROOT / "sdk" / "openapi.json"
DEFAULT_EVIDENCE = ROOT / "tmp" / "gate-evidence" / "verify-openapi-contract.json"


def _resolve_ref(spec: dict, ref: str) -> dict:
    """Resolve a local $ref (components/schemas/...) inside *spec*."""
    node: object = spec
    for part in ref.lstrip("#/").split("/"):
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def _required_fields(spec: dict, schema: dict) -> set[str]:
    """Required top-level field names of a request-body object schema.

    Follows $ref and flattens allOf members so composed schemas are covered.
    """
    required: set[str] = set()
    seen: set[str] = set()
    queue = [schema]
    while queue:
        node = queue.pop()
        if not isinstance(node, dict):
            continue
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref in seen:
                continue
            seen.add(ref)
            queue.append(_resolve_ref(spec, ref))
            continue
        for field in node.get("required") or []:
            if isinstance(field, str):
                required.add(field)
        for member in node.get("allOf") or []:
            queue.append(member)
    return required


def _request_required(spec: dict, operation: dict) -> set[str]:
    body = (operation or {}).get("requestBody") or {}
    content = body.get("content") or {}
    fields: set[str] = set()
    for media in content.values():
        schema = (media or {}).get("schema")
        if isinstance(schema, dict):
            fields |= _required_fields(spec, schema)
    return fields


def compare_specs(baseline: dict, current: dict) -> list[str]:
    """Return the list of contract violations between *baseline* and *current*.

    Empty list means the public contract is preserved. This function is pure:
    no network, no live stack, deterministic on the two documents.
    """
    violations: list[str] = []
    base_paths = baseline.get("paths") or {}
    cur_paths = current.get("paths") or {}

    for path, methods in base_paths.items():
        cur_methods = cur_paths.get(path)
        if cur_methods is None:
            violations.append(f"path removed: {path}")
            continue
        for method, op in (methods or {}).items():
            cur_op = (cur_methods or {}).get(method)
            if cur_op is None:
                violations.append(f"method removed: {method.upper()} {path}")
                continue
            if not isinstance(op, dict):
                continue
            base_op_id = op.get("operationId")
            if base_op_id and cur_op.get("operationId") != base_op_id:
                violations.append(
                    f"operationId changed: {method.upper()} {path} "
                    f"'{base_op_id}' -> '{cur_op.get('operationId')}'"
                )
            lost = _request_required(baseline, op) - _request_required(current, cur_op)
            for field in sorted(lost):
                violations.append(
                    f"required request field lost: {method.upper()} {path} field '{field}'"
                )
            base_codes = set(((op.get("responses") or {}).keys()))
            cur_codes = set(((cur_op.get("responses") or {}).keys()))
            for code in sorted(base_codes - cur_codes):
                violations.append(
                    f"response status lost: {method.upper()} {path} status '{code}'"
                )

    base_schemes = ((baseline.get("components") or {}).get("securitySchemes") or {})
    cur_schemes = ((current.get("components") or {}).get("securitySchemes") or {})
    for name in sorted(set(base_schemes) - set(cur_schemes)):
        violations.append(f"securityScheme removed: {name}")

    return violations


def _spec_stats(spec: dict) -> dict:
    paths = spec.get("paths") or {}
    operations = sum(
        1
        for methods in paths.values()
        for method in (methods or {})
        if method in {"get", "post", "put", "patch", "delete", "head", "options"}
    )
    return {"paths": len(paths), "operations": operations}


def run_real_gate(baseline_path: Path, evidence_path: Path) -> int:
    if not baseline_path.exists():
        print(f"GATE ERROR: baseline not found: {baseline_path}", file=sys.stderr)
        return 2
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"GATE ERROR: cannot read baseline {baseline_path}: {exc}", file=sys.stderr)
        return 2

    # Build the spec in-process from the real FastAPI app object. This import
    # must never be turned into a skip: an app that cannot be constructed is a
    # gate failure, not a skipped gate.
    try:
        sys.path.insert(0, str(ROOT))
        from src.main import create_app  # noqa: PLC0415

        current = create_app().openapi()
    except Exception as exc:  # fail closed on any construction error
        print(f"GATE ERROR: cannot build in-process OpenAPI spec: {exc}", file=sys.stderr)
        return 2

    violations = compare_specs(baseline, current)
    base_stats = _spec_stats(baseline)
    cur_stats = _spec_stats(current)
    evidence = {
        "gate": "verify-openapi-contract",
        "tier": "L1-offline-in-process",
        "baseline": str(baseline_path.relative_to(ROOT)),
        "baseline_stats": base_stats,
        "current_stats": cur_stats,
        "violations": violations,
        "result": "pass" if not violations else "fail",
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"baseline {base_stats['paths']} paths / {base_stats['operations']} operations; "
        f"in-process app {cur_stats['paths']} paths / {cur_stats['operations']} operations"
    )
    if violations:
        print(f"OPENAPI CONTRACT DRIFT: {len(violations)} violation(s)", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        print(
            "If the drift is intentional, regenerate the published baseline with "
            "`make snapshot-gateway-openapi` and commit it.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: public OpenAPI contract preserved (evidence: {evidence_path.relative_to(ROOT)})")
    return 0


def _selftest() -> int:
    """Negative + positive self-test on synthetic drift pairs."""
    baseline = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "components": {
            "securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer"}},
            "schemas": {
                "CreateThing": {
                    "type": "object",
                    "required": ["name", "kind"],
                    "properties": {"name": {"type": "string"}, "kind": {"type": "string"}},
                }
            },
        },
        "paths": {
            "/things": {
                "post": {
                    "operationId": "create_thing",
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/CreateThing"}}
                        }
                    },
                    "responses": {"201": {}, "422": {}},
                },
                "get": {"operationId": "list_things", "responses": {"200": {}}},
            },
            "/things/{id}": {"get": {"operationId": "get_thing", "responses": {"200": {}, "404": {}}}},
        },
    }

    import copy

    cases: list[tuple[str, dict, bool]] = []  # (name, current, expect_violation)

    def variant(name: str, expect_violation: bool, mutate) -> None:
        cur = copy.deepcopy(baseline)
        mutate(cur)
        cases.append((name, cur, expect_violation))

    variant("unchanged spec passes", False, lambda cur: None)

    def drop_path(cur: dict) -> None:
        del cur["paths"]["/things/{id}"]

    variant("removed path fails", True, drop_path)

    def drop_method(cur: dict) -> None:
        del cur["paths"]["/things"]["get"]

    variant("removed method fails", True, drop_method)

    def lose_required(cur: dict) -> None:
        cur["components"]["schemas"]["CreateThing"]["required"] = ["name"]

    variant("lost required request field fails", True, lose_required)

    def rename_op(cur: dict) -> None:
        cur["paths"]["/things"]["post"]["operationId"] = "make_thing"

    variant("changed operationId fails", True, rename_op)

    def drop_status(cur: dict) -> None:
        del cur["paths"]["/things"]["post"]["responses"]["422"]

    variant("removed response status fails", True, drop_status)

    def drop_scheme(cur: dict) -> None:
        cur["components"]["securitySchemes"] = {}

    variant("removed securityScheme fails", True, drop_scheme)

    def compatible_growth(cur: dict) -> None:
        cur["paths"]["/things/new"] = {"get": {"operationId": "new_thing", "responses": {"200": {}}}}
        cur["components"]["schemas"]["CreateThing"]["properties"]["note"] = {"type": "string"}
        cur["paths"]["/things"]["post"]["responses"]["202"] = {}

    variant("compatible growth passes", False, compatible_growth)

    failures = 0
    for name, current, expect_violation in cases:
        violations = compare_specs(baseline, current)
        got_violation = bool(violations)
        status = "ok" if got_violation == expect_violation else "FAIL"
        if status == "FAIL":
            failures += 1
        expect = "violation" if expect_violation else "no violation"
        print(f"[{status}] {name} (expected {expect}, got {len(violations)} violation(s))")
        for violation in violations:
            print(f"        - {violation}")

    if failures:
        print(f"SELFTEST FAILED: {failures} case(s) misclassified", file=sys.stderr)
        return 1
    print(f"SELFTEST OK: {len(cases)} synthetic drift cases classified correctly")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--evidence-out", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--selftest", action="store_true", help="run synthetic drift self-test only")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    return run_real_gate(args.baseline, args.evidence_out)


if __name__ == "__main__":
    raise SystemExit(main())
