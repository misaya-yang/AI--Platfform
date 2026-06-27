import { Alert, App as AntApp, Button, Descriptions, Input, Select, Space, Tabs, Tag } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, Beaker, Database, Download, Play, SearchCheck, Sparkles } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  createEvalDataset,
  createEvalEvaluator,
  createEvalExampleFromTrace,
  createEvalExperiment,
  createAgentTraceScore,
  exportAgentTrace,
  getAgentTraceDetail,
  getEvalExperimentRun,
  getEvalSummary,
  listAgentTraces,
  listEvalDatasets,
  listEvalEvaluators,
  listEvalExperiments,
  runEvalEvaluatorAsync,
  type EvalDataset,
  type EvalEvaluator,
  type EvalExperiment,
  type EvalExperimentRun,
  type EvalTraceExportResponse,
  type AgentTraceScoreCreate,
  type AgentTraceSummary,
  type ListAgentTracesParams,
  type TraceFamily,
  type TraceStatus,
} from "@/api/eval";

import {
  AssistantTraceList,
  type AssistantTraceFilters,
} from "./components/AssistantTraceList";
import { AssistantTraceDetail } from "./components/AssistantTraceDetail";
import { TraceScorePanel } from "./components/TraceScorePanel";

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

function buildServerFilters(
  filters: AssistantTraceFilters,
  traceFamily: TraceFamily
): ListAgentTracesParams {
  return {
    trace_family: traceFamily,
    status: filters.status && filters.status !== "all" ? (filters.status as TraceStatus) : undefined,
    model_id: filters.model_id?.trim() || undefined,
    user_id: filters.user_id?.trim() || undefined,
    session_id: filters.session_id?.trim() || undefined,
    run_id: filters.run_id?.trim() || undefined,
    request_id: filters.request_id?.trim() || undefined,
    transcript_query: filters.transcript_query?.trim() || undefined,
    turn_index: filters.turn_index,
    limit: 100,
    offset: 0,
  };
}

export function EvalPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { message } = AntApp.useApp();
  const [filters, setFilters] = useState<AssistantTraceFilters>({
    status: "all",
    score_status: "all",
  });
  const [activeTraceFamily, setActiveTraceFamily] = useState<TraceFamily>("assistant");
  const [selectedTraceId, setSelectedTraceId] = useState<string | undefined>();
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
    targetConfigText: JSON.stringify({ trace_family: "assistant", model_id: "current" }, null, 2),
  });
  const [exportPreview, setExportPreview] = useState<EvalTraceExportResponse | null>(null);
  const [queuedRunId, setQueuedRunId] = useState<string | undefined>();
  const [latestRun, setLatestRun] = useState<EvalExperimentRun | null>(null);
  const serverFilters = useMemo(
    () => buildServerFilters(filters, activeTraceFamily),
    [activeTraceFamily, filters]
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
    if (visibleTraces.length === 0) {
      setSelectedTraceId(undefined);
      return;
    }
    if (!selectedTraceId || !visibleTraces.some((trace) => trace.trace_id === selectedTraceId)) {
      setSelectedTraceId(visibleTraces[0].trace_id);
    }
  }, [selectedTraceId, visibleTraces]);

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
  const datasets = useMemo(() => datasetsQuery.data?.datasets || [], [datasetsQuery.data?.datasets]);
  const evaluators = useMemo(() => evaluatorsQuery.data?.evaluators || [], [evaluatorsQuery.data?.evaluators]);
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
  const activeExperiment = useMemo(() => {
    if (selectedExperimentId) {
      return experiments.find((experiment) => experiment.experiment_id === selectedExperimentId)
        || (createdExperiment?.experiment_id === selectedExperimentId ? createdExperiment : null);
    }
    return createdExperiment;
  }, [createdExperiment, experiments, selectedExperimentId]);

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
    mutationFn: async () => {
      if (!activeDataset) throw new Error(t("eval.workbench.createDatasetFirst"));
      if (!selectedTraceId) throw new Error(t("eval.workbench.selectTrace"));
      return createEvalExampleFromTrace(activeDataset.dataset_id, {
        source_trace_id: selectedTraceId,
        trace_family: activeTraceFamily,
        split: exampleDraft.split.trim() || "regression",
        expected_output: parseJsonObjectDraft(exampleDraft.expectedOutputText, {}),
        metadata: { source: "eval_console", trace_family: activeTraceFamily },
      });
    },
    onSuccess: () => message.success(t("eval.workbench.exampleCreated")),
    onError: (error) => message.error(toError(error).message),
  });

  const evaluatorMutation = useMutation({
    mutationFn: () =>
      createEvalEvaluator({
        name: evaluatorDraft.name.trim() || "quality",
        evaluator_type: evaluatorDraft.evaluator_type,
        rubric: evaluatorDraft.rubric.trim(),
        version: evaluatorDraft.version.trim() || "v1",
        sampling_config: parseJsonObjectDraft(evaluatorDraft.samplingConfigText, {}),
        filter_config: parseJsonObjectDraft(evaluatorDraft.filterConfigText, {}),
        metadata: { source: "eval_console", trace_family: activeTraceFamily },
      }),
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
          model_id: serverFilters.model_id || "current",
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
      if (job.run_id) setQueuedRunId(job.run_id);
      message.success(t("eval.workbench.evaluatorQueued"));
    },
    onError: (error) => message.error(toError(error).message),
  });

  useEffect(() => {
    if (!queuedRunId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const run = await getEvalExperimentRun(queuedRunId);
        if (cancelled) return;
        setLatestRun(run);
        if (run.status === "queued" || run.status === "running") {
          window.setTimeout(poll, 2000);
        }
      } catch {
        if (!cancelled) setLatestRun(null);
      }
    };
    void poll();
    return () => {
      cancelled = true;
    };
  }, [queuedRunId]);

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
    setActiveTraceFamily(nextFamily);
    setFilters({ status: "all", score_status: "all" });
    setSelectedTraceId(undefined);
    setScoreError(null);
    setExportPreview(null);
  };

  const traceFamilyPanel = (
    <>
      {familyCoverageMessage ? (
        <Alert
          className="eval-family-coverage-note"
          type={hasCapturedFamilyTraces ? "success" : "warning"}
          showIcon
          message={familyCoverageMessage}
        />
      ) : null}
      <div className="eval-workbench-actions">
        <Space size={8} wrap>
          <Button
            icon={<Download size={15} />}
            onClick={() => exportMutation.mutate()}
            loading={exportMutation.isPending}
            disabled={!selectedTraceId}
          >
            {t("eval.workbench.exportOpenInference")}
          </Button>
          <Button
            icon={<Database size={15} />}
            onClick={() => exampleMutation.mutate()}
            loading={exampleMutation.isPending}
            disabled={!selectedTraceId || !activeDataset}
          >
            {t("eval.workbench.addTraceToDataset")}
          </Button>
          {exportPreview ? <Tag color="blue">{t("eval.workbench.exportFormat", { format: exportPreview.format })}</Tag> : null}
        </Space>
      </div>
      <div className="eval-assistant-grid">
        <AssistantTraceList
          traces={visibleTraces}
          total={tracesQuery.data?.total || 0}
          filters={filters}
          setFilters={setFilters}
          title={traceListCopy.title}
          ariaLabel={traceListCopy.aria}
          emptyText={traceListCopy.empty}
          selectedTraceId={selectedTraceId}
          loading={tracesQuery.isLoading || tracesQuery.isFetching}
          error={tracesQuery.error ? toError(tracesQuery.error) : null}
          onSelect={setSelectedTraceId}
          onRefresh={() => tracesQuery.refetch()}
        />
        <AssistantTraceDetail
          detail={detailQuery.data}
          loading={detailQuery.isLoading || detailQuery.isFetching}
          error={detailQuery.error ? toError(detailQuery.error) : null}
        />
        <TraceScorePanel
          traceId={selectedTraceId}
          scores={detailQuery.data?.scores || []}
          loading={detailQuery.isLoading || detailQuery.isFetching}
          submitting={scoreMutation.isPending}
          error={scoreError}
          onSubmit={async (payload) => {
            await scoreMutation.mutateAsync(payload);
          }}
        />
      </div>
    </>
  );

  const traceExplorerTab = (
    <Tabs
      className="eval-family-tabs"
      activeKey={activeTraceFamily}
      onChange={handleTraceFamilyChange}
      items={[
        {
          key: "assistant",
          label: t("eval.tabs.assistant"),
          children: traceFamilyPanel,
        },
        {
          key: "langgraph_proxy",
          label: t("eval.tabs.langgraphProxy"),
          children: traceFamilyPanel,
        },
        {
          key: "rag",
          label: t("eval.tabs.rag"),
          children: traceFamilyPanel,
        },
      ]}
    />
  );

  const datasetsTab = (
    <WorkbenchPanel
      title={t("eval.workbench.datasets")}
      description={t("eval.workbench.datasetsDescription")}
      icon={<Database size={22} />}
    >
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
        >
          {t("eval.workbench.createDataset")}
        </Button>
        <Button
          icon={<SearchCheck size={15} />}
          onClick={() => exampleMutation.mutate()}
          loading={exampleMutation.isPending}
          disabled={!activeDataset || !selectedTraceId}
        >
          {t("eval.workbench.addTraceToDataset")}
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
        ]}
      />
    </WorkbenchPanel>
  );

  const experimentsTab = (
    <WorkbenchPanel
      title={t("eval.workbench.experiments")}
      description={t("eval.workbench.experimentsDescription")}
      icon={<Beaker size={22} />}
    >
      <div className="eval-workbench-form-grid">
        <Select
          className="eval-workbench-wide"
          allowClear
          aria-label={t("eval.workbench.selectExperiment", "Select experiment")}
          placeholder={t("eval.workbench.selectExperiment", "Select experiment")}
          value={selectedExperimentId}
          options={experiments.map((experiment) => ({
            label: experiment.name,
            value: experiment.experiment_id,
          }))}
          onChange={(value) => setSelectedExperimentId(value)}
        />
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
      </div>
      <Space size={10} wrap>
        <Button
          type="primary"
          icon={<Beaker size={15} />}
          onClick={() => experimentMutation.mutate()}
          loading={experimentMutation.isPending}
        >
          {t("eval.workbench.createExperiment")}
        </Button>
        <Button
          icon={<Play size={15} />}
          onClick={() => evaluatorRunMutation.mutate()}
          loading={evaluatorRunMutation.isPending}
          disabled={!activeEvaluator}
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
          { key: "experiment", label: t("eval.workbench.currentExperiment"), children: activeExperiment?.name || "-" },
          { key: "dataset", label: t("eval.workbench.currentDataset"), children: activeDataset?.name || "-" },
          { key: "target", label: t("eval.workbench.target"), children: `${activeTraceFamily}:${serverFilters.model_id || "current"}` },
          { key: "listed", label: t("eval.workbench.listedExperiments", "Listed experiments"), children: String(experimentsQuery.data?.total ?? 0) },
          { key: "run", label: t("eval.workbench.latestRun", "Latest run"), children: latestRun?.status || "-" },
        ]}
      />
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
          options={[
            { label: "Human", value: "human" },
            { label: "Rule", value: "rule" },
            { label: "LLM", value: "llm" },
            { label: "Composite", value: "composite" },
          ]}
          onChange={(value: EvalEvaluator["evaluator_type"]) =>
            setEvaluatorDraft((draft) => ({ ...draft, evaluator_type: value }))}
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
        >
          {t("eval.workbench.createEvaluator")}
        </Button>
        <Button
          icon={<Play size={15} />}
          onClick={() => evaluatorRunMutation.mutate()}
          loading={evaluatorRunMutation.isPending}
          disabled={!activeEvaluator}
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

      <section className="eval-summary-grid" aria-label={t("eval.summary.ariaLabel")}>
        {[
          {
            key: "total",
            label: t("eval.summary.totalTraces"),
            value: summaryQuery.data?.total_traces ?? "—",
          },
          {
            key: "failed",
            label: t("eval.summary.failedTraces"),
            value: summaryQuery.data?.failed_traces ?? "—",
          },
          {
            key: "scored",
            label: t("eval.summary.scoredTraces"),
            value: summaryQuery.data?.scored_traces ?? "—",
          },
          {
            key: "latency",
            label: t("eval.summary.avgLatency"),
            value:
              summaryQuery.data?.avg_latency_ms != null
                ? `${summaryQuery.data.avg_latency_ms}ms`
                : "—",
          },
          {
            key: "assistant",
            label: t("eval.summary.assistantTraces"),
            value: summaryQuery.data?.assistant_traces ?? "—",
          },
          {
            key: "langgraph",
            label: t("eval.summary.langgraphTraces"),
            value: summaryQuery.data?.langgraph_traces ?? "—",
          },
          {
            key: "rag",
            label: t("eval.summary.ragTraces"),
            value: summaryQuery.data?.rag_traces ?? "—",
          },
          {
            key: "window",
            label: t("eval.summary.windowDays"),
            value: summaryQuery.data?.window_days ?? 7,
          },
        ].map((card) => (
          <article key={card.key} className="eval-summary-card">
            <span className="eval-summary-label">{card.label}</span>
            <strong className="eval-summary-value">{card.value}</strong>
          </article>
        ))}
      </section>

      <Tabs
        className="eval-tabs"
        defaultActiveKey="trace_explorer"
        items={[
          {
            key: "trace_explorer",
            label: t("eval.workbench.traceExplorer"),
            children: traceExplorerTab,
          },
          {
            key: "datasets",
            label: t("eval.workbench.datasets"),
            children: datasetsTab,
          },
          {
            key: "experiments",
            label: t("eval.workbench.experiments"),
            children: experimentsTab,
          },
          {
            key: "evaluators",
            label: t("eval.workbench.evaluators"),
            children: evaluatorsTab,
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
        .eval-summary-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
          gap: 10px;
          margin-bottom: 16px;
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
