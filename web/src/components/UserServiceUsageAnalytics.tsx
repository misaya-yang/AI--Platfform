import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Card, Col, Empty, Row, Select, Spin, Statistic, Tabs } from "antd";
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend } from "recharts";
import { SafeResponsiveChart } from "@/components/SafeResponsiveChart";
import dayjs from "dayjs";
import { SyncOutlined } from "@ant-design/icons";

import { getUsageBreakdown, getUsageSummary, getUsageTimeSeries } from "@/api/usage";
import { useAppStore } from "@/store/useAppStore";

function formatCost(value?: number) {
  if (value == null) return "--";
  if (value >= 1) return `$${value.toFixed(2)}`;
  return `$${value.toFixed(4)}`;
}

function formatLatency(value?: number) {
  if (value == null) return "--";
  return `${Math.round(value)} ms`;
}

export function UserServiceUsageAnalytics({
  dateRange,
  granularity,
}: {
  dateRange: [dayjs.Dayjs, dayjs.Dayjs];
  granularity: "day" | "hour";
}) {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();

  const startDate = dateRange[0].format("YYYY-MM-DD");
  const endDate = dateRange[1].format("YYYY-MM-DD");

  const userBreakdownQuery = useQuery({
    queryKey: ["usage-breakdown-user", startDate, endDate, granularity],
    queryFn: () =>
      getUsageBreakdown({
        dimension: "user",
        start_date: startDate,
        end_date: endDate,
        limit: 20,
      }),
    staleTime: 60000,
  });

  const serviceBreakdownQuery = useQuery({
    queryKey: ["usage-breakdown-service", startDate, endDate, "analytics", granularity],
    queryFn: () =>
      getUsageBreakdown({
        dimension: "service",
        start_date: startDate,
        end_date: endDate,
        limit: 20,
      }),
    staleTime: 60000,
  });

  const providerBreakdownQuery = useQuery({
    queryKey: ["usage-breakdown-provider", startDate, endDate, granularity],
    queryFn: () =>
      getUsageBreakdown({
        dimension: "provider",
        start_date: startDate,
        end_date: endDate,
        limit: 20,
      }),
    staleTime: 60000,
  });

  const userOptions = useMemo(
    () =>
      (userBreakdownQuery.data?.items || []).map((item) => ({
        label: item.user || "unknown",
        value: item.user || "unknown",
      })),
    [userBreakdownQuery.data]
  );

  const serviceOptions = useMemo(
    () =>
      (serviceBreakdownQuery.data?.items || []).map((item) => ({
        label: item.service || "unknown",
        value: item.service || "unknown",
      })),
    [serviceBreakdownQuery.data]
  );

  const providerOptions = useMemo(
    () =>
      (providerBreakdownQuery.data?.items || []).map((item) => ({
        label: item.provider || "unknown",
        value: item.provider || "unknown",
      })),
    [providerBreakdownQuery.data]
  );

  const [selectedUser, setSelectedUser] = useState<string>();
  const [selectedService, setSelectedService] = useState<string>();
  const [selectedProvider, setSelectedProvider] = useState<string>();

  const effectiveSelectedUser = useMemo(() => {
    if (!userOptions.length) return undefined;
    if (selectedUser && userOptions.some((opt) => opt.value === selectedUser)) {
      return selectedUser;
    }
    return userOptions[0].value;
  }, [selectedUser, userOptions]);

  const effectiveSelectedService = useMemo(() => {
    if (!serviceOptions.length) return undefined;
    if (selectedService && serviceOptions.some((opt) => opt.value === selectedService)) {
      return selectedService;
    }
    return serviceOptions[0].value;
  }, [selectedService, serviceOptions]);

  const effectiveSelectedProvider = useMemo(() => {
    if (!providerOptions.length) return undefined;
    if (selectedProvider && providerOptions.some((opt) => opt.value === selectedProvider)) {
      return selectedProvider;
    }
    return providerOptions[0].value;
  }, [selectedProvider, providerOptions]);

  const userSummaryQuery = useQuery({
    queryKey: ["usage-summary-user", startDate, endDate, effectiveSelectedUser, granularity],
    queryFn: () =>
      getUsageSummary({
        start_date: startDate,
        end_date: endDate,
        user_id: effectiveSelectedUser,
      }),
    enabled: !!effectiveSelectedUser,
    staleTime: 60000,
  });

  const serviceSummaryQuery = useQuery({
    queryKey: ["usage-summary-service", startDate, endDate, effectiveSelectedService, granularity],
    queryFn: () =>
      getUsageSummary({
        start_date: startDate,
        end_date: endDate,
        service_id: effectiveSelectedService,
      }),
    enabled: !!effectiveSelectedService,
    staleTime: 60000,
  });

  const userTimeSeriesQuery = useQuery({
    queryKey: ["usage-timeseries-user", startDate, endDate, effectiveSelectedUser, granularity],
    queryFn: () =>
      getUsageTimeSeries({
        start_date: startDate,
        end_date: endDate,
        user_id: effectiveSelectedUser,
        granularity,
      }),
    enabled: !!effectiveSelectedUser,
    staleTime: 60000,
  });

  const serviceTimeSeriesQuery = useQuery({
    queryKey: ["usage-timeseries-service", startDate, endDate, effectiveSelectedService, granularity],
    queryFn: () =>
      getUsageTimeSeries({
        start_date: startDate,
        end_date: endDate,
        service_id: effectiveSelectedService,
        granularity,
      }),
    enabled: !!effectiveSelectedService,
    staleTime: 60000,
  });

  const providerSummaryQuery = useQuery({
    queryKey: ["usage-summary-provider", startDate, endDate, effectiveSelectedProvider, granularity],
    queryFn: () =>
      getUsageSummary({
        start_date: startDate,
        end_date: endDate,
        provider: effectiveSelectedProvider,
      }),
    enabled: !!effectiveSelectedProvider,
    staleTime: 60000,
  });

  const providerTimeSeriesQuery = useQuery({
    queryKey: ["usage-timeseries-provider", startDate, endDate, effectiveSelectedProvider, granularity],
    queryFn: () =>
      getUsageTimeSeries({
        start_date: startDate,
        end_date: endDate,
        provider: effectiveSelectedProvider,
        granularity,
      }),
    enabled: !!effectiveSelectedProvider,
    staleTime: 60000,
  });

  const userChartData = useMemo(
    () =>
      (userTimeSeriesQuery.data?.data || []).map((point) => ({
        date: point.date,
        requests: point.requests,
        cost: point.cost_usd,
      })),
    [userTimeSeriesQuery.data]
  );

  const serviceChartData = useMemo(
    () =>
      (serviceTimeSeriesQuery.data?.data || []).map((point) => ({
        date: point.date,
        requests: point.requests,
        cost: point.cost_usd,
      })),
    [serviceTimeSeriesQuery.data]
  );

  const providerChartData = useMemo(
    () =>
      (providerTimeSeriesQuery.data?.data || []).map((point) => ({
        date: point.date,
        requests: point.requests,
        cost: point.cost_usd,
      })),
    [providerTimeSeriesQuery.data]
  );

  const chartGridColor = darkMode ? "#334155" : "#e2e8f0";

  const renderChart = (data: Array<{ date: string; requests: number; cost: number }>) => {
    if (!data.length) {
      return (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t("dashboard.usageAnalytics.noData", "No usage data")}
        />
      );
    }

    return (
      <SafeResponsiveChart height={280} minWidth={100} minHeight={100}>
        <LineChart data={data} margin={{ top: 10, right: 24, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={chartGridColor} strokeDasharray="3 3" />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 12 }}
            tickFormatter={(value) =>
              dayjs(value).format(granularity === "hour" ? "MM-DD HH:00" : "MM-DD")
            }
          />
          <YAxis
            yAxisId="left"
            tick={{ fontSize: 12 }}
            width={40}
          />
          <YAxis
            yAxisId="right"
            orientation="right"
            tick={{ fontSize: 12 }}
            width={50}
          />
          <Tooltip
            formatter={(value, name) => {
              if (value === undefined) return [0, name ?? ""];
              if (name === "requests") return [value, t("dashboard.usageAnalytics.requests", "Requests")];
              return [formatCost(Number(value)), t("dashboard.usageAnalytics.cost", "Cost")];
            }}
            labelFormatter={(label) =>
              dayjs(label).format(granularity === "hour" ? "YYYY-MM-DD HH:00" : "YYYY-MM-DD")
            }
          />
          <Legend />
          <Line
            type="monotone"
            dataKey="requests"
            yAxisId="left"
            stroke="#3b82f6"
            strokeWidth={2}
            dot={false}
            name={t("dashboard.usageAnalytics.requests", "Requests")}
          />
          <Line
            type="monotone"
            dataKey="cost"
            yAxisId="right"
            stroke="#10b981"
            strokeWidth={2}
            dot={false}
            name={t("dashboard.usageAnalytics.cost", "Cost")}
          />
        </LineChart>
      </SafeResponsiveChart>
    );
  };

  const renderSummary = (summary?: { total_requests: number; total_cost_usd: number; avg_latency_ms: number }) => (
    <Row gutter={[16, 16]}>
      <Col xs={24} md={8}>
        <Statistic
          title={t("dashboard.usageAnalytics.requests", "Requests")}
          value={summary?.total_requests ?? 0}
        />
      </Col>
      <Col xs={24} md={8}>
        <Statistic
          title={t("dashboard.usageAnalytics.cost", "Cost")}
          value={formatCost(summary?.total_cost_usd)}
        />
      </Col>
      <Col xs={24} md={8}>
        <Statistic
          title={t("dashboard.usageAnalytics.avgLatency", "Avg Latency")}
          value={formatLatency(summary?.avg_latency_ms)}
        />
      </Col>
    </Row>
  );

  const [activeTab, setActiveTab] = useState<"user" | "service" | "provider">("user");
  const activeSummary =
    activeTab === "user"
      ? userSummaryQuery.data
      : activeTab === "service"
        ? serviceSummaryQuery.data
        : providerSummaryQuery.data;
  const statusLabel = activeSummary?.data_status
    ? t(`dashboard.dataStatus.${activeSummary.data_status}`, activeSummary.data_status)
    : undefined;
  const freshnessMinutes = activeSummary?.data_freshness_minutes;

  return (
    <Card
      style={{
        borderRadius: 16,
        border: darkMode ? "1px solid #334155" : "1px solid #e2e8f0",
        background: darkMode ? "#1e293b" : "#ffffff",
      }}
      styles={{ body: { padding: 20 } }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ fontSize: 16, fontWeight: 600 }}>
            {t("dashboard.usageAnalytics.title", "Usage Analytics")}
          </div>
          {statusLabel && (
            <span
              style={{
                fontSize: 12,
                padding: "4px 10px",
                borderRadius: 999,
                background: darkMode ? "#0f172a" : "#f1f5f9",
                color: darkMode ? "#e2e8f0" : "#475569",
                border: `1px solid ${darkMode ? "#334155" : "#e2e8f0"}`,
              }}
            >
              {statusLabel}
              {typeof freshnessMinutes === "number" ? ` · ${freshnessMinutes}m` : ""}
            </span>
          )}
        </div>
      </div>

      <Tabs
        activeKey={activeTab}
        onChange={(key) => setActiveTab(key as "user" | "service" | "provider")}
        items={[
          {
            key: "user",
            label: t("dashboard.usageAnalytics.byUser", "By User"),
            children: (
              <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <Select
                    value={effectiveSelectedUser}
                    options={userOptions}
                    placeholder={t("dashboard.usageAnalytics.selectUser", "Select user")}
                    onChange={(value) => setSelectedUser(value)}
                    style={{ minWidth: 220 }}
                    loading={userBreakdownQuery.isLoading}
                    disabled={userOptions.length === 0}
                  />
                  {userBreakdownQuery.isFetching && <SyncOutlined spin />}
                </div>

                {userSummaryQuery.isLoading ? (
                  <Spin />
                ) : (
                  renderSummary(userSummaryQuery.data)
                )}

                {userTimeSeriesQuery.isLoading ? <Spin /> : renderChart(userChartData)}
              </div>
            ),
          },
          {
            key: "service",
            label: t("dashboard.usageAnalytics.byService", "By Service"),
            children: (
              <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <Select
                    value={effectiveSelectedService}
                    options={serviceOptions}
                    placeholder={t("dashboard.usageAnalytics.selectService", "Select service")}
                    onChange={(value) => setSelectedService(value)}
                    style={{ minWidth: 220 }}
                    loading={serviceBreakdownQuery.isLoading}
                    disabled={serviceOptions.length === 0}
                  />
                  {serviceBreakdownQuery.isFetching && <SyncOutlined spin />}
                </div>

                {serviceSummaryQuery.isLoading ? (
                  <Spin />
                ) : (
                  renderSummary(serviceSummaryQuery.data)
                )}

                {serviceTimeSeriesQuery.isLoading ? <Spin /> : renderChart(serviceChartData)}
              </div>
            ),
          },
          {
            key: "provider",
            label: t("dashboard.usageAnalytics.byProvider", "By Vendor"),
            children: (
              <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <Select
                    value={effectiveSelectedProvider}
                    options={providerOptions}
                    placeholder={t("dashboard.usageAnalytics.selectProvider", "Select vendor")}
                    onChange={(value) => setSelectedProvider(value)}
                    style={{ minWidth: 220 }}
                    loading={providerBreakdownQuery.isLoading}
                    disabled={providerOptions.length === 0}
                  />
                  {providerBreakdownQuery.isFetching && <SyncOutlined spin />}
                </div>

                {providerSummaryQuery.isLoading ? (
                  <Spin />
                ) : (
                  renderSummary(providerSummaryQuery.data)
                )}

                {providerTimeSeriesQuery.isLoading ? <Spin /> : renderChart(providerChartData)}
              </div>
            ),
          },
        ]}
      />
    </Card>
  );
}
