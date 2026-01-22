"""
Scenario Analysis Prompts for Enterprise AI Assistant.

These prompts enable intelligent scenario-aware responses with advanced
context engineering principles from Manus and Anthropic best practices.

Core Capabilities:
1. Scenario Detection - Classify user intent with confidence scoring
2. Multi-dimensional Analysis - Expert-level domain analysis
3. Knowledge Integration - RAG-enhanced responses with source attribution
4. Document Analysis - Deep understanding of uploaded content

Design Philosophy (Manus Context Engineering):
- KV-Cache Optimization: Static sections first, dynamic content appended
- Attention Anchoring: Repeat key objectives to maintain focus
- Error Retention: Keep failed attempts visible for implicit learning
- Anti-Pattern Overfitting: Introduce structural variation to prevent mimicry
- Grounding Protocol: Every claim must trace to retrieved sources

Architecture:
- SCENARIO_TYPES: Static metadata for all scenario types (cache-friendly)
- Detection Prompts: Lightweight classification with confidence thresholds
- Analysis Prompts: Deep analysis with source attribution requirements
- Expert Templates: Domain-specific response structures

References:
- https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
- https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-4-best-practices
"""

from typing import Any, Dict, List, Optional, Tuple


# =============================================================================
# Scenario Types Definition (KV-Cache Friendly - Static Metadata)
# =============================================================================

# NOTE: This dictionary is intentionally comprehensive and static to maximize
# KV-cache hits. Do not add dynamic content (timestamps, session IDs, etc.)
# Reference: https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus

SCENARIO_TYPES = {
    "customer_service": {
        "name": "Customer Service",
        "description": "Handling customer complaints, issue resolution, service inquiries",
        "keywords": ["complaint", "issue", "problem", "feedback", "refund", "support", "help", "broken", "not working"],
        "analysis_dimensions": ["Issue Diagnosis", "Empathy Response", "Solution Options", "Prevention"],
        # Manus-inspired enhancements
        "urgency_weight": 0.8,  # Higher = more likely to be urgent
        "tool_affinity": ["kb_search", "ticket_create", "escalation_check"],
        "retrieval_strategy": "semantic_first",  # semantic_first | keyword_first | hybrid
        "confidence_threshold": 0.7,  # Minimum confidence for auto-classification
    },
    "sales_consultation": {
        "name": "Sales Consultation",
        "description": "Product recommendations, pricing inquiries, promotions, purchase decisions",
        "keywords": ["buy", "purchase", "price", "cost", "discount", "recommend", "compare", "budget", "deal"],
        "analysis_dimensions": ["Needs Analysis", "Product Match", "Value Proposition", "Purchase Guidance"],
        "urgency_weight": 0.5,
        "tool_affinity": ["kb_search", "product_catalog", "pricing_lookup"],
        "retrieval_strategy": "hybrid",
        "confidence_threshold": 0.7,
    },
    "technical_support": {
        "name": "Technical Support",
        "description": "Technical issues, troubleshooting, configuration, setup assistance",
        "keywords": ["how to", "error", "cannot", "configure", "install", "setup", "upgrade", "fix", "not working"],
        "analysis_dimensions": ["Problem Identification", "Root Cause Analysis", "Step-by-Step Solution", "Verification"],
        "urgency_weight": 0.7,
        "tool_affinity": ["kb_search", "documentation_search", "code_executor"],
        "retrieval_strategy": "keyword_first",  # Technical queries often have specific terms
        "confidence_threshold": 0.75,
    },
    "product_inquiry": {
        "name": "Product Inquiry",
        "description": "Product features, specifications, use cases, comparisons",
        "keywords": ["feature", "capability", "specification", "support", "compatible", "difference", "version"],
        "analysis_dimensions": ["Feature Overview", "Use Cases", "Technical Specs", "Selection Guidance"],
        "urgency_weight": 0.3,
        "tool_affinity": ["kb_search", "product_catalog", "comparison_tool"],
        "retrieval_strategy": "hybrid",
        "confidence_threshold": 0.7,
    },
    "policy_inquiry": {
        "name": "Policy Inquiry",
        "description": "Company policies, procedures, compliance requirements, process explanations",
        "keywords": ["policy", "rule", "procedure", "requirement", "compliance", "standard", "approval", "process"],
        "analysis_dimensions": ["Policy Explanation", "Applicability", "Process Steps", "Important Notes"],
        "urgency_weight": 0.4,
        "tool_affinity": ["kb_search", "policy_database", "compliance_checker"],
        "retrieval_strategy": "semantic_first",  # Policies need contextual understanding
        "confidence_threshold": 0.8,  # Higher threshold for policy accuracy
    },
    "data_analysis": {
        "name": "Data Analysis",
        "description": "Data interpretation, trend analysis, report understanding, metrics explanation",
        "keywords": ["data", "report", "metric", "trend", "analysis", "statistics", "compare", "growth", "decline"],
        "analysis_dimensions": ["Data Interpretation", "Trend Analysis", "Causal Factors", "Recommendations"],
        "urgency_weight": 0.4,
        "tool_affinity": ["data_query", "chart_generator", "statistical_analysis"],
        "retrieval_strategy": "keyword_first",  # Data queries often reference specific metrics
        "confidence_threshold": 0.75,
    },
    "general_inquiry": {
        "name": "General Inquiry",
        "description": "General questions, information requests, knowledge queries",
        "keywords": [],  # Fallback scenario - no specific keywords
        "analysis_dimensions": ["Information Summary", "Key Points", "Additional Context", "Related Resources"],
        "urgency_weight": 0.2,
        "tool_affinity": ["kb_search", "web_search"],
        "retrieval_strategy": "semantic_first",
        "confidence_threshold": 0.5,  # Lower threshold as fallback
    },
}


# =============================================================================
# Scenario Detection Prompt (Lightweight Classification)
# =============================================================================

# NOTE: This prompt is designed to be fast and deterministic.
# It uses structured output to maximize cache stability.
# Reference: Manus - "Action space should be masked, not removed"

SCENARIO_DETECTION_PROMPT = """<task>
Classify user intent into predefined scenario types with confidence scoring.
</task>

<user_query>
{user_query}
</user_query>

<scenario_types>
{scenario_types}
</scenario_types>

<classification_protocol>
## Classification Rules

### Step 1: Signal Extraction
- Identify explicit keywords matching scenario definitions
- Detect implicit intent signals (tone, question structure, domain terms)
- Note any ambiguity or conflicting signals

### Step 2: Confidence Scoring
- HIGH (0.8-1.0): Clear keyword match + unambiguous intent
- MEDIUM (0.6-0.79): Partial keyword match OR clear intent without keywords
- LOW (0.4-0.59): Weak signals, multiple possible scenarios
- UNCERTAIN (<0.4): Insufficient signals, recommend clarification

### Step 3: Entity Extraction
- Extract ONLY entities explicitly mentioned in the query
- Do NOT infer or hallucinate entity values
- Mark missing entities as null, not placeholder text

### Step 4: Retrieval Strategy Selection
- `semantic_first`: Use for abstract, conceptual queries
- `keyword_first`: Use for technical, specific-term queries
- `hybrid`: Use for mixed or unclear queries
</classification_protocol>

<anti_hallucination>
## Grounding Requirements

- Base classification ONLY on content present in the query
- If entity is not explicitly mentioned, output `null`
- If scenario is ambiguous, output lower confidence, not a guess
- Do NOT add information not present in the original query
</anti_hallucination>

<output_format>
## Output Specification

Respond with valid JSON only (no markdown code blocks, no explanation):
{{
    "primary_scenario": "scenario_type_code",
    "secondary_scenarios": ["code_1", "code_2"],
    "confidence": 0.0-1.0,
    "confidence_reasoning": "Brief explanation of confidence level",
    "entities": {{
        "product": "extracted product name or null",
        "issue": "extracted issue description or null",
        "customer_need": "extracted core requirement or null",
        "mentioned_terms": ["term1", "term2"]
    }},
    "urgency": {{
        "level": "urgent|normal|low",
        "signals": ["signal that indicated urgency level"]
    }},
    "retrieval_strategy": "semantic_first|keyword_first|hybrid",
    "requires_kb_search": true|false,
    "suggested_kb_queries": ["query1", "query2"],
    "clarification_needed": true|false,
    "clarification_question": "Question to ask if clarification_needed is true, or null"
}}
</output_format>"""


# =============================================================================
# Fast Scenario Detection (Minimal Token Version)
# =============================================================================

# Use this for latency-sensitive scenarios where TTFT matters
FAST_SCENARIO_DETECTION_PROMPT = """Classify this query into one scenario type.

Query: {user_query}

Types: {scenario_codes}

Output JSON only:
{{"scenario": "type_code", "confidence": 0.0-1.0, "kb_search": true|false}}"""


# =============================================================================
# Multi-Dimensional Analysis Prompt (Enhanced with Manus Principles)
# =============================================================================

# NOTE: This prompt implements several Manus context engineering principles:
# 1. Attention Anchoring - Repeats the user goal at the end
# 2. Source Attribution - Requires citation of KB content
# 3. Error Retention - Acknowledges what's NOT in the sources
# Reference: https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus

MULTI_DIMENSIONAL_ANALYSIS_PROMPT = """<role>
You are a senior {scenario_name} expert providing comprehensive, evidence-based analysis.
</role>

<user_query>
{user_query}
</user_query>

<kb_context>
## Retrieved Knowledge Base Content
{kb_context}
</kb_context>

<analysis_framework>
## Required Analysis Dimensions
{analysis_dimensions}
</analysis_framework>

<grounding_protocol>
## Source Attribution Requirements (Non-Negotiable)

### Citation Rules
- Every factual claim MUST cite its source using [^n] footnote notation
- Sources from KB content: [^KB-n] where n is the chunk/document number
- If a claim cannot be traced to a source, explicitly state "Based on general knowledge"
- Do NOT present speculation as fact

### Confidence Calibration
- **High Confidence**: Direct quote or paraphrase from KB content
- **Medium Confidence**: Inference from multiple KB sources
- **Low Confidence**: General knowledge with no KB support (must be labeled)
- **No Basis**: Do not include—state information gap instead

### Information Gaps
- Explicitly note what information is NOT available in the provided context
- Suggest what additional information would strengthen the analysis
- Never fill gaps with plausible-sounding fabrications
</grounding_protocol>

<quality_requirements>
## Response Quality Standards

1. **Evidence-Based**: Every recommendation traceable to source material
2. **Actionable**: Specific next steps, not generic advice
3. **Professional**: Domain-appropriate terminology and tone
4. **Structured**: Clear hierarchy with scannable formatting
5. **Complete**: Address edge cases and limitations
</quality_requirements>

<response_structure>
## Response Structure

### Query Understanding
[Restate the user's core question in your own words to confirm understanding]

{dimension_sections}

### Summary & Recommendations
**Key Actions:**
1. [Most important action with source reference]
2. [Second priority action with source reference]
3. [Additional actions as needed]

**Confidence Assessment**: [Overall confidence in recommendations]

### Source References
[List all sources cited, with brief description of each]

### Information Gaps
[What information was NOT available that would improve this analysis]
</response_structure>

<attention_anchor>
## Reminder: Your Primary Objective
Provide a comprehensive {scenario_name} analysis for: "{user_query}"
Ensure ALL claims cite their sources. Do not hallucinate information.
</attention_anchor>"""


# =============================================================================
# Expert Response Templates (With Source Attribution Requirements)
# =============================================================================

# NOTE: Each template includes [^source] placeholders to enforce citation behavior.
# This prevents hallucination by requiring every claim to trace to a source.
# Structural variation is introduced to prevent pattern overfitting.

EXPERT_TEMPLATES = {
    "customer_service": """### Issue Diagnosis
**Reported Problem**: [Describe the specific issue from the customer's perspective]
**Impact Assessment**: [How this affects the customer - cite support docs if available [^KB-n]]
**Classification**: [Issue category based on KB taxonomy, if applicable]

### Empathy Response
[Acknowledge the customer's experience. Be genuine, not formulaic.]

### Solution Options
**Recommended Solution** [^KB-n]:
1. **Immediate Action**: [What the customer can do right now]
2. **Resolution Path**: [Steps if escalation is needed, with expected timeline]
3. **Alternative Approach**: [Backup option if primary solution doesn't work]

*Source: [Reference to relevant support documentation]*

### Prevention
[How to avoid this issue in the future, based on documented best practices [^KB-n]]

### Confidence Note
[State confidence level: "Based on documented procedures" vs "General guidance"]""",

    "sales_consultation": """### Needs Analysis
**Stated Requirements**: [What the customer explicitly asked for]
**Inferred Needs**: [What they might also need based on context - label as inference]
**Budget Considerations**: [If mentioned, otherwise note "Not specified"]

### Product Recommendation [^KB-n]
| Recommendation | Match Reason | Price Point | Source |
|----------------|--------------|-------------|--------|
| [Product 1] | [Why it fits] | [If available] | [^KB-n] |
| [Product 2] | [Why it fits] | [If available] | [^KB-n] |

### Value Proposition
[Key benefits specific to this customer's stated needs - not generic marketing]

### Purchase Guidance
**Next Steps**: [Specific actions with any relevant links or contacts]
**Timing Considerations**: [If promotions/seasons are relevant, cite source]

### Information Gaps
[What additional information would improve this recommendation]""",

    "technical_support": """### Problem Identification
**Reported Symptoms**: [Exact description from user]
**Environment**: [Platform/version/configuration if mentioned]
**Reproducibility**: [When/how the issue occurs]

### Root Cause Analysis
**Most Likely Cause** [^KB-n]: [Explanation with supporting evidence]
**Alternative Possibilities**:
1. [Second most likely cause] - [Why this might be the case]
2. [Third possibility] - [What would indicate this]

### Solution Steps [^KB-n]
```
Step 1: [Action]
   → Expected: [What should happen]
   → If not: [What to do instead]

Step 2: [Action]
   → Expected: [What should happen]
   → If not: [What to do instead]
```

### Verification
**Success Criteria**: [How to confirm the issue is resolved]
**Rollback Plan**: [How to undo changes if needed]

### Caveats
[Any warnings or edge cases from the documentation]""",

    "product_inquiry": """### Feature Overview [^KB-n]
| Feature | Description | Availability |
|---------|-------------|--------------|
| [Feature 1] | [What it does] | [Version/tier] |
| [Feature 2] | [What it does] | [Version/tier] |

### Differentiators
[What makes this product unique - cite competitive analysis if available]

### Use Cases [^KB-n]
- **Best For**: [Ideal use case with example]
- **Also Good For**: [Secondary use cases]
- **Not Recommended For**: [Limitations or anti-patterns]

### Technical Specifications
[Only include specs that are documented - mark any estimates]

### Selection Guidance
**For Your Situation**: [Specific recommendation based on stated needs]
**Consider Also**: [Related products if relevant]

### Documentation Links
[References to detailed product documentation]""",

    "policy_inquiry": """### Policy Statement [^KB-n]
**Policy Name**: [Official policy name]
**Effective Date**: [When it applies from]
**Summary**: [Core requirement in plain language]

### Applicability
**Who This Applies To**: [Specific roles/situations]
**Exceptions**: [Documented exceptions, if any]

### Required Process [^KB-n]
1. **Prerequisite**: [What must be true before starting]
2. **Step 1**: [Action] - [Who does it] - [Timeline]
3. **Step 2**: [Action] - [Who does it] - [Timeline]
4. **Completion**: [How to confirm process is complete]

### Key Requirements
- [Non-negotiable requirement 1]
- [Non-negotiable requirement 2]

### Escalation Path
[What to do if standard process doesn't apply]

### Policy Source
[Direct reference to policy document with version]""",

    "data_analysis": """### Data Summary
**Dataset**: [What data was analyzed]
**Period**: [Time range if applicable]
**Key Metrics**: [Primary metrics examined]

### Findings [^KB-n]
| Metric | Value | Change | Significance |
|--------|-------|--------|--------------|
| [Metric 1] | [Value] | [Trend] | [Interpretation] |
| [Metric 2] | [Value] | [Trend] | [Interpretation] |

### Trend Analysis
**Pattern Observed**: [What the data shows]
**Historical Context**: [How this compares to baseline - cite source]
**Confidence Level**: [How reliable this interpretation is]

### Contributing Factors
**Likely Causes** (with evidence level):
1. [Factor 1] - [Evidence: Strong/Moderate/Weak]
2. [Factor 2] - [Evidence: Strong/Moderate/Weak]

### Recommendations
**Data-Supported Actions**:
1. [Action with clear rationale tied to findings]
2. [Action with clear rationale tied to findings]

**Requires Further Analysis**:
[Questions that the current data cannot answer]""",

    "general_inquiry": """### Direct Answer
[Concise answer to the question, with source if from KB]

### Supporting Information [^KB-n]
[Context that helps understand the answer]

### Key Points
1. [Most important takeaway]
2. [Second most important]
3. [Additional relevant point]

### Related Topics
[Other information that might be helpful, with links if available]

### Source Notes
**From Knowledge Base**: [What came from KB]
**General Knowledge**: [What is not specific to KB content]""",
}


# =============================================================================
# Document Analysis Prompts (Enhanced with Grounding Protocol)
# =============================================================================

# NOTE: Document analysis is high-risk for hallucination.
# These prompts enforce strict grounding to document content.
# Reference: Manus - "Never fill gaps with plausible-sounding fabrications"

DOCUMENT_ANALYSIS_PROMPT = """<role>
You are a professional document analyst. Your analysis must be strictly grounded in the document content provided.
</role>

<document_content>
{document_content}
</document_content>

<analysis_task>
{analysis_task}
</analysis_task>

<grounding_rules>
## Anti-Hallucination Protocol (Mandatory)

### What You MUST Do
- Quote or closely paraphrase the document when making claims
- Use page/section references when available
- Distinguish between "document states" and "document implies"
- Note any formatting issues that may affect interpretation

### What You MUST NOT Do
- Add information not present in the document
- Speculate about author intent beyond explicit statements
- Fill gaps with general knowledge without clearly labeling it
- Present inferences as if they were explicit document content
</grounding_rules>

<analysis_framework>
## Analysis Dimensions

1. **Structure Analysis**: Document organization, hierarchy, and flow
2. **Key Information**: Most important points (with location references)
3. **Data Points**: Specific numbers, metrics, dates (exact values only)
4. **Arguments**: Core claims and supporting evidence presented
5. **Gaps**: What the document does NOT address
</analysis_framework>

<output_format>
## Response Structure

### Document Metadata
- **Type**: [Document type based on structure/content]
- **Subject**: [Main topic]
- **Scope**: [What the document covers and doesn't cover]
- **Quality Notes**: [Any formatting/clarity issues observed]

### Structure Map
[Hierarchical outline of document sections with brief descriptions]

### Key Findings
| Finding | Location | Confidence |
|---------|----------|------------|
| [Key point 1] | [Section/Page] | [High/Medium/Low] |
| [Key point 2] | [Section/Page] | [High/Medium/Low] |

### Data Points Extracted
[Specific numbers, dates, metrics - exact values only, no interpretation]

### Core Arguments
**Main Thesis**: [Central claim of the document]
**Supporting Evidence**: [How the document supports this claim]
**Counterpoints Addressed**: [Any objections the document anticipates]

### Implicit Information
[Inferences that can be reasonably drawn - CLEARLY LABELED AS INFERENCE]

### Not Covered
[Topics related but not addressed by this document]

### Application Recommendations
[How this document could be used - based on its actual content]
</output_format>

<attention_anchor>
Remember: Every claim must trace to document content. Do not add external information.
</attention_anchor>"""


DOCUMENT_QA_PROMPT = """<task>
Answer the user's question using ONLY the document content provided.
</task>

<document_content>
{document_content}
</document_content>

<user_query>
{user_query}
</user_query>

<answering_protocol>
## Grounding Requirements (Non-Negotiable)

### Citation Requirements
- Every factual claim must reference the document location
- Use format: "According to [section/page], ..."
- Direct quotes should use quotation marks

### Confidence Levels
- **Directly Stated**: Document explicitly contains this information
- **Strongly Implied**: Logical inference from explicit content
- **Possibly Related**: Tangential information that may be relevant
- **Not Found**: Information not present in the document

### Handling Missing Information
- If the answer is NOT in the document, say so clearly
- Do NOT fabricate information to answer the question
- Suggest what type of document might contain the answer
</answering_protocol>

<output_format>
## Response Structure

### Answer
[Direct answer with confidence level]
*Confidence: [Directly Stated / Strongly Implied / Possibly Related]*

### Evidence from Document
> "[Relevant quote from document]"
- Location: [Section/Page reference]

### Context
[Additional relevant information from the document]

### Limitations
**Not Found in Document**: [Aspects of the question the document doesn't address]
**Possible Sources**: [Where this information might be found instead]

### Related Content
[Other information from the document that might be relevant to the user]
</output_format>

<attention_anchor>
Question to answer: "{user_query}"
Source: Only the document provided above. Do not use external knowledge.
</attention_anchor>"""


# =============================================================================
# KB-Enhanced Analysis Prompt (Multi-Source Integration)
# =============================================================================

# NOTE: This prompt handles the complex case of multiple information sources.
# Key Manus principles applied:
# 1. Source reliability assessment
# 2. Conflict resolution between sources
# 3. Clear separation of KB vs general knowledge
# 4. Attention anchoring at the end

KB_ENHANCED_ANALYSIS_PROMPT = """<role>
You are an AI analyst with access to enterprise knowledge bases. Your analysis must clearly distinguish between different information sources and their reliability.
</role>

<user_query>
{user_query}
</user_query>

<kb_results>
## Knowledge Base Search Results
{kb_results}
</kb_results>

<document_content>
## User-Uploaded Document (if provided)
{document_content}
</document_content>

<source_hierarchy>
## Information Source Priority

When sources conflict, use this priority order:
1. **User-uploaded document** (if relevant to query) - Most specific to user's context
2. **Knowledge base content** - Verified enterprise information
3. **General knowledge** - Use only when KB gaps exist, MUST be labeled

### Source Labeling Requirements
- [^KB-n]: Knowledge base source with chunk/document identifier
- [^DOC]: From user-uploaded document
- [^GK]: General knowledge (not from provided sources)
</source_hierarchy>

<analysis_workflow>
## Structured Analysis Process

### Phase 1: Source Assessment
- Catalog all information sources available
- Assess relevance of each source to the query
- Note any conflicts between sources
- Identify information gaps

### Phase 2: Information Synthesis
- Extract relevant facts from highest-priority sources first
- Cross-reference between sources when possible
- Note where sources agree vs disagree
- Identify claims with single-source vs multi-source support

### Phase 3: Analysis Generation
- Build analysis from synthesized information
- Clearly attribute each claim to its source
- Flag any inferences or interpretations
- Note confidence level for each major conclusion

### Phase 4: Recommendation Development
- Base recommendations on analyzed information
- Explain the evidence basis for each recommendation
- Consider implementation feasibility
- Note any assumptions made
</analysis_workflow>

<grounding_protocol>
## Anti-Hallucination Requirements

### Mandatory Behaviors
- Every factual claim must have a source tag [^KB-n], [^DOC], or [^GK]
- Distinguish "source says X" from "this implies Y"
- When sources conflict, present both views with assessment
- Acknowledge gaps rather than filling with speculation

### Confidence Calibration
| Label | Meaning | When to Use |
|-------|---------|-------------|
| High Confidence | Multiple sources agree | Cross-referenced information |
| Medium Confidence | Single reliable source | KB or document content |
| Low Confidence | Inference or general knowledge | Clearly labeled interpretation |
| Cannot Determine | Insufficient information | State what's missing |
</grounding_protocol>

<output_format>
## Response Structure

### Query Understanding
**User's Question**: [Restate in your own words]
**Key Information Needs**: [What specific information will answer this]

### Source Inventory
| Source | Type | Relevance | Quality |
|--------|------|-----------|---------|
| [Source 1] | KB/DOC/GK | High/Med/Low | Assessment |
| [Source 2] | KB/DOC/GK | High/Med/Low | Assessment |

### Key Findings
**From Knowledge Base** [^KB-n]:
- [Finding 1 with specific source reference]
- [Finding 2 with specific source reference]

**From Uploaded Document** [^DOC] (if applicable):
- [Finding 1]
- [Finding 2]

**Source Conflicts** (if any):
- [KB says X, but DOC says Y - assessment of which is more reliable]

### Analysis
[Professional analysis synthesizing all sources]
*Confidence: [High/Medium/Low] based on [reasoning]*

### Recommendations
**Primary Recommendation**: [Action] [^source]
- Evidence basis: [Why this is recommended]
- Implementation: [How to do it]

**Alternative Options**:
1. [Option with source reference]
2. [Option with source reference]

### Information Gaps
**Not Found**: [What information was not available]
**Would Improve Analysis**: [What additional information would help]
**Suggested Next Steps**: [How to get missing information]

### Source Reference List
[Complete list of all sources cited with brief descriptions]
</output_format>

<attention_anchor>
## Primary Objective Reminder
Answer: "{user_query}"
Using: KB results + uploaded document + labeled general knowledge
Requirement: Every claim must cite its source. No unattributed information.
</attention_anchor>"""


# =============================================================================
# Prompt Builder Functions
# =============================================================================

def get_scenario_types_description() -> str:
    """Get formatted description of all scenario types."""
    lines = []
    for code, info in SCENARIO_TYPES.items():
        lines.append(f"- **{code}** ({info['name']}): {info['description']}")
    return "\n".join(lines)


def get_scenario_codes() -> str:
    """Get comma-separated list of scenario codes for fast detection."""
    return ", ".join(SCENARIO_TYPES.keys())


def build_scenario_detection_prompt(user_query: str, fast_mode: bool = False) -> str:
    """
    Build prompt for scenario detection.

    Args:
        user_query: User's query to classify
        fast_mode: If True, use minimal token prompt for TTFT optimization

    Returns:
        Formatted scenario detection prompt
    """
    if fast_mode:
        return FAST_SCENARIO_DETECTION_PROMPT.format(
            user_query=user_query,
            scenario_codes=get_scenario_codes(),
        )
    return SCENARIO_DETECTION_PROMPT.format(
        user_query=user_query,
        scenario_types=get_scenario_types_description(),
    )


def build_analysis_prompt(
    user_query: str,
    scenario_type: str,
    kb_context: str = "",
) -> str:
    """
    Build multi-dimensional analysis prompt based on scenario type.

    This prompt includes:
    - Source attribution requirements (anti-hallucination)
    - Attention anchoring (repeats objective at end)
    - Expert template for the scenario type

    Args:
        user_query: User's question
        scenario_type: Detected scenario type code
        kb_context: Retrieved knowledge base context

    Returns:
        Formatted analysis prompt with Manus-style enhancements
    """
    scenario_info = SCENARIO_TYPES.get(scenario_type, SCENARIO_TYPES["general_inquiry"])
    scenario_name = scenario_info["name"]
    dimensions = scenario_info["analysis_dimensions"]

    # Build dimension sections from expert template
    dimension_sections = EXPERT_TEMPLATES.get(
        scenario_type,
        EXPERT_TEMPLATES["general_inquiry"]
    )

    # Format dimensions list
    dimensions_text = "\n".join(f"- {dim}" for dim in dimensions)

    return MULTI_DIMENSIONAL_ANALYSIS_PROMPT.format(
        scenario_name=scenario_name,
        user_query=user_query,
        kb_context=kb_context if kb_context else "(No relevant knowledge base content retrieved)",
        analysis_dimensions=dimensions_text,
        dimension_sections=dimension_sections,
    )


def build_document_analysis_prompt(
    document_content: str,
    analysis_task: str = "Provide comprehensive document analysis",
) -> str:
    """
    Build document analysis prompt with grounding protocol.

    Args:
        document_content: The document text to analyze
        analysis_task: Specific analysis task or question

    Returns:
        Formatted document analysis prompt
    """
    return DOCUMENT_ANALYSIS_PROMPT.format(
        document_content=document_content,
        analysis_task=analysis_task,
    )


def build_document_qa_prompt(
    document_content: str,
    user_query: str,
) -> str:
    """
    Build document Q&A prompt with strict grounding requirements.

    Args:
        document_content: The document to query against
        user_query: User's question about the document

    Returns:
        Formatted Q&A prompt with attention anchoring
    """
    return DOCUMENT_QA_PROMPT.format(
        document_content=document_content,
        user_query=user_query,
    )


def build_kb_enhanced_prompt(
    user_query: str,
    kb_results: str,
    document_content: str = "",
) -> str:
    """
    Build KB-enhanced analysis prompt for multi-source integration.

    This prompt handles the complex case of multiple information sources
    with clear source attribution and conflict resolution.

    Args:
        user_query: User's question
        kb_results: Retrieved knowledge base content
        document_content: Optional user-uploaded document content

    Returns:
        Formatted multi-source analysis prompt
    """
    return KB_ENHANCED_ANALYSIS_PROMPT.format(
        user_query=user_query,
        kb_results=kb_results,
        document_content=document_content if document_content else "(No user-uploaded document)",
    )


# =============================================================================
# Scenario Metadata Accessors
# =============================================================================

def get_scenario_keywords(scenario_type: str) -> List[str]:
    """Get keywords for a specific scenario type."""
    scenario_info = SCENARIO_TYPES.get(scenario_type, {})
    return scenario_info.get("keywords", [])


def get_analysis_dimensions(scenario_type: str) -> List[str]:
    """Get analysis dimensions for a specific scenario type."""
    scenario_info = SCENARIO_TYPES.get(scenario_type, SCENARIO_TYPES["general_inquiry"])
    return scenario_info.get("analysis_dimensions", [])


def get_expert_template(scenario_type: str) -> str:
    """Get expert response template for a specific scenario type."""
    return EXPERT_TEMPLATES.get(scenario_type, EXPERT_TEMPLATES["general_inquiry"])


def get_scenario_metadata(scenario_type: str) -> Dict[str, Any]:
    """
    Get complete metadata for a scenario type.

    Returns dict with: name, description, keywords, analysis_dimensions,
    urgency_weight, tool_affinity, retrieval_strategy, confidence_threshold
    """
    return SCENARIO_TYPES.get(scenario_type, SCENARIO_TYPES["general_inquiry"])


def get_retrieval_strategy(scenario_type: str) -> str:
    """
    Get recommended retrieval strategy for a scenario type.

    Returns:
        'semantic_first', 'keyword_first', or 'hybrid'
    """
    scenario_info = SCENARIO_TYPES.get(scenario_type, SCENARIO_TYPES["general_inquiry"])
    return scenario_info.get("retrieval_strategy", "semantic_first")


def get_tool_affinity(scenario_type: str) -> List[str]:
    """
    Get list of tools commonly needed for a scenario type.

    This helps with tool pre-loading and action space management.
    """
    scenario_info = SCENARIO_TYPES.get(scenario_type, SCENARIO_TYPES["general_inquiry"])
    return scenario_info.get("tool_affinity", ["kb_search"])


def get_confidence_threshold(scenario_type: str) -> float:
    """
    Get minimum confidence threshold for auto-classification.

    Below this threshold, clarification should be requested.
    """
    scenario_info = SCENARIO_TYPES.get(scenario_type, SCENARIO_TYPES["general_inquiry"])
    return scenario_info.get("confidence_threshold", 0.7)


# =============================================================================
# Scenario Detection Helpers
# =============================================================================

def detect_scenario_by_keywords(query: str) -> Tuple[str, float]:
    """
    Fast keyword-based scenario detection (no LLM call).

    This is useful for:
    1. Pre-filtering before LLM classification
    2. Fallback when LLM is unavailable
    3. Validation of LLM classification results

    Args:
        query: User's query string

    Returns:
        Tuple of (scenario_type, confidence_score)
    """
    query_lower = query.lower()
    scores: Dict[str, float] = {}

    for scenario_type, info in SCENARIO_TYPES.items():
        keywords = info.get("keywords", [])
        if not keywords:
            continue

        matches = sum(1 for kw in keywords if kw.lower() in query_lower)
        if matches > 0:
            # Score based on match ratio and keyword specificity
            score = matches / len(keywords)
            scores[scenario_type] = min(score * 1.2, 0.9)  # Cap at 0.9 for keyword-only

    if not scores:
        return ("general_inquiry", 0.3)

    best_scenario = max(scores, key=scores.get)
    return (best_scenario, scores[best_scenario])


def validate_scenario_detection(
    detection_result: Dict[str, Any],
    user_query: str,
) -> Dict[str, Any]:
    """
    Validate LLM scenario detection result against keyword heuristics.

    This implements the Manus principle of keeping error attempts visible
    by flagging potential misclassifications.

    Args:
        detection_result: LLM classification result
        user_query: Original user query

    Returns:
        Validated result with potential warnings
    """
    llm_scenario = detection_result.get("primary_scenario", "general_inquiry")
    llm_confidence = detection_result.get("confidence", 0.5)

    keyword_scenario, keyword_confidence = detect_scenario_by_keywords(user_query)

    result = detection_result.copy()

    # Flag potential issues
    if llm_scenario != keyword_scenario and keyword_confidence > 0.5:
        result["validation_warning"] = {
            "llm_choice": llm_scenario,
            "keyword_suggestion": keyword_scenario,
            "message": f"LLM classified as {llm_scenario}, but keywords suggest {keyword_scenario}",
        }

    # Boost confidence if LLM and keywords agree
    if llm_scenario == keyword_scenario:
        result["validated_confidence"] = min(llm_confidence + 0.1, 1.0)
    else:
        result["validated_confidence"] = llm_confidence

    return result


def get_all_scenario_types() -> Dict[str, Dict[str, Any]]:
    """Get the complete SCENARIO_TYPES dictionary."""
    return SCENARIO_TYPES.copy()


def list_scenario_codes() -> List[str]:
    """Get list of all valid scenario type codes."""
    return list(SCENARIO_TYPES.keys())
