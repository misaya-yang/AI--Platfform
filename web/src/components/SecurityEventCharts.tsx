import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Card, DatePicker, Empty, Spin, Tabs } from "antd";
import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend } from "recharts";
import dayjs from "dayjs";
import { SyncOutlined } from "@ant-design/icons";

import { getSecurityEventBreakdown } from "@/api/metrics";
import { useAppStore } from "@/store/useAppStore";

const { RangePicker } = DatePicker;

type BreakdownDatum = {
  name: string;
  auth_failed: number;
  rate_limited: number;
  total: number;
};

export function SecurityEventCharts() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const [dateRange, setDateRange] = useState<[dayjs.Dayjs, dayjs.Dayjs]>([
    dayjs().subtract(30, "day"),
    dayjs(),
  ]);

  const startDate = dateRange[0].format("YYYY-MM-DD");
  const endDate = dateRange[1].format("YYYY-MM-DD");

  const userAuthQuery = useQuery({
    queryKey: ["security-breakdown", "user", "auth_failed", startDate, endDate],
    queryFn: () =>
      getSecurityEventBreakdown({
        dimension: "user",
        event_type: "auth_failed",
        start_date: startDate,
        end_date: endDate,
        limit: 10,
      }),
    staleTime: 60000,
  });

  const userRateQuery = useQuery({
    queryKey: ["security-breakdown", "user", "rate_limited", startDate, endDate],
    queryFn: () =>
      getSecurityEventBreakdown({
        dimension: "user",
        event_type: "rate_limited",
        start_date: startDate,
        end_date: endDate,
        limit: 10,
      }),
    staleTime: 60000,
  });

  const serviceAuthQuery = useQuery({
    queryKey: ["security-breakdown", "service", "auth_failed", startDate, endDate],
    queryFn: () =>
      getSecurityEventBreakdown({
        dimension: "service",
        event_type: "auth_failed",
        start_date: startDate,
        end_date: endDate,
        limit: 10,
      }),
    staleTime: 60000,
  });

  const serviceRateQuery = useQuery({
    queryKey: ["security-breakdown", "service", "rate_limited", startDate, endDate],
    queryFn: () =>
      getSecurityEventBreakdown({
        dimension: "service",
        event_type: "rate_limited",
        start_date: startDate,
        end_date: endDate,
        limit: 10,
      }),
    staleTime: 60000,
  });

  const mergeData = (authItems: Array<{ name: string; count: number }>, rateItems: Array<{ name: string; count: number }>) => {
    const map = new Map<string, BreakdownDatum>();
    authItems.forEach((item) => {
      map.set(item.name, {
        name: item.name,
        auth_failed: item.count,
        rate_limited: 0,
        total: item.count,
      });
    });
    rateItems.forEach((item) => {
      const existing = map.get(item.name);
      if (existing) {
        existing.rate_limited = item.count;
        existing.total = existing.auth_failed + existing.rate_limited;
      } else {
        map.set(item.name, {
          name: item.name,
          auth_failed: 0,
          rate_limited: item.count,
          total: item.count,
        });
      }
    });
    return Array.from(map.values()).sort((a, b) => b.total - a.total);
  };

  const userData = useMemo(
    () =>
      mergeData(
        userAuthQuery.data?.items || [],
        userRateQuery.data?.items || []
      ),
    [userAuthQuery.data, userRateQuery.data]
  );

  const serviceData = useMemo(
    () =>
      mergeData(
        serviceAuthQuery.data?.items || [],
        serviceRateQuery.data?.items || []
      ),
    [serviceAuthQuery.data, serviceRateQuery.data]
  );

  const chartGridColor = darkMode ? "#334155" : "#e2e8f0";

  const renderBarChart = (data: BreakdownDatum[], loading: boolean) => {
    if (loading) return <Spin />;
    if (!data.length) {
      return (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t("dashboard.securityEvents.noData", "No event data")}
        />
      );
    }

    const height = Math.max(240, data.length * 34);
    return (
      <div style={{ width: "100%", height }}>
        <ResponsiveContainer>
          <BarChart
            data={data}
            layout="vertical"
            margin={{ top: 10, right: 24, left: 40, bottom: 0 }}
          >
            <CartesianGrid stroke={chartGridColor} strokeDasharray="3 3" />
            <XAxis type="number" tick={{ fontSize: 12 }} />
            <YAxis
              dataKey="name"
              type="category"
              tick={{ fontSize: 12 }}
              width={120}
            />
            <Tooltip />
            <Legend />
            <Bar
              dataKey="auth_failed"
              fill="#ef4444"
              name={t("dashboard.securityEvents.authFailed", "Auth Failed")}
              radius={[4, 4, 4, 4]}
            />
            <Bar
              dataKey="rate_limited"
              fill="#f59e0b"
              name={t("dashboard.securityEvents.rateLimited", "Rate Limited")}
              radius={[4, 4, 4, 4]}
            />
          </BarChart>
        </ResponsiveContainer>
      </div>
    );
  };

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
          {t("dashboard.securityEvents.title", "Auth & Rate Limit Events")}
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
            label: t("dashboard.securityEvents.byUser", "By User"),
            children: (
              <div>
                {(userAuthQuery.isFetching || userRateQuery.isFetching) && (
                  <div style={{ marginBottom: 8 }}>
                    <SyncOutlined spin />
                  </div>
                )}
                {renderBarChart(
                  userData,
                  userAuthQuery.isLoading || userRateQuery.isLoading
                )}
              </div>
            ),
          },
          {
            key: "service",
            label: t("dashboard.securityEvents.byService", "By Service"),
            children: (
              <div>
                {(serviceAuthQuery.isFetching || serviceRateQuery.isFetching) && (
                  <div style={{ marginBottom: 8 }}>
                    <SyncOutlined spin />
                  </div>
                )}
                {renderBarChart(
                  serviceData,
                  serviceAuthQuery.isLoading || serviceRateQuery.isLoading
                )}
              </div>
            ),
          },
        ]}
      />
    </Card>
  );
}
