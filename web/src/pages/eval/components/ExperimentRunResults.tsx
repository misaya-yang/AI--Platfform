import { Alert, Button, Empty, Input, Progress, Segmented, Space, Tag } from "antd";
import { ExternalLink, RefreshCw, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type {
  EvalExperimentCaseResult,
  EvalExperimentRun,
  EvalExperimentRunResultsResponse,
} from "@/api/eval";

type ResultFilter = "all" | "failed" | "review" | "passed";

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function statusColor(status: EvalExperimentCaseResult["status"]) {
  if (status === "passed") return "success";
  if (status === "failed") return "error";
  if (status === "review") return "warning";
  return "default";
}

function runStatusColor(status: EvalExperimentRun["status"]) {
  if (status === "succeeded") return "success";
  if (status === "failed" || status === "cancelled") return "error";
  if (status === "running") return "processing";
  return "default";
}

export function ExperimentRunResults({
  run,
  results,
  loading,
  error,
  onRetry,
  onOpenTrace,
}: {
  run: EvalExperimentRun | null;
  results: EvalExperimentRunResultsResponse | null;
  loading: boolean;
  error: Error | null;
  onRetry: () => void;
  onOpenTrace: (item: EvalExperimentCaseResult) => void;
}) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState<ResultFilter>("all");
  const [query, setQuery] = useState("");
  const cases = useMemo(() => results?.cases || [], [results?.cases]);
  const visibleCases = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return [...cases]
      .filter((item) => filter === "all" || item.status === filter)
      .filter((item) => {
        if (!normalizedQuery) return true;
        return [item.case_id, item.failure_reason, item.candidate_trace_id]
          .filter(Boolean)
          .some((value) => String(value).toLowerCase().includes(normalizedQuery));
      })
      .sort((left, right) => {
        const rank = { failed: 0, review: 1, unscored: 2, passed: 3 };
        return rank[left.status] - rank[right.status];
      });
  }, [cases, filter, query]);

  if (!run) {
    return (
      <div className="eval-run-empty">
        {error ? (
          <Alert
            type="error"
            showIcon
            message={t("eval.workbench.runLoadFailed", "Could not load this run")}
            description={error.message}
            action={<Button onClick={onRetry}>{t("common.retry", "Retry")}</Button>}
          />
        ) : (
          <Empty description={t("eval.workbench.runEmpty", "Run a test set or select a previous run to inspect results.")} />
        )}
      </div>
    );
  }

  const summary = run.score_summary || {};
  const isPending = run.status === "queued" || run.status === "running";
  const averageScore = numberValue(summary.average_score ?? summary.overall_score);
  const scoredCount = numberValue(summary.scored_count);
  const reviewCount = numberValue(summary.review_count);
  const skippedCount = numberValue(summary.skipped_count);

  return (
    <section className="eval-run-results" aria-live="polite">
      <div className="eval-run-results-header">
        <div>
          <Space size={8} wrap>
            <h3>{t("eval.workbench.runResults", "Run results")} · {cases.length}/{results?.total ?? cases.length}</h3>
            <Tag color={runStatusColor(run.status)}>{run.status}</Tag>
          </Space>
          <code>{run.run_id}</code>
        </div>
        <Button icon={<RefreshCw size={15} />} onClick={onRetry} loading={loading}>
          {t("common.refresh", "Refresh")}
        </Button>
      </div>

      {isPending ? (
        <div className="eval-run-progress" role="status">
          <Progress percent={run.status === "running" ? 60 : 20} status="active" showInfo={false} />
          <span>{run.status === "running" ? t("eval.workbench.scoringCases", "Scoring test cases…") : t("eval.workbench.waitingEvaluator", "Waiting for evaluator worker…")}</span>
        </div>
      ) : null}
      {run.error_message ? <Alert type="error" showIcon message={run.error_message} /> : null}
      {error ? (
        <Alert
          type="error"
          showIcon
          message={t("eval.workbench.resultsLoadFailed", "Could not load case results")}
          description={error.message}
          action={<Button onClick={onRetry}>{t("common.retry", "Retry")}</Button>}
        />
      ) : null}

      <div className="eval-run-metrics">
        <div><span>{t("eval.workbench.averageScore", "Average score")}</span><strong>{averageScore === null ? "—" : averageScore.toFixed(3)}</strong></div>
        <div><span>{t("eval.workbench.scored", "Scored")}</span><strong>{scoredCount ?? "—"}</strong></div>
        <div><span>{t("eval.workbench.needsReview", "Needs review")}</span><strong>{reviewCount ?? "—"}</strong></div>
        <div><span>{t("eval.workbench.skipped", "Skipped")}</span><strong>{skippedCount ?? "—"}</strong></div>
      </div>

      <div className="eval-run-toolbar">
        <Segmented<ResultFilter>
          value={filter}
          onChange={setFilter}
          options={[
            { label: `All ${cases.length}`, value: "all" },
            { label: `Failed ${cases.filter((item) => item.status === "failed").length}`, value: "failed" },
            { label: `Review ${cases.filter((item) => item.status === "review").length}`, value: "review" },
            { label: `Passed ${cases.filter((item) => item.status === "passed").length}`, value: "passed" },
          ]}
        />
        <Input
          allowClear
          prefix={<Search size={14} />}
          placeholder={t("eval.workbench.searchRunResults", "Search case, failure, or trace")}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>

      {visibleCases.length ? (
        <div className="eval-case-results" role="list" aria-label="Evaluation case results">
          {visibleCases.map((item) => (
            <article key={`${item.example_id || item.case_id}:${item.candidate_trace_id}`} className="eval-case-result" role="listitem">
              <div className="eval-case-result-main">
                <Tag color={statusColor(item.status)}>{item.status}</Tag>
                <div>
                  <strong>{item.case_id}</strong>
                  <span>{item.trace.model_id || "unknown model"} · {item.trace.total_latency_ms || 0} ms</span>
                </div>
              </div>
              <div className="eval-case-score-list">
                {item.scores.map((score) => (
                  <Tag key={`${score.score_name}:${score.target_type || "trace"}:${score.target_id || score.span_id || ""}`} color={score.label === "pass" ? "success" : score.label === "fail" ? "error" : "warning"}>
                    {score.score_name}: {typeof score.numeric_value === "number" ? score.numeric_value.toFixed(3) : score.label || "review"}
                  </Tag>
                ))}
              </div>
              <p title={item.failure_reason || undefined}>
                {item.failure_reason || item.trace.output_preview || t("eval.workbench.noFailureDetail", "No failure detail")}
              </p>
              <Button
                type="link"
                icon={<ExternalLink size={14} />}
                onClick={() => onOpenTrace(item)}
                disabled={!item.candidate_trace_id}
              >
                {t("eval.workbench.openTrace", "Open trace")}
              </Button>
            </article>
          ))}
        </div>
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={isPending ? t("eval.workbench.resultsPending", "Results will appear when scoring completes.") : t("eval.workbench.noMatchingCases", "No cases match this filter.")} />
      )}
    </section>
  );
}
