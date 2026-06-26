from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TraceFamily = Literal["assistant", "langgraph_proxy", "rag"]
TraceStatus = Literal["running", "succeeded", "failed", "cancelled", "timeout"]
SpanStatus = Literal["running", "succeeded", "failed", "cancelled", "skipped"]
ScoreType = Literal["numeric", "categorical", "boolean", "text"]
ScorerType = Literal["human", "llm", "rule", "system"]
EvaluatorType = Literal["human", "rule", "llm", "composite"]
TraceExportFormat = Literal["openinference", "otel", "langsmith-jsonl"]
EvalRunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
ScoreTargetType = Literal["trace", "span", "thread", "dataset_run", "example"]


class AgentTraceSummary(BaseModel):
    trace_id: str
    trace_family: TraceFamily
    workflow_kind: str
    tenant_id: str
    user_id: str
    session_id: str | None = None
    run_id: str | None = None
    request_id: str | None = None
    model_id: str | None = None
    provider: str | None = None
    status: TraceStatus
    started_at: datetime | None = None
    ended_at: datetime | None = None
    first_token_latency_ms: int = 0
    total_latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    total_cost_cents: int = 0
    input_preview: str = ""
    output_preview: str = ""
    redaction_state: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    scores_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AgentTraceSpan(BaseModel):
    span_id: str
    trace_id: str
    parent_span_id: str | None = None
    span_kind: str
    name: str
    status: SpanStatus
    sequence_no: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int = 0
    input_preview: str = ""
    output_preview: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None


class AgentTraceEvent(BaseModel):
    event_id: str
    trace_id: str
    span_id: str | None = None
    event_type: str
    sequence_no: int
    occurred_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_size_bytes: int = 0
    redacted: bool = True
    created_at: datetime | None = None


class AgentTraceScore(BaseModel):
    score_id: str
    trace_id: str
    span_id: str | None = None
    score_name: str
    score_type: ScoreType = "numeric"
    numeric_value: float | None = None
    boolean_value: bool | None = None
    categorical_value: str | None = None
    text_value: str | None = None
    label: str | None = None
    explanation: str | None = None
    scorer_type: ScorerType = "human"
    evaluator_version: str | None = None
    target_type: ScoreTargetType = "trace"
    target_id: str | None = None
    evaluator_id: str | None = None
    evaluator_name: str | None = None
    score_source: str = "human"
    confidence: float | None = None
    created_by: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class AgentTraceListResponse(BaseModel):
    traces: list[AgentTraceSummary]
    total: int
    limit: int
    offset: int


class AgentTraceDetailResponse(BaseModel):
    trace: AgentTraceSummary
    spans: list[AgentTraceSpan] = Field(default_factory=list)
    events: list[AgentTraceEvent] = Field(default_factory=list)
    scores: list[AgentTraceScore] = Field(default_factory=list)


class AgentTraceScoreCreate(BaseModel):
    span_id: str | None = None
    score_name: str = Field(..., min_length=1, max_length=96)
    score_type: ScoreType = "numeric"
    numeric_value: float | None = None
    boolean_value: bool | None = None
    categorical_value: str | None = Field(default=None, max_length=96)
    text_value: str | None = Field(default=None, max_length=2000)
    label: str | None = Field(default=None, max_length=96)
    explanation: str | None = Field(default=None, max_length=2000)
    scorer_type: ScorerType = "human"
    evaluator_version: str | None = Field(default=None, max_length=64)
    target_type: ScoreTargetType = "trace"
    target_id: str | None = Field(default=None, max_length=128)
    evaluator_id: str | None = None
    evaluator_name: str | None = Field(default=None, max_length=128)
    score_source: str = Field(default="human", max_length=32)
    confidence: float | None = Field(default=None, ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TraceArtifactRef(BaseModel):
    artifact_id: str
    artifact_type: str
    name: str | None = None
    url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TraceEnvelope(BaseModel):
    trace_id: str | None = None
    trace_family: TraceFamily = "assistant"
    workflow_kind: str = "ai_assistant_chat"
    thread_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    request_id: str | None = None
    user_id: str | None = None
    model_id: str | None = None
    provider: str | None = None
    status: TraceStatus = "succeeded"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    input_preview: str = ""
    output_preview: str = ""
    metrics: dict[str, Any] = Field(default_factory=dict)
    privacy: dict[str, Any] = Field(default_factory=dict)
    redaction_state: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_adapter: str = "api"
    spans: list[AgentTraceSpan] = Field(default_factory=list)
    events: list[AgentTraceEvent] = Field(default_factory=list)
    scores: list[AgentTraceScore] = Field(default_factory=list)
    artifacts: list[TraceArtifactRef] = Field(default_factory=list)


class AgentTraceIngestRequest(BaseModel):
    trace: TraceEnvelope
    enqueue: bool = True


class AgentTraceIngestResponse(BaseModel):
    trace_id: str
    status: str
    job_id: str | None = None


class EvalTraceExportResponse(BaseModel):
    trace_id: str
    format: TraceExportFormat
    payload: dict[str, Any] | list[dict[str, Any]]


class EvalTraceThreadResponse(BaseModel):
    thread_id: str
    traces: list[AgentTraceSummary]
    total: int
    metrics: dict[str, Any] = Field(default_factory=dict)


class EvalDatasetCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    version: str = Field(default="v1", min_length=1, max_length=64)
    json_schema: dict[str, Any] = Field(default_factory=dict, alias="schema")
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalDataset(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    dataset_id: str
    tenant_id: str
    name: str
    description: str = ""
    version: str = "v1"
    json_schema: dict[str, Any] = Field(default_factory=dict, alias="schema")
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EvalExampleFromTraceCreate(BaseModel):
    split: str = Field(default="regression", max_length=32)
    source_trace_id: str
    source_span_id: str | None = None
    trace_family: TraceFamily = "assistant"
    expected_output: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalExample(BaseModel):
    example_id: str
    dataset_id: str
    tenant_id: str
    split: str = "regression"
    input: dict[str, Any] = Field(default_factory=dict)
    expected_output: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_trace_id: str | None = None
    source_span_id: str | None = None
    created_by: str
    created_at: datetime | None = None


class EvalEvaluatorCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    evaluator_type: EvaluatorType = "human"
    rubric: str = Field(default="", max_length=8000)
    version: str = Field(default="v1", min_length=1, max_length=64)
    sampling_config: dict[str, Any] = Field(default_factory=dict)
    filter_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalEvaluator(BaseModel):
    evaluator_id: str
    tenant_id: str
    name: str
    evaluator_type: EvaluatorType = "human"
    rubric: str = ""
    version: str = "v1"
    sampling_config: dict[str, Any] = Field(default_factory=dict)
    filter_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EvalExperimentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    dataset_id: str | None = None
    target_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalExperimentRun(BaseModel):
    run_id: str
    experiment_id: str | None = None
    tenant_id: str
    evaluator_id: str | None = None
    dataset_id: str | None = None
    status: EvalRunStatus = "queued"
    target_snapshot: dict[str, Any] = Field(default_factory=dict)
    score_summary: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    created_by: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EvalExperiment(BaseModel):
    experiment_id: str
    tenant_id: str
    dataset_id: str | None = None
    name: str
    description: str = ""
    target_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    runs: list[EvalExperimentRun] = Field(default_factory=list)


class EvalEvaluatorRunRequest(BaseModel):
    experiment_id: str | None = None
    dataset_id: str | None = None
    trace_id: str | None = None
    target_snapshot: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalAsyncJobResponse(BaseModel):
    job_id: str
    status: EvalRunStatus = "queued"
    run_id: str | None = None


class EvalDatasetListResponse(BaseModel):
    datasets: list[EvalDataset]
    total: int
    limit: int
    offset: int


class EvalExampleListResponse(BaseModel):
    examples: list[EvalExample]
    total: int
    limit: int
    offset: int


class EvalEvaluatorListResponse(BaseModel):
    evaluators: list[EvalEvaluator]
    total: int
    limit: int
    offset: int


class EvalExperimentListResponse(BaseModel):
    experiments: list[EvalExperiment]
    total: int
    limit: int
    offset: int


class EvalTraceMonitoringSummary(BaseModel):
    total_traces: int = 0
    failed_traces: int = 0
    succeeded_traces: int = 0
    assistant_traces: int = 0
    langgraph_traces: int = 0
    rag_traces: int = 0
    avg_latency_ms: int = 0
    p95_latency_ms: int = 0
    total_tokens: int = 0
    total_cost_cents: int = 0
    scored_traces: int = 0
    window_days: int = 7
