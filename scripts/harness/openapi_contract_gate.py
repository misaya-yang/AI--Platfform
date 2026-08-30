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
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = ROOT / "sdk" / "openapi.json"
DEFAULT_EVIDENCE = ROOT / "tmp" / "gate-evidence" / "verify-openapi-contract.json"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


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


def _parameters_by_key(spec: dict, *parameter_lists: object) -> dict[tuple[str, str], dict]:
    result: dict[tuple[str, str], dict] = {}
    for raw_list in parameter_lists:
        if not isinstance(raw_list, list):
            continue
        for raw in raw_list:
            parameter = raw
            if isinstance(raw, dict) and isinstance(raw.get("$ref"), str):
                parameter = _resolve_ref(spec, raw["$ref"])
            if not isinstance(parameter, dict):
                continue
            name = parameter.get("name")
            location = parameter.get("in")
            if isinstance(name, str) and isinstance(location, str):
                result[(name, location)] = parameter
    return result


def _schema_nodes(spec: dict, schema: object) -> list[dict]:
    """Resolve a schema plus its allOf members without following cycles."""
    nodes: list[dict] = []
    queue = [schema]
    seen_refs: set[str] = set()
    while queue:
        node = queue.pop()
        if not isinstance(node, dict):
            continue
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            queue.append(_resolve_ref(spec, ref))
            continue
        nodes.append(node)
        queue.extend(node.get("allOf") or [])
    return nodes


def _schema_property_names(spec: dict, schema: object) -> set[str]:
    names: set[str] = set()
    for node in _schema_nodes(spec, schema):
        properties = node.get("properties")
        if isinstance(properties, dict):
            names.update(str(name) for name in properties)
    return names


def _response_schema_violations(
    baseline: dict,
    current: dict,
    baseline_response: dict,
    current_response: dict,
    *,
    label: str,
) -> list[str]:
    violations: list[str] = []
    base_content = baseline_response.get("content") or {}
    cur_content = current_response.get("content") or {}
    for media_type, base_media in base_content.items():
        if media_type not in cur_content:
            violations.append(f"response media type lost: {label} media '{media_type}'")
            continue
        base_schema = (base_media or {}).get("schema")
        cur_schema = (cur_content[media_type] or {}).get("schema")
        base_properties = _schema_property_names(baseline, base_schema)
        cur_properties = _schema_property_names(current, cur_schema)
        for field in sorted(base_properties - cur_properties):
            violations.append(
                f"response field lost: {label} media '{media_type}' field '{field}'"
            )
    return violations


def compare_specs(baseline: dict, current: dict) -> list[str]:
    """Return the list of contract violations between *baseline* and *current*.

    Empty list means the public contract is preserved. This function is pure:
    no network, no live stack, deterministic on the two documents.
    """
    violations: list[str] = []
    base_paths = baseline.get("paths") or {}
    cur_paths = current.get("paths") or {}

    for path, path_item in base_paths.items():
        cur_path_item = cur_paths.get(path)
        if cur_path_item is None:
            violations.append(f"path removed: {path}")
            continue
        for method, op in (path_item or {}).items():
            if method not in HTTP_METHODS:
                continue
            cur_op = (cur_path_item or {}).get(method)
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
            newly_required = _request_required(current, cur_op) - _request_required(
                baseline, op
            )
            for field in sorted(newly_required):
                violations.append(
                    f"new required request field: {method.upper()} {path} field '{field}'"
                )
            base_body = op.get("requestBody") or {}
            cur_body = cur_op.get("requestBody") or {}
            if not base_body.get("required") and cur_body.get("required"):
                violations.append(f"request body became required: {method.upper()} {path}")
            base_media = set((base_body.get("content") or {}).keys())
            cur_media = set((cur_body.get("content") or {}).keys())
            for media_type in sorted(base_media - cur_media):
                violations.append(
                    f"request media type lost: {method.upper()} {path} media '{media_type}'"
                )

            base_params = _parameters_by_key(
                baseline,
                (path_item or {}).get("parameters"),
                op.get("parameters"),
            )
            cur_params = _parameters_by_key(
                current,
                (cur_path_item or {}).get("parameters"),
                cur_op.get("parameters"),
            )
            for key in sorted(set(base_params) - set(cur_params)):
                violations.append(
                    f"parameter removed: {method.upper()} {path} {key[1]} '{key[0]}'"
                )
            for key, cur_param in cur_params.items():
                base_param = base_params.get(key)
                if cur_param.get("required") and not (
                    base_param and base_param.get("required")
                ):
                    violations.append(
                        f"new required parameter: {method.upper()} {path} "
                        f"{key[1]} '{key[0]}'"
                    )
                if base_param is None:
                    continue
                base_schema = base_param.get("schema") or {}
                cur_schema = cur_param.get("schema") or {}
                base_type = base_schema.get("type")
                cur_type = cur_schema.get("type")
                if base_type and cur_type != base_type:
                    violations.append(
                        f"parameter type changed: {method.upper()} {path} "
                        f"{key[1]} '{key[0]}' '{base_type}' -> '{cur_type}'"
                    )
                base_enum = set(base_schema.get("enum") or [])
                cur_enum = set(cur_schema.get("enum") or [])
                for value in sorted(base_enum - cur_enum, key=repr):
                    violations.append(
                        f"parameter enum value lost: {method.upper()} {path} "
                        f"{key[1]} '{key[0]}' value {value!r}"
                    )
            base_codes = set((op.get("responses") or {}).keys())
            cur_codes = set((cur_op.get("responses") or {}).keys())
            for code in sorted(base_codes - cur_codes):
                violations.append(
                    f"response status lost: {method.upper()} {path} status '{code}'"
                )
            for code in sorted(base_codes & cur_codes):
                base_response = (op.get("responses") or {}).get(code) or {}
                cur_response = (cur_op.get("responses") or {}).get(code) or {}
                violations.extend(
                    _response_schema_violations(
                        baseline,
                        current,
                        base_response,
                        cur_response,
                        label=f"{method.upper()} {path} status '{code}'",
                    )
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
        if method in HTTP_METHODS
    )
    return {"paths": len(paths), "operations": operations}


def _operation_id_map(spec: dict) -> dict[str, str | None]:
    """Return the public method/path -> operationId mapping."""
    result: dict[str, str | None] = {}
    for path, methods in (spec.get("paths") or {}).items():
        for method, operation in (methods or {}).items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            result[f"{method.upper()} {path}"] = operation.get("operationId")
    return result


def _operation_id_drift(
    expected: dict[str, str | None], observed: dict[str, str | None]
) -> list[str]:
    drift: list[str] = []
    for key in sorted(set(expected) | set(observed)):
        if expected.get(key) != observed.get(key):
            drift.append(
                f"operationId is process-dependent: {key} "
                f"'{expected.get(key)}' -> '{observed.get(key)}'"
            )
    return drift


def _subprocess_openapi(seed: str) -> dict:
    """Export the real app in a fresh interpreter with a fixed hash seed."""
    script = (
        "import json, pathlib, sys; "
        "from src.main import create_app; "
        "pathlib.Path(sys.argv[1]).write_text("
        "json.dumps(create_app().openapi(), sort_keys=True), encoding='utf-8')"
    )
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = seed
    with tempfile.TemporaryDirectory(prefix="openapi-determinism-") as tmp:
        output = Path(tmp) / "openapi.json"
        process = subprocess.run(
            [sys.executable, "-c", script, str(output)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if process.returncode != 0 or not output.is_file():
            detail = (process.stderr or process.stdout).strip()[-1000:]
            raise RuntimeError(
                f"OpenAPI export failed under PYTHONHASHSEED={seed}: {detail or 'no output'}"
            )
        return json.loads(output.read_text(encoding="utf-8"))


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
    current_ids = _operation_id_map(current)
    determinism_violations: list[str] = []
    try:
        for seed in ("1", "2"):
            seeded_ids = _operation_id_map(_subprocess_openapi(seed))
            determinism_violations.extend(_operation_id_drift(current_ids, seeded_ids))
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"GATE ERROR: cannot verify OpenAPI determinism: {exc}", file=sys.stderr)
        return 2
    violations.extend(sorted(set(determinism_violations)))
    base_stats = _spec_stats(baseline)
    cur_stats = _spec_stats(current)
    evidence = {
        "gate": "verify-openapi-contract",
        "tier": "L1-offline-in-process",
        "baseline": str(baseline_path.relative_to(ROOT)),
        "baseline_stats": base_stats,
        "current_stats": cur_stats,
        "determinism_hash_seeds": [1, 2],
        "operation_ids_checked": len(current_ids),
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
                    "properties": {
                        "name": {"type": "string"},
                        "kind": {"type": "string"},
                        "note": {"type": "string"},
                    },
                },
                "ThingResponse": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                    },
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
                    "responses": {
                        "201": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ThingResponse"}
                                }
                            }
                        },
                        "422": {},
                    },
                },
                "get": {
                    "operationId": "list_things",
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "enum": [10, 20]},
                        }
                    ],
                    "responses": {"200": {}},
                },
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

    variant("unchanged spec passes", False, lambda _cur: None)

    def drop_path(cur: dict) -> None:
        del cur["paths"]["/things/{id}"]

    variant("removed path fails", True, drop_path)

    def drop_method(cur: dict) -> None:
        del cur["paths"]["/things"]["get"]

    variant("removed method fails", True, drop_method)

    def lose_required(cur: dict) -> None:
        cur["components"]["schemas"]["CreateThing"]["required"] = ["name"]

    variant("required request field becoming optional passes", False, lose_required)

    def add_required(cur: dict) -> None:
        cur["components"]["schemas"]["CreateThing"]["required"].append("note")

    variant("new required request field fails", True, add_required)

    def rename_op(cur: dict) -> None:
        cur["paths"]["/things"]["post"]["operationId"] = "make_thing"

    variant("changed operationId fails", True, rename_op)

    def drop_status(cur: dict) -> None:
        del cur["paths"]["/things"]["post"]["responses"]["422"]

    variant("removed response status fails", True, drop_status)

    def drop_parameter(cur: dict) -> None:
        cur["paths"]["/things"]["get"]["parameters"] = []

    variant("removed parameter fails", True, drop_parameter)

    def require_parameter(cur: dict) -> None:
        cur["paths"]["/things"]["get"]["parameters"][0]["required"] = True

    variant("optional parameter becoming required fails", True, require_parameter)

    def narrow_parameter_enum(cur: dict) -> None:
        cur["paths"]["/things"]["get"]["parameters"][0]["schema"]["enum"] = [10]

    variant("parameter enum narrowing fails", True, narrow_parameter_enum)

    def drop_response_field(cur: dict) -> None:
        del cur["components"]["schemas"]["ThingResponse"]["properties"]["name"]

    variant("removed response field fails", True, drop_response_field)

    def drop_scheme(cur: dict) -> None:
        cur["components"]["securitySchemes"] = {}

    variant("removed securityScheme fails", True, drop_scheme)

    def compatible_growth(cur: dict) -> None:
        cur["paths"]["/things/new"] = {"get": {"operationId": "new_thing", "responses": {"200": {}}}}
        cur["components"]["schemas"]["CreateThing"]["properties"]["extra"] = {"type": "string"}
        cur["paths"]["/things"]["post"]["responses"]["202"] = {}

    variant("compatible growth passes", False, compatible_growth)

    stable_ids = _operation_id_map(baseline)
    changed_ids = dict(stable_ids)
    changed_ids["POST /things"] = "hash_seed_dependent_id"
    id_drift = _operation_id_drift(stable_ids, changed_ids)

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

    if len(id_drift) != 1 or "POST /things" not in id_drift[0]:
        failures += 1
        print("[FAIL] process-dependent operationId drift was not detected")
    else:
        print("[ok] process-dependent operationId drift is detected")

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
