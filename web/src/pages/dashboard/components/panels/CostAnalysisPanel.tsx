// web/src/pages/dashboard/components/panels/CostAnalysisPanel.tsx

import { Row, Col } from "antd";
import { useQuery } from "@tanstack/react-query";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  PieChart,
  Pie,
  Cell,
} from "recharts";
import { SafeResponsiveChart } from "@/components/SafeResponsiveChart";
import dayjs from "dayjs";
import { PanelWrapper } from "../PanelWrapper";
import { useDashboardContext } from "../../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getUsageSummary, getUsageBreakdown, getUsageTimeSeries } from "@/api/usage";
import { useTranslation } from "react-i18next";

function formatCost(value: number): string {
  if (value >= 1) return `$${value.toFixed(2)}`;
  return `$${value.toFixed(4)}`;
}

export function CostAnalysisPanel() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const { dateRange, granularity, serviceId, userId, lastRefresh } = useDashboardContext();

  // Today's data
  const todayQuery = useQuery({
    queryKey: ["dashboard-cost-today", serviceId, userId, lastRefresh.getTime()],
    queryFn: () =>
      getUsageSummary({
        start_date: dayjs().format("YYYY-MM-DD"),
        end_date: dayjs().format("YYYY-MM-DD"),
        service_id: serviceId !== "all" ? serviceId : undefined,
        user_id: userId !== "all" ? userId : undefined,
      }),
    staleTime: 30000,
  });

  // This week's data
  const weekQuery = useQuery({
    queryKey: ["dashboard-cost-week", serviceId, userId, lastRefresh.getTime()],
    queryFn: () =>
      getUsageSummary({
        start_date: dayjs().startOf("week").format("YYYY-MM-DD"),
        end_date: dayjs().format("YYYY-MM-DD"),
        service_id: serviceId !== "all" ? serviceId : undefined,
        user_id: userId !== "all" ? userId : undefined,
      }),
    staleTime: 30000,
  });

  // This month's data
  const monthQuery = useQuery({
    queryKey: ["dashboard-cost-month", serviceId, userId, lastRefresh.getTime()],
    queryFn: () =>
      getUsageSummary({
        start_date: dayjs().startOf("month").format("YYYY-MM-DD"),
        end_date: dayjs().format("YYYY-MM-DD"),
        service_id: serviceId !== "all" ? serviceId : undefined,
        user_id: userId !== "all" ? userId : undefined,
      }),
    staleTime: 30000,
  });

  // Service breakdown for pie chart
  const breakdownQuery = useQuery({
    queryKey: ["dashboard-cost-breakdown", dateRange, serviceId, userId, lastRefresh.getTime()],
    queryFn: () =>
      getUsageBreakdown({
        dimension: "service",
        start_date: dateRange[0],
        end_date: dateRange[1],
        service_id: serviceId !== "all" ? serviceId : undefined,
        user_id: userId !== "all" ? userId : undefined,
        limit: 5,
      }),
    staleTime: 30000,
  });

  // Time series for trend
  const timeseriesQuery = useQuery({
    queryKey: ["dashboard-cost-timeseries", dateRange, granularity, serviceId, userId, lastRefresh.getTime()],
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
    todayQuery.refetch();
    weekQuery.refetch();
    monthQuery.refetch();
    breakdownQuery.refetch();
    timeseriesQuery.refetch();
  };

  const pieData = (breakdownQuery.data?.items || []).map((item) => ({
    name: item.service || t("common.unknown"),
    value: item.cost_usd || 0,
  }));

  const chartData = (timeseriesQuery.data?.data || []).map((point) => ({
    date: point.date,
    cost: point.cost_usd,
  }));

  const pieColors = ["#3b82f6", "#8b5cf6", "#10b981", "#f59e0b", "#ef4444"];
  const gridColor = darkMode ? "#334155" : "#e2e8f0";

  return (
    <PanelWrapper
      title={t("dashboard.cost.title")}
      onRefresh={refetch}
      loading={todayQuery.isLoading}
    >
      {/* Cost summary cards */}
      <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
        <Col span={8}>
          <div
            style={{
              padding: 12,
              borderRadius: 8,
              background: darkMode ? "#0f172a" : "#f8fafc",
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: 20, fontWeight: 700, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
              {formatCost(todayQuery.data?.total_cost_usd || 0)}
            </div>
            <div style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>{t("dashboard.cost.today")}</div>
          </div>
        </Col>
        <Col span={8}>
          <div
            style={{
              padding: 12,
              borderRadius: 8,
              background: darkMode ? "#0f172a" : "#f8fafc",
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: 20, fontWeight: 700, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
              {formatCost(weekQuery.data?.total_cost_usd || 0)}
            </div>
            <div style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>{t("dashboard.cost.week")}</div>
          </div>
        </Col>
        <Col span={8}>
          <div
            style={{
              padding: 12,
              borderRadius: 8,
              background: darkMode ? "#0f172a" : "#f8fafc",
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: 20, fontWeight: 700, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
              {formatCost(monthQuery.data?.total_cost_usd || 0)}
            </div>
            <div style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>{t("dashboard.cost.month")}</div>
          </div>
        </Col>
      </Row>

      {/* Pie chart and trend */}
      <Row gutter={16}>
        <Col span={10}>
          <div style={{ fontSize: 12, fontWeight: 600, color: darkMode ? "#94a3b8" : "#64748b", marginBottom: 8 }}>
            {t("dashboard.cost.serviceBreakdown")}
          </div>
          <SafeResponsiveChart height={120} minWidth={80} minHeight={80}>
            <PieChart>
              <Pie
                data={pieData}
                cx="50%"
                cy="50%"
                innerRadius={30}
                outerRadius={50}
                dataKey="value"
                label={({ percent }) => `${((percent || 0) * 100).toFixed(0)}%`}
                labelLine={false}
              >
                {pieData.map((_, index) => (
                  <Cell key={index} fill={pieColors[index % pieColors.length]} />
                ))}
              </Pie>
              <Tooltip formatter={(value) => formatCost(Number(value || 0))} />
            </PieChart>
          </SafeResponsiveChart>
          {/* Legend */}
          <div style={{ marginTop: 8 }}>
            {pieData.slice(0, 3).map((item, index) => (
              <div key={index} style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
                <div
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: 2,
                    background: pieColors[index],
                  }}
                />
                <span style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>
                  {item.name}
                </span>
              </div>
            ))}
          </div>
        </Col>
        <Col span={14}>
          <div style={{ fontSize: 12, fontWeight: 600, color: darkMode ? "#94a3b8" : "#64748b", marginBottom: 8 }}>
            {t("dashboard.cost.trend")}
          </div>
          <SafeResponsiveChart height={150} minWidth={100} minHeight={100}>
            <AreaChart data={chartData} margin={{ top: 5, right: 5, left: 0, bottom: 0 }}>
              <CartesianGrid stroke={gridColor} strokeDasharray="3 3" />
              <XAxis
                dataKey="date"
                tick={{ fontSize: 10 }}
                tickFormatter={(v) => dayjs(v).format("MM-DD")}
              />
              <YAxis tick={{ fontSize: 10 }} width={35} />
              <Tooltip
                formatter={(value) => [formatCost(Number(value || 0)), t("analytics.cost")]}
                labelFormatter={(label: string) => dayjs(label).format("YYYY-MM-DD")}
              />
              <Area
                type="monotone"
                dataKey="cost"
                stroke="#10b981"
                fill="rgba(16, 185, 129, 0.2)"
                strokeWidth={2}
              />
            </AreaChart>
          </SafeResponsiveChart>
        </Col>
      </Row>
    </PanelWrapper>
  );
}
