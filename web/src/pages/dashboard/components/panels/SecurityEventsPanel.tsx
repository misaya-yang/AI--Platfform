// web/src/pages/dashboard/components/panels/SecurityEventsPanel.tsx

import { Row, Col, Select } from "antd";
import { useQuery } from "@tanstack/react-query";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from "recharts";
import { SafeResponsiveChart } from "@/components/SafeResponsiveChart";
import dayjs from "dayjs";
import { useState } from "react";
import { PanelWrapper } from "../PanelWrapper";
import { useDashboardContext } from "../../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { api } from "@/lib/api";
import { useTranslation } from "react-i18next";

interface SecurityBreakdownResponse {
  items: Array<{
    user?: string;
    service?: string;
    event_type: string;
    count: number;
  }>;
  start_date: string;
  end_date: string;
}

interface SecurityTimeseriesResponse {
  data: Array<{
    date: string;
    auth_failed: number;
    rate_limited: number;
  }>;
}

async function getSecurityBreakdown(params: {
  dimension: string;
  event_type?: string;
  start_date?: string;
  end_date?: string;
}): Promise<SecurityBreakdownResponse> {
  const searchParams = new URLSearchParams();
  searchParams.set("dimension", params.dimension);
  if (params.event_type) searchParams.set("event_type", params.event_type);
  if (params.start_date) searchParams.set("start_date", params.start_date);
  if (params.end_date) searchParams.set("end_date", params.end_date);
  const response = await api.get<SecurityBreakdownResponse>(`/api/v1/metrics/security/breakdown?${searchParams}`);
  return response.data;
}

async function getSecurityTimeseries(params: {
  start_date?: string;
  end_date?: string;
  granularity?: string;
}): Promise<SecurityTimeseriesResponse> {
  const searchParams = new URLSearchParams();
  if (params.start_date) searchParams.set("start_date", params.start_date);
  if (params.end_date) searchParams.set("end_date", params.end_date);
  if (params.granularity) searchParams.set("granularity", params.granularity);
  const response = await api.get<SecurityTimeseriesResponse>(`/api/v1/metrics/security/timeseries?${searchParams}`);
  return response.data;
}

export function SecurityEventsPanel() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const { dateRange, granularity, lastRefresh } = useDashboardContext();
  const [timeRange, setTimeRange] = useState<"today" | "week">("today");

  const actualStartDate = timeRange === "today" ? dayjs().format("YYYY-MM-DD") : dateRange[0];
  const actualEndDate = timeRange === "today" ? dayjs().format("YYYY-MM-DD") : dateRange[1];

  const breakdownQuery = useQuery({
    queryKey: ["dashboard-security-breakdown", actualStartDate, actualEndDate, lastRefresh.getTime()],
    queryFn: () =>
      getSecurityBreakdown({
        dimension: "user",
        start_date: actualStartDate,
        end_date: actualEndDate,
      }),
    staleTime: 30000,
  });

  const timeseriesQuery = useQuery({
    queryKey: ["dashboard-security-timeseries", actualStartDate, actualEndDate, granularity, lastRefresh.getTime()],
    queryFn: () =>
      getSecurityTimeseries({
        start_date: actualStartDate,
        end_date: actualEndDate,
        granularity,
      }),
    staleTime: 30000,
  });

  const refetch = () => {
    breakdownQuery.refetch();
    timeseriesQuery.refetch();
  };

  const breakdown = breakdownQuery.data?.items || [];
  const chartData = timeseriesQuery.data?.data || [];

  // Calculate totals
  const authFailures = breakdown.filter((i) => i.event_type === "auth_failed").reduce((sum, i) => sum + i.count, 0);
  const rateLimitHits = breakdown.filter((i) => i.event_type === "rate_limited").reduce((sum, i) => sum + i.count, 0);

  // Top users with events
  const userEvents = breakdown.reduce((acc, item) => {
    const key = item.user || "unknown";
    if (!acc[key]) acc[key] = { user: key, authFailed: 0, rateLimited: 0 };
    if (item.event_type === "auth_failed") acc[key].authFailed += item.count;
    if (item.event_type === "rate_limited") acc[key].rateLimited += item.count;
    return acc;
  }, {} as Record<string, { user: string; authFailed: number; rateLimited: number }>);

  const topUsers = Object.values(userEvents)
    .sort((a, b) => b.authFailed + b.rateLimited - (a.authFailed + a.rateLimited))
    .slice(0, 3);

  const gridColor = darkMode ? "#334155" : "#e2e8f0";

  return (
    <PanelWrapper
      title={t("dashboard.security.title")}
      onRefresh={refetch}
      loading={breakdownQuery.isLoading}
      extra={
        <Select
          value={timeRange}
          onChange={setTimeRange}
          size="small"
          style={{ width: 80 }}
          options={[
            { value: "today", label: t("dashboard.security.range.today") },
            { value: "week", label: t("dashboard.security.range.week") },
          ]}
        />
      }
    >
      {/* Summary stats */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={8}>
          <div
            style={{
              padding: 12,
              borderRadius: 8,
              background: darkMode ? "#0f172a" : "#f8fafc",
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: 24, fontWeight: 700, color: "#ef4444" }}>{authFailures}</div>
            <div style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>{t("dashboard.security.authFailed")}</div>
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
            <div style={{ fontSize: 24, fontWeight: 700, color: "#f59e0b" }}>{rateLimitHits}</div>
            <div style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>{t("dashboard.security.rateLimited")}</div>
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
            <div style={{ fontSize: 24, fontWeight: 700, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
              {authFailures + rateLimitHits}
            </div>
            <div style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>{t("dashboard.security.total")}</div>
          </div>
        </Col>
      </Row>

      {/* Chart */}
      <SafeResponsiveChart height={120} minWidth={100} minHeight={100} style={{ marginBottom: 12 }}>
        <BarChart data={chartData} margin={{ top: 5, right: 5, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={gridColor} strokeDasharray="3 3" />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 10 }}
            tickFormatter={(v) => dayjs(v).format(granularity === "hour" ? "HH:mm" : "MM-DD")}
          />
          <YAxis tick={{ fontSize: 10 }} width={30} />
          <Tooltip
            labelFormatter={(label) => dayjs(label).format("YYYY-MM-DD HH:mm")}
          />
          <Bar dataKey="auth_failed" name={t("dashboard.security.authFailed")} fill="#ef4444" stackId="a" />
          <Bar dataKey="rate_limited" name={t("dashboard.security.rateLimitedShort")} fill="#f59e0b" stackId="a" />
        </BarChart>
      </SafeResponsiveChart>

      {/* Top users */}
      <div>
        <div style={{ fontSize: 12, fontWeight: 600, color: darkMode ? "#94a3b8" : "#64748b", marginBottom: 8 }}>
          {t("dashboard.security.topUsers")}
        </div>
        {topUsers.map((user, index) => (
          <div
            key={user.user}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "6px 0",
              borderBottom: index < topUsers.length - 1 ? `1px solid ${gridColor}` : "none",
            }}
          >
            <span style={{ fontSize: 12, color: darkMode ? "#e2e8f0" : "#475569" }}>
              {index + 1}. {user.user}
            </span>
            <div style={{ display: "flex", gap: 12 }}>
              {user.authFailed > 0 && (
                <span style={{ fontSize: 11, color: "#ef4444" }}>{t("dashboard.security.authFailedCount", { count: user.authFailed })}</span>
              )}
              {user.rateLimited > 0 && (
                <span style={{ fontSize: 11, color: "#f59e0b" }}>{t("dashboard.security.rateLimitedCount", { count: user.rateLimited })}</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </PanelWrapper>
  );
}
