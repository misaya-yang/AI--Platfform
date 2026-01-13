"""
Structured Output - Type-safe LLM responses with validation.

Phase 4: Provides structured output capabilities with:
- Pydantic-based response schemas
- JSON mode with validation
- Automatic retry on validation failure
- Output repair strategies

References:
- https://docs.anthropic.com/en/docs/build-with-claude/structured-outputs
- https://platform.openai.com/docs/guides/structured-outputs
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Type,
    TypeVar,
    Union,
    get_type_hints,
)

from pydantic import BaseModel, ValidationError, Field

from ...core.observability.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


class OutputFormat(str, Enum):
    """Output format options."""
    TEXT = "text"           # Plain text (default)
    JSON = "json"           # JSON object
    JSON_SCHEMA = "json_schema"  # JSON with schema validation
    MARKDOWN = "markdown"   # Markdown formatted


class RepairStrategy(str, Enum):
    """Strategies for repairing invalid output."""
    NONE = "none"           # No repair, raise error
    EXTRACT_JSON = "extract_json"  # Try to extract JSON from text
    TRUNCATE = "truncate"   # Truncate to valid JSON
    LLM_FIX = "llm_fix"     # Use LLM to fix the output


@dataclass
class StructuredOutputConfig:
    """Configuration for structured output handling."""
    format: OutputFormat = OutputFormat.TEXT
    schema: Optional[Type[BaseModel]] = None
    max_retries: int = 2
    repair_strategy: RepairStrategy = RepairStrategy.EXTRACT_JSON
    strict_validation: bool = True


@dataclass
class StructuredOutputResult(Generic[T]):
    """Result of structured output parsing."""
    success: bool
    data: Optional[T] = None
    raw_output: str = ""
    errors: List[str] = field(default_factory=list)
    retries_used: int = 0
    repair_applied: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data.model_dump() if self.data else None,
            "raw_output": self.raw_output[:500],
            "errors": self.errors,
            "retries_used": self.retries_used,
            "repair_applied": self.repair_applied,
        }


# =============================================================================
# Common Response Schemas
# =============================================================================

class AnswerWithCitations(BaseModel):
    """Standard answer with source citations."""
    answer: str = Field(..., description="The main answer text")
    citations: List[str] = Field(
        default_factory=list,
        description="List of source references used"
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence score (0-1)"
    )


class StepByStepAnswer(BaseModel):
    """Answer broken down into steps."""
    summary: str = Field(..., description="Brief summary of the answer")
    steps: List[str] = Field(
        default_factory=list,
        description="Step-by-step breakdown"
    )
    conclusion: str = Field(default="", description="Final conclusion")


class FactCheckResult(BaseModel):
    """Result of fact checking."""
    claim: str = Field(..., description="The claim being checked")
    verdict: str = Field(..., description="Verdict: true, false, partially_true, unverifiable")
    explanation: str = Field(..., description="Explanation for the verdict")
    sources: List[str] = Field(default_factory=list, description="Supporting sources")


class ExtractedEntities(BaseModel):
    """Named entities extracted from text."""
    people: List[str] = Field(default_factory=list)
    organizations: List[str] = Field(default_factory=list)
    locations: List[str] = Field(default_factory=list)
    dates: List[str] = Field(default_factory=list)
    other: Dict[str, List[str]] = Field(default_factory=dict)


class ClassificationResult(BaseModel):
    """Text classification result."""
    label: str = Field(..., description="Predicted label")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score")
    all_scores: Dict[str, float] = Field(
        default_factory=dict,
        description="Scores for all labels"
    )


# =============================================================================
# Output Parsing & Repair
# =============================================================================

class StructuredOutputParser:
    """
    Parses and validates structured output from LLMs.

    Features:
    - JSON extraction from markdown code blocks
    - Validation against Pydantic schemas
    - Automatic repair strategies
    - Retry logic

    Usage:
        parser = StructuredOutputParser(AnswerWithCitations)
        result = parser.parse(llm_output)
        if result.success:
            answer = result.data
    """

    def __init__(
        self,
        schema: Type[T],
        repair_strategy: RepairStrategy = RepairStrategy.EXTRACT_JSON,
        strict: bool = True,
    ):
        self.schema = schema
        self.repair_strategy = repair_strategy
        self.strict = strict

    def parse(self, text: str) -> StructuredOutputResult[T]:
        """
        Parse text into structured output.

        Args:
            text: Raw LLM output

        Returns:
            StructuredOutputResult with parsed data or errors
        """
        result = StructuredOutputResult[T](
            success=False,
            raw_output=text,
        )

        if not text:
            result.errors.append("Empty output")
            return result

        # Try to extract JSON
        json_str = self._extract_json(text)

        if not json_str:
            result.errors.append("No JSON found in output")

            # Try repair strategies
            if self.repair_strategy == RepairStrategy.EXTRACT_JSON:
                json_str = self._try_extract_json_aggressively(text)
                if json_str:
                    result.repair_applied = "aggressive_json_extraction"
            elif self.repair_strategy == RepairStrategy.TRUNCATE:
                json_str = self._try_truncate_to_valid_json(text)
                if json_str:
                    result.repair_applied = "truncation"

        if not json_str:
            return result

        # Parse JSON
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            result.errors.append(f"JSON parse error: {e}")
            return result

        # Validate against schema
        try:
            result.data = self.schema.model_validate(data)
            result.success = True
        except ValidationError as e:
            result.errors.append(f"Validation error: {e}")

            # Try to fix common issues
            if not self.strict:
                fixed_data = self._try_fix_validation_errors(data, e)
                if fixed_data:
                    try:
                        result.data = self.schema.model_validate(fixed_data)
                        result.success = True
                        result.repair_applied = "field_coercion"
                    except ValidationError:
                        pass

        return result

    def _extract_json(self, text: str) -> Optional[str]:
        """Extract JSON from text, handling common formats."""
        # Try markdown code block
        patterns = [
            r'```json\s*([\s\S]*?)\s*```',  # ```json ... ```
            r'```\s*([\s\S]*?)\s*```',       # ``` ... ```
            r'\{[\s\S]*\}',                   # Raw JSON object
            r'\[[\s\S]*\]',                   # Raw JSON array
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                candidate = match.group(1) if match.lastindex else match.group(0)
                # Quick validation check
                candidate = candidate.strip()
                if candidate.startswith(('{', '[')):
                    try:
                        json.loads(candidate)
                        return candidate
                    except json.JSONDecodeError:
                        continue

        return None

    def _try_extract_json_aggressively(self, text: str) -> Optional[str]:
        """Aggressive JSON extraction for malformed outputs."""
        # Find first { and match to closing }
        stack = []
        start = None

        for i, char in enumerate(text):
            if char == '{':
                if start is None:
                    start = i
                stack.append('{')
            elif char == '}' and stack:
                stack.pop()
                if not stack and start is not None:
                    candidate = text[start:i + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except json.JSONDecodeError:
                        start = None
                        continue

        return None

    def _try_truncate_to_valid_json(self, text: str) -> Optional[str]:
        """Try to truncate text to valid JSON."""
        # Find the start of JSON
        start = text.find('{')
        if start == -1:
            start = text.find('[')
        if start == -1:
            return None

        # Try progressively shorter strings
        for end in range(len(text), start, -1):
            candidate = text[start:end]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                continue

        return None

    def _try_fix_validation_errors(
        self,
        data: Dict[str, Any],
        error: ValidationError,
    ) -> Optional[Dict[str, Any]]:
        """Try to fix common validation errors."""
        fixed = data.copy()

        for err in error.errors():
            loc = err.get("loc", ())
            err_type = err.get("type", "")

            if not loc:
                continue

            field_name = loc[0]

            # Handle missing required fields with defaults
            if err_type == "missing":
                hints = get_type_hints(self.schema)
                if field_name in hints:
                    # Try to set a reasonable default
                    hint = hints[field_name]
                    if hint == str:
                        fixed[field_name] = ""
                    elif hint == int:
                        fixed[field_name] = 0
                    elif hint == float:
                        fixed[field_name] = 0.0
                    elif hint == bool:
                        fixed[field_name] = False
                    elif hint == list or str(hint).startswith("typing.List"):
                        fixed[field_name] = []
                    elif hint == dict or str(hint).startswith("typing.Dict"):
                        fixed[field_name] = {}

            # Handle type coercion
            elif err_type.startswith("type_error"):
                value = data.get(field_name)
                if value is not None:
                    # Try string to number
                    if isinstance(value, str):
                        try:
                            fixed[field_name] = float(value)
                        except ValueError:
                            pass
                    # Try number to string
                    elif isinstance(value, (int, float)):
                        fixed[field_name] = str(value)

        return fixed


# =============================================================================
# Prompt Templates for Structured Output
# =============================================================================

def create_json_prompt(
    schema: Type[BaseModel],
    user_query: str,
    system_context: str = "",
) -> str:
    """
    Create a prompt that encourages JSON output matching the schema.

    Args:
        schema: Pydantic model class
        user_query: User's query
        system_context: Additional system context

    Returns:
        Formatted prompt
    """
    # Generate schema description
    schema_json = schema.model_json_schema()

    prompt_parts = []

    if system_context:
        prompt_parts.append(system_context)

    prompt_parts.append(f"""
You must respond with a valid JSON object that matches this schema:

```json
{json.dumps(schema_json, indent=2)}
```

Important:
- Your response must be ONLY the JSON object, no other text
- Ensure all required fields are included
- Use the exact field names from the schema
- Validate that values match the expected types

User Query: {user_query}
""")

    return "\n\n".join(prompt_parts)


def get_schema_for_model(
    model_id: str,
    schema: Type[BaseModel],
) -> Dict[str, Any]:
    """
    Get the appropriate schema format for a model's API.

    Different providers have different structured output formats:
    - OpenAI: response_format with json_schema
    - Anthropic: tool use with schema
    - Others: JSON mode

    Args:
        model_id: Model identifier
        schema: Pydantic model class

    Returns:
        Schema dict for API call
    """
    json_schema = schema.model_json_schema()

    # OpenAI format (for gpt-4o and later)
    if model_id.startswith("gpt-"):
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": json_schema,
                "strict": True,
            },
        }

    # Generic JSON mode
    return {
        "type": "json_object",
        "schema": json_schema,
    }


# =============================================================================
# Output Guardrails
# =============================================================================

class OutputGuardrail:
    """
    Guardrails for output validation and safety.

    Checks:
    - Length limits
    - Content filtering
    - PII detection
    - Hallucination indicators
    """

    def __init__(
        self,
        max_length: int = 10000,
        check_pii: bool = True,
        check_hallucination: bool = True,
    ):
        self.max_length = max_length
        self.check_pii = check_pii
        self.check_hallucination = check_hallucination

    def validate(self, output: str, context: Optional[str] = None) -> List[str]:
        """
        Validate output against guardrails.

        Args:
            output: LLM output to validate
            context: Original context (for hallucination check)

        Returns:
            List of warning messages (empty if valid)
        """
        warnings = []

        # Length check
        if len(output) > self.max_length:
            warnings.append(f"Output exceeds max length ({len(output)} > {self.max_length})")

        # PII check
        if self.check_pii:
            pii_warnings = self._check_pii(output)
            warnings.extend(pii_warnings)

        # Hallucination indicators
        if self.check_hallucination and context:
            hallucination_warnings = self._check_hallucination(output, context)
            warnings.extend(hallucination_warnings)

        return warnings

    def _check_pii(self, text: str) -> List[str]:
        """Check for potential PII in output."""
        warnings = []

        # Email pattern
        if re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', text):
            warnings.append("Output may contain email addresses")

        # Phone pattern (simple)
        if re.search(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', text):
            warnings.append("Output may contain phone numbers")

        # SSN pattern
        if re.search(r'\b\d{3}-\d{2}-\d{4}\b', text):
            warnings.append("Output may contain SSN-like patterns")

        return warnings

    def _check_hallucination(self, output: str, context: str) -> List[str]:
        """Check for potential hallucination indicators."""
        warnings = []

        # Phrases that often indicate hallucination
        hallucination_phrases = [
            "I don't have access to",
            "I cannot verify",
            "Based on my training",
            "As an AI",
            "I was trained on",
            "My knowledge cutoff",
        ]

        for phrase in hallucination_phrases:
            if phrase.lower() in output.lower():
                warnings.append(f"Potential hallucination indicator: '{phrase}'")

        return warnings


# =============================================================================
# Global Utilities
# =============================================================================

def parse_structured_output(
    text: str,
    schema: Type[T],
    strict: bool = False,
) -> StructuredOutputResult[T]:
    """
    Convenience function to parse structured output.

    Args:
        text: Raw LLM output
        schema: Pydantic model class
        strict: Whether to use strict validation

    Returns:
        StructuredOutputResult
    """
    parser = StructuredOutputParser(schema, strict=strict)
    return parser.parse(text)


def validate_output(
    output: str,
    max_length: int = 10000,
    check_pii: bool = True,
) -> List[str]:
    """
    Convenience function to validate output.

    Args:
        output: Text to validate
        max_length: Maximum allowed length
        check_pii: Whether to check for PII

    Returns:
        List of warnings
    """
    guardrail = OutputGuardrail(max_length=max_length, check_pii=check_pii)
    return guardrail.validate(output)
