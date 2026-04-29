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
import { useDashboardEntityLabels } from "../../hooks/useDashboardEntityLabels";
import { getChartPalette, getColors } from "../../styles";

function formatCost(value: number): string {
  if (value >= 1) return `$${value.toFixed(2)}`;
  return `$${value.toFixed(4)}`;
}

export function CostAnalysisPanel() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const { dateRange, granularity, serviceId, userId, lastRefresh } = useDashboardContext();
  const { resolveServiceLabel } = useDashboardEntityLabels();

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

  // Provider breakdown for provider pie chart
  const providerBreakdownQuery = useQuery({
    queryKey: ["dashboard-cost-provider-breakdown", dateRange, serviceId, userId, lastRefresh.getTime()],
    queryFn: () =>
      getUsageBreakdown({
        dimension: "provider",
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
    providerBreakdownQuery.refetch();
    timeseriesQuery.refetch();
  };

  const pieData = (breakdownQuery.data?.items || []).map((item) => ({
    name: resolveServiceLabel(item.service || "unattributed_service"),
    value: item.cost_usd || 0,
  }));

  const providerPieData = (providerBreakdownQuery.data?.items || []).map((item) => ({
    name: item.provider || "unattributed_provider",
    value: item.cost_usd || 0,
  }));

  // 检测 unattributed 条目
  const hasUnattributedService = pieData.some((d) => d.name.startsWith("unattributed"));
  const hasUnattributedProvider = providerPieData.some((d) => d.name.startsWith("unattributed"));

  const chartData = (timeseriesQuery.data?.data || []).map((point) => ({
    date: point.date,
    cost: point.cost_usd,
  }));

  const chartPalette = getChartPalette(darkMode);
  const pieColors = [colors.accentDeep, colors.success, colors.gold, colors.info, colors.error];
  const providerPieColors = chartPalette;
  const gridColor = colors.divider;

  return (
    <PanelWrapper
      title={t("dashboard.cost.title")}
      onRefresh={refetch}
      loading={todayQuery.isLoading}
    >
      <div
        style={{
          fontSize: 11,
          color: colors.textMuted,
          marginBottom: 10,
        }}
      >
        {t("dashboard.cost.estimatedHint", "USD 估算成本（按模型定价表换算）")}
      </div>

      {/* Cost summary cards */}
      <Row gutter={[10, 10]} style={{ marginBottom: 12 }}>
        <Col span={8}>
          <div
            style={{
              padding: 10,
              borderRadius: 8,
              background: colors.innerBg,
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: 18, fontWeight: 700, color: colors.textPrimary }}>
              {formatCost(todayQuery.data?.total_cost_usd || 0)}
            </div>
            <div style={{ fontSize: 12, color: colors.textMuted }}>{t("dashboard.cost.today")}</div>
          </div>
        </Col>
        <Col span={8}>
          <div
            style={{
              padding: 10,
              borderRadius: 8,
              background: colors.innerBg,
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: 18, fontWeight: 700, color: colors.textPrimary }}>
              {formatCost(weekQuery.data?.total_cost_usd || 0)}
            </div>
            <div style={{ fontSize: 12, color: colors.textMuted }}>{t("dashboard.cost.week")}</div>
          </div>
        </Col>
        <Col span={8}>
          <div
            style={{
              padding: 10,
              borderRadius: 8,
              background: colors.innerBg,
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: 18, fontWeight: 700, color: colors.textPrimary }}>
              {formatCost(monthQuery.data?.total_cost_usd || 0)}
            </div>
            <div style={{ fontSize: 12, color: colors.textMuted }}>{t("dashboard.cost.month")}</div>
          </div>
        </Col>
      </Row>

      {/* Unattributed 归因警告 */}
      {(hasUnattributedService || hasUnattributedProvider) && (
        <div
          style={{
            padding: "8px 12px",
            marginBottom: 10,
            borderRadius: 6,
            background: colors.warningBg,
            border: `1px solid ${colors.warning}66`,
            fontSize: 12,
            color: colors.warning,
            fontWeight: 500,
          }}
        >
          ⚠ {t("dashboard.cost.unattributedWarning", "部分请求未归因到具体厂商/模型，请检查服务配置")}
        </div>
      )}

      {/* Pie charts and trend */}
      <Row gutter={14}>
        <Col span={5}>
          <div style={{ fontSize: 12, fontWeight: 600, color: colors.textSecondary, marginBottom: 8 }}>
            {t("dashboard.cost.serviceBreakdown")}
          </div>
          <SafeResponsiveChart height={76} minWidth={72} minHeight={72}>
            <PieChart>
              <Pie
                data={pieData}
                cx="50%"
                cy="50%"
                innerRadius={20}
                outerRadius={34}
                dataKey="value"
                labelLine={false}
              >
                {pieData.map((_, index) => (
                  <Cell key={index} fill={pieColors[index % pieColors.length]} />
                ))}
              </Pie>
              <Tooltip formatter={(value) => formatCost(Number(value || 0))} />
            </PieChart>
          </SafeResponsiveChart>
          <div style={{ marginTop: 4 }}>
            {pieData.slice(0, 3).map((item, index) => (
              <div key={index} style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 3 }}>
                <div style={{ width: 6, height: 6, borderRadius: 2, background: pieColors[index], flexShrink: 0 }} />
                <span style={{ fontSize: 10, color: colors.textMuted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {item.name}
                </span>
              </div>
            ))}
          </div>
        </Col>
        <Col span={5}>
          <div style={{ fontSize: 12, fontWeight: 600, color: colors.textSecondary, marginBottom: 8 }}>
            {t("dashboard.cost.providerBreakdown", "按厂商分解")}
          </div>
          <SafeResponsiveChart height={76} minWidth={72} minHeight={72}>
            <PieChart>
              <Pie
                data={providerPieData}
                cx="50%"
                cy="50%"
                innerRadius={20}
                outerRadius={34}
                dataKey="value"
                labelLine={false}
              >
                {providerPieData.map((_, index) => (
                  <Cell key={index} fill={providerPieColors[index % providerPieColors.length]} />
                ))}
              </Pie>
              <Tooltip formatter={(value) => formatCost(Number(value || 0))} />
            </PieChart>
          </SafeResponsiveChart>
          <div style={{ marginTop: 4 }}>
            {providerPieData.slice(0, 3).map((item, index) => (
              <div key={index} style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 3 }}>
                <div style={{ width: 6, height: 6, borderRadius: 2, background: providerPieColors[index], flexShrink: 0 }} />
                <span style={{ fontSize: 10, color: colors.textMuted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {item.name}
                </span>
              </div>
            ))}
          </div>
        </Col>
        <Col span={14}>
          <div style={{ fontSize: 12, fontWeight: 600, color: colors.textSecondary, marginBottom: 8 }}>
            {t("dashboard.cost.trend")}
          </div>
          <SafeResponsiveChart height={112} minWidth={100} minHeight={90}>
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
                stroke={colors.success}
                fill={colors.successBg}
                strokeWidth={2}
              />
            </AreaChart>
          </SafeResponsiveChart>
        </Col>
      </Row>
    </PanelWrapper>
  );
}
