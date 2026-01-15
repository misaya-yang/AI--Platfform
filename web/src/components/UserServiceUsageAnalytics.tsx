import { useMemo, useState, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Card, Col, DatePicker, Empty, Row, Select, Spin, Statistic, Tabs } from "antd";
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend } from "recharts";
import dayjs from "dayjs";
import { SyncOutlined } from "@ant-design/icons";

import { getUsageBreakdown, getUsageSummary, getUsageTimeSeries } from "@/api/usage";
import { useAppStore } from "@/store/useAppStore";

const { RangePicker } = DatePicker;

function formatCost(value?: number) {
  if (value == null) return "--";
  if (value >= 1) return `$${value.toFixed(2)}`;
  return `$${value.toFixed(4)}`;
}

function formatLatency(value?: number) {
  if (value == null) return "--";
  return `${Math.round(value)} ms`;
}

export function UserServiceUsageAnalytics() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();

  const [dateRange, setDateRange] = useState<[dayjs.Dayjs, dayjs.Dayjs]>([
    dayjs().subtract(30, "day"),
    dayjs(),
  ]);

  const startDate = dateRange[0].format("YYYY-MM-DD");
  const endDate = dateRange[1].format("YYYY-MM-DD");

  const userBreakdownQuery = useQuery({
    queryKey: ["usage-breakdown-user", startDate, endDate],
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
    queryKey: ["usage-breakdown-service", startDate, endDate, "analytics"],
    queryFn: () =>
      getUsageBreakdown({
        dimension: "service",
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

  const [selectedUser, setSelectedUser] = useState<string>();
  const [selectedService, setSelectedService] = useState<string>();

  useEffect(() => {
    if (!userOptions.length) {
      setSelectedUser(undefined);
      return;
    }
    if (!selectedUser || !userOptions.some((opt) => opt.value === selectedUser)) {
      setSelectedUser(userOptions[0].value);
    }
  }, [userOptions, selectedUser]);

  useEffect(() => {
    if (!serviceOptions.length) {
      setSelectedService(undefined);
      return;
    }
    if (!selectedService || !serviceOptions.some((opt) => opt.value === selectedService)) {
      setSelectedService(serviceOptions[0].value);
    }
  }, [serviceOptions, selectedService]);

  const userSummaryQuery = useQuery({
    queryKey: ["usage-summary-user", startDate, endDate, selectedUser],
    queryFn: () =>
      getUsageSummary({
        start_date: startDate,
        end_date: endDate,
        user_id: selectedUser,
      }),
    enabled: !!selectedUser,
    staleTime: 60000,
  });

  const serviceSummaryQuery = useQuery({
    queryKey: ["usage-summary-service", startDate, endDate, selectedService],
    queryFn: () =>
      getUsageSummary({
        start_date: startDate,
        end_date: endDate,
        service_id: selectedService,
      }),
    enabled: !!selectedService,
    staleTime: 60000,
  });

  const userTimeSeriesQuery = useQuery({
    queryKey: ["usage-timeseries-user", startDate, endDate, selectedUser],
    queryFn: () =>
      getUsageTimeSeries({
        start_date: startDate,
        end_date: endDate,
        user_id: selectedUser,
      }),
    enabled: !!selectedUser,
    staleTime: 60000,
  });

  const serviceTimeSeriesQuery = useQuery({
    queryKey: ["usage-timeseries-service", startDate, endDate, selectedService],
    queryFn: () =>
      getUsageTimeSeries({
        start_date: startDate,
        end_date: endDate,
        service_id: selectedService,
      }),
    enabled: !!selectedService,
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
      <div style={{ width: "100%", height: 280 }}>
        <ResponsiveContainer>
          <LineChart data={data} margin={{ top: 10, right: 24, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={chartGridColor} strokeDasharray="3 3" />
            <XAxis
              dataKey="date"
              tick={{ fontSize: 12 }}
              tickFormatter={(value) => dayjs(value).format("MM-DD")}
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
              formatter={(value: number, name: string) => {
                if (name === "requests") return [value, t("dashboard.usageAnalytics.requests", "Requests")];
                return [formatCost(value), t("dashboard.usageAnalytics.cost", "Cost")];
              }}
              labelFormatter={(label) => dayjs(label).format("YYYY-MM-DD")}
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
        </ResponsiveContainer>
      </div>
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
        <div style={{ fontSize: 16, fontWeight: 600 }}>
          {t("dashboard.usageAnalytics.title", "Usage by User & Service")}
        </div>
        <RangePicker
          value={dateRange}
          onChange={(value) => value && setDateRange(value as [dayjs.Dayjs, dayjs.Dayjs])}
        />
      </div>

      <Tabs
        items={[
          {
            key: "user",
            label: t("dashboard.usageAnalytics.byUser", "By User"),
            children: (
              <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <Select
                    value={selectedUser}
                    options={userOptions}
                    placeholder={t("dashboard.usageAnalytics.selectUser", "Select user")}
                    onChange={(value) => setSelectedUser(value)}
                    style={{ minWidth: 220 }}
                    loading={userBreakdownQuery.isLoading}
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
                    value={selectedService}
                    options={serviceOptions}
                    placeholder={t("dashboard.usageAnalytics.selectService", "Select service")}
                    onChange={(value) => setSelectedService(value)}
                    style={{ minWidth: 220 }}
                    loading={serviceBreakdownQuery.isLoading}
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
        ]}
      />
    </Card>
  );
}
