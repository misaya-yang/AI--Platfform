import { Button, DatePicker, Empty, Input, InputNumber, Select, Space, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Activity, Filter, RefreshCw, Search } from "lucide-react";
import type { Dispatch, SetStateAction } from "react";
import { useTranslation } from "react-i18next";

import type { AgentTraceSummary, TraceStatus } from "@/api/eval";

const { RangePicker } = DatePicker;

export type ScoreStatusFilter = "all" | "scored" | "unscored";

export interface AssistantTraceFilters {
  status?: TraceStatus | "all";
  model_id?: string;
  user_id?: string;
  session_id?: string;
  run_id?: string;
  request_id?: string;
  transcript_query?: string;
  turn_index?: number;
  score_status?: ScoreStatusFilter;
  start_date?: string;
  end_date?: string;
}

interface AssistantTraceListProps {
  traces: AgentTraceSummary[];
  total: number;
  filters: AssistantTraceFilters;
  setFilters: Dispatch<SetStateAction<AssistantTraceFilters>>;
  title?: string;
  ariaLabel?: string;
  emptyText?: string;
  selectedTraceId?: string;
  loading: boolean;
  error?: Error | null;
  onSelect: (traceId: string) => void;
  onRefresh: () => void;
}

function formatDuration(ms: number) {
  if (!ms) return "0ms";
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  return `${ms}ms`;
}

function formatDate(value: string | null | undefined, locale: string) {
  if (!value) return "-";
  return new Date(value).toLocaleString(locale);
}

function statusColor(status: TraceStatus) {
  if (status === "succeeded") return "success";
  if (status === "failed" || status === "timeout") return "error";
  if (status === "cancelled") return "warning";
  return "processing";
}

function traceLocator(trace: AgentTraceSummary): Record<string, unknown> {
  const locator = trace.metadata?.transcript_locator;
  return locator && typeof locator === "object" && !Array.isArray(locator)
    ? (locator as Record<string, unknown>)
    : {};
}

function traceTurn(trace: AgentTraceSummary): string | null {
  const turn = traceLocator(trace).turn_index;
  if (typeof turn === "number" || typeof turn === "string") return String(turn);
  return null;
}

export function AssistantTraceList({
  traces,
  total,
  filters,
  setFilters,
  title,
  ariaLabel,
  emptyText,
  selectedTraceId,
  loading,
  error,
  onSelect,
  onRefresh,
}: AssistantTraceListProps) {
  const { t, i18n } = useTranslation();
  const updateFilter = <K extends keyof AssistantTraceFilters>(
    key: K,
    value: AssistantTraceFilters[K]
  ) => {
    setFilters((current) => ({ ...current, [key]: value }));
  };

  const clearFilters = () => setFilters({ status: "all", score_status: "all" });

  const columns: ColumnsType<AgentTraceSummary> = [
    {
      title: t("eval.list.columns.trace"),
      dataIndex: "trace_id",
      key: "trace",
      width: 250,
      fixed: "left",
      render: (traceId, item) => {
        const turn = traceTurn(item);
        return (
          <div className="eval-trace-id-cell">
            <div className="eval-trace-id-line">{traceId}</div>
            <div className="eval-trace-subline">
              {turn ? t("eval.list.turnShort", { turn }) + " · " : ""}
              {item.session_id || "-"} · {item.run_id || "-"}
            </div>
          </div>
        );
      },
    },
    {
      title: t("eval.list.columns.status"),
      dataIndex: "status",
      key: "status",
      width: 116,
      render: (status: TraceStatus, item) => (
        <Space size={6} wrap>
          <Tag color={statusColor(status)} className="eval-status-tag">
            {t(`eval.status.${status}`, status)}
          </Tag>
          {item.scores_count > 0 ? <Tag className="eval-status-tag">{t("eval.score.scored")}</Tag> : null}
        </Space>
      ),
    },
    {
      title: t("eval.list.columns.model"),
      key: "model",
      width: 170,
      render: (_, item) => (
        <div className="eval-trace-id-cell">
          <div className="eval-trace-model">{item.model_id || "-"}</div>
          <div className="eval-trace-subline">{item.provider || "-"}</div>
        </div>
      ),
    },
    {
      title: t("eval.list.columns.latency"),
      key: "latency",
      width: 128,
      render: (_, item) => (
        <div>
          <div className="eval-trace-metric">{formatDuration(item.total_latency_ms)}</div>
          <div className="eval-trace-subline">
            {t("eval.list.firstTokenShort", { duration: formatDuration(item.first_token_latency_ms) })}
          </div>
        </div>
      ),
    },
    {
      title: t("eval.list.columns.tokens"),
      dataIndex: "total_tokens",
      key: "tokens",
      width: 110,
      render: (tokens: number) => <span className="eval-trace-metric">{tokens.toLocaleString()}</span>,
    },
    {
      title: t("eval.list.columns.user"),
      dataIndex: "user_id",
      key: "user",
      width: 150,
      ellipsis: true,
    },
    {
      title: t("eval.list.columns.started"),
      dataIndex: "started_at",
      key: "started",
      width: 168,
      render: (value) => formatDate(value, i18n.language),
    },
  ];

  return (
    <section className="eval-panel eval-trace-list" aria-label={ariaLabel || t("eval.list.ariaLabel")}>
      <div className="eval-panel-heading">
        <div>
          <h2>{title || t("eval.list.title")}</h2>
          <p>
            {t("eval.list.counts", {
              total: total.toLocaleString(),
              visible: traces.length.toLocaleString(),
            })}
          </p>
        </div>
        <Space size={8}>
          <Tooltip title={t("eval.list.resetFilters")}>
            <Button icon={<Filter size={15} />} onClick={clearFilters}>
              {t("common.reset")}
            </Button>
          </Tooltip>
          <Tooltip title={t("eval.list.refreshTraces")}>
            <Button icon={<RefreshCw size={15} />} onClick={onRefresh} loading={loading}>
              {t("common.refresh")}
            </Button>
          </Tooltip>
        </Space>
      </div>

      <div className="eval-filter-grid" aria-label="Trace filters">
        <Input
          className="eval-transcript-filter"
          aria-label={t("eval.filters.transcript")}
          prefix={<Search size={14} />}
          placeholder={t("eval.filters.transcript")}
          value={filters.transcript_query}
          onChange={(event) => updateFilter("transcript_query", event.target.value)}
          allowClear
        />
        <Select
          aria-label={t("eval.filters.status")}
          value={filters.status || "all"}
          onChange={(value) => updateFilter("status", value as TraceStatus | "all")}
          options={[
            { label: t("eval.filters.allStatuses"), value: "all" },
            { label: t("eval.status.succeeded"), value: "succeeded" },
            { label: t("eval.status.running"), value: "running" },
            { label: t("eval.status.failed"), value: "failed" },
            { label: t("eval.status.cancelled"), value: "cancelled" },
            { label: t("eval.status.timeout"), value: "timeout" },
          ]}
        />
        <Input
          aria-label={t("eval.filters.model")}
          prefix={<Search size={14} />}
          placeholder={t("eval.filters.model")}
          value={filters.model_id}
          onChange={(event) => updateFilter("model_id", event.target.value)}
          allowClear
        />
        <Input
          aria-label={t("eval.filters.user")}
          placeholder={t("eval.filters.user")}
          value={filters.user_id}
          onChange={(event) => updateFilter("user_id", event.target.value)}
          allowClear
        />
        <Input
          aria-label={t("eval.filters.session")}
          placeholder={t("eval.filters.session")}
          value={filters.session_id}
          onChange={(event) => updateFilter("session_id", event.target.value)}
          allowClear
        />
        <Input
          aria-label={t("eval.filters.run")}
          placeholder={t("eval.filters.run")}
          value={filters.run_id}
          onChange={(event) => updateFilter("run_id", event.target.value)}
          allowClear
        />
        <Input
          aria-label={t("eval.filters.request")}
          placeholder={t("eval.filters.request")}
          value={filters.request_id}
          onChange={(event) => updateFilter("request_id", event.target.value)}
          allowClear
        />
        <InputNumber
          aria-label={t("eval.filters.turn")}
          placeholder={t("eval.filters.turn")}
          min={1}
          precision={0}
          value={filters.turn_index}
          onChange={(value) =>
            updateFilter("turn_index", typeof value === "number" ? value : undefined)
          }
          style={{ width: "100%" }}
        />
        <Select
          aria-label={t("eval.filters.scoreStatus")}
          value={filters.score_status || "all"}
          onChange={(value) => updateFilter("score_status", value as ScoreStatusFilter)}
          options={[
            { label: t("eval.filters.allScoreStates"), value: "all" },
            { label: t("eval.score.scored"), value: "scored" },
            { label: t("eval.score.unscored"), value: "unscored" },
          ]}
        />
        <RangePicker
          aria-label={t("eval.filters.dateRange")}
          className="eval-date-range"
          placeholder={[t("eval.filters.startDate"), t("eval.filters.endDate")]}
          onChange={(_dates, dateStrings) => {
            updateFilter("start_date", dateStrings[0] || undefined);
            updateFilter("end_date", dateStrings[1] || undefined);
          }}
        />
      </div>

      {error ? (
        <div className="eval-state-panel" role="alert">
          <Activity size={18} />
          <div>
            <strong>{t("eval.list.unavailableTitle")}</strong>
            <span>{error.message || t("eval.list.unavailableDescription")}</span>
          </div>
        </div>
      ) : (
        <Table
          className="eval-trace-table"
          columns={columns}
          dataSource={traces}
          loading={loading}
          rowKey="trace_id"
          size="small"
          pagination={false}
          scroll={{ x: 1080, y: 420 }}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={emptyText || t("eval.list.empty")}
              />
            ),
          }}
          rowClassName={(trace) => (trace.trace_id === selectedTraceId ? "eval-trace-row-selected" : "")}
          onRow={(trace) => ({
            onClick: () => onSelect(trace.trace_id),
            onKeyDown: (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onSelect(trace.trace_id);
              }
            },
            tabIndex: 0,
            "aria-label": t("eval.list.openTrace", {
              traceId: trace.trace_id,
              turn: traceTurn(trace) || "-",
            }),
          })}
        />
      )}
    </section>
  );
}
