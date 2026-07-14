import { Alert, App as AntApp, Button, Descriptions, Input, InputNumber, Progress, Select, Space, Tabs } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Beaker,
  CheckCircle2,
  Database,
  Download,
  GitCompare,
  Play,
  SearchCheck,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  createEvalDataset,
  createEvalEvaluator,
  createEvalExampleFromTrace,
  createEvalExperiment,
  createAgentTraceScore,
  compareEvalExperimentRuns,
  dryRunEvalGate,
  exportAgentTrace,
  exportEvalExamples,
  getAgentTraceDetail,
  getEvalDashboard,
  getEvalExperiment,
  getEvalExperimentRun,
  getEvalExperimentRunResults,
  getEvalSummary,
  importEvalExamples,
  listAgentTraces,
  listEvalDatasets,
  listEvalEvaluators,
  listEvalExamples,
  listEvalExperiments,
  promoteEvalExperimentBaseline,
  runEvalExperiment,
  runEvalEvaluatorAsync,
  updateEvalExample,
  type EvalDataset,
  type EvalEvaluator,
  type EvalExperiment,
  type EvalExperimentCaseResult,
  type EvalExperimentRunMode,
  type EvalExperimentRunComparisonResponse,
  type EvalGateDryRunResponse,
  type EvalExamplesExportResponse,
  type EvalReviewStatus,
  type EvalTraceExportResponse,
  type AgentTraceScoreCreate,
  type AgentTraceSummary,
  type ListAgentTracesParams,
  type TraceFamily,
  type TraceStatus,
} from "@/api/eval";
import { usePermission } from "@/store/useAuthStore";

import {
  type AssistantTraceFilters,
} from "./components/AssistantTraceList";
import { GoldenJsonlImport } from "./components/GoldenJsonlImport";
import { BehaviorContractEditor } from "./components/BehaviorContractEditor";
import { ExperimentRunComparison } from "./components/ExperimentRunComparison";
import { ExperimentRunResults } from "./components/ExperimentRunResults";
import { KbRagasPanel } from "./components/KbRagasPanel";
import { TraceExplorerShell } from "./components/TraceExplorerShell";

import "./styles.css";

function toError(value: unknown): Error {
  return value instanceof Error ? value : new Error(String(value || "Unknown error"));
}

function isWithinDateRange(trace: AgentTraceSummary, filters: AssistantTraceFilters) {
  const sourceDate = trace.started_at || trace.created_at;
  if (!sourceDate) return true;
  const timestamp = new Date(sourceDate).getTime();
  if (filters.start_date) {
    const start = new Date(filters.start_date).setHours(0, 0, 0, 0);
    if (timestamp < start) return false;
  }
  if (filters.end_date) {
    const end = new Date(filters.end_date).setHours(23, 59, 59, 999);
    if (timestamp > end) return false;
  }
  return true;
}

function isWithinScoreFilter(trace: AgentTraceSummary, filters: AssistantTraceFilters) {
  if (!filters.score_status || filters.score_status === "all") return true;
  if (filters.score_status === "scored") return trace.scores_count > 0;
  return trace.scores_count === 0;
}

function WorkbenchPanel({
  title,
  description,
  icon,
  children,
}: {
  title: string;
  description: string;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="eval-panel eval-workbench-panel">
      <div className="eval-panel-heading">
        <div>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
        <div className="eval-workbench-icon">{icon}</div>
      </div>
      <div className="eval-workbench-body">{children}</div>
    </section>
  );
}

function parseJsonObjectDraft(text: string, fallback: Record<string, unknown>): Record<string, unknown> {
  const trimmed = text.trim();
  if (!trimmed) return fallback;
  const parsed: unknown = JSON.parse(trimmed);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("JSON config must be an object");
  }
  return parsed as Record<string, unknown>;
}

function evaluatorPresetForType(
  evaluatorType: EvalEvaluator["evaluator_type"],
  traceFamily: TraceFamily,
  translate: (key: string, fallback?: string) => string,
): {
  rubric: string;
  filterConfigText: string;
  samplingConfigText: string;
} {
  const defaultSampling = JSON.stringify(
    {
      online: {
        enabled: false,
        rate: 0.05,
        trace_families: [traceFamily],
        only_failed: false,
      },
    },
    null,
    2,
  );

  if (evaluatorType === "llm" || evaluatorType === "llm_judge") {
    const rubric =
      traceFamily === "rag"
        ? "Score RAG faithfulness and retrieval usefulness from 0 to 1 using bounded previews and trajectory summary."
        : "Score helpfulness, grounding, tool-use quality, and safety from 0 to 1 using bounded previews and trajectory summary.";
    return { rubric, filterConfigText: "{}", samplingConfigText: defaultSampling };
  }
  if (evaluatorType === "span") {
    const spanKinds =
      traceFamily === "rag"
        ? ["retriever", "model_invocation", "lifecycle"]
        : ["tool_execution", "model_invocation", "retriever"];
    return {
      rubric: "Score each execution span for correctness, grounding, and failure-free completion.",
      filterConfigText: JSON.stringify(
        {
          mode: "rule",
          span_kinds: spanKinds,
          rules: [{ type: "no_error_spans" }, { type: "output_not_empty" }],
          pass_threshold: 0.8,
        },
        null,
        2,
      ),
      samplingConfigText: defaultSampling,
    };
  }
  if (evaluatorType === "rule") {
    const rules =
      traceFamily === "rag"
        ? [
            { type: "status_eq", value: "succeeded" },
            { type: "retrieval_document_count_gte", value: 1 },
            { type: "output_not_empty" },
            { type: "no_error_spans" },
          ]
        : [
            { type: "status_eq", value: "succeeded" },
            { type: "output_not_empty" },
            { type: "no_error_spans" },
          ];
    return {
      rubric: "",
      filterConfigText: JSON.stringify({ rules, pass_threshold: 0.8 }, null, 2),
      samplingConfigText: defaultSampling,
    };
  }
  if (evaluatorType === "trajectory") {
    const required =
      traceFamily === "assistant"
        ? ["lifecycle", "model_invocation"]
        : traceFamily === "rag"
          ? ["lifecycle", "retriever"]
          : ["lifecycle"];
    return {
      rubric: "",
      filterConfigText: JSON.stringify({ required_span_kinds: required }, null, 2),
      samplingConfigText: defaultSampling,
    };
  }
  if (evaluatorType === "composite") {
    return {
      rubric: "",
      filterConfigText: JSON.stringify(
        {
          pass_threshold: 0.8,
          components: [
            { type: "rule", weight: 0.4, config: { rules: [{ type: "output_not_empty" }] } },
            { type: "trajectory", weight: 0.3, config: { required_span_kinds: ["lifecycle"] } },
            { type: "llm_judge", weight: 0.3 },
          ],
        },
        null,
        2,
      ),
      samplingConfigText: defaultSampling,
    };
  }
  if (evaluatorType === "ragas") {
    return {
      rubric: translate("eval.ragas.evaluatorRubric"),
      filterConfigText: JSON.stringify(
        {
          metrics: ["context_relevancy"],
          required_span_kinds: ["retriever"],
          pass_threshold: 0.7,
        },
        null,
        2,
      ),
      samplingConfigText: JSON.stringify(
        {
          online: {
            enabled: traceFamily === "rag",
            rate: 0.05,
            trace_families: ["rag"],
            only_failed: false,
          },
        },
        null,
        2,
      ),
    };
  }
  return {
    rubric: "Score helpfulness, grounding, and safety from bounded trace previews.",
    filterConfigText: "{}",
    samplingConfigText: defaultSampling,
  };
}

function buildServerFilters(
  filters: AssistantTraceFilters,
  traceFamily: TraceFamily,
  kbDatasetId?: string
): ListAgentTracesParams {
  const datasetFilter = kbDatasetId?.trim() || undefined;
  return {
    trace_family: traceFamily,
    metadata_dataset_id: traceFamily === "rag" && datasetFilter ? datasetFilter : undefined,
    status: filters.status && filters.status !== "all" ? (filters.status as TraceStatus) : undefined,
    model_id: filters.model_id?.trim() || undefined,
    user_id: filters.user_id?.trim() || undefined,
    session_id: filters.session_id?.trim() || undefined,
    run_id: filters.run_id?.trim() || undefined,
    request_id: filters.request_id?.trim() || undefined,
    transcript_query: filters.transcript_query?.trim() || undefined,
    turn_index: filters.turn_index,
    span_kind: filters.span_kind?.trim() || undefined,
    score_name: filters.score_name?.trim() || undefined,
    min_score: filters.min_score,
    max_score: filters.max_score,
    min_latency_ms: filters.min_latency_ms,
    max_latency_ms: filters.max_latency_ms,
    limit: 100,
    offset: 0,
  };
}

function asNumber(value: unknown, fallback = 0) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function pct(value: unknown) {
  return `${Math.round(asNumber(value) * 100)}%`;
}

type ExampleActionOptions = {
  split?: string;
  reviewStatus?: EvalReviewStatus;
  metadata?: Record<string, unknown>;
  expectedOutput?: Record<string, unknown>;
};

export function EvalPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { message, modal } = AntApp.useApp();
  const canRunEvaluations = usePermission("console:eval:run");
  const [filters, setFilters] = useState<AssistantTraceFilters>({
    status: "all",
    score_status: "all",
  });
  const [activeTraceFamily, setActiveTraceFamily] = useState<TraceFamily>("assistant");
  const [selectedTraceId, setSelectedTraceId] = useState<string | undefined>();
  const [pinnedTraceId, setPinnedTraceId] = useState<string | undefined>();
  const [scoreError, setScoreError] = useState<Error | null>(null);
  const [createdDataset, setCreatedDataset] = useState<EvalDataset | null>(null);
  const [createdEvaluator, setCreatedEvaluator] = useState<EvalEvaluator | null>(null);
  const [createdExperiment, setCreatedExperiment] = useState<EvalExperiment | null>(null);
  const [selectedDatasetId, setSelectedDatasetId] = useState<string | undefined>();
  const [selectedEvaluatorId, setSelectedEvaluatorId] = useState<string | undefined>();
  const [selectedExperimentId, setSelectedExperimentId] = useState<string | undefined>();
  const [datasetDraft, setDatasetDraft] = useState({
    name: "assistant-regression",
    description: "Assistant trace regression dataset",
    version: "v1",
    schemaText: JSON.stringify(
      { input: "bounded trace preview", expected_output: "bounded output preview" },
      null,
      2
    ),
  });
  const [exampleDraft, setExampleDraft] = useState({
    split: "regression",
    expectedOutputText: JSON.stringify({ output_preview: "expected bounded answer" }, null, 2),
  });
  const [evaluatorDraft, setEvaluatorDraft] = useState<{
    name: string;
    evaluator_type: EvalEvaluator["evaluator_type"];
    rubric: string;
    version: string;
    samplingConfigText: string;
    filterConfigText: string;
  }>({
    name: "quality",
    evaluator_type: "human",
    rubric: "Score helpfulness, grounding, and safety from bounded trace previews.",
    version: "v1",
    samplingConfigText: "{}",
    filterConfigText: "{}",
  });
  const [experimentDraft, setExperimentDraft] = useState({
    name: "assistant-baseline",
    description: "",
    targetConfigText: JSON.stringify({ trace_family: "assistant", model_id: "qwen3.7-plus" }, null, 2),
  });
  const [exportPreview, setExportPreview] = useState<EvalTraceExportResponse | null>(null);
  const [examplesExport, setExamplesExport] = useState<EvalExamplesExportResponse | null>(null);
  const [runComparison, setRunComparison] = useState<EvalExperimentRunComparisonResponse | null>(null);
  const [gateResult, setGateResult] = useState<EvalGateDryRunResponse | null>(null);
  const [queuedRunId, setQueuedRunId] = useState<string | undefined>();
  const [runMode, setRunMode] = useState<EvalExperimentRunMode>("live_candidate");
  const [repetitions, setRepetitions] = useState(3);
  const [systemPromptOverride, setSystemPromptOverride] = useState("");
  const [baselineRunId, setBaselineRunId] = useState<string>();
  const [candidateRunId, setCandidateRunId] = useState<string>();
  const [traceRunFocusRevision, setTraceRunFocusRevision] = useState(0);
  const [searchParams, setSearchParams] = useSearchParams();
  const kbDatasetFilter = searchParams.get("dataset_id") || "";
  const [activeWorkbenchTab, setActiveWorkbenchTab] = useState("overview");
  const [activeRunTab, setActiveRunTab] = useState("experiment_runs");
  const [activeAssetsTab, setActiveAssetsTab] = useState("golden_sets");
  const [contractPrefillRevision, setContractPrefillRevision] = useState(0);
  const serverFilters = useMemo(
    () => buildServerFilters(filters, activeTraceFamily, kbDatasetFilter),
    [activeTraceFamily, filters, kbDatasetFilter]
  );
  const traceFamilyEnabled =
    activeTraceFamily === "assistant"
    || activeTraceFamily === "langgraph_proxy"
    || activeTraceFamily === "rag";

  const summaryQuery = useQuery({
    queryKey: ["eval", "summary"],
    queryFn: () => getEvalSummary(7),
    staleTime: 30_000,
  });

  const dashboardQuery = useQuery({
    queryKey: ["eval", "dashboard"],
    queryFn: () => getEvalDashboard(7),
    staleTime: 30_000,
  });

  const tracesQuery = useQuery({
    queryKey: ["eval", "traces", activeTraceFamily, serverFilters],
    queryFn: () => listAgentTraces(serverFilters),
    enabled: traceFamilyEnabled,
    staleTime: 20_000,
  });

  const visibleTraces = useMemo(() => {
    if (!traceFamilyEnabled) return [];
    const traces = tracesQuery.data?.traces || [];
    return traces.filter((trace) => isWithinDateRange(trace, filters) && isWithinScoreFilter(trace, filters));
  }, [filters, traceFamilyEnabled, tracesQuery.data?.traces]);

  useEffect(() => {
    if (pinnedTraceId) return;
    if (visibleTraces.length === 0) {
      setSelectedTraceId(undefined);
      return;
    }
    if (!selectedTraceId || !visibleTraces.some((trace) => trace.trace_id === selectedTraceId)) {
      setSelectedTraceId(visibleTraces[0].trace_id);
    }
  }, [pinnedTraceId, selectedTraceId, visibleTraces]);

  const datasetsQuery = useQuery({
    queryKey: ["eval", "datasets"],
    queryFn: () => listEvalDatasets(),
    staleTime: 30_000,
  });

  const evaluatorsQuery = useQuery({
    queryKey: ["eval", "evaluators"],
    queryFn: () => listEvalEvaluators(),
    staleTime: 30_000,
  });

  const experimentsQuery = useQuery({
    queryKey: ["eval", "experiments"],
    queryFn: () => listEvalExperiments(),
    staleTime: 30_000,
  });
  const experimentDetailQuery = useQuery({
    queryKey: ["eval", "experiment", selectedExperimentId],
    queryFn: () => getEvalExperiment(selectedExperimentId || ""),
    enabled: Boolean(selectedExperimentId),
    staleTime: 10_000,
  });
  const datasets = useMemo(() => datasetsQuery.data?.datasets || [], [datasetsQuery.data?.datasets]);
  const evaluators = useMemo(() => evaluatorsQuery.data?.evaluators || [], [evaluatorsQuery.data?.evaluators]);
  const ragasEvaluators = useMemo(() => {
    const listed = evaluators.filter((evaluator) => evaluator.evaluator_type === "ragas");
    if (
      createdEvaluator?.evaluator_type === "ragas"
      && !listed.some((evaluator) => evaluator.evaluator_id === createdEvaluator.evaluator_id)
    ) {
      return [createdEvaluator, ...listed];
    }
    return listed;
  }, [createdEvaluator, evaluators]);
  const evaluatorTypeOptions = useMemo(
    () =>
      (["human", "rule", "trajectory", "span", "llm", "llm_judge", "composite", "ragas"] as const).map((value) => ({
        label: t(`eval.workbench.evaluatorTypes.${value}`),
        value,
      })),
    [t],
  );
  const experiments = useMemo(() => experimentsQuery.data?.experiments || [], [experimentsQuery.data?.experiments]);
  const activeDataset = useMemo(() => {
    if (selectedDatasetId) {
      return datasets.find((dataset) => dataset.dataset_id === selectedDatasetId)
        || (createdDataset?.dataset_id === selectedDatasetId ? createdDataset : null);
    }
    return createdDataset;
  }, [createdDataset, datasets, selectedDatasetId]);
  const activeEvaluator = useMemo(() => {
    if (selectedEvaluatorId) {
      return evaluators.find((evaluator) => evaluator.evaluator_id === selectedEvaluatorId)
        || (createdEvaluator?.evaluator_id === selectedEvaluatorId ? createdEvaluator : null);
    }
    return createdEvaluator;
  }, [createdEvaluator, evaluators, selectedEvaluatorId]);
  const activeRagasEvaluator = useMemo(() => {
    if (selectedEvaluatorId) {
      return ragasEvaluators.find((evaluator) => evaluator.evaluator_id === selectedEvaluatorId) || null;
    }
    return createdEvaluator?.evaluator_type === "ragas" ? createdEvaluator : null;
  }, [createdEvaluator, ragasEvaluators, selectedEvaluatorId]);
  const activeExperiment = useMemo(() => {
    if (selectedExperimentId) {
      return experimentDetailQuery.data
        || experiments.find((experiment) => experiment.experiment_id === selectedExperimentId)
        || (createdExperiment?.experiment_id === selectedExperimentId ? createdExperiment : null);
    }
    return createdExperiment;
  }, [createdExperiment, experimentDetailQuery.data, experiments, selectedExperimentId]);
  const comparableRuns = useMemo(() => activeExperiment?.runs || [], [activeExperiment?.runs]);
  const selectedRunSummary = useMemo(
    () => comparableRuns.find((run) => run.run_id === queuedRunId) || null,
    [comparableRuns, queuedRunId],
  );
  const selectedRunQuery = useQuery({
    queryKey: ["eval", "run", queuedRunId],
    queryFn: () => getEvalExperimentRun(queuedRunId || ""),
    enabled: Boolean(queuedRunId),
    initialData: selectedRunSummary || undefined,
    refetchInterval: (query) => {
      const run = query.state.data;
      return run?.status === "queued" || run?.status === "running" ? 2_000 : false;
    },
    retry: 2,
  });
  const latestRun = selectedRunQuery.data || selectedRunSummary;
  const terminalRunId = latestRun && latestRun.status !== "queued" && latestRun.status !== "running"
    ? latestRun.run_id
    : undefined;

  useEffect(() => {
    if (!selectedExperimentId && experiments[0]?.experiment_id) {
      setSelectedExperimentId(experiments[0].experiment_id);
      setSelectedDatasetId(experiments[0].dataset_id || undefined);
    }
  }, [experiments, selectedExperimentId]);

  useEffect(() => {
    setRunComparison(null);
    setGateResult(null);
    setQueuedRunId(undefined);
    setCandidateRunId(undefined);
    setBaselineRunId(undefined);
  }, [selectedExperimentId]);

  useEffect(() => {
    const officialBaseline = activeExperiment?.baseline_run_id || undefined;
    const successful = comparableRuns.filter((run) => run.status === "succeeded");
    setBaselineRunId((current) => current && comparableRuns.some((run) => run.run_id === current)
      ? current
      : officialBaseline || successful[1]?.run_id);
    setCandidateRunId((current) => current && comparableRuns.some((run) => run.run_id === current)
      ? current
      : successful.find((run) => run.run_id !== officialBaseline)?.run_id || comparableRuns[0]?.run_id);
  }, [activeExperiment?.baseline_run_id, comparableRuns]);

  useEffect(() => {
    if (!terminalRunId) return;
    void Promise.all([
      queryClient.invalidateQueries({ queryKey: ["eval", "experiment", selectedExperimentId] }),
      queryClient.invalidateQueries({ queryKey: ["eval", "run-results", terminalRunId] }),
      queryClient.invalidateQueries({ queryKey: ["eval", "dashboard"] }),
    ]);
  }, [queryClient, selectedExperimentId, terminalRunId]);

  useEffect(() => {
    const runId = searchParams.get("run_id");
    if (runId && runId !== queuedRunId) {
      setQueuedRunId(runId);
      setCandidateRunId(runId);
    }
  }, [queuedRunId, searchParams]);

  const examplesQuery = useQuery({
    queryKey: ["eval", "examples", activeDataset?.dataset_id],
    queryFn: () => listEvalExamples(activeDataset?.dataset_id || ""),
    enabled: Boolean(activeDataset?.dataset_id),
    staleTime: 20_000,
  });

  const detailQuery = useQuery({
    queryKey: ["eval", "trace-detail", activeTraceFamily, selectedTraceId],
    queryFn: () => getAgentTraceDetail(selectedTraceId || "", activeTraceFamily),
    enabled: traceFamilyEnabled && Boolean(selectedTraceId),
    staleTime: 20_000,
  });

  const scoreMutation = useMutation({
    mutationFn: async (payload: AgentTraceScoreCreate) => {
      if (!selectedTraceId) throw new Error(t("eval.score.selectTraceFirst"));
      return createAgentTraceScore(selectedTraceId, payload, activeTraceFamily);
    },
    onSuccess: async () => {
      setScoreError(null);
      message.success(t("eval.score.submitted"));
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["eval", "traces"] }),
        queryClient.invalidateQueries({ queryKey: ["eval", "trace-detail", activeTraceFamily, selectedTraceId] }),
      ]);
    },
    onError: (error) => {
      setScoreError(toError(error));
    },
  });

  const exportMutation = useMutation({
    mutationFn: async () => {
      if (!selectedTraceId) throw new Error(t("eval.workbench.selectTrace"));
      return exportAgentTrace(selectedTraceId, "openinference", activeTraceFamily);
    },
    onSuccess: (result) => {
      setExportPreview(result);
      message.success(t("eval.workbench.exportReady"));
    },
    onError: (error) => message.error(toError(error).message),
  });

  const datasetMutation = useMutation({
    mutationFn: () =>
      createEvalDataset({
        name: datasetDraft.name.trim() || "assistant-regression",
        description: datasetDraft.description.trim(),
        version: datasetDraft.version.trim() || "v1",
        schema: parseJsonObjectDraft(datasetDraft.schemaText, {
          input: "bounded trace preview",
          expected_output: "bounded output preview",
        }),
        metadata: { source: "eval_console", trace_family: activeTraceFamily },
      }),
    onSuccess: async (dataset) => {
      setCreatedDataset(dataset);
      setSelectedDatasetId(dataset.dataset_id);
      message.success(t("eval.workbench.datasetCreated"));
      await queryClient.invalidateQueries({ queryKey: ["eval", "datasets"] });
    },
    onError: (error) => message.error(toError(error).message),
  });

  const exampleMutation = useMutation({
    mutationFn: async (options?: ExampleActionOptions) => {
      if (!activeDataset) throw new Error(t("eval.workbench.createDatasetFirst"));
      if (!selectedTraceId) throw new Error(t("eval.workbench.selectTrace"));
      return createEvalExampleFromTrace(activeDataset.dataset_id, {
        source_trace_id: selectedTraceId,
        trace_family: activeTraceFamily,
        split: options?.split || exampleDraft.split.trim() || "regression",
        expected_output: options?.expectedOutput || parseJsonObjectDraft(exampleDraft.expectedOutputText, {}),
        metadata: {
          source: "eval_console",
          trace_family: activeTraceFamily,
          review_status: options?.reviewStatus || "approved",
          ...options?.metadata,
        },
      });
    },
    onSuccess: async () => {
      message.success(t("eval.workbench.exampleCreated"));
      await queryClient.invalidateQueries({ queryKey: ["eval", "examples"] });
    },
    onError: (error) => message.error(toError(error).message),
  });

  const prefillSelectedTraceContract = () => {
    setActiveAssetsTab("golden_sets");
    setContractPrefillRevision((revision) => revision + 1);
    setActiveWorkbenchTab("assets");
  };

  const promoteSelectedTraceToGolden = prefillSelectedTraceContract;

  const addSelectedTraceToReview = () =>
    exampleMutation.mutate({ reviewStatus: "pending", metadata: { tags: ["review"] } });

  const createSelectedFailureCase = prefillSelectedTraceContract;

  const createRagasEvaluatorMutation = useMutation({
    mutationFn: () => {
      const preset = evaluatorPresetForType("ragas", "rag", (key, fallback) => t(key, fallback));
      return createEvalEvaluator({
        name: "kb-ragas",
        evaluator_type: "ragas",
        rubric: preset.rubric,
        version: "v1",
        sampling_config: parseJsonObjectDraft(preset.samplingConfigText, {}),
        filter_config: parseJsonObjectDraft(preset.filterConfigText, {}),
        metadata: { source: "eval_console", trace_family: "rag" },
      });
    },
    onSuccess: async (evaluator) => {
      setCreatedEvaluator(evaluator);
      setSelectedEvaluatorId(evaluator.evaluator_id);
      message.success(t("eval.ragas.evaluatorCreated"));
      await queryClient.invalidateQueries({ queryKey: ["eval", "evaluators"] });
    },
    onError: (error) => message.error(toError(error).message),
  });

  const evaluatorMutation = useMutation({
    mutationFn: () => {
      const metadata: Record<string, unknown> = {
        source: "eval_console",
        trace_family: activeTraceFamily,
      };
      if (evaluatorDraft.evaluator_type === "llm" || evaluatorDraft.evaluator_type === "llm_judge") {
        metadata.judge_model_id = "qwen3.7-plus";
      }
      return createEvalEvaluator({
        name: evaluatorDraft.name.trim() || "quality",
        evaluator_type: evaluatorDraft.evaluator_type,
        rubric: evaluatorDraft.rubric.trim(),
        version: evaluatorDraft.version.trim() || "v1",
        sampling_config: parseJsonObjectDraft(evaluatorDraft.samplingConfigText, {}),
        filter_config: parseJsonObjectDraft(evaluatorDraft.filterConfigText, {}),
        metadata,
      });
    },
    onSuccess: async (evaluator) => {
      setCreatedEvaluator(evaluator);
      setSelectedEvaluatorId(evaluator.evaluator_id);
      message.success(t("eval.workbench.evaluatorCreated"));
      await queryClient.invalidateQueries({ queryKey: ["eval", "evaluators"] });
    },
    onError: (error) => message.error(toError(error).message),
  });

  const experimentMutation = useMutation({
    mutationFn: () =>
      createEvalExperiment({
        name: experimentDraft.name.trim() || "assistant-baseline",
        description: experimentDraft.description.trim(),
        dataset_id: activeDataset?.dataset_id || null,
        target_config: {
          ...parseJsonObjectDraft(experimentDraft.targetConfigText, {}),
          trace_family: activeTraceFamily,
          model_id: serverFilters.model_id || "qwen3.7-plus",
        },
        metadata: { source: "eval_console", trace_family: activeTraceFamily },
      }),
    onSuccess: async (experiment) => {
      setCreatedExperiment(experiment);
      setSelectedExperimentId(experiment.experiment_id);
      message.success(t("eval.workbench.experimentCreated"));
      await queryClient.invalidateQueries({ queryKey: ["eval", "experiments"] });
    },
    onError: (error) => message.error(toError(error).message),
  });

  const evaluatorRunMutation = useMutation({
    mutationFn: async () => {
      if (!activeEvaluator) throw new Error(t("eval.workbench.createEvaluatorFirst"));
      return runEvalEvaluatorAsync(activeEvaluator.evaluator_id, {
        experiment_id: activeExperiment?.experiment_id || null,
        dataset_id: activeDataset?.dataset_id || null,
        trace_id: selectedTraceId || null,
        target_snapshot: {
          trace_family: activeTraceFamily,
          trace_id: selectedTraceId || null,
          dataset_id: activeDataset?.dataset_id || null,
          experiment_id: activeExperiment?.experiment_id || null,
          evaluator_id: activeEvaluator.evaluator_id,
          target_config: activeExperiment?.target_config || {},
        },
        metadata: { source: "eval_console", trace_family: activeTraceFamily },
      });
    },
    onSuccess: (job) => {
      if (job.run_id) {
        setQueuedRunId(job.run_id);
        setCandidateRunId(job.run_id);
      }
      message.success(t("eval.workbench.evaluatorQueued"));
    },
    onError: (error) => message.error(toError(error).message),
  });

  const ragasEvaluatorRunMutation = useMutation({
    mutationFn: async () => {
      if (!activeRagasEvaluator) throw new Error(t("eval.workbench.createEvaluatorFirst"));
      return runEvalEvaluatorAsync(activeRagasEvaluator.evaluator_id, {
        experiment_id: null,
        dataset_id: null,
        trace_id: selectedTraceId || null,
        target_snapshot: {
          trace_family: "rag",
          trace_id: selectedTraceId || null,
          evaluator_id: activeRagasEvaluator.evaluator_id,
        },
        metadata: { source: "eval_console", trace_family: "rag", evaluator_type: "ragas" },
      });
    },
    onSuccess: (job) => {
      if (job.run_id) {
        setQueuedRunId(job.run_id);
        setCandidateRunId(job.run_id);
      }
      message.success(t("eval.workbench.evaluatorQueued"));
    },
    onError: (error) => message.error(toError(error).message),
  });

  const experimentBatchMutation = useMutation({
    mutationFn: async () => {
      if (!activeExperiment) throw new Error(t("eval.workbench.createExperimentFirst", "Create an experiment first"));
      if (!activeEvaluator) throw new Error(t("eval.workbench.createEvaluatorFirst"));
      const datasetId = activeDataset?.dataset_id || activeExperiment.dataset_id || null;
      return runEvalExperiment(activeExperiment.experiment_id, {
        dataset_id: datasetId,
        evaluator_ids: [activeEvaluator.evaluator_id],
        run_mode: runMode,
        repetitions: runMode === "live_candidate" ? repetitions : 1,
        baseline_run_id: baselineRunId || activeExperiment.baseline_run_id || null,
        candidate_config: systemPromptOverride.trim()
          ? { system_prompt_override: systemPromptOverride.trim() }
          : undefined,
        candidate_label: "candidate",
        baseline_label: "baseline",
        target_snapshot: {
          trace_family: activeTraceFamily,
          dataset_id: datasetId,
          trace_id: datasetId ? null : selectedTraceId || null,
          run_mode: runMode,
        },
        metadata: { source: "eval_console", trace_family: activeTraceFamily },
      });
    },
    onSuccess: (batch) => {
      const firstRunId = batch.jobs[0]?.run_id;
      if (firstRunId) {
        setQueuedRunId(firstRunId);
        setCandidateRunId(firstRunId);
        setRunComparison(null);
        setGateResult(null);
        const nextParams = new URLSearchParams(searchParams);
        nextParams.set("run_id", firstRunId);
        setSearchParams(nextParams, { replace: true });
      }
      message.success(t("eval.workbench.evaluatorQueued"));
    },
    onError: (error) => message.error(toError(error).message),
  });

  const examplesExportMutation = useMutation({
    mutationFn: async () => {
      if (!activeDataset) throw new Error(t("eval.workbench.createDatasetFirst"));
      return exportEvalExamples(activeDataset.dataset_id);
    },
    onSuccess: (result) => {
      setExamplesExport(result);
      message.success(t("eval.workbench.exportReady"));
    },
    onError: (error) => message.error(toError(error).message),
  });

  const examplesImportMutation = useMutation({
    mutationFn: async () => {
      if (!activeDataset) throw new Error(t("eval.workbench.createDatasetFirst"));
      const messageText = String(detailQuery.data?.trace.input_preview || "").trim();
      if (!selectedTraceId || !messageText) throw new Error(t("eval.workbench.selectTrace"));
      return importEvalExamples(activeDataset.dataset_id, [
        {
          case_id: `console.${Date.now()}`,
          split: exampleDraft.split.trim() || "regression",
          input: { message: messageText, source: "eval_console_seed", trace_id: selectedTraceId },
          expected_output: parseJsonObjectDraft(exampleDraft.expectedOutputText, {}),
          expected_trajectory: { required_span_kinds: ["lifecycle", "model_invocation"] },
          assertions: [{ type: "no_sensitive_output" }],
          metadata: {
            review_status: "pending",
            behavior_confirmed: false,
            tags: ["console-import"],
          },
        },
      ]);
    },
    onSuccess: async () => {
      message.success(t("eval.workbench.exampleCreated"));
      await queryClient.invalidateQueries({ queryKey: ["eval", "examples"] });
    },
    onError: (error) => message.error(toError(error).message),
  });

  const reviewMutation = useMutation({
    mutationFn: async (payload: { exampleId: string; status: "approved" | "rejected" | "needs_fix" }) => {
      if (!activeDataset) throw new Error(t("eval.workbench.createDatasetFirst"));
      return updateEvalExample(activeDataset.dataset_id, payload.exampleId, {
        review_status: payload.status,
        metadata: { reviewed_from: "eval_console" },
      });
    },
    onSuccess: async () => {
      message.success(t("eval.score.submitted"));
      await queryClient.invalidateQueries({ queryKey: ["eval", "examples"] });
    },
    onError: (error) => message.error(toError(error).message),
  });

  const baselineRun = comparableRuns.find((run) => run.run_id === baselineRunId) || null;
  const candidateRun = comparableRuns.find((run) => run.run_id === candidateRunId) || null;
  const canCompareRuns = Boolean(
    baselineRunId
    && candidateRunId
    && baselineRunId !== candidateRunId
    && baselineRun?.status === "succeeded"
    && candidateRun?.status === "succeeded"
  );
  const latestScoreSummary = latestRun?.score_summary || {};
  const canDryRunGate = Boolean(
    latestRun?.status === "succeeded"
    && typeof (latestScoreSummary.overall_score ?? latestScoreSummary.average_score) === "number"
    && typeof latestScoreSummary.trajectory_pass_rate === "number"
    && typeof latestScoreSummary.critical_pass_rate === "number"
  );
  const effectiveRunDatasetId = activeDataset?.dataset_id || activeExperiment?.dataset_id || null;
  const canRunExperiment = Boolean(
    canRunEvaluations
    && activeExperiment
    && activeEvaluator
    && (runMode === "live_candidate"
      ? effectiveRunDatasetId
      : effectiveRunDatasetId || selectedTraceId)
  );
  const missingRunInputs = [
    !activeExperiment ? "experiment" : null,
    !activeEvaluator ? "evaluator" : null,
    runMode === "live_candidate"
      ? !effectiveRunDatasetId ? "test set" : null
      : !effectiveRunDatasetId && !selectedTraceId ? "test set or trace" : null,
  ].filter(Boolean);

  const selectRun = (runId: string) => {
    setQueuedRunId(runId);
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set("run_id", runId);
    setSearchParams(nextParams, { replace: true });
  };

  const compareMutation = useMutation({
    mutationFn: async () => {
      const candidate = candidateRunId;
      const baseline = baselineRunId;
      if (!baseline || !candidate) {
        throw new Error(t("eval.workbench.needTwoRuns", "Need two runs to compare"));
      }
      return compareEvalExperimentRuns(baseline, candidate);
    },
    onMutate: () => setRunComparison(null),
    onSuccess: (result) => setRunComparison(result),
    onError: (error) => message.error(toError(error).message),
  });

  const promoteBaselineMutation = useMutation({
    mutationFn: async () => {
      if (!activeExperiment || !candidateRunId) {
        throw new Error(t("eval.workbench.selectCandidateFirst", "Select a candidate run first"));
      }
      return promoteEvalExperimentBaseline(activeExperiment.experiment_id, candidateRunId);
    },
    onSuccess: async (promotion) => {
      setBaselineRunId(promotion.baseline_run_id);
      setRunComparison(null);
      message.success(t("eval.workbench.baselinePromoted", "Baseline promoted"));
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["eval", "experiments"] }),
        queryClient.invalidateQueries({ queryKey: ["eval", "experiment", selectedExperimentId] }),
      ]);
    },
    onError: (error) => message.error(toError(error).message),
  });

  const gateMutation = useMutation({
    mutationFn: () =>
      dryRunEvalGate({
        result_payload: {
          metrics: {
            overall_score: asNumber(latestScoreSummary.overall_score ?? latestScoreSummary.average_score),
            trajectory_pass_rate: asNumber(latestScoreSummary.trajectory_pass_rate),
            critical_pass_rate: asNumber(latestScoreSummary.critical_pass_rate),
          },
        },
      }),
    onSuccess: (result) => setGateResult(result),
    onError: (error) => message.error(toError(error).message),
  });

  const runResultsQuery = useQuery({
    queryKey: ["eval", "run-results", queuedRunId],
    queryFn: () => getEvalExperimentRunResults(queuedRunId || ""),
    enabled: Boolean(
      queuedRunId
      && latestRun
      && latestRun.run_id === queuedRunId
      && latestRun.status !== "queued"
      && latestRun.status !== "running"
    ),
    staleTime: 10_000,
  });

  const traceListCopy = useMemo(() => {
    if (activeTraceFamily === "rag") {
      return {
        title: t("eval.list.ragTitle"),
        empty: t("eval.list.ragEmpty"),
        aria: t("eval.list.ragAriaLabel"),
      };
    }
    if (activeTraceFamily === "langgraph_proxy") {
      return {
        title: t("eval.list.langgraphTitle"),
        empty: t("eval.list.langgraphEmpty"),
        aria: t("eval.list.langgraphAriaLabel"),
      };
    }
    return {
      title: t("eval.list.title"),
      empty: t("eval.list.empty"),
      aria: t("eval.list.ariaLabel"),
    };
  }, [activeTraceFamily, t]);
  const hasCapturedFamilyTraces = (tracesQuery.data?.total || 0) > 0;
  const familyCoverageMessage = useMemo(() => {
    if (activeTraceFamily === "assistant") return null;
    if (hasCapturedFamilyTraces) {
      return t("eval.workbench.familyCovered", "Trace family wired with captured backend data");
    }
    return t("eval.workbench.familyPartial", "Trace family wired but not fully covered");
  }, [activeTraceFamily, hasCapturedFamilyTraces, t]);

  const handleTraceFamilyChange = (key: string) => {
    const nextFamily = key as TraceFamily;
    setPinnedTraceId(undefined);
    setActiveTraceFamily(nextFamily);
    setFilters({ status: "all", score_status: "all" });
    setSelectedTraceId(undefined);
    setScoreError(null);
    setExportPreview(null);
  };

  const handleWorkbenchTabChange = (key: string) => {
    setActiveWorkbenchTab(key);
  };

  const openRunResultTrace = (item: EvalExperimentCaseResult) => {
    const family = item.trace.trace_family || activeTraceFamily;
    if (family !== activeTraceFamily) handleTraceFamilyChange(family);
    setPinnedTraceId(item.candidate_trace_id);
    setSelectedTraceId(item.candidate_trace_id);
    setTraceRunFocusRevision((revision) => revision + 1);
    setActiveWorkbenchTab("traces");
  };

  const openComparedTrace = (traceId: string) => {
    setPinnedTraceId(traceId);
    setSelectedTraceId(traceId);
    setTraceRunFocusRevision((revision) => revision + 1);
    setActiveWorkbenchTab("traces");
  };

  const dashboardMetrics = dashboardQuery.data?.metrics || {};
  const runtimeHealth = dashboardQuery.data?.runtime_health || {};
  const overviewTab = (
    <>
      {summaryQuery.error || dashboardQuery.error ? (
        <Alert
          type="error"
          showIcon
          title={t("eval.workbench.overviewLoadFailed", "Could not load evaluation overview")}
          description={toError(summaryQuery.error || dashboardQuery.error).message}
          action={<Button onClick={() => void Promise.all([summaryQuery.refetch(), dashboardQuery.refetch()])}>{t("common.retry", "Retry")}</Button>}
        />
      ) : null}
      <div className="eval-platform-grid">
      {[
        { key: "rag", label: t("eval.summary.ragTraces"), value: summaryQuery.data?.rag_traces ?? "—" },
        { key: "captured", label: t("eval.summary.totalTraces"), value: summaryQuery.data?.total_traces ?? "—" },
        { key: "scored", label: t("eval.summary.scoredTraces"), value: summaryQuery.data?.scored_traces ?? "—" },
        { key: "golden", label: t("eval.workbench.goldenCases", "Golden cases"), value: dashboardMetrics.example_count ?? "—" },
        { key: "pass", label: t("eval.workbench.passRate", "Pass rate"), value: pct(dashboardMetrics.pass_rate) },
        { key: "trajectory", label: t("eval.workbench.trajectoryPass", "Trajectory pass"), value: pct(dashboardMetrics.trajectory_pass_rate) },
        { key: "critical", label: t("eval.workbench.criticalFailures", "Critical failures"), value: dashboardMetrics.critical_failures ?? 0 },
        { key: "toolSafety", label: t("eval.workbench.toolSafetyFailures", "Tool-safety failures"), value: runtimeHealth.tool_safety_failures ?? 0 },
        { key: "outbox", label: t("eval.workbench.outboxFailures", "Outbox failures"), value: dashboardQuery.data?.queue_health?.failed_jobs ?? 0 },
        { key: "judge", label: t("eval.workbench.judgePending", "Judge pending"), value: dashboardMetrics.judge_pending_count ?? 0 },
      ].map((card) => (
        <article key={card.key} className="eval-summary-card">
          <span className="eval-summary-label">{card.label}</span>
          <strong className="eval-summary-value">{card.value}</strong>
        </article>
      ))}
      <section className="eval-panel eval-workbench-panel eval-platform-wide">
        <div className="eval-panel-heading">
          <div>
            <h2>{t("eval.workbench.platformHealth", "Platform health")}</h2>
            <p>{t("eval.workbench.platformHealthDescription", "Latest run, queue, and offline gate readiness")}</p>
          </div>
          <ShieldCheck size={22} />
        </div>
        <div className="eval-overview-bars">
          <div>
            <span>{t("eval.workbench.passRate", "Pass rate")}</span>
            <Progress percent={Math.round(asNumber(dashboardMetrics.pass_rate) * 100)} size="small" />
          </div>
          <div>
            <span>{t("eval.workbench.trajectoryPass", "Trajectory pass")}</span>
            <Progress percent={Math.round(asNumber(dashboardMetrics.trajectory_pass_rate) * 100)} size="small" />
          </div>
        </div>
        <Descriptions
          className="eval-workbench-descriptions"
          size="small"
          bordered
          column={1}
          items={[
            { key: "assistant", label: t("eval.workbench.assistantRuntime", "Assistant runtime"), children: String(runtimeHealth.assistant_status || "unknown") },
            { key: "ragRuntime", label: t("eval.workbench.ragRuntime", "RAG runtime"), children: String(runtimeHealth.rag_status || "partial") },
            { key: "langgraphRuntime", label: t("eval.workbench.langgraphRuntime", "LangGraph runtime"), children: String(runtimeHealth.langgraph_status || "partial") },
            { key: "trajectoryTraces", label: t("eval.workbench.runtimeTrajectoryTraces", "Runtime trajectories"), children: String(runtimeHealth.runtime_trajectory_traces ?? 0) },
            { key: "traceWriterIssues", label: t("eval.workbench.traceWriterIssues", "Trace writer issues"), children: String(runtimeHealth.trace_writer_issue_traces ?? 0) },
            { key: "baseline", label: t("eval.workbench.latestBaseline", "Latest baseline"), children: String(dashboardMetrics.latest_baseline || "-") },
            { key: "candidate", label: t("eval.workbench.latestCandidate", "Latest candidate"), children: String(dashboardMetrics.latest_candidate || "-") },
            { key: "gate", label: t("eval.workbench.latestGate", "Latest gate"), children: String(dashboardQuery.data?.latest_gate_status?.status || "not_run") },
          ]}
        />
      </section>
      </div>
    </>
  );

  const traceExplorerTab = (
    <TraceExplorerShell
      activeTraceFamily={activeTraceFamily}
      onTraceFamilyChange={handleTraceFamilyChange}
      familyCoverageMessage={familyCoverageMessage}
      hasCapturedFamilyTraces={hasCapturedFamilyTraces}
      traces={visibleTraces}
      traceTotal={tracesQuery.data?.total || 0}
      filters={filters}
      setFilters={setFilters}
      traceListCopy={traceListCopy}
      selectedTraceId={selectedTraceId}
      runFocusRevision={traceRunFocusRevision}
      onSelectTrace={(traceId) => {
        setPinnedTraceId(undefined);
        setSelectedTraceId(traceId);
      }}
      onRefresh={() => tracesQuery.refetch()}
      tracesLoading={tracesQuery.isLoading || tracesQuery.isFetching}
      tracesError={tracesQuery.error ? toError(tracesQuery.error) : null}
      detail={detailQuery.data}
      detailLoading={detailQuery.isLoading || detailQuery.isFetching}
      detailError={detailQuery.error ? toError(detailQuery.error) : null}
      scoreError={scoreError}
      scoreSubmitting={scoreMutation.isPending}
      onScoreSubmit={async (payload) => {
        await scoreMutation.mutateAsync(payload);
      }}
      exportPreview={exportPreview}
      exportLoading={exportMutation.isPending}
      onExport={() => exportMutation.mutate()}
      activeDatasetName={activeDataset?.name || null}
      datasetActionLoading={exampleMutation.isPending}
      onPromoteToGolden={promoteSelectedTraceToGolden}
      onAddToReview={addSelectedTraceToReview}
      onCreateFailureCase={createSelectedFailureCase}
      dashboard={dashboardQuery.data}
      readOnly={!canRunEvaluations}
    />
  );

  const goldenSetsTab = (
    <WorkbenchPanel
      title={t("eval.workbench.goldenSets", "Golden Sets")}
      description={t("eval.workbench.goldenSetsDescription", "Versioned regression cases backed by repo JSONL and DB review state.")}
      icon={<Database size={22} />}
    >
      {datasetsQuery.error || examplesQuery.error ? (
        <Alert
          type="error"
          showIcon
          title={t("eval.workbench.assetsLoadFailed", "Could not load evaluation assets")}
          description={toError(datasetsQuery.error || examplesQuery.error).message}
          action={<Button onClick={() => void Promise.all([datasetsQuery.refetch(), examplesQuery.refetch()])}>{t("common.retry", "Retry")}</Button>}
        />
      ) : null}
      <div className="eval-workbench-form-grid">
        <Select
          className="eval-workbench-wide"
          allowClear
          aria-label={t("eval.workbench.selectDataset", "Select dataset")}
          placeholder={t("eval.workbench.selectDataset", "Select dataset")}
          value={selectedDatasetId}
          options={datasets.map((dataset) => ({
            label: `${dataset.name} (${dataset.version})`,
            value: dataset.dataset_id,
          }))}
          onChange={(value) => setSelectedDatasetId(value)}
        />
        <Input
          aria-label={t("eval.workbench.datasetName", "Dataset name")}
          value={datasetDraft.name}
          onChange={(event) => setDatasetDraft((draft) => ({ ...draft, name: event.target.value }))}
        />
        <Input
          aria-label={t("eval.workbench.version")}
          value={datasetDraft.version}
          onChange={(event) => setDatasetDraft((draft) => ({ ...draft, version: event.target.value }))}
        />
        <Input
          className="eval-workbench-wide"
          aria-label={t("eval.workbench.datasetDescription", "Dataset description")}
          value={datasetDraft.description}
          onChange={(event) => setDatasetDraft((draft) => ({ ...draft, description: event.target.value }))}
        />
        <Input
          aria-label={t("eval.workbench.exampleSplit", "Example split")}
          value={exampleDraft.split}
          onChange={(event) => setExampleDraft((draft) => ({ ...draft, split: event.target.value }))}
        />
        <Input.TextArea
          className="eval-workbench-textarea eval-workbench-wide"
          aria-label={t("eval.workbench.datasetSchema", "Dataset schema JSON")}
          value={datasetDraft.schemaText}
          autoSize={{ minRows: 3, maxRows: 6 }}
          onChange={(event) => setDatasetDraft((draft) => ({ ...draft, schemaText: event.target.value }))}
        />
        <Input.TextArea
          className="eval-workbench-textarea eval-workbench-wide"
          aria-label={t("eval.workbench.expectedOutput", "Expected output JSON")}
          value={exampleDraft.expectedOutputText}
          autoSize={{ minRows: 2, maxRows: 5 }}
          onChange={(event) => setExampleDraft((draft) => ({ ...draft, expectedOutputText: event.target.value }))}
        />
      </div>
      <Space size={10} wrap>
        <Button
          type="primary"
          icon={<Database size={15} />}
          onClick={() => datasetMutation.mutate()}
          loading={datasetMutation.isPending}
          disabled={!canRunEvaluations}
        >
          {t("eval.workbench.createDataset")}
        </Button>
        <Button
          icon={<SearchCheck size={15} />}
          onClick={promoteSelectedTraceToGolden}
          loading={exampleMutation.isPending}
          disabled={!canRunEvaluations || !activeDataset || !selectedTraceId}
        >
          {t("eval.workbench.addTraceToDataset")}
        </Button>
        <Button
          icon={<Download size={15} />}
          onClick={() => examplesExportMutation.mutate()}
          loading={examplesExportMutation.isPending}
          disabled={!activeDataset}
        >
          {t("eval.workbench.exportJsonl", "Export JSONL")}
        </Button>
        <Button
          icon={<Database size={15} />}
          onClick={() => examplesImportMutation.mutate()}
          loading={examplesImportMutation.isPending}
          disabled={!canRunEvaluations || !activeDataset || !selectedTraceId || !detailQuery.data?.trace.input_preview}
        >
          {t("eval.workbench.importSeed", "Import seed case")}
        </Button>
      </Space>
      <Descriptions
        className="eval-workbench-descriptions"
        size="small"
        bordered
        column={1}
        items={[
          { key: "dataset", label: t("eval.workbench.currentDataset"), children: activeDataset?.name || "-" },
          { key: "version", label: t("eval.workbench.version"), children: activeDataset?.version || "-" },
          { key: "source", label: t("eval.workbench.selectedTrace"), children: selectedTraceId || "-" },
          { key: "listed", label: t("eval.workbench.listedDatasets", "Listed datasets"), children: String(datasetsQuery.data?.total ?? 0) },
          { key: "cases", label: t("eval.workbench.goldenCases", "Golden cases"), children: String(examplesQuery.data?.total ?? 0) },
          { key: "exported", label: t("eval.workbench.exportedCases", "Exported cases"), children: String(examplesExport?.examples.length ?? 0) },
        ]}
      />
      <BehaviorContractEditor
        datasetId={activeDataset?.dataset_id ?? null}
        examples={examplesQuery.data?.examples || []}
        selectedTrace={detailQuery.data}
        prefillRevision={contractPrefillRevision}
        readOnly={!canRunEvaluations}
        onSaved={async () => {
          await queryClient.invalidateQueries({ queryKey: ["eval", "examples"] });
          await queryClient.invalidateQueries({ queryKey: ["eval", "dashboard"] });
        }}
      />
      <GoldenJsonlImport
        datasetId={activeDataset?.dataset_id ?? null}
        readOnly={!canRunEvaluations}
        onImported={async () => {
          await queryClient.invalidateQueries({ queryKey: ["eval", "examples"] });
          await queryClient.invalidateQueries({ queryKey: ["eval", "dashboard"] });
        }}
      />
    </WorkbenchPanel>
  );

  const experimentsTab = (
    <WorkbenchPanel
      title={t("eval.workbench.runAndResults", "Run & Results")}
      description={t("eval.workbench.runAndResultsDescription", "Select a test set and evaluator, run it, then inspect scores and failed traces in one place.")}
      icon={<Beaker size={22} />}
    >
      {!canRunEvaluations ? (
        <Alert type="info" showIcon title={t("eval.workbench.readOnly", "Read-only mode: you can inspect results but cannot start or edit evaluations.")} />
      ) : null}
      {experimentsQuery.error || experimentDetailQuery.error || datasetsQuery.error || evaluatorsQuery.error ? (
        <Alert
          type="error"
          showIcon
          title={t("eval.workbench.runConfigLoadFailed", "Could not load run configuration")}
          description={toError(experimentsQuery.error || experimentDetailQuery.error || datasetsQuery.error || evaluatorsQuery.error).message}
          action={<Button onClick={() => void Promise.all([experimentsQuery.refetch(), experimentDetailQuery.refetch(), datasetsQuery.refetch(), evaluatorsQuery.refetch()])}>{t("common.retry", "Retry")}</Button>}
        />
      ) : null}
      <div className="eval-workbench-form-grid">
        <label className="eval-field">
          <span>{t("eval.workbench.selectExperiment", "Select experiment")}</span>
          <Select
            allowClear
            value={selectedExperimentId}
            options={experiments.map((experiment) => ({ label: experiment.name, value: experiment.experiment_id }))}
            onChange={(value) => {
              setSelectedExperimentId(value);
              setSelectedDatasetId(
                experiments.find((experiment) => experiment.experiment_id === value)?.dataset_id
                || undefined,
              );
              const nextParams = new URLSearchParams(searchParams);
              nextParams.delete("run_id");
              setSearchParams(nextParams, { replace: true });
            }}
          />
        </label>
        <label className="eval-field">
          <span>{t("eval.workbench.selectDataset", "Select test set")}</span>
          <Select allowClear value={selectedDatasetId} options={datasets.map((dataset) => ({ label: `${dataset.name} (${dataset.version})`, value: dataset.dataset_id }))} onChange={setSelectedDatasetId} />
        </label>
        <label className="eval-field">
          <span>{t("eval.workbench.selectEvaluator", "Select evaluator")}</span>
          <Select allowClear value={selectedEvaluatorId} options={evaluators.map((evaluator) => ({ label: `${evaluator.name} · ${evaluator.evaluator_type}`, value: evaluator.evaluator_id }))} onChange={setSelectedEvaluatorId} />
        </label>
        <label className="eval-field">
          <span>{t("eval.workbench.runMode", "Run mode")}</span>
          <Select<EvalExperimentRunMode>
            value={runMode}
            options={[
              { label: t("eval.workbench.liveCandidate", "Run current Agent"), value: "live_candidate" },
              { label: t("eval.workbench.rescoreTrace", "Re-score stored traces"), value: "rescore_trace" },
            ]}
            onChange={(value) => setRunMode(value)}
          />
        </label>
        <label className="eval-field">
          <span>{t("eval.workbench.repetitions", "Repetitions")}</span>
          <InputNumber min={1} max={10} value={runMode === "live_candidate" ? repetitions : 1} disabled={runMode !== "live_candidate"} onChange={(value) => setRepetitions(value ?? 3)} />
        </label>
        <label className="eval-field eval-field-wide">
          <span>{t("eval.workbench.promptOverride", "System prompt override (optional)")}</span>
          <Input.TextArea
            value={systemPromptOverride}
            disabled={runMode !== "live_candidate"}
            placeholder={t("eval.workbench.promptOverrideHint", "Leave empty to evaluate the deployed prompt")}
            autoSize={{ minRows: 2, maxRows: 6 }}
            onChange={(event) => setSystemPromptOverride(event.target.value)}
          />
        </label>
      </div>
      {missingRunInputs.length ? (
        <Alert
          type="warning"
          showIcon
          title={t("eval.workbench.missingRunInputs", "Select {{items}} before running.", { items: missingRunInputs.join(", ") })}
        />
      ) : null}
      <Space size={10} wrap>
        <Button
          type="primary"
          icon={<Play size={15} />}
          onClick={() => experimentBatchMutation.mutate()}
          loading={experimentBatchMutation.isPending}
          disabled={!canRunExperiment}
        >
          {runMode === "live_candidate" ? t("eval.workbench.runCurrentAgent", "Run current Agent") : t("eval.workbench.rescoreStored", "Re-score stored traces")}
        </Button>
      </Space>
      <Descriptions
        className="eval-workbench-descriptions"
        size="small"
        bordered
        column={{ xs: 1, sm: 2, lg: 4 }}
        items={[
          { key: "mode", label: t("eval.workbench.runMode", "Run mode"), children: runMode },
          { key: "calls", label: t("eval.workbench.estimatedCalls", "Estimated calls"), children: effectiveRunDatasetId ? String((examplesQuery.data?.total || 0) * (runMode === "live_candidate" ? repetitions : 1)) : "1" },
          { key: "baseline", label: t("eval.workbench.currentBaseline", "Current baseline"), children: activeExperiment?.baseline_run_id || t("eval.workbench.noBaseline", "Not set") },
          { key: "fingerprint", label: t("eval.workbench.candidateFingerprint", "Candidate fingerprint"), children: String(asRecord(latestRun?.metrics?.actual_fingerprint).system_prompt_hash || latestRun?.candidate_fingerprint?.prompt_override_hash || latestRun?.runtime_fingerprint?.prompt_hash || activeExperiment?.target_config?.prompt_hash || "—") },
        ]}
      />
      <div className="eval-comparison-controls">
        <label className="eval-field">
          <span>{t("eval.workbench.baselineRun", "Baseline run")}</span>
          <Select
            allowClear
            value={baselineRunId}
            placeholder={t("eval.workbench.selectBaseline", "Select baseline")}
            options={comparableRuns.map((run) => ({ label: `${run.status} · ${(run.created_at || run.run_id).slice(0, 19)}`, value: run.run_id, disabled: run.status !== "succeeded" }))}
            onChange={(value) => { setBaselineRunId(value); setRunComparison(null); setGateResult(null); }}
          />
        </label>
        <label className="eval-field">
          <span>{t("eval.workbench.candidateRun", "Candidate run")}</span>
          <Select
            allowClear
            value={candidateRunId}
            placeholder={t("eval.workbench.selectCandidate", "Select candidate")}
            options={comparableRuns.map((run) => ({ label: `${run.status} · ${(run.created_at || run.run_id).slice(0, 19)}`, value: run.run_id, disabled: run.status !== "succeeded" }))}
            onChange={(value) => {
              setCandidateRunId(value);
              setRunComparison(null);
              setGateResult(null);
              if (value) selectRun(value);
            }}
          />
        </label>
        <Button
          icon={<GitCompare size={15} />}
          onClick={() => compareMutation.mutate()}
          loading={compareMutation.isPending}
          disabled={!canCompareRuns}
        >
          {t("eval.workbench.compareRuns", "Compare runs")}
        </Button>
        <Button
          onClick={() => modal.confirm({
            title: t("eval.workbench.promoteBaseline", "Set candidate as baseline?"),
            content: t("eval.workbench.promoteBaselineHint", "Only a successful compatible live run that passes critical gates can be promoted."),
            okText: t("eval.workbench.confirmPromote", "Promote baseline"),
            onOk: () => promoteBaselineMutation.mutateAsync(),
          })}
          loading={promoteBaselineMutation.isPending}
          disabled={
            !canRunEvaluations
            || !candidateRun
            || candidateRun.status !== "succeeded"
            || (candidateRun.run_mode || candidateRun.target_snapshot?.run_mode) !== "live_candidate"
            || candidateRunId === activeExperiment?.baseline_run_id
          }
        >
          {t("eval.workbench.setBaseline", "Set as baseline")}
        </Button>
      </div>
      {compareMutation.error ? (
        <Alert
          type="error"
          showIcon
          title={t("eval.workbench.compareFailed", "Could not compare these runs")}
          description={toError(compareMutation.error).message}
        />
      ) : null}
      <ExperimentRunComparison
        comparison={runComparison}
        runs={comparableRuns}
        baselineRunId={activeExperiment?.baseline_run_id || baselineRunId}
        onOpenTrace={openComparedTrace}
      />
      <ExperimentRunResults
        run={latestRun?.run_id === queuedRunId ? latestRun : null}
        results={latestRun?.run_id === queuedRunId ? runResultsQuery.data || null : null}
        loading={selectedRunQuery.isFetching || runResultsQuery.isFetching}
        error={selectedRunQuery.error ? toError(selectedRunQuery.error) : runResultsQuery.error ? toError(runResultsQuery.error) : null}
        onRetry={() => {
          void selectedRunQuery.refetch();
          void runResultsQuery.refetch();
        }}
        onOpenTrace={openRunResultTrace}
      />
      <details className="eval-run-advanced">
        <summary>{t("eval.workbench.createOrConfigure", "Create or configure an experiment")}</summary>
        <div className="eval-workbench-form-grid">
          <Input
            aria-label={t("eval.workbench.experimentName", "Experiment name")}
            value={experimentDraft.name}
            onChange={(event) => setExperimentDraft((draft) => ({ ...draft, name: event.target.value }))}
          />
          <Input
            aria-label={t("eval.workbench.experimentDescription", "Experiment description")}
            value={experimentDraft.description}
            onChange={(event) => setExperimentDraft((draft) => ({ ...draft, description: event.target.value }))}
          />
          <Input.TextArea
            className="eval-workbench-textarea eval-workbench-wide"
            aria-label={t("eval.workbench.targetConfig", "Target config JSON")}
            value={experimentDraft.targetConfigText}
            autoSize={{ minRows: 3, maxRows: 6 }}
            onChange={(event) => setExperimentDraft((draft) => ({ ...draft, targetConfigText: event.target.value }))}
          />
          <Button
            icon={<Beaker size={15} />}
            onClick={() => experimentMutation.mutate()}
            loading={experimentMutation.isPending}
            disabled={!canRunEvaluations}
          >
            {t("eval.workbench.createExperiment")}
          </Button>
        </div>
      </details>
    </WorkbenchPanel>
  );

  const evaluatorsTab = (
    <WorkbenchPanel
      title={t("eval.workbench.evaluators")}
      description={t("eval.workbench.evaluatorsDescription")}
      icon={<Sparkles size={22} />}
    >
      <div className="eval-evaluator-config-grid">
        <Select
          className="eval-workbench-wide"
          allowClear
          aria-label={t("eval.workbench.selectEvaluator", "Select evaluator")}
          placeholder={t("eval.workbench.selectEvaluator", "Select evaluator")}
          value={selectedEvaluatorId}
          options={evaluators.map((evaluator) => ({
            label: `${evaluator.name} (${evaluator.version})`,
            value: evaluator.evaluator_id,
          }))}
          onChange={(value) => setSelectedEvaluatorId(value)}
        />
        <Input
          value={evaluatorDraft.name}
          aria-label={t("eval.workbench.evaluatorName")}
          onChange={(event) => setEvaluatorDraft((draft) => ({ ...draft, name: event.target.value }))}
        />
        <Select
          value={evaluatorDraft.evaluator_type}
          aria-label={t("eval.workbench.evaluatorType")}
          options={evaluatorTypeOptions}
          onChange={(value: EvalEvaluator["evaluator_type"]) => {
            const preset = evaluatorPresetForType(value, activeTraceFamily, (key, fallback) => t(key, fallback));
            setEvaluatorDraft((draft) => ({
              ...draft,
              evaluator_type: value,
              rubric: preset.rubric,
              filterConfigText: preset.filterConfigText,
              samplingConfigText: preset.samplingConfigText,
            }));
          }}
        />
        <Input
          value={evaluatorDraft.version}
          aria-label={t("eval.workbench.version")}
          onChange={(event) => setEvaluatorDraft((draft) => ({ ...draft, version: event.target.value }))}
        />
        <Input.TextArea
          className="eval-workbench-textarea eval-workbench-wide"
          aria-label={t("eval.workbench.rubric", "Rubric")}
          value={evaluatorDraft.rubric}
          autoSize={{ minRows: 3, maxRows: 6 }}
          onChange={(event) => setEvaluatorDraft((draft) => ({ ...draft, rubric: event.target.value }))}
        />
        <Input.TextArea
          className="eval-workbench-textarea"
          aria-label={t("eval.workbench.samplingConfig", "Sampling config JSON")}
          value={evaluatorDraft.samplingConfigText}
          autoSize={{ minRows: 2, maxRows: 5 }}
          onChange={(event) => setEvaluatorDraft((draft) => ({ ...draft, samplingConfigText: event.target.value }))}
        />
        <Input.TextArea
          className="eval-workbench-textarea"
          aria-label={t("eval.workbench.filterConfig", "Filter config JSON")}
          value={evaluatorDraft.filterConfigText}
          autoSize={{ minRows: 2, maxRows: 5 }}
          onChange={(event) => setEvaluatorDraft((draft) => ({ ...draft, filterConfigText: event.target.value }))}
        />
      </div>
      <Space size={10} wrap>
        <Button
          type="primary"
          icon={<Sparkles size={15} />}
          onClick={() => evaluatorMutation.mutate()}
          loading={evaluatorMutation.isPending}
          disabled={!canRunEvaluations}
        >
          {t("eval.workbench.createEvaluator")}
        </Button>
        <Button
          icon={<Play size={15} />}
          onClick={() => evaluatorRunMutation.mutate()}
          loading={evaluatorRunMutation.isPending}
          disabled={!canRunEvaluations || !activeEvaluator}
        >
          {t("eval.workbench.queueEvaluator")}
        </Button>
      </Space>
      <Descriptions
        className="eval-workbench-descriptions"
        size="small"
        bordered
        column={1}
        items={[
          { key: "evaluator", label: t("eval.workbench.currentEvaluator"), children: activeEvaluator?.name || "-" },
          { key: "version", label: t("eval.workbench.version"), children: activeEvaluator?.version || "-" },
          { key: "selected", label: t("eval.workbench.selectedTrace"), children: selectedTraceId || "-" },
          { key: "listed", label: t("eval.workbench.listedEvaluators", "Listed evaluators"), children: String(evaluatorsQuery.data?.total ?? 0) },
        ]}
      />
    </WorkbenchPanel>
  );

  const pendingExamples = (examplesQuery.data?.examples || []).filter(
    (example) => example.metadata?.review_status === "pending"
      || example.metadata?.review_status === "needs_fix"
  );

  const reviewQueueTab = (
    <WorkbenchPanel
      title={t("eval.workbench.reviewQueue", "Review Queue")}
      description={t("eval.workbench.reviewQueueDescription", "Approve golden candidates and judge disagreements before they affect regression gates.")}
      icon={<CheckCircle2 size={22} />}
    >
      <Space size={10} wrap>
        <Button
          icon={<CheckCircle2 size={15} />}
          onClick={addSelectedTraceToReview}
          loading={exampleMutation.isPending}
          disabled={!canRunEvaluations || !activeDataset || !selectedTraceId}
        >
          {t("eval.workbench.addToReview", "Add to Review")}
        </Button>
      </Space>
      <div className="eval-review-list">
        {pendingExamples.length > 0 ? pendingExamples.slice(0, 8).map((example) => (
          <article className="eval-review-row" key={example.example_id}>
            <div>
              <strong>{String(example.metadata?.case_id || example.example_id)}</strong>
              <span>{String(example.metadata?.review_status || "pending")} · {example.split}</span>
            </div>
            <Space size={8}>
              <Button size="small" disabled={!canRunEvaluations} onClick={() => reviewMutation.mutate({ exampleId: example.example_id, status: "approved" })}>
                {t("common.approve", "Approve")}
              </Button>
              <Button size="small" disabled={!canRunEvaluations} onClick={() => reviewMutation.mutate({ exampleId: example.example_id, status: "needs_fix" })}>
                {t("common.review", "Needs fix")}
              </Button>
            </Space>
          </article>
        )) : (
          <Alert type="info" showIcon title={t("eval.workbench.reviewEmpty", "No pending review cases for the active dataset")} />
        )}
      </div>
    </WorkbenchPanel>
  );

  const kbRagasTab = (
    <KbRagasPanel
      traces={visibleTraces}
      traceTotal={tracesQuery.data?.total || 0}
      selectedTraceId={selectedTraceId}
      detail={detailQuery.data}
      detailLoading={detailQuery.isLoading || detailQuery.isFetching}
      ragasEvaluators={ragasEvaluators}
      selectedEvaluatorId={activeRagasEvaluator?.evaluator_id}
      onSelectEvaluator={setSelectedEvaluatorId}
      onQueueEvaluator={() => ragasEvaluatorRunMutation.mutate()}
      queueLoading={ragasEvaluatorRunMutation.isPending}
      onCreateRagasEvaluator={() => createRagasEvaluatorMutation.mutate()}
      createLoading={createRagasEvaluatorMutation.isPending}
      initialDatasetId={kbDatasetFilter}
      canRunEvaluations={canRunEvaluations}
    />
  );

  const gatesTab = (
    <WorkbenchPanel
      title={t("eval.workbench.gates", "Gates")}
      description={t("eval.workbench.gatesDescription", "Dry-run the offline regression thresholds before they enter CI.")}
      icon={<ShieldCheck size={22} />}
    >
      <Space size={10} wrap>
        <Button
          type="primary"
          icon={<ShieldCheck size={15} />}
          onClick={() => gateMutation.mutate()}
          loading={gateMutation.isPending}
          disabled={!canRunEvaluations || !canDryRunGate}
        >
          {t("eval.workbench.dryRunGate", "Dry-run gate")}
        </Button>
      </Space>
      {!canDryRunGate ? (
        <Alert
          type="info"
          showIcon
          title={t("eval.workbench.gateNeedsRun", "Select a successful run with complete gate metrics before running the gate.")}
        />
      ) : null}
      <Descriptions
        className="eval-workbench-descriptions"
        size="small"
        bordered
        column={1}
        items={[
          { key: "status", label: t("eval.workbench.gateStatus", "Gate status"), children: gateResult?.status || "not_run" },
          { key: "overall", label: t("eval.workbench.overallScore", "Overall score"), children: String(gateResult?.metrics?.overall_score ?? "-") },
          { key: "trajectory", label: t("eval.workbench.trajectoryPass", "Trajectory pass"), children: String(gateResult?.metrics?.trajectory_pass_rate ?? "-") },
          { key: "critical", label: t("eval.workbench.criticalPass", "Critical pass"), children: String(gateResult?.metrics?.critical_pass_rate ?? "-") },
          { key: "failures", label: t("eval.workbench.failures", "Failures"), children: gateResult?.failures.join(", ") || "-" },
        ]}
      />
    </WorkbenchPanel>
  );

  const runAndResultsTab = (
    <Tabs
      className="eval-subtabs"
      activeKey={activeRunTab}
      onChange={(key) => {
        setActiveRunTab(key);
        if (key === "rag_quality" && activeTraceFamily !== "rag") {
          handleTraceFamilyChange("rag");
        }
      }}
      items={[
        {
          key: "experiment_runs",
          label: t("eval.workbench.testRuns", "Test runs"),
          children: experimentsTab,
        },
        {
          key: "rag_quality",
          label: t("eval.ragas.tab", "RAG quality"),
          children: kbRagasTab,
        },
      ]}
    />
  );

  const assetsTab = (
    <Tabs
      className="eval-subtabs"
      activeKey={activeAssetsTab}
      onChange={setActiveAssetsTab}
      items={[
        { key: "golden_sets", label: t("eval.workbench.goldenSets", "Golden Sets"), children: goldenSetsTab },
        { key: "evaluators", label: t("eval.workbench.evaluators"), children: evaluatorsTab },
        { key: "review_queue", label: t("eval.workbench.reviewQueue", "Review Queue"), children: reviewQueueTab },
      ]}
    />
  );

  return (
    <main className="eval-console" data-testid="eval-console">
      <div className="eval-page-heading">
        <div>
          <div className="eval-eyebrow">
            <SearchCheck size={15} />
            {t("eval.eyebrow")}
          </div>
          <h1>{t("eval.title")}</h1>
          <p>{t("eval.description")}</p>
        </div>
        <div className="eval-latency-note">
          <Activity size={16} />
          {t("eval.latencyNote")}
        </div>
      </div>

      <Tabs
        className="eval-tabs"
        activeKey={activeWorkbenchTab}
        onChange={handleWorkbenchTabChange}
        items={[
          {
            key: "overview",
            label: t("eval.workbench.overview", "Overview"),
            children: overviewTab,
          },
          {
            key: "runs",
            label: t("eval.workbench.runAndResults", "Run & Results"),
            children: runAndResultsTab,
          },
          {
            key: "traces",
            label: t("eval.workbench.traces", "Traces"),
            children: traceExplorerTab,
          },
          {
            key: "assets",
            label: t("eval.workbench.assets", "Assets"),
            children: assetsTab,
          },
          {
            key: "gates",
            label: t("eval.workbench.gates", "Gates"),
            children: gatesTab,
          },
        ]}
      />

      <style>{`
        .eval-console {
          display: flex;
          min-height: calc(100vh - 112px);
          flex-direction: column;
          min-width: 0;
          color: hsl(var(--foreground));
        }
        .eval-page-heading {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          gap: 16px;
          margin-bottom: 14px;
        }
        .eval-page-heading h1 {
          margin: 4px 0 4px;
          font-size: 24px;
          line-height: 1.18;
          font-weight: 720;
          letter-spacing: 0;
        }
        .eval-page-heading p {
          margin: 0;
          color: hsl(var(--muted-foreground));
          font-size: 13px;
        }
        .eval-platform-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          gap: 10px;
        }
        .eval-platform-wide {
          grid-column: 1 / -1;
        }
        .eval-overview-bars {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
          gap: 12px;
          margin-bottom: 12px;
        }
        .eval-overview-bars span {
          display: block;
          margin-bottom: 4px;
          color: hsl(var(--muted-foreground));
          font-size: 12px;
        }
        .eval-summary-card {
          display: flex;
          flex-direction: column;
          gap: 4px;
          padding: 12px 14px;
          border: 1px solid hsl(var(--border));
          border-radius: 12px;
          background: hsl(var(--card));
        }
        .eval-summary-label {
          font-size: 11px;
          color: hsl(var(--muted-foreground));
          letter-spacing: 0.02em;
        }
        .eval-summary-value {
          font-size: 18px;
          line-height: 1.2;
          font-weight: 700;
        }
        .eval-eyebrow,
        .eval-latency-note {
          display: inline-flex;
          align-items: center;
          gap: 7px;
          color: hsl(var(--primary));
          font-size: 12px;
          font-weight: 650;
          letter-spacing: 0;
        }
        .eval-latency-note {
          max-width: 340px;
          padding: 9px 12px;
          border-radius: 8px;
          background: hsl(var(--primary) / 0.08);
          color: hsl(var(--foreground));
          box-shadow: 0 8px 28px rgb(18 24 38 / 0.08);
        }
        .eval-tabs > .ant-tabs-nav {
          margin-bottom: 10px;
        }
        .eval-tabs,
        .eval-tabs > .ant-tabs-content-holder,
        .eval-tabs > .ant-tabs-content-holder > .ant-tabs-content,
        .eval-tabs > .ant-tabs-content-holder > .ant-tabs-content > .ant-tabs-tabpane {
          min-height: 0;
        }
        .eval-tabs {
          display: flex;
          flex: 1;
          flex-direction: column;
        }
        .eval-tabs > .ant-tabs-content-holder {
          flex: 1;
        }
        .eval-tabs > .ant-tabs-content-holder > .ant-tabs-content,
        .eval-tabs > .ant-tabs-content-holder > .ant-tabs-content > .ant-tabs-tabpane-active {
          height: 100%;
        }
        .eval-tabs .ant-tabs-tab {
          border-radius: 8px;
          padding-inline: 12px;
        }
        .eval-family-tabs {
          display: flex;
          min-height: 0;
          height: 100%;
          flex-direction: column;
        }
        .eval-family-tabs > .ant-tabs-nav {
          flex: 0 0 auto;
          margin-bottom: 10px;
        }
        .eval-family-tabs > .ant-tabs-content-holder {
          flex: 1;
          min-height: 0;
        }
        .eval-family-tabs > .ant-tabs-content-holder > .ant-tabs-content,
        .eval-family-tabs > .ant-tabs-content-holder > .ant-tabs-content > .ant-tabs-tabpane-active {
          height: 100%;
        }
        .eval-workbench-actions {
          display: flex;
          flex: 0 0 auto;
          justify-content: space-between;
          align-items: center;
          gap: 10px;
          margin-bottom: 10px;
          min-width: 0;
        }
        .eval-family-coverage-note {
          margin-bottom: 10px;
        }
        .eval-assistant-grid {
          display: grid;
          grid-template-columns: minmax(360px, 0.95fr) minmax(460px, 1.22fr) minmax(292px, 0.62fr);
          gap: 12px;
          align-items: stretch;
          height: min(760px, calc(100vh - 202px));
          min-height: 560px;
          min-width: 0;
        }
        .eval-workbench-panel {
          min-height: min(560px, calc(100vh - 220px));
        }
        .eval-workbench-icon {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          width: 34px;
          height: 34px;
          border-radius: 8px;
          background: hsl(var(--primary) / 0.08);
          color: hsl(var(--primary));
        }
        .eval-workbench-body {
          display: flex;
          flex-direction: column;
          gap: 14px;
          padding: 14px;
          min-width: 0;
        }
        .eval-workbench-descriptions {
          max-width: 760px;
        }
        .eval-workbench-descriptions .ant-descriptions-item-content {
          word-break: break-all;
        }
        .eval-workbench-form-grid,
        .eval-evaluator-config-grid {
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 280px));
          gap: 10px;
          max-width: 760px;
        }
        .eval-workbench-wide {
          grid-column: 1 / -1;
        }
        .eval-review-list {
          display: grid;
          gap: 8px;
          margin-top: 12px;
        }
        .eval-review-row {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          padding: 10px 12px;
          border: 1px solid hsl(var(--border));
          border-radius: 8px;
          background: hsl(var(--card));
        }
        .eval-review-row div {
          display: grid;
          gap: 2px;
          min-width: 0;
        }
        .eval-review-row span {
          color: hsl(var(--muted-foreground));
          font-size: 12px;
        }
        .eval-workbench-textarea textarea {
          font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
          font-size: 12px;
          line-height: 1.45;
        }
        .eval-panel {
          display: flex;
          min-height: 0;
          flex-direction: column;
          min-width: 0;
          border: 1px solid hsl(var(--border) / 0.72);
          border-radius: 8px;
          background: hsl(var(--card));
          box-shadow: 0 1px 2px rgb(20 24 32 / 0.04), 0 8px 28px rgb(20 24 32 / 0.06);
          overflow: hidden;
        }
        .eval-panel-heading {
          display: flex;
          flex: 0 0 auto;
          align-items: flex-start;
          justify-content: space-between;
          gap: 10px;
          padding: 14px 14px 10px;
          border-bottom: 1px solid hsl(var(--border) / 0.72);
        }
        .eval-panel-heading h2 {
          margin: 0;
          font-size: 14px;
          line-height: 1.2;
          font-weight: 700;
          letter-spacing: 0;
        }
        .eval-panel-heading p {
          margin: 3px 0 0;
          color: hsl(var(--muted-foreground));
          font-size: 12px;
        }
        .eval-filter-grid {
          display: grid;
          flex: 0 0 auto;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 8px;
          padding: 12px 14px;
          border-bottom: 1px solid hsl(var(--border) / 0.6);
        }
        .eval-date-range {
          grid-column: span 2;
        }
        .eval-transcript-filter {
          grid-column: span 2;
        }
        .eval-trace-table {
          display: flex;
          min-height: 0;
          flex: 1;
          flex-direction: column;
          padding: 0 10px 12px;
        }
        .eval-trace-table .ant-spin-nested-loading,
        .eval-trace-table .ant-spin-container,
        .eval-trace-table .ant-table,
        .eval-trace-table .ant-table-container {
          min-height: 0;
          height: 100%;
        }
        .eval-trace-table .ant-spin-container {
          display: flex;
          flex-direction: column;
        }
        .eval-trace-table .ant-table-wrapper,
        .eval-trace-table .ant-table {
          flex: 1;
        }
        .eval-trace-table .ant-table-body {
          max-height: none !important;
        }
        .eval-trace-table .ant-table {
          border-radius: 8px;
          overflow: hidden;
        }
        .eval-trace-table .ant-table-row {
          cursor: pointer;
        }
        .eval-trace-table .ant-table-row:focus-visible {
          outline: 2px solid hsl(var(--ring));
          outline-offset: -2px;
        }
        .eval-trace-row-selected > td {
          background: hsl(var(--primary) / 0.08) !important;
        }
        .eval-trace-id-cell {
          min-width: 0;
        }
        .eval-trace-id-line,
        .eval-trace-model {
          max-width: 220px;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          font-size: 12px;
          font-weight: 650;
        }
        .eval-trace-subline {
          max-width: 220px;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          color: hsl(var(--muted-foreground));
          font-size: 11px;
          margin-top: 2px;
        }
        .eval-trace-metric {
          font-size: 12px;
          font-weight: 700;
        }
        .eval-status-tag {
          margin: 0;
          border-radius: 5px;
        }
        .eval-state-panel,
        .eval-detail-empty {
          flex: 1;
          min-height: 240px;
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 10px;
          padding: 24px;
          color: hsl(var(--muted-foreground));
        }
        .eval-trace-detail {
          overflow-y: auto;
          padding-bottom: 14px;
        }
        .eval-redaction-banner {
          margin: 12px 14px;
        }
        .eval-metric-grid {
          display: grid;
          grid-template-columns: repeat(4, minmax(0, 1fr));
          gap: 8px;
          padding: 0 14px 12px;
        }
        .eval-metric-card {
          min-width: 0;
          border-radius: 8px;
          background: hsl(var(--muted) / 0.55);
          padding: 10px;
        }
        .eval-metric-card span {
          display: block;
          color: hsl(var(--muted-foreground));
          font-size: 11px;
        }
        .eval-metric-card strong {
          display: block;
          margin-top: 2px;
          font-size: 15px;
        }
        .eval-descriptions {
          margin: 0 14px 12px;
        }
        .eval-descriptions .ant-descriptions-item-label {
          width: 92px;
          white-space: nowrap;
        }
        .eval-descriptions .ant-descriptions-item-content {
          min-width: 0;
          word-break: break-all;
        }
        .eval-locator-panel {
          margin: 0 0 12px;
        }
        .eval-locator-panel .eval-preview-grid {
          margin-bottom: 0;
        }
        .eval-locator-descriptions .ant-descriptions-item-label {
          width: 110px;
        }
        .eval-section-title-compact {
          margin-top: 4px;
        }
        .eval-preview-grid {
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 8px;
          margin: 0 14px 12px;
        }
        .eval-preview-block {
          min-width: 0;
          border-radius: 8px;
          background: hsl(var(--muted) / 0.46);
          padding: 10px;
        }
        .eval-preview-label {
          margin-bottom: 5px;
          color: hsl(var(--muted-foreground));
          font-size: 11px;
          font-weight: 650;
        }
        .eval-preview-text {
          margin-bottom: 0 !important;
          font-size: 12px;
          overflow-wrap: anywhere;
        }
        .eval-section-title {
          display: flex;
          justify-content: space-between;
          align-items: center;
          gap: 8px;
          margin: 16px 14px 10px;
        }
        .eval-section-title h3 {
          margin: 0;
          font-size: 13px;
          font-weight: 700;
        }
        .eval-section-title span {
          color: hsl(var(--muted-foreground));
          font-size: 11px;
        }
        .eval-timeline {
          padding: 0 14px;
        }
        .eval-timeline-card,
        .eval-event-row {
          min-width: 0;
          border: 1px solid hsl(var(--border) / 0.48);
          border-radius: 8px;
          background: hsl(var(--muted) / 0.4);
          padding: 10px;
        }
        .eval-timeline-title,
        .eval-event-main {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          gap: 8px;
          min-width: 0;
          font-size: 12px;
          font-weight: 700;
        }
        .eval-timeline-meta {
          color: hsl(var(--muted-foreground));
          font-size: 11px;
          font-weight: 400;
        }
        .eval-json-block {
          max-height: 180px;
          overflow: auto;
          margin: 8px 0 0;
          border-radius: 8px;
          background: hsl(var(--background));
          color: hsl(var(--foreground));
          padding: 9px;
          font-size: 11px;
          line-height: 1.45;
          white-space: pre-wrap;
          overflow-wrap: anywhere;
        }
        .eval-event-list {
          display: flex;
          flex-direction: column;
          gap: 8px;
          padding: 0 14px;
        }
        .eval-event-type {
          overflow-wrap: anywhere;
        }
        .eval-score-panel {
          overflow: hidden;
          padding-bottom: 14px;
        }
        .eval-score-list {
          flex: 0 1 30%;
          max-height: 240px;
          min-height: 96px;
          overflow: auto;
          padding: 0 14px;
        }
        .eval-score-record {
          border-bottom: 1px solid hsl(var(--border) / 0.62);
          padding: 10px 0;
        }
        .eval-score-record:first-child {
          padding-top: 0;
        }
        .eval-score-record:last-child {
          border-bottom: 0;
          padding-bottom: 0;
        }
        .eval-score-state {
          display: flex;
          min-height: 96px;
          align-items: center;
          justify-content: center;
        }
        .eval-score-description {
          display: flex;
          flex-direction: column;
          gap: 2px;
          font-size: 12px;
        }
        .eval-score-form {
          margin-top: auto;
          padding: 12px 14px 0;
          border-top: 1px solid hsl(var(--border) / 0.62);
        }
        .eval-score-form-grid {
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 8px;
        }
        .eval-future-panel {
          min-height: 360px;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          gap: 16px;
          padding: 28px;
          text-align: center;
        }
        .eval-future-icon {
          width: 54px;
          height: 54px;
          border-radius: 8px;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          color: hsl(var(--primary));
          background: hsl(var(--primary) / 0.1);
        }
        .eval-future-copy {
          display: flex;
          flex-direction: column;
          gap: 4px;
        }
        .eval-future-copy span {
          color: hsl(var(--muted-foreground));
        }
        @media (max-width: 1280px) {
          .eval-assistant-grid {
            grid-template-columns: minmax(360px, 0.9fr) minmax(420px, 1.1fr);
            height: auto;
            min-height: 0;
          }
          .eval-score-panel {
            grid-column: 1 / -1;
            min-height: 420px;
          }
          .eval-trace-list,
          .eval-trace-detail {
            min-height: 620px;
            max-height: 720px;
          }
        }
        @media (max-width: 860px) {
          .eval-page-heading,
          .eval-panel-heading {
            flex-direction: column;
            align-items: stretch;
          }
          .eval-latency-note {
            max-width: none;
          }
          .eval-assistant-grid {
            grid-template-columns: minmax(0, 1fr);
            height: auto;
            min-height: 0;
          }
          .eval-trace-list,
          .eval-trace-detail,
          .eval-score-panel {
            min-height: 0;
            max-height: none;
          }
          .eval-trace-detail {
            overflow: visible;
          }
          .eval-score-list {
            max-height: 220px;
          }
          .eval-filter-grid,
          .eval-metric-grid,
          .eval-preview-grid,
          .eval-workbench-form-grid,
          .eval-evaluator-config-grid,
          .eval-score-form-grid {
            grid-template-columns: minmax(0, 1fr);
          }
          .eval-date-range {
            grid-column: span 1;
          }
          .eval-transcript-filter {
            grid-column: span 1;
          }
        }
        .dark .eval-latency-note,
        .dark .eval-panel {
          box-shadow: none;
        }
        .dark .eval-panel {
          border-color: hsl(var(--border) / 0.86);
        }
        .dark .eval-redaction-banner {
          background: hsl(var(--primary) / 0.12);
          border-color: hsl(var(--primary) / 0.28);
        }
        .dark .eval-metric-card,
        .dark .eval-preview-block,
        .dark .eval-timeline-card,
        .dark .eval-event-row {
          background: hsl(var(--muted) / 0.58);
        }
        .dark .eval-json-block {
          border: 1px solid hsl(var(--border) / 0.58);
          background: hsl(var(--background) / 0.72);
        }
        @media (prefers-reduced-motion: reduce) {
          .eval-panel,
          .eval-tabs .ant-tabs-tab {
            transition-duration: 1ms !important;
          }
        }
      `}</style>
    </main>
  );
}
