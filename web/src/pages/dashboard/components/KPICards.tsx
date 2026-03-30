// web/src/pages/dashboard/components/KPICards.tsx
// KPI Summary Cards - Unified Layout System

import { Spin } from "antd";
import {
  ArrowUp,
  ArrowDown,
  Minus,
  Activity,
  DollarSign,
  Zap,
  CheckCircle,
  Database,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useDashboardContext } from "../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getUsageSummary } from "@/api/usage";
import { LAYOUT, getColors, gridStyles, TRANSITION } from "../styles";
import { useTranslation } from "react-i18next";
import dayjs from "dayjs";

function formatNumber(num: number): string {
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`;
  if (num >= 1_000) return `${(num / 1_000).toFixed(1)}K`;
  return num.toLocaleString();
}

function formatCurrency(value: number): string {
  if (value >= 1) return `$${value.toFixed(2)}`;
  return `$${value.toFixed(4)}`;
}

/** 计算环比变化率: (current - previous) / previous * 100 */
function computeTrend(current: number, previous: number): number | null {
  if (previous === 0 && current === 0) return null;
  if (previous === 0) return null; // NEW data, no baseline
  return parseFloat(((current - previous) / previous * 100).toFixed(1));
}

/** 根据当前日期范围计算等长的"上一周期"范围 */
function getPreviousPeriod(dateRange: [string, string]): [string, string] {
  const start = dayjs(dateRange[0]);
  const end = dayjs(dateRange[1]);
  const durationDays = end.diff(start, "day") + 1;
  const prevEnd = start.subtract(1, "day");
  const prevStart = prevEnd.subtract(durationDays - 1, "day");
  return [prevStart.format("YYYY-MM-DD"), prevEnd.format("YYYY-MM-DD")];
}

interface TrendProps {
  value: number | null;
  isPositiveGood?: boolean;
  isNewData?: boolean;
}

function Trend({ value, isPositiveGood = true, isNewData = false }: TrendProps) {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);

  if (isNewData) {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 4,
          fontSize: 11,
          fontWeight: 600,
          color: colors.accent,
          marginTop: 4,
        }}
      >
        <span>NEW</span>
        <span style={{ color: colors.textSecondary, fontWeight: 400, marginLeft: 2 }}>{t("dashboard.trend.noBaseline", "无基线对比")}</span>
      </div>
    );
  }

  if (value === null || value === undefined) {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 4,
          fontSize: 11,
          fontWeight: 600,
          color: colors.textSecondary,
          marginTop: 4,
        }}
      >
        <Minus size={12} />
        <span>--</span>
      </div>
    );
  }

  const isUp = value > 0;
  const isGood = isUp === isPositiveGood;
  const color = isGood ? colors.success : colors.error;

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 4,
        fontSize: 11,
        fontWeight: 600,
        color: color,
        marginTop: 4,
      }}
    >
      {isUp ? <ArrowUp size={12} /> : <ArrowDown size={12} />}
      <span>{Math.abs(value)}%</span>
      <span style={{ color: colors.textSecondary, fontWeight: 400, marginLeft: 2 }}>{t("dashboard.trend.vsPrevious")}</span>
    </div>
  );
}

interface KPICardProps {
  title: string;
  value: string | number;
  icon: React.ReactNode;
  iconColor: string;
  iconGradient: string;
  suffix?: string;
  loading?: boolean;
  trend?: number | null;
  isPositiveGood?: boolean;
  isNewData?: boolean;
  onClick?: () => void;
  noData?: boolean;
}

function KPICard({ 
  title, 
  value, 
  icon, 
  iconColor, 
  iconGradient,
  suffix, 
  loading,
  trend,
  isPositiveGood,
  isNewData,
  onClick,
  noData,
}: KPICardProps) {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);

  return (
    <div
      style={{
        padding: LAYOUT.CARD_PADDING,
        borderRadius: LAYOUT.CARD_RADIUS,
        background: colors.cardBg,
        border: `1px solid ${colors.border}`,
        boxShadow: colors.shadowSm,
        height: "100%",
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
        minWidth: 0,
        transition: TRANSITION.slow,
        position: "relative",
        overflow: "hidden",
        cursor: onClick ? "pointer" : "default",
      }}
      onClick={onClick}
      onMouseEnter={(e) => {
        e.currentTarget.style.transform = "translateY(-4px)";
        e.currentTarget.style.boxShadow = colors.shadowLg;
        e.currentTarget.style.borderColor = `${iconColor}40`;
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.transform = "translateY(0)";
        e.currentTarget.style.boxShadow = colors.shadowSm;
        e.currentTarget.style.borderColor = colors.border;
      }}
    >
      {/* Decorative gradient blur */}
      <div 
        style={{
          position: "absolute",
          top: -20,
          right: -20,
          width: 80,
          height: 80,
          background: iconGradient,
          opacity: 0.05,
          filter: "blur(20px)",
          borderRadius: "50%",
          pointerEvents: "none",
        }}
      />

      {loading ? (
        <div style={{ textAlign: "center", padding: 16 }}>
          <Spin size="small" />
        </div>
      ) : (
        <div style={{ display: "flex", alignItems: "flex-start", gap: 16 }}>
          {/* Icon */}
          <div
            style={{
              width: 44,
              height: 44,
              borderRadius: 12,
              background: iconGradient,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 20,
              color: "#ffffff",
              flexShrink: 0,
              boxShadow: `0 4px 10px ${iconColor}30`,
            }}
          >
            {icon}
          </div>

          {/* Content */}
          <div style={{ flex: 1, minWidth: 0 }}>
            <div
              style={{
                fontSize: 12,
                fontWeight: 500,
                color: colors.textSecondary,
                marginBottom: 2,
                whiteSpace: "nowrap",
                textTransform: "uppercase",
                letterSpacing: "0.025em",
              }}
            >
              {title}
            </div>
            <div
              style={{
                display: "flex",
                alignItems: "baseline",
                gap: 2,
              }}
            >
              <span
                style={{
                  fontSize: 26,
                  fontWeight: 700,
                  color: colors.textPrimary,
                  lineHeight: 1.1,
                  letterSpacing: "-0.02em",
                }}
              >
                {value}
              </span>
              {suffix && (
                <span
                  style={{
                    fontSize: 14,
                    fontWeight: 600,
                    color: colors.textSecondary,
                  }}
                >
                  {suffix}
                </span>
              )}
            </div>
            {noData ? (
              <div style={{ fontSize: 11, color: colors.textSecondary, marginTop: 4 }}>
                {t("dashboard.kpi.noRealData")}
              </div>
            ) : (
              <Trend value={trend ?? null} isPositiveGood={isPositiveGood} isNewData={isNewData} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export function KPICards() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const { dateRange, serviceId, userId, lastRefresh, setTraceFilter } = useDashboardContext();

  // 当前周期数据
  const { data, isLoading } = useQuery({
    queryKey: ["dashboard-kpi", dateRange, serviceId, userId, lastRefresh.getTime()],
    queryFn: () =>
      getUsageSummary({
        start_date: dateRange[0],
        end_date: dateRange[1],
        service_id: serviceId !== "all" ? serviceId : undefined,
        user_id: userId !== "all" ? userId : undefined,
      }),
    staleTime: 30000,
  });

  // 上一周期数据（用于真实环比计算）
  const [prevStart, prevEnd] = getPreviousPeriod(dateRange);
  const { data: prevData } = useQuery({
    queryKey: ["dashboard-kpi-prev", prevStart, prevEnd, serviceId, userId, lastRefresh.getTime()],
    queryFn: () =>
      getUsageSummary({
        start_date: prevStart,
        end_date: prevEnd,
        service_id: serviceId !== "all" ? serviceId : undefined,
        user_id: userId !== "all" ? userId : undefined,
      }),
    staleTime: 60000,
  });

  const hasData = (data?.total_requests || 0) > 0;
  const prevHasData = (prevData?.total_requests || 0) > 0;
  const isNewData = hasData && !prevHasData;

  // 真实环比计算
  const requestsTrend = computeTrend(data?.total_requests || 0, prevData?.total_requests || 0);
  const costTrend = computeTrend(data?.total_cost_usd || 0, prevData?.total_cost_usd || 0);
  const latencyTrend = computeTrend(data?.avg_latency_ms || 0, prevData?.avg_latency_ms || 0);
  const successRateTrend = computeTrend(data?.success_rate || 0, prevData?.success_rate || 0);
  const tokensTrend = computeTrend(data?.total_tokens || 0, prevData?.total_tokens || 0);

  const kpiData = [
    {
      title: t("metrics.totalRequests"),
      value: hasData ? formatNumber(data?.total_requests || 0) : "--",
      icon: <Activity size={20} />,
      iconColor: colors.accent,
      iconGradient: colors.accentGradient,
      trend: requestsTrend,
      isNewData,
      noData: !hasData,
    },
    {
      title: t("dashboard.kpi.totalCostUsd", "总成本 (USD)"),
      value: hasData ? formatCurrency(data?.total_cost_usd || 0) : "--",
      icon: <DollarSign size={20} />,
      iconColor: colors.success,
      iconGradient: colors.successGradient,
      trend: costTrend,
      isPositiveGood: false,
      isNewData,
      noData: !hasData,
    },
    {
      title: t("metrics.avgLatency"),
      value: hasData ? Math.round(data?.avg_latency_ms || 0) : "--",
      suffix: hasData ? "ms" : undefined,
      icon: <Zap size={20} />,
      iconColor: colors.warning,
      iconGradient: colors.warningGradient,
      trend: latencyTrend,
      isPositiveGood: false,
      isNewData,
      noData: !hasData,
      onClick: () => {
        setTraceFilter({ sample_reason: "slow_request" });
        document.getElementById("request-trace-panel")?.scrollIntoView({ behavior: "smooth" });
      },
    },
    {
      title: t("metrics.successRate"),
      value: hasData ? (data?.success_rate || 0).toFixed(1) : "--",
      suffix: hasData ? "%" : undefined,
      icon: <CheckCircle size={20} />,
      iconColor: data?.success_rate && data.success_rate >= 95 ? colors.success : colors.warning,
      iconGradient: data?.success_rate && data.success_rate >= 95 ? colors.successGradient : colors.warningGradient,
      trend: successRateTrend,
      isNewData,
      noData: !hasData,
      onClick: () => {
        setTraceFilter({ status: "error" });
        document.getElementById("request-trace-panel")?.scrollIntoView({ behavior: "smooth" });
      },
    },
    {
      title: t("metrics.totalTokens"),
      value: hasData ? formatNumber(data?.total_tokens || 0) : "--",
      icon: <Database size={20} />,
      iconColor: colors.purple,
      iconGradient: colors.purpleGradient,
      trend: tokensTrend,
      isNewData,
      noData: !hasData,
    },
  ];

  return (
    <div
      style={{
        ...gridStyles.fiveColumnResponsive,
      }}
    >
      {kpiData.map((kpi, index) => (
        <KPICard key={index} {...kpi} loading={isLoading} />
      ))}
    </div>
  );
}
