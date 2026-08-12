"""Bounded structured-output contracts for sub-agent terminal receipts.

The model receives a deliberately small, local-only JSON Schema subset.  The
host is authoritative: it parses exact JSON, rejects duplicate keys and
non-finite numbers, validates formats, and never resolves remote references.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

MAX_OUTPUT_SCHEMA_BYTES = 16 * 1024
MAX_OUTPUT_SCHEMA_DEPTH = 8
MAX_OUTPUT_SCHEMA_NODES = 256
MAX_STRUCTURED_OUTPUT_BYTES = 64 * 1024
MAX_VALIDATION_ERRORS = 8

_SCHEMA_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_FORMATS = frozenset(
    {"date", "date-time", "email", "hostname", "ipv4", "ipv6", "time", "uri", "uuid"}
)
_ALLOWED_SCHEMA_KEYS = frozenset(
    {
        "type",
        "description",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "enum",
        "const",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "format",
    }
)


class OutputContractError(ValueError):
    """A schema or candidate violated the bounded structured-output contract."""


@dataclass(frozen=True)
class StructuredOutput:
    """A host-parsed and schema-validated model payload."""

    payload: dict[str, Any]
    canonical_json: str


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _bounded_json_tree(value: Any, *, label: str, max_bytes: int) -> None:
    try:
        encoded = _canonical_json(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OutputContractError(f"{label} must be finite JSON data") from exc
    if len(encoded) > max_bytes:
        raise OutputContractError(f"{label} exceeds {max_bytes} bytes")

    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_OUTPUT_SCHEMA_NODES:
            raise OutputContractError(f"{label} exceeds {MAX_OUTPUT_SCHEMA_NODES} JSON nodes")
        if depth > MAX_OUTPUT_SCHEMA_DEPTH:
            raise OutputContractError(f"{label} exceeds depth {MAX_OUTPUT_SCHEMA_DEPTH}")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise OutputContractError(f"{label} object keys must be strings")
            for child in item.values():
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, float) and not math.isfinite(item):
            raise OutputContractError(f"{label} must not contain non-finite numbers")
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise OutputContractError(f"{label} contains non-JSON data")

    visit(value, 1)


def _validate_schema_node(schema: Any, *, path: str) -> None:
    if not isinstance(schema, dict):
        raise OutputContractError(f"{path} must be a JSON Schema object")
    unsupported = sorted(set(schema).difference(_ALLOWED_SCHEMA_KEYS))
    if unsupported:
        raise OutputContractError(f"{path} uses unsupported keyword(s): {', '.join(unsupported)}")

    schema_type = schema.get("type")
    if schema_type is not None and (
        not isinstance(schema_type, str) or schema_type not in _SCHEMA_TYPES
    ):
        raise OutputContractError(f"{path}.type must be one supported JSON type")
    description = schema.get("description")
    if description is not None and not isinstance(description, str):
        raise OutputContractError(f"{path}.description must be a string")
    schema_format = schema.get("format")
    if schema_format is not None and (
        not isinstance(schema_format, str) or schema_format not in _FORMATS
    ):
        raise OutputContractError(f"{path}.format is not in the supported format set")

    properties = schema.get("properties")
    if schema_type == "object" or properties is not None:
        if not isinstance(properties, dict):
            raise OutputContractError(f"{path}.properties must be an object")
        if schema.get("additionalProperties") is not False:
            raise OutputContractError(
                f"{path}.additionalProperties must be false for strict objects"
            )
        required = schema.get("required", [])
        if (
            not isinstance(required, list)
            or any(not isinstance(name, str) for name in required)
            or len(required) != len(set(required))
            or not set(required).issubset(properties)
        ):
            raise OutputContractError(
                f"{path}.required must be unique property names declared in properties"
            )
        for name, child in properties.items():
            if not name:
                raise OutputContractError(f"{path}.properties contains an empty name")
            _validate_schema_node(child, path=f"{path}.properties[{name!r}]")
    elif "additionalProperties" in schema or "required" in schema:
        raise OutputContractError(
            f"{path} may use properties/required/additionalProperties only for objects"
        )

    if schema_type == "array" or "items" in schema:
        if schema_type != "array" or "items" not in schema:
            raise OutputContractError(f"{path} array schemas require type and items")
        _validate_schema_node(schema["items"], path=f"{path}.items")

    for keyword in ("anyOf", "oneOf", "allOf"):
        children = schema.get(keyword)
        if children is None:
            continue
        if not isinstance(children, list) or not 1 <= len(children) <= 8:
            raise OutputContractError(f"{path}.{keyword} must contain 1 to 8 schemas")
        for index, child in enumerate(children):
            _validate_schema_node(child, path=f"{path}.{keyword}[{index}]")
    if "not" in schema:
        _validate_schema_node(schema["not"], path=f"{path}.not")


def normalize_output_schema(value: Any) -> dict[str, Any] | None:
    """Validate and copy a local, bounded strict JSON Schema."""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise OutputContractError("output_schema must be an object")
    _bounded_json_tree(
        value,
        label="output_schema",
        max_bytes=MAX_OUTPUT_SCHEMA_BYTES,
    )
    _validate_schema_node(value, path="output_schema")
    if value.get("type") != "object":
        raise OutputContractError("output_schema root type must be object")
    try:
        Draft202012Validator.check_schema(value)
    except SchemaError as exc:
        raise OutputContractError(f"output_schema is invalid: {exc.message}") from exc
    return copy.deepcopy(value)


def output_schema_prompt(schema: dict[str, Any]) -> str:
    """Return the already-bounded canonical schema text for a model prompt."""

    normalized = normalize_output_schema(schema)
    if normalized is None:  # pragma: no cover - narrowed by the public signature
        raise OutputContractError("output_schema is required")

    # Descriptions are not validation semantics. Excluding them from the
    # privileged system block prevents model-proposed prose from becoming an
    # instruction channel while retaining the exact host-side schema.
    def without_descriptions(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: without_descriptions(child)
                for key, child in value.items()
                if key != "description"
            }
        if isinstance(value, list):
            return [without_descriptions(child) for child in value]
        return value

    return _canonical_json(without_descriptions(normalized))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OutputContractError(f"structured output contains duplicate key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise OutputContractError(f"structured output contains non-finite number: {value}")


def parse_structured_output(
    text: str,
    schema: dict[str, Any],
) -> tuple[StructuredOutput | None, list[str]]:
    """Parse exact JSON and validate it without resolving external references."""

    try:
        normalized = normalize_output_schema(schema)
        if normalized is None:  # pragma: no cover - narrowed by the public signature
            raise OutputContractError("output_schema is required")
        if not isinstance(text, str) or not text.strip():
            raise OutputContractError("structured output is empty")
        if len(text.encode("utf-8")) > MAX_STRUCTURED_OUTPUT_BYTES:
            raise OutputContractError(
                f"structured output exceeds {MAX_STRUCTURED_OUTPUT_BYTES} bytes"
            )
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        if not isinstance(payload, dict):
            raise OutputContractError("structured output root must be an object")
    except (json.JSONDecodeError, OutputContractError) as exc:
        return None, [str(exc)]

    validator = Draft202012Validator(normalized, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.absolute_path))
    if errors:
        messages: list[str] = []
        for error in errors[:MAX_VALIDATION_ERRORS]:
            location = "$"
            for part in error.absolute_path:
                location += f"[{part}]" if isinstance(part, int) else f".{part}"
            validator_name = str(error.validator or "schema")
            if validator_name == "required":
                detail = "required property is missing"
            elif validator_name == "additionalProperties":
                detail = "undeclared properties are not allowed"
            elif validator_name == "type":
                detail = f"expected type {error.validator_value}"
            elif validator_name == "enum":
                detail = "value is not in the allowed enum"
            elif validator_name == "const":
                detail = "value does not match the required constant"
            elif validator_name == "format":
                detail = f"format constraint failed ({error.validator_value})"
            else:
                detail = f"{validator_name} constraint failed"
            messages.append(f"{location}: {detail}")
        if len(errors) > MAX_VALIDATION_ERRORS:
            messages.append(
                f"{len(errors) - MAX_VALIDATION_ERRORS} additional validation error(s) omitted"
            )
        return None, messages
    return StructuredOutput(payload=payload, canonical_json=_canonical_json(payload)), []


def correction_prompt(errors: list[str]) -> str:
    """Build a bounded repair request that never repeats the invalid payload."""

    bounded = [" ".join(str(error).split())[:500] for error in errors[:MAX_VALIDATION_ERRORS]]
    details = "\n".join(f"- {error}" for error in bounded) or "- invalid JSON output"
    return (
        "Your previous response failed the host-enforced structured-output contract. "
        "Return one corrected JSON object only: no Markdown fence, prose, or tool call. "
        "Do not add undeclared properties. Validation errors:\n"
        f"{details}"
    )
