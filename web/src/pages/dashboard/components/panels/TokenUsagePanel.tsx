// web/src/pages/dashboard/components/panels/TokenUsagePanel.tsx

import { Progress } from "antd";
import { useQuery } from "@tanstack/react-query";
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from "recharts";
import dayjs from "dayjs";
import { PanelWrapper } from "../PanelWrapper";
import { useDashboardContext } from "../../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getUsageSummary, getUsageBreakdown, getUsageTimeSeries } from "@/api/usage";

function formatTokens(num: number): string {
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`;
  if (num >= 1_000) return `${(num / 1_000).toFixed(1)}K`;
  return num.toLocaleString();
}

export function TokenUsagePanel() {
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
    timeseriesQuery.refetch();
  };

  const summary = summaryQuery.data;
  const breakdown = breakdownQuery.data?.items || [];
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
      title="Token 用量"
      onRefresh={refetch}
      loading={summaryQuery.isLoading}
    >
      {/* Total and breakdown */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 24, fontWeight: 700, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
          {formatTokens(totalTokens)}
          <span style={{ fontSize: 14, fontWeight: 400, marginLeft: 4 }}>tokens</span>
        </div>

        {/* Input/Output bar */}
        <div style={{ marginTop: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
            <span style={{ color: darkMode ? "#94a3b8" : "#64748b" }}>
              输入: {formatTokens(inputTokens)} ({inputPercent.toFixed(0)}%)
            </span>
            <span style={{ color: darkMode ? "#94a3b8" : "#64748b" }}>
              输出: {formatTokens(outputTokens)} ({(100 - inputPercent).toFixed(0)}%)
            </span>
          </div>
          <Progress
            percent={100}
            success={{ percent: inputPercent }}
            showInfo={false}
            strokeColor="#8b5cf6"
            trailColor={darkMode ? "#334155" : "#e2e8f0"}
          />
        </div>
      </div>

      {/* Model breakdown */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: darkMode ? "#94a3b8" : "#64748b", marginBottom: 8 }}>
          模型分布
        </div>
        {breakdown.slice(0, 4).map((item, index) => (
          <div
            key={item.model || index}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              marginBottom: 6,
            }}
          >
            <div
              style={{
                width: 8,
                height: 8,
                borderRadius: 2,
                background: modelColors[index % modelColors.length],
              }}
            />
            <span
              style={{
                flex: 1,
                fontSize: 12,
                color: darkMode ? "#e2e8f0" : "#475569",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {item.model || "Unknown"}
            </span>
            <span style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>
              {formatTokens(item.total_tokens)}
            </span>
            <span style={{ fontSize: 11, color: darkMode ? "#64748b" : "#94a3b8", width: 40 }}>
              {item.percentage.toFixed(0)}%
            </span>
          </div>
        ))}
      </div>

      {/* Trend chart */}
      <div style={{ width: "100%", height: 100 }}>
        <ResponsiveContainer minWidth={100} minHeight={80}>
          <LineChart data={chartData} margin={{ top: 5, right: 5, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={gridColor} strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="date" hide />
            <YAxis hide />
            <Tooltip
              formatter={(value: number) => [formatTokens(value), "Tokens"]}
              labelFormatter={(label) => dayjs(label).format("MM-DD")}
            />
            <Line
              type="monotone"
              dataKey="tokens"
              stroke="#8b5cf6"
              strokeWidth={2}
              dot={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </PanelWrapper>
  );
}
