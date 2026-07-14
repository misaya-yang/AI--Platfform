import { Alert, Button, Empty, Segmented, Tag } from "antd";
import { ExternalLink, GitCompare, TrendingUp } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type { EvalExperimentRun, EvalExperimentRunComparisonResponse } from "@/api/eval";

type WindowDays = 7 | 30 | 90;

const METRICS = [
  { key: "quality_score", fallbacks: ["overall_score", "average_score"], format: "score" },
  { key: "latency_ms", fallbacks: ["latency_p50_ms", "p50_latency_ms", "avg_latency_ms"], format: "ms" },
  { key: "total_tokens_per_task", fallbacks: ["tokens_per_task", "total_tokens"], format: "number" },
  { key: "cost_per_task_cents", fallbacks: ["cost_cents_per_task", "total_cost_cents"], format: "cost" },
  { key: "execution_error_rate", fallbacks: ["error_rate"], format: "percent" },
] as const;

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function pick(source: Record<string, unknown>, keys: readonly string[]): number | null {
  for (const key of keys) {
    const value = number(source[key]);
    if (value !== null) return value;
  }
  return null;
}

function runMetric(run: EvalExperimentRun, keys: readonly string[]): number | null {
  return pick(run.metrics || {}, keys) ?? pick(run.score_summary || {}, keys);
}

function formatMetric(value: number | null, format: string): string {
  if (value === null) return "—";
  if (format === "percent") return `${(value * 100).toFixed(1)}%`;
  if (format === "ms") return `${Math.round(value)} ms`;
  if (format === "cost") return `${value.toFixed(3)}¢`;
  if (format === "score") return value.toFixed(3);
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function comparisonMetric(
  comparison: EvalExperimentRunComparisonResponse,
  key: string,
  fallbacks: readonly string[],
) {
  const keys = [key, ...fallbacks];
  const metricDiffs = record(comparison.metric_diffs);
  for (const metricKey of keys) {
    const rich = record(metricDiffs[metricKey]);
    if (Object.keys(rich).length) {
      return {
        baseline: number(rich.baseline ?? rich.baseline_value),
        candidate: number(rich.candidate ?? rich.candidate_value),
        delta: number(rich.delta),
        status: String(rich.status || rich.classification || ""),
      };
    }
  }
  return {
    baseline: pick(comparison.baseline_summary, keys),
    candidate: pick(comparison.candidate_summary, keys),
    delta: pick(comparison.deltas, keys),
    status: "",
  };
}

function statusColor(status: string) {
  if (/regress|fail|block/i.test(status)) return "error";
  if (/improv|pass/i.test(status)) return "success";
  if (/warn|inconclusive|insufficient|confound/i.test(status)) return "warning";
  return "default";
}

function Sparkline({ values, markerIndex }: { values: number[]; markerIndex?: number }) {
  if (values.length < 2) return <span className="eval-trend-insufficient">—</span>;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const spread = max - min || 1;
  const points = values.map((value, index) => {
    const x = (index / (values.length - 1)) * 100;
    const y = 27 - ((value - min) / spread) * 22;
    return `${x},${y}`;
  }).join(" ");
  return (
    <svg className="eval-trend-sparkline" viewBox="0 0 100 32" preserveAspectRatio="none" aria-hidden="true">
      <polyline points={points} fill="none" stroke="currentColor" strokeWidth="2" vectorEffect="non-scaling-stroke" />
      {markerIndex !== undefined && markerIndex >= 0 && markerIndex < values.length ? (
        <circle
          cx={(markerIndex / (values.length - 1)) * 100}
          cy={27 - ((values[markerIndex] - min) / spread) * 22}
          r="2.7"
          fill="var(--eval-danger)"
          vectorEffect="non-scaling-stroke"
        />
      ) : null}
    </svg>
  );
}

function liveRun(run: EvalExperimentRun) {
  const snapshot = record(run.target_snapshot);
  return (run.run_mode || snapshot.run_mode) === "live_candidate";
}

export function ExperimentRunComparison({
  comparison,
  runs,
  baselineRunId,
  onOpenTrace,
}: {
  comparison: EvalExperimentRunComparisonResponse | null;
  runs: EvalExperimentRun[];
  baselineRunId?: string;
  onOpenTrace: (traceId: string) => void;
}) {
  const { t } = useTranslation();
  const [windowDays, setWindowDays] = useState<WindowDays>(30);
  const trendRuns = useMemo(() => {
    const cutoff = Date.now() - windowDays * 86_400_000;
    return runs
      .filter((run) => run.status === "succeeded" && liveRun(run))
      .filter((run) => !run.created_at || new Date(run.created_at).getTime() >= cutoff)
      .sort((left, right) => String(left.created_at || "").localeCompare(String(right.created_at || "")))
      .slice(-100);
  }, [runs, windowDays]);
  const changedDimensions = comparison?.changed_dimensions || [];
  const attribution = comparison?.attribution || comparison?.attribution_status
    || String(comparison?.regression_summary?.attribution_status || "");
  const gateStatus = String(
    comparison?.gate?.status
    || comparison?.gate_status
    || comparison?.regression_summary?.gate_status
    || comparison?.regression_summary?.status
    || "",
  );
  const gateFailures = Array.isArray(comparison?.gate?.failures) ? comparison.gate.failures : [];
  const gateWarnings = Array.isArray(comparison?.gate?.warnings) ? comparison.gate.warnings : [];
  const gateAlertType = !gateStatus
    ? "info"
    : statusColor(gateStatus || attribution) === "error"
      ? "error"
      : gateWarnings.length || statusColor(gateStatus || attribution) === "warning"
        ? "warning"
        : "success";
  const compatibility = comparison?.compatibility;
  const compatibilityRecord = record(compatibility);
  const compatibilityText = typeof compatibility === "object"
    ? String(
        compatibilityRecord.status
        || compatibilityRecord.reason
        || (typeof compatibilityRecord.compatible === "boolean"
          ? compatibilityRecord.compatible ? "compatible" : "incompatible"
          : ""),
      )
    : compatibility === undefined ? "" : String(compatibility);
  const compatibilityReasons = Array.isArray(compatibilityRecord.reasons) ? compatibilityRecord.reasons : [];
  const statistics = record(comparison?.statistics);
  const confidenceInterval = statistics.quality_delta_ci_95
    ?? statistics.confidence_interval_95
    ?? statistics.ci95
    ?? statistics.confidence_interval;
  const statisticsVisible = Object.keys(statistics).length > 0;
  const caseDiffs = [...(comparison?.case_diffs || [])].sort((left, right) => {
    const rank = (item: Record<string, unknown>) => /regress|fail/i.test(String(item.status || item.classification || "")) ? 0 : 1;
    return rank(left) - rank(right);
  });

  return (
    <section className="eval-comparison-panel" aria-live="polite">
      <div className="eval-comparison-heading">
        <div>
          <h3>{t("eval.comparison.title")}</h3>
          <p>{comparison ? `${comparison.baseline_run_id.slice(0, 8)} → ${comparison.candidate_run_id.slice(0, 8)}` : t("eval.comparison.description")}</p>
        </div>
        <GitCompare size={18} />
      </div>

      {comparison ? (
        <>
          <Alert
            type={gateAlertType}
            showIcon
            title={`${t("eval.comparison.gate")}: ${gateStatus || t("eval.comparison.notAvailable")}`}
            description={[
              compatibilityText ? `${t("eval.comparison.compatibility")}: ${compatibilityText}` : "",
              compatibilityReasons.length ? `${t("eval.comparison.reasons")}: ${compatibilityReasons.join(", ")}` : "",
              attribution ? `${t("eval.comparison.attribution")}: ${attribution}` : "",
              changedDimensions.length ? `${t("eval.comparison.changedDimensions")}: ${changedDimensions.join(", ")}` : "",
              gateFailures.length ? `${t("eval.comparison.failures")}: ${gateFailures.join(", ")}` : "",
              gateWarnings.length ? `${t("eval.comparison.warnings")}: ${gateWarnings.join(", ")}` : "",
            ].filter(Boolean).join(" · ")}
          />
          <div className="eval-comparison-metrics">
            {METRICS.map((metric) => {
              const values = comparisonMetric(comparison, metric.key, metric.fallbacks);
              return (
                <article key={metric.key}>
                  <span>{t(`eval.comparison.metrics.${metric.key}`)}</span>
                  <strong>{formatMetric(values.candidate, metric.format)}</strong>
                  <small>
                    {t("eval.comparison.baselineShort")} {formatMetric(values.baseline, metric.format)} · Δ {formatMetric(values.delta, metric.format)}
                  </small>
                  {values.status ? <Tag color={statusColor(values.status)}>{values.status}</Tag> : null}
                </article>
              );
            })}
          </div>
          {statisticsVisible ? (
            <div className="eval-comparison-statistics">
              <span>{t("eval.comparison.evidence")}: <strong>{String(statistics.evidence_status || statistics.status || statistics.conclusion || "—")}</strong></span>
              <span>{t("eval.comparison.pairedCases")}: <strong>{String(statistics.paired_case_count ?? statistics.sample_size ?? "—")}</strong></span>
              <span>{t("eval.comparison.winTieLoss")}: <strong>{`${String(statistics.wins ?? "—")} / ${String(statistics.ties ?? "—")} / ${String(statistics.losses ?? "—")}`}</strong></span>
              <span>{t("eval.comparison.confidenceInterval")}: <strong>{confidenceInterval === undefined ? "—" : JSON.stringify(confidenceInterval)}</strong></span>
            </div>
          ) : null}
        </>
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("eval.comparison.empty")} />
      )}

      <div className="eval-trend-heading">
        <div><TrendingUp size={16} /><strong>{t("eval.comparison.trends")}</strong></div>
        <Segmented<WindowDays> value={windowDays} onChange={setWindowDays} options={[7, 30, 90].map((value) => ({ label: `${value}d`, value: value as WindowDays }))} />
      </div>
      <div className="eval-trend-grid">
        {METRICS.map((metric) => {
          const samples = trendRuns
            .map((run) => ({ run, value: runMetric(run, [metric.key, ...metric.fallbacks]) }))
            .filter((sample): sample is { run: EvalExperimentRun; value: number } => sample.value !== null);
          const values = samples.map((sample) => sample.value);
          const markerIndex = samples.findIndex((sample) => sample.run.run_id === baselineRunId);
          return (
            <article key={metric.key}>
              <span>{t(`eval.comparison.metrics.${metric.key}`)}</span>
              <Sparkline values={values} markerIndex={markerIndex} />
              <small>
                {values.length ? `${formatMetric(values.at(-1) ?? null, metric.format)} · ${values.length} runs` : t("eval.comparison.noTrendData")}
                {markerIndex >= 0 ? ` · ${t("eval.comparison.baselineMarker")}` : ""}
              </small>
            </article>
          );
        })}
      </div>

      {caseDiffs.length ? (
        <div className="eval-case-diffs">
          <h4>{t("eval.comparison.caseRegressions")}</h4>
          {caseDiffs.slice(0, 50).map((item, index) => {
            const status = String(item.status || item.classification || "changed");
            const baselineTraceId = String(item.baseline_trace_id || "");
            const candidateTraceId = String(item.candidate_trace_id || "");
            const toolDiffs = Array.isArray(item.tool_diffs) ? item.tool_diffs : [];
            const ragDiffs = Array.isArray(item.rag_diffs) ? item.rag_diffs : [];
            const baselineOutput = item.baseline_output;
            const candidateOutput = item.candidate_output;
            return (
              <article key={`${String(item.case_id || index)}:${candidateTraceId}`}>
                <div><Tag color={statusColor(status)}>{status}</Tag><strong>{String(item.case_id || `case-${index + 1}`)}</strong></div>
                <p>{String(item.failure_reason || item.summary || item.output_diff || t("eval.comparison.noCaseDetail"))}</p>
                {toolDiffs.length ? <small>{t("eval.comparison.toolChanges", { count: toolDiffs.length })}</small> : null}
                {ragDiffs.length ? <small>{t("eval.comparison.ragChanges", { count: ragDiffs.length })}</small> : null}
                <div>
                  {baselineTraceId ? <Button type="link" icon={<ExternalLink size={13} />} onClick={() => onOpenTrace(baselineTraceId)}>{t("eval.comparison.baselineTrace")}</Button> : null}
                  {candidateTraceId ? <Button type="link" icon={<ExternalLink size={13} />} onClick={() => onOpenTrace(candidateTraceId)}>{t("eval.comparison.candidateTrace")}</Button> : null}
                </div>
                {baselineOutput !== undefined || candidateOutput !== undefined ? (
                  <div className="eval-case-output-diff">
                    <div><small>{t("eval.comparison.baselineOutput")}</small><p>{String(baselineOutput ?? "—")}</p></div>
                    <div><small>{t("eval.comparison.candidateOutput")}</small><p>{String(candidateOutput ?? "—")}</p></div>
                  </div>
                ) : null}
                {toolDiffs.length || ragDiffs.length ? (
                  <details className="eval-case-evidence">
                    <summary>{t("eval.comparison.trajectoryEvidence")}</summary>
                    <pre>{JSON.stringify({ tool_diffs: toolDiffs, rag_diffs: ragDiffs }, null, 2)}</pre>
                  </details>
                ) : null}
              </article>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}
