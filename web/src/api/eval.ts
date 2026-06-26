import { api } from "@/lib/api";

export type TraceFamily = "assistant" | "langgraph_proxy" | "rag";
export type TraceStatus = "running" | "succeeded" | "failed" | "cancelled" | "timeout";
export type SpanStatus = "running" | "succeeded" | "failed" | "cancelled" | "skipped";
export type ScoreType = "numeric" | "categorical" | "boolean" | "text";
export type ScorerType = "human" | "llm" | "rule" | "system";

export interface AgentTraceSummary {
  trace_id: string;
  trace_family: TraceFamily;
  workflow_kind: string;
  tenant_id: string;
  user_id: string;
  session_id?: string | null;
  run_id?: string | null;
  request_id?: string | null;
  model_id?: string | null;
  provider?: string | null;
  status: TraceStatus;
  started_at?: string | null;
  ended_at?: string | null;
  first_token_latency_ms: number;
  total_latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  total_cost_cents: number;
  input_preview: string;
  output_preview: string;
  redaction_state: Record<string, unknown>;
  metadata: Record<string, unknown>;
  scores_count: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AgentTraceSpan {
  span_id: string;
  trace_id: string;
  parent_span_id?: string | null;
  span_kind: string;
  name: string;
  status: SpanStatus;
  sequence_no: number;
  started_at?: string | null;
  ended_at?: string | null;
  duration_ms: number;
  input_preview: string;
  output_preview: string;
  attributes: Record<string, unknown>;
  error_type?: string | null;
  error_message?: string | null;
  created_at?: string | null;
}

export interface AgentTraceEvent {
  event_id: string;
  trace_id: string;
  span_id?: string | null;
  event_type: string;
  sequence_no: number;
  occurred_at?: string | null;
  payload: Record<string, unknown>;
  payload_size_bytes: number;
  redacted: boolean;
  created_at?: string | null;
}

export interface AgentTraceScore {
  score_id: string;
  trace_id: string;
  span_id?: string | null;
  score_name: string;
  score_type: ScoreType;
  numeric_value?: number | null;
  boolean_value?: boolean | null;
  categorical_value?: string | null;
  text_value?: string | null;
  label?: string | null;
  explanation?: string | null;
  scorer_type: ScorerType;
  evaluator_version?: string | null;
  created_by: string;
  metadata: Record<string, unknown>;
  created_at?: string | null;
}

export interface AgentTraceListResponse {
  traces: AgentTraceSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface AgentTraceDetailResponse {
  trace: AgentTraceSummary;
  spans: AgentTraceSpan[];
  events: AgentTraceEvent[];
  scores: AgentTraceScore[];
}

export interface ListAgentTracesParams {
  trace_family?: TraceFamily;
  status?: TraceStatus;
  model_id?: string;
  user_id?: string;
  session_id?: string;
  run_id?: string;
  request_id?: string;
  transcript_query?: string;
  turn_index?: number;
  limit?: number;
  offset?: number;
}

export interface EvalTraceThreadResponse {
  thread_id: string;
  traces: AgentTraceSummary[];
  total: number;
  metrics: Record<string, unknown>;
}

export interface EvalTraceExportResponse {
  trace_id: string;
  format: "openinference" | "otel" | "langsmith-jsonl";
  payload: Record<string, unknown> | Array<Record<string, unknown>>;
}

export interface EvalDataset {
  dataset_id: string;
  tenant_id: string;
  name: string;
  description: string;
  version: string;
  schema: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_by: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface EvalDatasetCreate {
  name: string;
  description?: string;
  version?: string;
  schema?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}

export interface EvalExample {
  example_id: string;
  dataset_id: string;
  tenant_id: string;
  split: string;
  input: Record<string, unknown>;
  expected_output: Record<string, unknown>;
  metadata: Record<string, unknown>;
  source_trace_id?: string | null;
  source_span_id?: string | null;
  created_by: string;
  created_at?: string | null;
}

export interface EvalEvaluator {
  evaluator_id: string;
  tenant_id: string;
  name: string;
  evaluator_type: "human" | "rule" | "llm" | "composite";
  rubric: string;
  version: string;
  sampling_config: Record<string, unknown>;
  filter_config: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_by: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface EvalExperiment {
  experiment_id: string;
  tenant_id: string;
  dataset_id?: string | null;
  name: string;
  description: string;
  target_config: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_by: string;
  created_at?: string | null;
  updated_at?: string | null;
  runs: Array<Record<string, unknown>>;
}

export interface EvalAsyncJobResponse {
  job_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  run_id?: string | null;
}

export interface EvalDatasetListResponse {
  datasets: EvalDataset[];
  total: number;
  limit: number;
  offset: number;
}

export interface EvalExampleListResponse {
  examples: EvalExample[];
  total: number;
  limit: number;
  offset: number;
}

export interface EvalEvaluatorListResponse {
  evaluators: EvalEvaluator[];
  total: number;
  limit: number;
  offset: number;
}

export interface EvalExperimentListResponse {
  experiments: EvalExperiment[];
  total: number;
  limit: number;
  offset: number;
}

export interface EvalTraceMonitoringSummary {
  total_traces: number;
  failed_traces: number;
  succeeded_traces: number;
  assistant_traces: number;
  langgraph_traces: number;
  rag_traces: number;
  avg_latency_ms: number;
  p95_latency_ms: number;
  total_tokens: number;
  total_cost_cents: number;
  scored_traces: number;
  window_days: number;
}

export interface EvalExperimentRun {
  run_id: string;
  experiment_id?: string | null;
  tenant_id: string;
  evaluator_id?: string | null;
  dataset_id?: string | null;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  target_snapshot?: Record<string, unknown>;
  score_summary: Record<string, unknown>;
  metrics: Record<string, unknown>;
  error_message?: string | null;
  created_by: string;
  started_at?: string | null;
  finished_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AgentTraceScoreCreate {
  span_id?: string | null;
  score_name: string;
  score_type?: ScoreType;
  numeric_value?: number | null;
  boolean_value?: boolean | null;
  categorical_value?: string | null;
  text_value?: string | null;
  label?: string | null;
  explanation?: string | null;
  scorer_type?: ScorerType;
  evaluator_version?: string | null;
  metadata?: Record<string, unknown>;
}

function compactParams(params: Record<string, unknown>) {
  return Object.fromEntries(
    Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== "")
  );
}

export async function getEvalSummary(days = 7): Promise<EvalTraceMonitoringSummary> {
  const response = await api.get<EvalTraceMonitoringSummary>("/api/v1/eval/summary", {
    params: { days },
  });
  return response.data;
}

export async function listAgentTraces(params: ListAgentTracesParams = {}): Promise<AgentTraceListResponse> {
  const response = await api.get<AgentTraceListResponse>("/api/v1/eval/traces", {
    params: compactParams({
      trace_family: "assistant",
      limit: 50,
      offset: 0,
      ...params,
    }),
  });
  return response.data;
}

export async function getTraceThread(threadId: string): Promise<EvalTraceThreadResponse> {
  const response = await api.get<EvalTraceThreadResponse>(
    `/api/v1/eval/threads/${encodeURIComponent(threadId)}`
  );
  return response.data;
}

export async function getAgentTraceDetail(
  traceId: string,
  traceFamily: TraceFamily = "assistant"
): Promise<AgentTraceDetailResponse> {
  const response = await api.get<AgentTraceDetailResponse>(
    `/api/v1/eval/traces/${encodeURIComponent(traceId)}`,
    { params: { trace_family: traceFamily } }
  );
  return response.data;
}

export async function createAgentTraceScore(
  traceId: string,
  payload: AgentTraceScoreCreate,
  traceFamily: TraceFamily = "assistant"
): Promise<AgentTraceScore> {
  const response = await api.post<AgentTraceScore>(
    `/api/v1/eval/traces/${encodeURIComponent(traceId)}/scores`,
    payload,
    { params: { trace_family: traceFamily } }
  );
  return response.data;
}

export async function exportAgentTrace(
  traceId: string,
  format: EvalTraceExportResponse["format"] = "openinference",
  traceFamily: TraceFamily = "assistant"
): Promise<EvalTraceExportResponse> {
  const response = await api.get<EvalTraceExportResponse>(
    `/api/v1/eval/traces/${encodeURIComponent(traceId)}/export`,
    { params: { trace_family: traceFamily, format } }
  );
  return response.data;
}

export async function listEvalDatasets(params: { limit?: number; offset?: number } = {}): Promise<EvalDatasetListResponse> {
  const response = await api.get<EvalDatasetListResponse>("/api/v1/eval/datasets", {
    params: compactParams({ limit: 50, offset: 0, ...params }),
  });
  return response.data;
}

export async function getEvalDataset(datasetId: string): Promise<EvalDataset> {
  const response = await api.get<EvalDataset>(`/api/v1/eval/datasets/${encodeURIComponent(datasetId)}`);
  return response.data;
}

export async function listEvalExamples(
  datasetId: string,
  params: { split?: string; limit?: number; offset?: number } = {}
): Promise<EvalExampleListResponse> {
  const response = await api.get<EvalExampleListResponse>(
    `/api/v1/eval/datasets/${encodeURIComponent(datasetId)}/examples`,
    { params: compactParams({ limit: 200, offset: 0, ...params }) }
  );
  return response.data;
}

export async function createEvalDataset(payload: EvalDatasetCreate): Promise<EvalDataset> {
  const response = await api.post<EvalDataset>("/api/v1/eval/datasets", payload);
  return response.data;
}

export async function createEvalExampleFromTrace(
  datasetId: string,
  payload: {
    source_trace_id: string;
    source_span_id?: string | null;
    trace_family?: TraceFamily;
    split?: string;
    expected_output?: Record<string, unknown>;
    metadata?: Record<string, unknown>;
  }
): Promise<EvalExample> {
  const response = await api.post<EvalExample>(
    `/api/v1/eval/datasets/${encodeURIComponent(datasetId)}/examples:from-trace`,
    payload
  );
  return response.data;
}

export async function listEvalEvaluators(params: { limit?: number; offset?: number } = {}): Promise<EvalEvaluatorListResponse> {
  const response = await api.get<EvalEvaluatorListResponse>("/api/v1/eval/evaluators", {
    params: compactParams({ limit: 50, offset: 0, ...params }),
  });
  return response.data;
}

export async function getEvalEvaluator(evaluatorId: string): Promise<EvalEvaluator> {
  const response = await api.get<EvalEvaluator>(`/api/v1/eval/evaluators/${encodeURIComponent(evaluatorId)}`);
  return response.data;
}

export async function createEvalEvaluator(payload: {
  name: string;
  evaluator_type?: EvalEvaluator["evaluator_type"];
  rubric?: string;
  version?: string;
  sampling_config?: Record<string, unknown>;
  filter_config?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}): Promise<EvalEvaluator> {
  const response = await api.post<EvalEvaluator>("/api/v1/eval/evaluators", payload);
  return response.data;
}

export async function listEvalExperiments(params: { limit?: number; offset?: number } = {}): Promise<EvalExperimentListResponse> {
  const response = await api.get<EvalExperimentListResponse>("/api/v1/eval/experiments", {
    params: compactParams({ limit: 50, offset: 0, ...params }),
  });
  return response.data;
}

export async function getEvalExperiment(experimentId: string): Promise<EvalExperiment> {
  const response = await api.get<EvalExperiment>(`/api/v1/eval/experiments/${encodeURIComponent(experimentId)}`);
  return response.data;
}

export async function getEvalExperimentRun(runId: string): Promise<EvalExperimentRun> {
  const response = await api.get<EvalExperimentRun>(`/api/v1/eval/experiment-runs/${encodeURIComponent(runId)}`);
  return response.data;
}

export async function createEvalExperiment(payload: {
  name: string;
  description?: string;
  dataset_id?: string | null;
  target_config?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}): Promise<EvalExperiment> {
  const response = await api.post<EvalExperiment>("/api/v1/eval/experiments", payload);
  return response.data;
}

export async function runEvalEvaluatorAsync(
  evaluatorId: string,
  payload: {
    experiment_id?: string | null;
    dataset_id?: string | null;
    trace_id?: string | null;
    target_snapshot?: Record<string, unknown>;
    metadata?: Record<string, unknown>;
  }
): Promise<EvalAsyncJobResponse> {
  const response = await api.post<EvalAsyncJobResponse>(
    `/api/v1/eval/evaluators/${encodeURIComponent(evaluatorId)}:run-async`,
    payload
  );
  return response.data;
}
