"""
Content Generation Prompts for Enterprise AI Assistant.

These prompts guide document and content creation with clear separation of:
- Guardrails (non-negotiable quality requirements)
- Agent Freedom (autonomous decision space)

Design Philosophy:
- Quality thresholds are guardrails—they must be met
- Style, structure, and approach are agent decisions
- Self-repair capability enables autonomous quality improvement
- Explicit instructions improve Claude 4.x model performance

Prompt Types:
- DOCUMENT_GENERATION_SYSTEM_PROMPT: General document generation
- OUTLINE_GENERATION_PROMPT: Document outline creation
- SECTION_GENERATION_PROMPT: Individual section writing
- REPAIR_PROMPT: Quality issue repair
- PRESENTATION_GENERATION_PROMPT: Slide deck creation
- REPORT_GENERATION_PROMPT: Formal report creation
- EMAIL_GENERATION_PROMPT: Professional email writing
- SUMMARY_GENERATION_PROMPT: Content summarization

References:
- https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-4-best-practices
- https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
"""

from typing import Any

from ..guardrails import BANNED_PHRASES, QUALITY_THRESHOLDS, DocumentType

# =============================================================================
# Document Generation System Prompt
# =============================================================================

DOCUMENT_GENERATION_SYSTEM_PROMPT = """You are a professional enterprise document assistant.

<guardrails>
## Quality Requirements (Non-Negotiable)

These requirements MUST be met. Outputs failing these checks will be rejected:

1. **Minimum Content**: {min_words} words total
2. **Minimum Sections**: {min_sections} distinct sections
3. **Prohibited Expressions**: Avoid vague phrases like {banned_phrases}
4. **Substantive Content**: Every point must include concrete explanation and supporting examples
5. **Completeness**: No placeholder text, ellipses for omission, or "to be continued" markers
</guardrails>

<agent_freedom>
## Your Decision Space

Within the guardrails above, you have full autonomy to decide:

- **Structure**: Document organization, section hierarchy, and flow
- **Argumentation**: Logical frameworks and analytical approaches
- **Evidence**: Selection of examples, case studies, and data points
- **Style**: Tone (formal/conversational), voice (active/passive), vocabulary level
- **Formatting**: Headers, lists, tables, emphasis—whatever serves clarity
</agent_freedom>

<workflow>
## Execution Approach

1. **Analyze**: Parse the request; identify key requirements and constraints
2. **Outline**: Design a comprehensive structure before writing
3. **Generate**: Write each section with appropriate depth and specificity
4. **Verify**: Self-check against quality requirements before output
5. **Repair**: Fix any issues autonomously—do not submit substandard work

Begin work now. Create high-quality, professional content.
</workflow>"""


# =============================================================================
# Outline Generation Prompt
# =============================================================================

OUTLINE_GENERATION_PROMPT = """Create a document outline for the following request:

<request>
{request}
</request>

<context>
Document Type: {doc_type}
</context>

<guardrails>
## Required Structure

- Minimum {min_sections} sections
- Must include: Introduction/Background, Main Content Body, Conclusion/Summary
- Each section title must be clear and descriptive
- Logical flow from section to section
</guardrails>

<agent_freedom>
## Your Decisions

- **Scope**: Total number of sections (above minimum)
- **Framing**: Specific section titles and angles
- **Organization**: Content grouping and sequence logic
- **Depth**: Whether to include subsections, appendices, or references
</agent_freedom>

<output_format>
Respond with valid JSON only (no markdown code blocks):
{{
    "title": "Document Title",
    "sections": ["Section 1 Title", "Section 2 Title", ...]
}}
</output_format>"""


# =============================================================================
# Section Generation Prompt
# =============================================================================

SECTION_GENERATION_PROMPT = """Write the "{section_title}" section for the document "{doc_title}".

<context>
Document Outline: {outline}
Completed Sections: {completed_sections}
</context>

<guardrails>
## Section Requirements

- Minimum {min_words_per_section} words
- Avoid vague expressions: {banned_phrases}
- Content must be specific and substantive
- Include concrete examples or data to support points
- Maintain consistency with previously completed sections
</guardrails>

<agent_freedom>
## Your Decisions

- **Structure**: Argumentation flow and internal organization
- **Evidence**: Examples, case studies, and data to include
- **Style**: Formal vs accessible; technical vs general
- **Format**: Whether to use subsections, lists, or tables
- **Transitions**: How to connect with adjacent sections
</agent_freedom>

<output_instructions>
Output the section content directly. Do not repeat the section title—it will be added automatically.
Write complete, polished content ready for final document.
</output_instructions>"""


# =============================================================================
# Repair Prompt
# =============================================================================

REPAIR_PROMPT = """Your generated content has quality issues that require repair.

<issues>
{issues}
</issues>

<original_content>
{content}
</original_content>

<guardrails>
## Repair Requirements

- Address each identified issue specifically
- Repaired content must pass quality validation
- Maintain coherence with the original's purpose and style
- Do not introduce new issues while fixing existing ones
</guardrails>

<agent_freedom>
## Repair Approach

You decide:
- **Method**: Expand, rewrite, or supplement as appropriate
- **Style**: Whether to adjust or maintain original tone
- **Additions**: What content to add to meet requirements
- **Integration**: How to weave fixes seamlessly into the whole
</agent_freedom>

<output_instructions>
1. Preserve all valuable content from the original
2. Do not remove existing good content unless it conflicts with repairs
3. Ensure the repaired version is more comprehensive
4. Output the complete, repaired content (not just the changes)
</output_instructions>"""


# =============================================================================
# Presentation Generation Prompt
# =============================================================================

PRESENTATION_GENERATION_PROMPT = """Create a professional presentation on the following topic:

<request>
Topic: {topic}
Target Audience: {audience}
Slide Count: {slide_count} slides (approximate)
</request>

<guardrails>
## Presentation Requirements

- Clear visual hierarchy on each slide
- Maximum 6 bullet points per slide
- Each slide must convey a single, clear message
- Include speaker notes with key talking points
- Consistent formatting and terminology throughout
</guardrails>

<agent_freedom>
## Design Decisions

- **Flow**: Slide organization and narrative arc
- **Visuals**: Imagery, charts, and diagram suggestions
- **Density**: Level of detail per slide
- **Balance**: Text vs visual elements ratio
- **Hooks**: Opening and closing approaches
</agent_freedom>

<output_format>
For each slide, provide:

### Slide N: [Title]
**Content:**
- Bullet point 1
- Bullet point 2

**Speaker Notes:**
[Key talking points for this slide]

**Visual Suggestion:**
[Optional: Suggested chart, image, or diagram]
</output_format>"""


# =============================================================================
# Report Generation Prompt
# =============================================================================

REPORT_GENERATION_PROMPT = """Generate a comprehensive report on the following topic:

<request>
Topic: {topic}
Report Type: {report_type}
Target Audience: {audience}
</request>

<guardrails>
## Report Requirements

- Executive summary (max 300 words)
- Clear section structure with descriptive headers
- Data and evidence to support key claims
- Actionable recommendations (if applicable)
- Minimum {min_words} words total
</guardrails>

<agent_freedom>
## Your Decisions

- **Framework**: Analytical approach and methodology
- **Depth**: Level of detail in each section
- **Format**: Data presentation (tables, prose, lists)
- **Tone**: Technical depth appropriate to audience
- **Scope**: Additional sections beyond core requirements
</agent_freedom>

<output_format>
## Expected Structure

1. Executive Summary
2. Introduction/Background
3. Methodology (if applicable)
4. Main Analysis (multiple sections as needed)
5. Findings/Results
6. Recommendations (if applicable)
7. Conclusion
8. References/Sources (if applicable)
</output_format>"""


# =============================================================================
# Email Generation Prompt
# =============================================================================

EMAIL_GENERATION_PROMPT = """Compose a professional email based on the following requirements:

<request>
Purpose: {purpose}
Recipient: {recipient}
Tone: {tone}
Key Points: {key_points}
</request>

<guardrails>
## Email Requirements

- Clear, specific subject line
- Appropriate greeting for the recipient relationship
- Purpose stated within the first two sentences
- Each paragraph focused on a single topic
- Clear call-to-action or next steps (if applicable)
- Professional sign-off
</guardrails>

<agent_freedom>
## Your Decisions

- **Length**: Concise vs detailed based on complexity
- **Structure**: Paragraph organization and flow
- **Formality**: Adjust within the specified tone range
- **Emphasis**: What to highlight and how
- **Closing**: How to end and what follow-up to suggest
</agent_freedom>

<output_format>
**Subject:** [Subject Line]

[Email Body]

[Sign-off]
</output_format>"""


# =============================================================================
# Summary Generation Prompt
# =============================================================================

SUMMARY_GENERATION_PROMPT = """Create a summary of the following content:

<content>
{content}
</content>

<request>
Summary Type: {summary_type}
Target Length: {target_length}
Focus Areas: {focus_areas}
</request>

<guardrails>
## Summary Requirements

- Capture all key points from the original
- Maintain factual accuracy—do not add information not in the source
- Preserve the original's conclusions and recommendations
- Stay within the target length (±10%)
- Use clear, accessible language
</guardrails>

<agent_freedom>
## Your Decisions

- **Structure**: How to organize the summary (chronological, thematic, importance-based)
- **Emphasis**: Which points to feature prominently
- **Abstraction**: Level of detail vs high-level synthesis
- **Framing**: How to introduce and conclude the summary
</agent_freedom>

<output_format>
Provide the summary directly, formatted appropriately for the summary type:
- **Executive**: Brief paragraph with key takeaways
- **Detailed**: Structured with headers for main topics
- **Bullet**: Key points as a bulleted list
</output_format>"""


# =============================================================================
# Prompt Builder Functions
# =============================================================================


def _format_banned_phrases(count: int = 5) -> str:
    """Format banned phrases for display in prompts."""
    banned_display = ", ".join(f'"{p}"' for p in BANNED_PHRASES[:count])
    if len(BANNED_PHRASES) > count:
        banned_display += ", etc."
    return banned_display


def build_generation_prompt(
    doc_type: DocumentType,
    custom_thresholds: dict[str, Any] | None = None,
) -> str:
    """
    Build document generation system prompt with guardrails.

    Args:
        doc_type: Document type (from DocumentType enum)
        custom_thresholds: Override default thresholds

    Returns:
        Formatted system prompt ready for use
    """
    thresholds = QUALITY_THRESHOLDS.get(doc_type, {})
    if custom_thresholds:
        thresholds = {**thresholds, **custom_thresholds}

    min_words = thresholds.get("min_words", thresholds.get("min_words_total", 500))
    min_sections = thresholds.get("min_sections", 4)

    return DOCUMENT_GENERATION_SYSTEM_PROMPT.format(
        min_words=min_words,
        min_sections=min_sections,
        banned_phrases=_format_banned_phrases(5),
    )


def build_outline_prompt(
    request: str,
    doc_type: DocumentType | str,
    custom_thresholds: dict[str, Any] | None = None,
) -> str:
    """
    Build outline generation prompt.

    Args:
        request: User's request describing the document
        doc_type: Document type (DocumentType enum or string)
        custom_thresholds: Override default thresholds

    Returns:
        Formatted outline prompt
    """
    # Handle both enum and string types
    if isinstance(doc_type, DocumentType):
        doc_type_value = doc_type.value
        thresholds = QUALITY_THRESHOLDS.get(doc_type, {})
    else:
        doc_type_value = doc_type
        thresholds = {}

    if custom_thresholds:
        thresholds = {**thresholds, **custom_thresholds}

    min_sections = thresholds.get("min_sections", 4)

    return OUTLINE_GENERATION_PROMPT.format(
        request=request,
        doc_type=doc_type_value,
        min_sections=min_sections,
    )


def build_section_prompt(
    doc_title: str,
    section_title: str,
    outline: list[str],
    completed_sections: list[str],
    doc_type: DocumentType,
) -> str:
    """
    Build section generation prompt.

    Args:
        doc_title: Document title
        section_title: Section to generate
        outline: Full outline as list of section titles
        completed_sections: Already completed section titles
        doc_type: Document type

    Returns:
        Formatted section prompt
    """
    thresholds = QUALITY_THRESHOLDS.get(doc_type, {})
    min_words_per_section = thresholds.get("min_words_per_section", 150)

    return SECTION_GENERATION_PROMPT.format(
        doc_title=doc_title,
        section_title=section_title,
        outline=", ".join(outline),
        completed_sections=", ".join(completed_sections) if completed_sections else "None",
        min_words_per_section=min_words_per_section,
        banned_phrases=_format_banned_phrases(3),
    )


def build_repair_prompt(
    content: str,
    issues: list[dict[str, Any]],
) -> str:
    """
    Build repair prompt for fixing quality issues.

    Args:
        content: Original content that needs repair
        issues: List of quality issues, each with 'severity' and 'message' keys

    Returns:
        Formatted repair prompt
    """
    issues_text = "\n".join(
        f"- [{issue.get('severity', 'warning').upper()}] {issue.get('message', '')}"
        for issue in issues
    )

    return REPAIR_PROMPT.format(
        issues=issues_text,
        content=content,
    )


def build_presentation_prompt(
    topic: str,
    audience: str = "general business audience",
    slide_count: int = 10,
) -> str:
    """
    Build presentation generation prompt.

    Args:
        topic: Presentation topic
        audience: Target audience description
        slide_count: Approximate number of slides

    Returns:
        Formatted presentation prompt
    """
    return PRESENTATION_GENERATION_PROMPT.format(
        topic=topic,
        audience=audience,
        slide_count=slide_count,
    )


def build_report_prompt(
    topic: str,
    report_type: str = "analytical",
    audience: str = "business stakeholders",
    min_words: int = 1500,
) -> str:
    """
    Build report generation prompt.

    Args:
        topic: Report topic
        report_type: Type of report (analytical, executive, technical, research)
        audience: Target audience description
        min_words: Minimum word count

    Returns:
        Formatted report prompt
    """
    return REPORT_GENERATION_PROMPT.format(
        topic=topic,
        report_type=report_type,
        audience=audience,
        min_words=min_words,
    )


def build_email_prompt(
    purpose: str,
    recipient: str,
    tone: str = "professional",
    key_points: list[str] | None = None,
) -> str:
    """
    Build email generation prompt.

    Args:
        purpose: Purpose of the email (e.g., "request meeting", "follow up on proposal")
        recipient: Description of recipient (e.g., "client", "manager", "team")
        tone: Email tone (professional, formal, friendly, urgent)
        key_points: List of key points to include

    Returns:
        Formatted email prompt
    """
    key_points_str = (
        "\n".join(f"- {point}" for point in key_points) if key_points else "None specified"
    )

    return EMAIL_GENERATION_PROMPT.format(
        purpose=purpose,
        recipient=recipient,
        tone=tone,
        key_points=key_points_str,
    )


def build_summary_prompt(
    content: str,
    summary_type: str = "executive",
    target_length: str = "200-300 words",
    focus_areas: list[str] | None = None,
) -> str:
    """
    Build summary generation prompt.

    Args:
        content: Content to summarize
        summary_type: Type of summary (executive, detailed, bullet)
        target_length: Target length description
        focus_areas: Specific areas to focus on

    Returns:
        Formatted summary prompt
    """
    focus_str = ", ".join(focus_areas) if focus_areas else "All key points"

    return SUMMARY_GENERATION_PROMPT.format(
        content=content,
        summary_type=summary_type,
        target_length=target_length,
        focus_areas=focus_str,
    )
