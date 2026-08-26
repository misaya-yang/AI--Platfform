import { api } from "@/lib/api";

export type TraceFamily = "assistant" | "langgraph_proxy" | "rag";
export type TraceStatus = "running" | "succeeded" | "failed" | "cancelled" | "timeout";
export type SpanStatus = "running" | "succeeded" | "failed" | "cancelled" | "skipped";
export type ScoreType = "numeric" | "categorical" | "boolean" | "text";
export type ScorerType = "human" | "llm" | "rule" | "system";
export type ScoreTargetType = "trace" | "span" | "thread" | "dataset_run" | "example";
export type EvalReviewStatus = "pending" | "approved" | "rejected" | "needs_fix";
export type EvalGateStatus = "pass" | "fail" | "warning";

export interface AgentTraceSummary {
  trace_id: string;
  trace_family: TraceFamily;
  workflow_kind: string;
  tenant_id: string;
  user_id: string;
  thread_id?: string | null;
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
  metrics: Record<string, unknown>;
  privacy: Record<string, unknown>;
  source_adapter?: string | null;
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
  target_type: ScoreTargetType;
  target_id?: string | null;
  evaluator_id?: string | null;
  evaluator_name?: string | null;
  score_source: string;
  confidence?: number | null;
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
  span_kind?: string;
  score_name?: string;
  score_label?: string;
  min_score?: number;
  max_score?: number;
  min_latency_ms?: number;
  max_latency_ms?: number;
  dataset_id?: string;
  metadata_dataset_id?: string;
  started_after?: string;
  started_before?: string;
  limit?: number;
  offset?: number;
}

export interface KbRagasMetricSummary {
  metric: string;
  average_score: number;
  scored_count: number;
  pass_count: number;
  fail_count: number;
  review_count: number;
}

export interface KbRagasKnowledgeSummary {
  window_days: number;
  dataset_id?: string | null;
  rag_traces: number;
  ragas_scored_traces: number;
  metrics: KbRagasMetricSummary[];
  latest_judge_model?: string | null;
}

export interface KbRagasScoreRetrievalResult {
  metric: string;
  score: number;
  explanation: string;
  label: string;
}

export interface KbRagasScoreRetrievalResponse {
  results: KbRagasScoreRetrievalResult[];
  judge_model: string;
}

export interface KbRagasBatchScoreResponse {
  queued: number;
  skipped: number;
  jobs: EvalAsyncJobResponse[];
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
  redaction_policy: Record<string, unknown>;
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
  case_id?: string;
  dataset_id: string;
  tenant_id: string;
  split: string;
  input: Record<string, unknown>;
  expected_output: Record<string, unknown>;
  expected_trajectory?: Record<string, unknown>;
  assertions?: Array<Record<string, unknown>>;
  metadata: Record<string, unknown>;
  source_trace_id?: string | null;
  source_span_id?: string | null;
  created_by: string;
  created_at?: string | null;
}

export interface EvalExampleImportItem {
  case_id: string;
  split?: string;
  input?: Record<string, unknown>;
  expected_output?: Record<string, unknown>;
  expected_trajectory?: Record<string, unknown>;
  assertions?: Array<Record<string, unknown>>;
  metadata?: Record<string, unknown>;
  source_trace_id?: string | null;
  source_span_id?: string | null;
}

export interface EvalExampleUpdate {
  split?: string;
  input?: Record<string, unknown>;
  expected_output?: Record<string, unknown>;
  expected_trajectory?: Record<string, unknown>;
  assertions?: Array<Record<string, unknown>>;
  tags?: string[];
  difficulty?: string;
  owner?: string;
  review_status?: EvalReviewStatus;
  metadata?: Record<string, unknown>;
}

export type EvalExamplesImportMode = "skip_duplicates" | "append";

export interface EvalExamplesImportResponse {
  imported: number;
  skipped: number;
  examples: EvalExample[];
}

export interface EvalExamplesExportResponse {
  dataset: EvalDataset;
  schema_version: string;
  examples: EvalExampleImportItem[];
}

export interface EvalEvaluator {
  evaluator_id: string;
  tenant_id: string;
  name: string;
  evaluator_type: "human" | "rule" | "trajectory" | "span" | "llm" | "llm_judge" | "composite" | "ragas";
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
  baseline_run_id?: string | null;
  runs: EvalExperimentRun[];
}

export type EvalExperimentRunMode = "rescore_trace" | "live_candidate";

export interface EvalExperimentRunProgress {
  completed?: number;
  failed?: number;
  total?: number;
  completed_cases?: number;
  failed_cases?: number;
  total_cases?: number;
  completed_trials?: number;
  failed_trials?: number;
  total_trials?: number;
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

export interface EvalDashboardResponse {
  metrics: Record<string, unknown>;
  run_health: Record<string, unknown>;
  queue_health: Record<string, unknown>;
  runtime_health: Record<string, unknown>;
  latest_gate_status: Record<string, unknown>;
}

export interface EvalExperimentRun {
  run_id: string;
  experiment_id?: string | null;
  tenant_id: string;
  evaluator_id?: string | null;
  dataset_id?: string | null;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  run_mode?: EvalExperimentRunMode;
  repetitions?: number;
  baseline_run_id?: string | null;
  dataset_manifest_hash?: string | null;
  evaluator_suite_hash?: string | null;
  candidate_fingerprint?: Record<string, unknown>;
  target_snapshot?: Record<string, unknown>;
  score_summary: Record<string, unknown>;
  metrics: Record<string, unknown>;
  progress?: EvalExperimentRunProgress;
  runtime_fingerprint?: Record<string, unknown>;
  gate_status?: string | null;
  attribution_status?: string | null;
  error_message?: string | null;
  created_by: string;
  started_at?: string | null;
  finished_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface EvalExperimentRunBatchResponse {
  jobs: EvalAsyncJobResponse[];
}

export interface EvalExperimentCaseScore {
  score_name: string;
  target_type?: string | null;
  target_id?: string | null;
  span_id?: string | null;
  numeric_value?: number | null;
  label?: string | null;
  explanation?: string | null;
  score_source?: string | null;
  failure_kind?: "semantic_review" | "infrastructure" | null;
}

export interface EvalExperimentCaseResult {
  example_id?: string | null;
  case_id: string;
  candidate_trace_id: string;
  baseline_trace_id?: string | null;
  source_trace_id?: string | null;
  status: "passed" | "failed" | "review" | "unscored";
  aggregate_score?: number | null;
  baseline_score?: number | null;
  score_delta?: number | null;
  trial_count?: number;
  score_stddev?: number | null;
  flaky?: boolean;
  observed_metrics?: Record<string, unknown>;
  tool_diffs?: Array<Record<string, unknown>>;
  rag_diffs?: Array<Record<string, unknown>>;
  failure_reason?: string | null;
  input: Record<string, unknown>;
  expected_output: Record<string, unknown>;
  trace: {
    trace_family?: TraceFamily | null;
    status?: TraceStatus | null;
    model_id?: string | null;
    provider?: string | null;
    total_latency_ms?: number;
    total_tokens?: number;
    output_preview?: string;
  };
  scores: EvalExperimentCaseScore[];
}

export interface EvalExperimentRunResultsResponse {
  run: EvalExperimentRun;
  cases: EvalExperimentCaseResult[];
  total: number;
  limit: number;
  offset: number;
}

export interface EvalExperimentRunComparisonResponse {
  baseline_run_id: string;
  candidate_run_id: string;
  baseline_summary: Record<string, unknown>;
  candidate_summary: Record<string, unknown>;
  deltas: Record<string, unknown>;
  regression_summary: Record<string, unknown>;
  case_diffs: Array<Record<string, unknown>>;
  compatibility?: string | boolean | Record<string, unknown>;
  attribution?: string;
  attribution_status?: string;
  changed_dimensions?: string[];
  metric_diffs?: Record<string, unknown>;
  statistics?: Record<string, unknown>;
  gate?: Record<string, unknown>;
  gate_status?: string;
}

export interface EvalBaselinePromotionResponse {
  experiment_id: string;
  baseline_run_id: string;
  previous_baseline_run_id?: string | null;
  promoted_by: string;
  promoted_at?: string | null;
}

export interface EvalGateDryRunResponse {
  status: EvalGateStatus;
  thresholds: Record<string, number>;
  metrics: Record<string, unknown>;
  failures: string[];
  skipped_thresholds: string[];
  coverage: Record<string, unknown>;
  compatibility: Record<string, unknown>;
  authoritative_gate: Record<string, unknown>;
  report: Record<string, unknown>;
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
  target_type?: ScoreTargetType;
  target_id?: string | null;
  evaluator_id?: string | null;
  evaluator_name?: string | null;
  score_source?: string;
  confidence?: number | null;
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

export async function getEvalDashboard(days = 7): Promise<EvalDashboardResponse> {
  const response = await api.get<EvalDashboardResponse>("/api/v1/eval/dashboard", {
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

export async function updateEvalExample(
  datasetId: string,
  exampleId: string,
  payload: EvalExampleUpdate
): Promise<EvalExample> {
  const response = await api.patch<EvalExample>(
    `/api/v1/eval/datasets/${encodeURIComponent(datasetId)}/examples/${encodeURIComponent(exampleId)}`,
    payload
  );
  return response.data;
}

export async function importEvalExamples(
  datasetId: string,
  examples: EvalExampleImportItem[],
  options: { mode?: EvalExamplesImportMode } = {}
): Promise<EvalExamplesImportResponse> {
  const response = await api.post<EvalExamplesImportResponse>(
    `/api/v1/eval/datasets/${encodeURIComponent(datasetId)}/examples:import`,
    { examples, mode: options.mode ?? "skip_duplicates" }
  );
  return response.data;
}

export async function exportEvalExamples(
  datasetId: string,
  params: { split?: string } = {}
): Promise<EvalExamplesExportResponse> {
  const response = await api.get<EvalExamplesExportResponse>(
    `/api/v1/eval/datasets/${encodeURIComponent(datasetId)}/examples:export`,
    { params: compactParams(params) }
  );
  return response.data;
}

export async function listEvalEvaluators(params: { limit?: number; offset?: number } = {}): Promise<EvalEvaluatorListResponse> {
  const response = await api.get<EvalEvaluatorListResponse>("/api/v1/eval/evaluators", {
    params: compactParams({ limit: 50, offset: 0, ...params }),
  });
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

export async function getEvalExperimentRunResults(
  runId: string,
  params: { limit?: number; offset?: number } = {}
): Promise<EvalExperimentRunResultsResponse> {
  const pageLimit = params.limit ?? 200;
  const first = await api.get<EvalExperimentRunResultsResponse>(
    `/api/v1/eval/experiment-runs/${encodeURIComponent(runId)}/results`,
    { params: compactParams({ limit: pageLimit, offset: 0, ...params }) }
  );
  if (params.limit !== undefined || first.data.cases.length >= first.data.total) {
    return first.data;
  }

  const cases = [...first.data.cases];
  let offset = first.data.offset + first.data.cases.length;
  while (offset < first.data.total) {
    const next = await api.get<EvalExperimentRunResultsResponse>(
      `/api/v1/eval/experiment-runs/${encodeURIComponent(runId)}/results`,
      { params: { limit: pageLimit, offset } }
    );
    if (next.data.cases.length === 0) break;
    cases.push(...next.data.cases);
    offset += next.data.cases.length;
  }
  return { ...first.data, cases, limit: cases.length };
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

export async function runEvalExperiment(
  experimentId: string,
  payload: {
    dataset_id?: string | null;
    evaluator_ids: string[];
    run_mode?: EvalExperimentRunMode;
    repetitions?: number;
    baseline_run_id?: string | null;
    candidate_config?: {
      system_prompt_override?: string;
      [key: string]: unknown;
    };
    target_snapshot?: Record<string, unknown>;
    candidate_label?: string;
    baseline_label?: string | null;
    metadata?: Record<string, unknown>;
  }
): Promise<EvalExperimentRunBatchResponse> {
  const response = await api.post<EvalExperimentRunBatchResponse>(
    `/api/v1/eval/experiments/${encodeURIComponent(experimentId)}:run`,
    payload
  );
  return response.data;
}

export async function promoteEvalExperimentBaseline(
  experimentId: string,
  runId: string,
): Promise<EvalBaselinePromotionResponse> {
  const response = await api.post<EvalBaselinePromotionResponse>(
    `/api/v1/eval/experiments/${encodeURIComponent(experimentId)}:promote-baseline`,
    { run_id: runId },
  );
  return response.data;
}

export async function compareEvalExperimentRuns(
  baselineRunId: string,
  candidateRunId: string
): Promise<EvalExperimentRunComparisonResponse> {
  const response = await api.get<EvalExperimentRunComparisonResponse>(
    "/api/v1/eval/experiment-runs:compare",
    { params: { baseline_run_id: baselineRunId, candidate_run_id: candidateRunId } }
  );
  return response.data;
}

export async function dryRunEvalGate(payload: {
  baseline_run_id?: string | null;
  candidate_run_id?: string | null;
  result_payload?: Record<string, unknown>;
  thresholds?: Record<string, number>;
}): Promise<EvalGateDryRunResponse> {
  const response = await api.post<EvalGateDryRunResponse>("/api/v1/eval/gates:dry-run", payload);
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

export async function getKbRagasKnowledgeSummary(params: {
  days?: number;
  dataset_id?: string;
} = {}): Promise<KbRagasKnowledgeSummary> {
  const response = await api.get<KbRagasKnowledgeSummary>("/api/v1/eval/knowledge/summary", {
    params: compactParams(params),
  });
  return response.data;
}

export async function batchScoreKbRagasDataset(
  datasetId: string,
  payload: {
    evaluator_id: string;
    limit?: number;
    only_unscored?: boolean;
  }
): Promise<KbRagasBatchScoreResponse> {
  const response = await api.post<KbRagasBatchScoreResponse>(
    `/api/v1/eval/knowledge/${encodeURIComponent(datasetId)}/batch-score`,
    payload
  );
  return response.data;
}

export async function scoreKbRagasRetrieval(payload: {
  query: string;
  contexts: string[];
  metrics?: string[];
  ground_truth?: string | null;
  dataset_id?: string | null;
  llm_config?: Record<string, unknown>;
}): Promise<KbRagasScoreRetrievalResponse> {
  const response = await api.post<KbRagasScoreRetrievalResponse>(
    "/api/v1/eval/knowledge/score-retrieval",
    payload
  );
  return response.data;
}
