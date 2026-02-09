import { Input, Empty, Tag, Spin, Button, List, Segmented } from "antd";
import {
  SearchOutlined,
  ReloadOutlined,
  ClockCircleOutlined,
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  CloseCircleOutlined,
} from "@ant-design/icons";
import { useState, useCallback, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { PanelWrapper } from "../PanelWrapper";
import { useDashboardContext } from "../../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getColors } from "../../styles";
import { getRequestTrace, listRequestTraces, type RequestTrace } from "@/api/usage";
import { useTranslation } from "react-i18next";
import { useDashboardEntityLabels } from "../../hooks/useDashboardEntityLabels";

type TraceTab = "all" | "error" | "slow" | "sampled";

interface TraceSpan {
  name: string;
  duration_ms: number;
  status: "success" | "error" | "pending";
  start_offset_ms: number;
}

function formatDuration(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  return `${ms}ms`;
}

function normalizeTraceSpans(trace: RequestTrace): TraceSpan[] {
  const raw = Array.isArray(trace.trace_steps) ? trace.trace_steps : [];
  const spans = raw
    .map((step) => ({
      name: step.name || "unknown_step",
      duration_ms: Math.max(Number(step.duration_ms || 0), 0),
      status: (step.status || "success") as "success" | "error" | "pending",
      start_offset_ms: Math.max(Number(step.start_offset_ms || 0), 0),
    }))
    .sort((a, b) => a.start_offset_ms - b.start_offset_ms);

  if (spans.length > 0) {
    return spans;
  }

  return [
    {
      name: "gateway.request_total",
      duration_ms: trace.request_total_duration_ms || 0,
      start_offset_ms: 0,
      status: trace.status,
    },
  ];
}

function TraceTimeline({ trace }: { trace: RequestTrace }) {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const { resolveServiceLabel, resolveUserLabel } = useDashboardEntityLabels();
  const spans = normalizeTraceSpans(trace);
  const maxDuration = Math.max(trace.request_total_duration_ms || 0, 1);

  return (
    <div style={{ marginTop: 16 }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 16,
          padding: "12px 16px",
          background: colors.innerBg,
          borderRadius: 8,
          border: `1px solid ${colors.border}`,
          gap: 12,
        }}
      >
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: colors.textPrimary }}>{trace.request_id}</div>
          <div style={{ fontSize: 11, color: colors.textMuted, marginTop: 2 }}>
            {trace.timestamp ? new Date(trace.timestamp).toLocaleString() : "-"}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", justifyContent: "flex-end" }}>
          <Tag color={trace.status === "success" ? "success" : "error"}>
            {trace.status === "success" ? t("dashboard.requestTrace.status.success") : t("dashboard.requestTrace.status.error")}
          </Tag>
          <Tag>{trace.model}</Tag>
          <Tag color="blue">{trace.provider}</Tag>
          <Tag color="purple">{resolveServiceLabel(trace.service_id)}</Tag>
          <Tag color="cyan">{resolveUserLabel(trace.user_id)}</Tag>
          {trace.error_type ? <Tag color="red">{trace.error_type}</Tag> : null}
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
          gap: 8,
          marginBottom: 16,
        }}
      >
        <div style={{ padding: 8, border: `1px solid ${colors.border}`, borderRadius: 6, background: colors.innerBg }}>
          <div style={{ fontSize: 11, color: colors.textMuted }}>TTFB</div>
          <div style={{ fontSize: 14, fontWeight: 700, color: "#06b6d4" }}>{formatDuration(trace.first_token_latency_ms || 0)}</div>
        </div>
        <div style={{ padding: 8, border: `1px solid ${colors.border}`, borderRadius: 6, background: colors.innerBg }}>
          <div style={{ fontSize: 11, color: colors.textMuted }}>LLM</div>
          <div style={{ fontSize: 14, fontWeight: 700, color: "#8b5cf6" }}>{formatDuration(trace.llm_inference_duration_ms || 0)}</div>
        </div>
        <div style={{ padding: 8, border: `1px solid ${colors.border}`, borderRadius: 6, background: colors.innerBg }}>
          <div style={{ fontSize: 11, color: colors.textMuted }}>Retrieval</div>
          <div style={{ fontSize: 14, fontWeight: 700, color: "#10b981" }}>{formatDuration(trace.retrieval_duration_ms || 0)}</div>
        </div>
        <div style={{ padding: 8, border: `1px solid ${colors.border}`, borderRadius: 6, background: colors.innerBg }}>
          <div style={{ fontSize: 11, color: colors.textMuted }}>Tool</div>
          <div style={{ fontSize: 14, fontWeight: 700, color: "#f59e0b" }}>{formatDuration(trace.tool_call_duration_ms || 0)}</div>
        </div>
      </div>

      <div
        style={{
          position: "relative",
          height: 24,
          background: colors.innerBg,
          borderRadius: 4,
          marginBottom: 16,
          overflow: "hidden",
          border: `1px solid ${colors.border}`,
        }}
      >
        {spans.map((span, index) => {
          const left = (span.start_offset_ms / maxDuration) * 100;
          const width = (span.duration_ms / maxDuration) * 100;
          const bgColor =
            span.status === "success"
              ? "rgba(16, 185, 129, 0.7)"
              : span.status === "error"
              ? "rgba(239, 68, 68, 0.7)"
              : "rgba(245, 158, 11, 0.7)";

          return (
            <div
              key={`${span.name}-${index}`}
              title={`${span.name}: ${formatDuration(span.duration_ms)}`}
              style={{
                position: "absolute",
                left: `${left}%`,
                width: `${Math.max(width, 1)}%`,
                height: "100%",
                background: bgColor,
                borderRight: "1px solid rgba(255,255,255,0.3)",
              }}
            />
          );
        })}
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {spans.map((span, index) => (
          <div
            key={`${span.name}-${index}`}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "8px 12px",
              background: colors.innerBg,
              borderRadius: 6,
              border: `1px solid ${colors.border}`,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              {span.status === "success" ? (
                <CheckCircleOutlined style={{ color: "#10b981", fontSize: 14 }} />
              ) : span.status === "error" ? (
                <ExclamationCircleOutlined style={{ color: "#ef4444", fontSize: 14 }} />
              ) : (
                <ClockCircleOutlined style={{ color: "#f59e0b", fontSize: 14 }} />
              )}
              <span style={{ fontSize: 12, color: colors.textPrimary }}>{span.name}</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <span style={{ fontSize: 11, color: colors.textMuted }}>+{formatDuration(span.start_offset_ms)}</span>
              <span
                style={{
                  fontSize: 12,
                  fontWeight: 600,
                  color: span.duration_ms > 100 ? "#f59e0b" : colors.textSecondary,
                  minWidth: 60,
                  textAlign: "right",
                }}
              >
                {formatDuration(span.duration_ms)}
              </span>
            </div>
          </div>
        ))}
      </div>

      <div
        style={{
          marginTop: 12,
          padding: "8px 12px",
          background: darkMode ? "rgba(59, 130, 246, 0.1)" : "rgba(59, 130, 246, 0.05)",
          borderRadius: 6,
          border: `1px solid ${darkMode ? "rgba(59, 130, 246, 0.3)" : "rgba(59, 130, 246, 0.2)"}`,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <span style={{ fontSize: 12, color: colors.textSecondary }}>{t("dashboard.requestTrace.totalDuration")}</span>
        <span style={{ fontSize: 14, fontWeight: 700, color: "#3b82f6" }}>
          {formatDuration(trace.request_total_duration_ms || 0)}
        </span>
      </div>
    </div>
  );
}

export function RequestTracePanel() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const { dateRange, serviceId, userId, lastRefresh, traceFilter, clearTraceFilter } = useDashboardContext();
  const { resolveServiceLabel, resolveUserLabel } = useDashboardEntityLabels();
  const [searchId, setSearchId] = useState("");
  const [loading, setLoading] = useState(false);
  const [trace, setTrace] = useState<RequestTrace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<TraceTab>("all");

  // 响应来自 KPI 卡片的联动筛选
  useEffect(() => {
    if (traceFilter.status === "error") {
      setActiveTab("error");
    } else if (traceFilter.sample_reason === "slow_request") {
      setActiveTab("slow");
    }
  }, [traceFilter]);

  // 根据 tab 和 traceFilter 构建查询参数
  const getQueryParams = () => {
    const params: Parameters<typeof listRequestTraces>[0] = {
      start_date: dateRange[0],
      end_date: dateRange[1],
      service_id: serviceId !== "all" ? serviceId : undefined,
      user_id: userId !== "all" ? userId : undefined,
      limit: 15,
    };
    if (activeTab === "error") {
      params.status = "error";
    }
    // Apply any traceFilter overrides
    if (traceFilter.error_type) {
      params.error_type = traceFilter.error_type;
    }
    if (traceFilter.service_id) {
      params.service_id = traceFilter.service_id;
    }
    return params;
  };

  const recentQuery = useQuery({
    queryKey: ["dashboard-request-trace-list", dateRange, serviceId, userId, activeTab, traceFilter, lastRefresh.getTime()],
    queryFn: () => listRequestTraces(getQueryParams()),
    staleTime: 30000,
  });

  // 对 slow / sampled 做客户端过滤 (API 不直接支持 sample_reason 查询)
  const filteredTraces = (() => {
    const raw = recentQuery.data || [];
    if (activeTab === "slow") {
      return raw.filter((t) => t.sample_reason === "slow_request" || t.request_total_duration_ms > 5000);
    }
    if (activeTab === "sampled") {
      return raw.filter((t) => t.sample_reason === "baseline_sample");
    }
    return raw;
  })();

  const handleSearch = useCallback(async () => {
    const reqId = searchId.trim();
    if (!reqId) return;

    setLoading(true);
    setError(null);
    try {
      const result = await getRequestTrace(reqId);
      setTrace(result);
      setError(null);
    } catch {
      setTrace(null);
      setError(t("dashboard.requestTrace.notFound"));
    } finally {
      setLoading(false);
    }
  }, [searchId, t]);

  const handleReset = useCallback(() => {
    setSearchId("");
    setTrace(null);
    setError(null);
  }, []);

  const hasActiveFilter = traceFilter.status || traceFilter.error_type || traceFilter.sample_reason;

  return (
    <PanelWrapper
      title={t("dashboard.requestTrace.title")}
      onRefresh={() => recentQuery.refetch()}
      loading={recentQuery.isLoading}
    >
      {/* 搜索栏 */}
      <div id="request-trace-panel" style={{ display: "flex", gap: 8, marginBottom: 8 }}>
        <Input
          placeholder={t("dashboard.requestTrace.searchPlaceholder")}
          prefix={<SearchOutlined style={{ color: colors.textMuted }} />}
          value={searchId}
          onChange={(e) => setSearchId(e.target.value)}
          onPressEnter={handleSearch}
          style={{ flex: 1 }}
        />
        <Button type="primary" icon={<SearchOutlined />} onClick={handleSearch} loading={loading}>
          {t("common.search")}
        </Button>
        {(trace || error) && (
          <Button icon={<ReloadOutlined />} onClick={handleReset}>
            {t("common.reset")}
          </Button>
        )}
      </div>

      {/* 筛选 Tab */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
        <Segmented
          size="small"
          value={activeTab}
          onChange={(val) => {
            setActiveTab(val as TraceTab);
            setTrace(null);
            setError(null);
            if (hasActiveFilter) clearTraceFilter();
          }}
          options={[
            { value: "all", label: t("dashboard.requestTrace.tab.all", "全部") },
            { value: "error", label: t("dashboard.requestTrace.tab.error", "失败请求") },
            { value: "slow", label: t("dashboard.requestTrace.tab.slow", "慢请求") },
            { value: "sampled", label: t("dashboard.requestTrace.tab.sampled", "采样") },
          ]}
        />
        {hasActiveFilter && (
          <Tag
            closable
            onClose={() => { clearTraceFilter(); setActiveTab("all"); }}
            color="blue"
            style={{ margin: 0 }}
          >
            <CloseCircleOutlined style={{ marginRight: 4 }} />
            {t("dashboard.requestTrace.kpiFilter", "来自KPI联动")}
          </Tag>
        )}
      </div>

      {loading ? (
        <div style={{ textAlign: "center", padding: 40 }}>
          <Spin tip={t("dashboard.requestTrace.loading")} />
        </div>
      ) : trace ? (
        <TraceTimeline trace={trace} />
      ) : error ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span style={{ color: colors.textMuted }}>{error}</span>} />
      ) : filteredTraces.length > 0 ? (
        <List
          size="small"
          dataSource={filteredTraces}
          renderItem={(item) => (
            <List.Item
              style={{ cursor: "pointer" }}
              onClick={() => {
                setTrace(item);
                setSearchId(item.request_id);
                setError(null);
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", width: "100%", gap: 8 }}>
                <div style={{ display: "flex", flexDirection: "column" }}>
                  <span style={{ fontSize: 12, color: colors.textPrimary }}>{item.request_id}</span>
                  <span style={{ fontSize: 11, color: colors.textMuted }}>
                    {resolveServiceLabel(item.service_id)} · {resolveUserLabel(item.user_id)} · {item.provider} / {item.model}
                  </span>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
                  <Tag color={item.status === "success" ? "success" : "error"} style={{ margin: 0 }}>{item.status}</Tag>
                  {item.error_type && <Tag color="red" style={{ margin: 0 }}>{item.error_type}</Tag>}
                  <span style={{ fontSize: 11, color: colors.textMuted }}>{formatDuration(item.request_total_duration_ms)}</span>
                  {item.total_cost_usd > 0 && (
                    <span style={{ fontSize: 11, color: "#10b981", fontWeight: 500 }}>
                      ${item.total_cost_usd >= 1 ? item.total_cost_usd.toFixed(2) : item.total_cost_usd.toFixed(4)}
                    </span>
                  )}
                </div>
              </div>
            </List.Item>
          )}
        />
      ) : (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <div style={{ color: colors.textMuted }}>
              <p>{activeTab !== "all"
                ? t("dashboard.requestTrace.emptyFiltered", "当前筛选条件下无匹配的请求追踪")
                : t("dashboard.requestTrace.emptyTitle")
              }</p>
              <p style={{ fontSize: 12, marginTop: 4 }}>{t("dashboard.requestTrace.emptyDesc")}</p>
            </div>
          }
        />
      )}
    </PanelWrapper>
  );
}
