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
import { getColors } from "../../styles";

type LatencyMetric = "p50" | "p95" | "p99" | "avg";

export function PerformancePanel() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
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
  const gridColor = colors.divider;

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
      <Row gutter={12} style={{ marginBottom: 10 }}>
        <Col span={6}>
          <Statistic
            title={<span style={{ fontSize: 11, color: colors.textMuted }}>P50</span>}
            value={latestData?.p50 || 0}
            suffix="ms"
            styles={{ content: { fontSize: 15, color: colors.textPrimary } }}
          />
        </Col>
        <Col span={6}>
          <Statistic
            title={<span style={{ fontSize: 11, color: colors.textMuted }}>P95</span>}
            value={latestData?.p95 || 0}
            suffix="ms"
            styles={{ content: { fontSize: 15, color: colors.warning } }}
          />
        </Col>
        <Col span={6}>
          <Statistic
            title={<span style={{ fontSize: 11, color: colors.textMuted }}>P99</span>}
            value={latestData?.p99 || 0}
            suffix="ms"
            styles={{ content: { fontSize: 15, color: colors.error } }}
          />
        </Col>
        <Col span={6}>
          <Statistic
            title={<span style={{ fontSize: 11, color: colors.textMuted }}>{t("dashboard.performance.avg")}</span>}
            value={latestData?.avgLatency || 0}
            suffix="ms"
            styles={{ content: { fontSize: 15, color: colors.info } }}
          />
        </Col>
      </Row>

      {/* Phase breakdown */}
      <Row gutter={12} style={{ marginBottom: 8 }}>
        <Col span={8}>
          <Statistic
            title={<span style={{ fontSize: 11, color: colors.textMuted }}>TTFB</span>}
            value={latestData?.ttfb || 0}
            suffix="ms"
            styles={{ content: { fontSize: 13, color: colors.info } }}
          />
        </Col>
        <Col span={8}>
          <Statistic
            title={<span style={{ fontSize: 11, color: colors.textMuted }}>LLM</span>}
            value={latestData?.llm || 0}
            suffix="ms"
            styles={{ content: { fontSize: 13, color: colors.gold } }}
          />
        </Col>
        <Col span={8}>
          <Statistic
            title={<span style={{ fontSize: 11, color: colors.textMuted }}>Retrieval</span>}
            value={latestData?.retrieval || 0}
            suffix="ms"
            styles={{ content: { fontSize: 13, color: colors.success } }}
          />
        </Col>
      </Row>
      <Row gutter={12} style={{ marginBottom: 6 }}>
        <Col span={12}>
          <Statistic
            title={<span style={{ fontSize: 11, color: colors.textMuted }}>Tool Calls</span>}
            value={latestData?.tool || 0}
            suffix="ms"
            styles={{ content: { fontSize: 13, color: colors.warning } }}
          />
        </Col>
        <Col span={12}>
          <Statistic
            title={<span style={{ fontSize: 11, color: colors.textMuted }}>Overhead</span>}
            value={latestData?.overhead || 0}
            suffix="ms"
            styles={{ content: { fontSize: 13, color: colors.error } }}
          />
        </Col>
      </Row>

      {/* Chart */}
      <SafeResponsiveChart height={138} minWidth={100} minHeight={100}>
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
            labelFormatter={(label) => dayjs(String(label)).format("YYYY-MM-DD HH:mm")}
          />
          <Area
            type="monotone"
            dataKey={selectedMetric === "avg" ? "avgLatency" : selectedMetric}
            stroke={colors.info}
            fill={colors.infoBg}
            strokeWidth={2}
          />
        </AreaChart>
      </SafeResponsiveChart>
    </PanelWrapper>
  );
}
