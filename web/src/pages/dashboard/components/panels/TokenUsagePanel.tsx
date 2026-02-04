// web/src/pages/dashboard/components/panels/TokenUsagePanel.tsx

import { Progress } from "antd";
import { useQuery } from "@tanstack/react-query";
import {
  LineChart,
  Line,
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
import { getUsageSummary, getUsageBreakdown, getUsageTimeSeries } from "@/api/usage";
import { useTranslation } from "react-i18next";

function formatTokens(num: number): string {
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`;
  if (num >= 1_000) return `${(num / 1_000).toFixed(1)}K`;
  return num.toLocaleString();
}

export function TokenUsagePanel() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const { dateRange, granularity, serviceId, userId, lastRefresh } = useDashboardContext();

  const summaryQuery = useQuery({
    queryKey: ["dashboard-token-summary", dateRange, serviceId, userId, lastRefresh.getTime()],
    queryFn: () =>
      getUsageSummary({
        start_date: dateRange[0],
        end_date: dateRange[1],
        service_id: serviceId !== "all" ? serviceId : undefined,
        user_id: userId !== "all" ? userId : undefined,
      }),
    staleTime: 30000,
  });

  const breakdownQuery = useQuery({
    queryKey: ["dashboard-token-breakdown", dateRange, serviceId, userId, lastRefresh.getTime()],
    queryFn: () =>
      getUsageBreakdown({
        dimension: "model",
        start_date: dateRange[0],
        end_date: dateRange[1],
        service_id: serviceId !== "all" ? serviceId : undefined,
        user_id: userId !== "all" ? userId : undefined,
        limit: 5,
      }),
    staleTime: 30000,
  });

  // Provider breakdown query
  const providerQuery = useQuery({
    queryKey: ["dashboard-provider-breakdown", dateRange, serviceId, userId, lastRefresh.getTime()],
    queryFn: () =>
      getUsageBreakdown({
        dimension: "provider",
        start_date: dateRange[0],
        end_date: dateRange[1],
        service_id: serviceId !== "all" ? serviceId : undefined,
        user_id: userId !== "all" ? userId : undefined,
        limit: 10,
      }),
    staleTime: 30000,
  });

  const timeseriesQuery = useQuery({
    queryKey: ["dashboard-token-timeseries", dateRange, granularity, serviceId, userId, lastRefresh.getTime()],
    queryFn: () =>
      getUsageTimeSeries({
        start_date: dateRange[0],
        end_date: dateRange[1],
        granularity,
        service_id: serviceId !== "all" ? serviceId : undefined,
        user_id: userId !== "all" ? userId : undefined,
      }),
    staleTime: 30000,
  });

  const refetch = () => {
    summaryQuery.refetch();
    breakdownQuery.refetch();
    providerQuery.refetch();
    timeseriesQuery.refetch();
  };

  const summary = summaryQuery.data;
  const breakdown = breakdownQuery.data?.items || [];
  const providerBreakdown = providerQuery.data?.items || [];
  const timeseries = timeseriesQuery.data?.data || [];

  const totalTokens = summary?.total_tokens || 0;
  const inputTokens = summary?.total_input_tokens || 0;
  const outputTokens = summary?.total_output_tokens || 0;
  const inputPercent = totalTokens > 0 ? (inputTokens / totalTokens) * 100 : 0;

  const chartData = timeseries.map((point) => ({
    date: point.date,
    tokens: point.total_tokens,
  }));

  const gridColor = darkMode ? "#334155" : "#e2e8f0";
  const modelColors = ["#3b82f6", "#8b5cf6", "#10b981", "#f59e0b", "#ef4444"];

  return (
    <PanelWrapper
      title={t("metrics.totalTokens")}
      onRefresh={refetch}
      loading={summaryQuery.isLoading}
    >
      {/* Total and breakdown */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 24, fontWeight: 700, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
          {formatTokens(totalTokens)}
          <span style={{ fontSize: 14, fontWeight: 400, marginLeft: 4 }}>{t("metrics.totalTokens")}</span>
        </div>

        {/* Input/Output bar */}
        <div style={{ marginTop: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
            <span style={{ color: darkMode ? "#94a3b8" : "#64748b" }}>
              {t("cost.inputTokens")}: {formatTokens(inputTokens)} ({inputPercent.toFixed(0)}%)
            </span>
            <span style={{ color: darkMode ? "#94a3b8" : "#64748b" }}>
              {t("cost.outputTokens")}: {formatTokens(outputTokens)} ({(100 - inputPercent).toFixed(0)}%)
            </span>
          </div>
          <Progress
            percent={100}
            success={{ percent: inputPercent }}
            showInfo={false}
            strokeColor="#8b5cf6"
            railColor={darkMode ? "#334155" : "#e2e8f0"}
          />
        </div>
      </div>

      {/* Distribution sections - side by side */}
      <div style={{ display: "flex", gap: 16, marginBottom: 16 }}>
        {/* Model breakdown */}
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: darkMode ? "#94a3b8" : "#64748b", marginBottom: 8 }}>
            {t("dashboard.tokenUsage.modelBreakdown")}
          </div>
          {breakdown.slice(0, 3).map((item, index) => (
            <div
              key={item.model || index}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                marginBottom: 5,
              }}
            >
              <div
                style={{
                  width: 6,
                  height: 6,
                  borderRadius: 2,
                  background: modelColors[index % modelColors.length],
                }}
              />
              <span
                style={{
                  flex: 1,
                  fontSize: 11,
                  color: darkMode ? "#e2e8f0" : "#475569",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {item.model || t("common.unknown")}
              </span>
              <span style={{ fontSize: 11, color: darkMode ? "#64748b" : "#94a3b8" }}>
                {item.percentage.toFixed(0)}%
              </span>
            </div>
          ))}
        </div>

        {/* Provider breakdown */}
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: darkMode ? "#94a3b8" : "#64748b", marginBottom: 8 }}>
            {t("dashboard.tokenUsage.providerBreakdown")}
          </div>
          {providerBreakdown.slice(0, 3).map((item, index) => {
            const providerColors = ["#06b6d4", "#f97316", "#22c55e", "#a855f7", "#ec4899"];
            const providerName = item.provider || "unknown";
            const displayName = t(`models.providers.${providerName}`, providerName);
            return (
              <div
                key={providerName}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  marginBottom: 5,
                }}
              >
                <div
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: 2,
                    background: providerColors[index % providerColors.length],
                  }}
                />
                <span
                  style={{
                    flex: 1,
                    fontSize: 11,
                    color: darkMode ? "#e2e8f0" : "#475569",
                  }}
                >
                  {displayName}
                </span>
                <span style={{ fontSize: 11, color: "#10b981", fontWeight: 500 }}>
                  ${(item.cost_usd || 0).toFixed(2)}
                </span>
                <span style={{ fontSize: 11, color: darkMode ? "#64748b" : "#94a3b8" }}>
                  {item.percentage.toFixed(0)}%
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {/* Trend chart */}
      <SafeResponsiveChart height={100} minWidth={100} minHeight={80}>
        <LineChart data={chartData} margin={{ top: 5, right: 5, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={gridColor} strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="date" hide />
          <YAxis hide />
          <Tooltip
            formatter={(value) => [formatTokens(Number(value ?? 0)), t("metrics.totalTokens")]}
            labelFormatter={(label: string) => dayjs(label).format("MM-DD")}
          />
          <Line
            type="monotone"
            dataKey="tokens"
            stroke="#8b5cf6"
            strokeWidth={2}
            dot={false}
          />
        </LineChart>
      </SafeResponsiveChart>
    </PanelWrapper>
  );
}
