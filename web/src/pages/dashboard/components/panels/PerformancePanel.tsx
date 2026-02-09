// web/src/pages/dashboard/components/panels/PerformancePanel.tsx

import { useState } from "react";
import { Select, Row, Col, Statistic } from "antd";
import { useQuery } from "@tanstack/react-query";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from "recharts";
import { SafeResponsiveChart } from "@/components/SafeResponsiveChart";
import dayjs from "dayjs";
import { PanelWrapper } from "../PanelWrapper";
import { useDashboardContext } from "../../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getPerformanceBreakdown } from "@/api/usage";
import { useTranslation } from "react-i18next";

type LatencyMetric = "p50" | "p95" | "p99" | "avg";

export function PerformancePanel() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const { dateRange, granularity, lastRefresh } = useDashboardContext();
  const [selectedMetric, setSelectedMetric] = useState<LatencyMetric>("p95");

  const { data, isLoading, refetch } = useQuery({
    queryKey: ["dashboard-performance", dateRange, granularity, lastRefresh.getTime()],
    queryFn: () =>
      getPerformanceBreakdown({
        start_date: dateRange[0],
        end_date: dateRange[1],
        granularity,
      }),
    staleTime: 30000,
  });

  const chartData = (data?.data || []).map((point) => ({
    date: point.date,
    avgLatency: point.avg_total_ms,
    p50: point.p50_total_ms,
    p95: point.p95_total_ms,
    p99: point.p99_total_ms,
    ttfb: point.avg_first_token_ms,
    llm: point.avg_llm_inference_ms,
    retrieval: point.avg_retrieval_ms,
    tool: point.avg_tool_call_ms,
    overhead: point.avg_overhead_ms,
  }));

  const latestData = chartData[chartData.length - 1];
  const gridColor = darkMode ? "#334155" : "#e2e8f0";

  const metricOptions = [
    { value: "avg", label: t("metrics.avgLatency") },
    { value: "p50", label: "P50" },
    { value: "p95", label: "P95" },
    { value: "p99", label: "P99" },
  ];

  return (
    <PanelWrapper
      title={t("dashboard.performance.title")}
      onRefresh={refetch}
      loading={isLoading}
      extra={
        <Select
          value={selectedMetric}
          onChange={setSelectedMetric}
          options={metricOptions}
          size="small"
          style={{ width: 100 }}
        />
      }
    >
      {/* Summary stats */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Statistic
            title={<span style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>P50</span>}
            value={latestData?.p50 || 0}
            suffix="ms"
            styles={{ content: { fontSize: 16, color: darkMode ? "#f1f5f9" : "#1e293b" } }}
          />
        </Col>
        <Col span={6}>
          <Statistic
            title={<span style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>P95</span>}
            value={latestData?.p95 || 0}
            suffix="ms"
            styles={{ content: { fontSize: 16, color: "#f59e0b" } }}
          />
        </Col>
        <Col span={6}>
          <Statistic
            title={<span style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>P99</span>}
            value={latestData?.p99 || 0}
            suffix="ms"
            styles={{ content: { fontSize: 16, color: "#ef4444" } }}
          />
        </Col>
        <Col span={6}>
          <Statistic
            title={<span style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>{t("dashboard.performance.avg")}</span>}
            value={latestData?.avgLatency || 0}
            suffix="ms"
            styles={{ content: { fontSize: 16, color: "#3b82f6" } }}
          />
        </Col>
      </Row>

      {/* Phase breakdown */}
      <Row gutter={12} style={{ marginBottom: 16 }}>
        <Col span={8}>
          <Statistic
            title={<span style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>TTFB</span>}
            value={latestData?.ttfb || 0}
            suffix="ms"
            styles={{ content: { fontSize: 14, color: "#06b6d4" } }}
          />
        </Col>
        <Col span={8}>
          <Statistic
            title={<span style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>LLM</span>}
            value={latestData?.llm || 0}
            suffix="ms"
            styles={{ content: { fontSize: 14, color: "#8b5cf6" } }}
          />
        </Col>
        <Col span={8}>
          <Statistic
            title={<span style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>Retrieval</span>}
            value={latestData?.retrieval || 0}
            suffix="ms"
            styles={{ content: { fontSize: 14, color: "#10b981" } }}
          />
        </Col>
      </Row>
      <Row gutter={12} style={{ marginBottom: 8 }}>
        <Col span={12}>
          <Statistic
            title={<span style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>Tool Calls</span>}
            value={latestData?.tool || 0}
            suffix="ms"
            styles={{ content: { fontSize: 14, color: "#f59e0b" } }}
          />
        </Col>
        <Col span={12}>
          <Statistic
            title={<span style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>Overhead</span>}
            value={latestData?.overhead || 0}
            suffix="ms"
            styles={{ content: { fontSize: 14, color: "#ef4444" } }}
          />
        </Col>
      </Row>

      {/* Chart */}
      <SafeResponsiveChart height={200} minWidth={100} minHeight={100}>
        <AreaChart data={chartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={gridColor} strokeDasharray="3 3" />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 11 }}
            tickFormatter={(value) =>
              dayjs(value).format(granularity === "hour" ? "HH:mm" : "MM-DD")
            }
          />
          <YAxis tick={{ fontSize: 11 }} width={40} />
          <Tooltip
            formatter={(value) => [`${value ?? 0} ms`, ""]}
            labelFormatter={(label: string) => dayjs(label).format("YYYY-MM-DD HH:mm")}
          />
          <Area
            type="monotone"
            dataKey={selectedMetric === "avg" ? "avgLatency" : selectedMetric}
            stroke="#3b82f6"
            fill="rgba(59, 130, 246, 0.2)"
            strokeWidth={2}
          />
        </AreaChart>
      </SafeResponsiveChart>
    </PanelWrapper>
  );
}
