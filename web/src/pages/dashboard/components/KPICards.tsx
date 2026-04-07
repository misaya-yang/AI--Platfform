// web/src/pages/dashboard/components/KPICards.tsx
// KPI Summary Cards — Top accent bar, content-sized, ARIA

import { Spin } from "antd";
import {
  ArrowUp,
  ArrowDown,
  Minus,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useDashboardContext } from "../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getUsageSummary } from "@/api/usage";
import { LAYOUT, getColors, TRANSITION, TYPOGRAPHY } from "../styles";
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

function computeTrend(current: number, previous: number): number | null {
  if (previous === 0 && current === 0) return null;
  if (previous === 0) return null;
  return parseFloat(((current - previous) / previous * 100).toFixed(1));
}

function getPreviousPeriod(dateRange: [string, string]): [string, string] {
  const start = dayjs(dateRange[0]);
  const end = dayjs(dateRange[1]);
  const durationDays = end.diff(start, "day") + 1;
  const prevEnd = start.subtract(1, "day");
  const prevStart = prevEnd.subtract(durationDays - 1, "day");
  return [prevStart.format("YYYY-MM-DD"), prevEnd.format("YYYY-MM-DD")];
}

// ── Trend indicator (triple-encoded: icon + color + text) ───────────
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
      <div style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 13, fontWeight: 600, color: colors.accent, marginTop: 6 }}>
        <span>NEW</span>
        <span style={{ color: colors.textSecondary, fontWeight: 400, marginLeft: 2 }}>
          {t("dashboard.trend.noBaseline")}
        </span>
      </div>
    );
  }

  if (value === null || value === undefined) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 13, fontWeight: 600, color: colors.textSecondary, marginTop: 6 }}>
        <Minus size={14} />
        <span>--</span>
      </div>
    );
  }

  const isUp = value > 0;
  const isGood = isUp === isPositiveGood;
  const color = isGood ? colors.success : colors.error;
  const trendLabel = isUp
    ? t("dashboard.trend.up")
    : t("dashboard.trend.down");

  return (
    <div
      style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 13, fontWeight: 600, color, marginTop: 6 }}
      aria-label={`${trendLabel} ${Math.abs(value)}%`}
    >
      {isUp ? <ArrowUp size={14} /> : <ArrowDown size={14} />}
      <span>{Math.abs(value)}%</span>
      <span style={{ fontSize: 11, opacity: 0.8 }}>{trendLabel}</span>
      <span style={{ color: colors.textSecondary, fontWeight: 400, fontSize: 11, marginLeft: 2 }}>
        {t("dashboard.trend.vsPrevious")}
      </span>
    </div>
  );
}

// ── KPI Card ────────────────────────────────────────────────────────
interface KPICardProps {
  id: string;
  title: string;
  value: string | number;
  accentColor: string;
  suffix?: string;
  loading?: boolean;
  trend?: number | null;
  isPositiveGood?: boolean;
  isNewData?: boolean;
  onClick?: () => void;
  noData?: boolean;
  ariaLabel?: string;
}

function KPICard({
  id,
  title,
  value,
  accentColor,
  suffix,
  loading,
  trend,
  isPositiveGood,
  isNewData,
  onClick,
  noData,
  ariaLabel,
}: KPICardProps) {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);

  return (
    <article
      role="group"
      aria-labelledby={`kpi-${id}`}
      aria-label={ariaLabel}
      tabIndex={onClick ? 0 : undefined}
      onClick={onClick}
      onKeyDown={(e) => {
        if (onClick && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          onClick();
        }
      }}
      style={{
        padding: LAYOUT.CARD_PADDING,
        borderRadius: LAYOUT.CARD_RADIUS,
        background: colors.cardBg,
        border: `1px solid ${colors.border}`,
        boxShadow: colors.shadowSm,
        minHeight: 100,
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
        minWidth: 0,
        transition: TRANSITION.normal,
        position: "relative",
        overflow: "hidden",
        cursor: onClick ? "pointer" : "default",
        outline: "none",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.transform = "translateY(-2px)";
        e.currentTarget.style.borderColor = colors.borderHover;
        e.currentTarget.style.boxShadow = colors.shadowMd;
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.transform = "translateY(0)";
        e.currentTarget.style.borderColor = colors.border;
        e.currentTarget.style.boxShadow = colors.shadowSm;
      }}
      onFocus={(e) => {
        e.currentTarget.style.outline = `2px solid ${colors.accent}`;
        e.currentTarget.style.outlineOffset = "2px";
      }}
      onBlur={(e) => {
        e.currentTarget.style.outline = "none";
      }}
    >
      {/* Top accent bar */}
      <div style={{ position: "absolute", left: 0, right: 0, top: 0, height: 2, background: accentColor, borderRadius: '12px 12px 0 0' }} />

      {loading ? (
        <div style={{ textAlign: "center", padding: 16 }}>
          <Spin size="small" />
        </div>
      ) : (
        <div style={{ paddingLeft: 0 }}>
          {/* Title */}
          <div
            id={`kpi-${id}`}
            style={{
              ...TYPOGRAPHY.cardLabel,
              color: colors.textSecondary,
              marginBottom: 4,
              whiteSpace: "nowrap",
            }}
          >
            {title}
          </div>

          {/* Value */}
          <div style={{ display: "flex", alignItems: "baseline", gap: 4 }}>
            <span
              style={{
                ...TYPOGRAPHY.kpiValue,
                color: colors.textPrimary,
                lineHeight: 1.1,
              }}
            >
              {value}
            </span>
            {suffix && (
              <span style={{ ...TYPOGRAPHY.kpiUnit, color: colors.textSecondary }}>
                {suffix}
              </span>
            )}
          </div>

          {/* Trend */}
          {noData ? (
            <div style={{ fontSize: 13, color: colors.textSecondary, marginTop: 6 }}>
              {t("dashboard.kpi.noRealData")}
            </div>
          ) : (
            <Trend value={trend ?? null} isPositiveGood={isPositiveGood} isNewData={isNewData} />
          )}
        </div>
      )}

    </article>
  );
}

// ── KPI Cards Container ─────────────────────────────────────────────
export function KPICards() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const { dateRange, serviceId, userId, lastRefresh, setTraceFilter } = useDashboardContext();

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

  const requestsTrend = computeTrend(data?.total_requests || 0, prevData?.total_requests || 0);
  const costTrend = computeTrend(data?.total_cost_usd || 0, prevData?.total_cost_usd || 0);
  const latencyTrend = computeTrend(data?.avg_latency_ms || 0, prevData?.avg_latency_ms || 0);
  const successRateTrend = computeTrend(data?.success_rate || 0, prevData?.success_rate || 0);
  const tokensTrend = computeTrend(data?.total_tokens || 0, prevData?.total_tokens || 0);

  const kpiData: KPICardProps[] = [
    {
      id: "requests",
      title: t("metrics.totalRequests"),
      value: hasData ? formatNumber(data?.total_requests || 0) : "--",
      accentColor: colors.accent,
      trend: requestsTrend,
      isNewData,
      noData: !hasData,
      ariaLabel: `${t("metrics.totalRequests")} ${hasData ? data?.total_requests : 0}`,
    },
    {
      id: "cost",
      title: t("dashboard.kpi.totalCostUsd"),
      value: hasData ? formatCurrency(data?.total_cost_usd || 0) : "--",
      accentColor: colors.success,
      trend: costTrend,
      isPositiveGood: false,
      isNewData,
      noData: !hasData,
      ariaLabel: `${t("dashboard.kpi.totalCostUsd")} ${hasData ? data?.total_cost_usd?.toFixed(2) : 0} USD`,
    },
    {
      id: "latency",
      title: t("metrics.avgLatency"),
      value: hasData ? Math.round(data?.avg_latency_ms || 0) : "--",
      suffix: hasData ? "ms" : undefined,
      accentColor: colors.warning,
      trend: latencyTrend,
      isPositiveGood: false,
      isNewData,
      noData: !hasData,
      ariaLabel: `${t("metrics.avgLatency")} ${hasData ? Math.round(data?.avg_latency_ms || 0) : 0} ms`,
      onClick: () => {
        setTraceFilter({ sample_reason: "slow_request" });
        document.getElementById("request-trace-panel")?.scrollIntoView({ behavior: "smooth" });
      },
    },
    {
      id: "success",
      title: t("metrics.successRate"),
      value: hasData ? (data?.success_rate || 0).toFixed(1) : "--",
      suffix: hasData ? "%" : undefined,
      accentColor: data?.success_rate && data.success_rate >= 95 ? colors.success : colors.warning,
      trend: successRateTrend,
      isNewData,
      noData: !hasData,
      ariaLabel: `${t("metrics.successRate")} ${hasData ? (data?.success_rate || 0).toFixed(1) : 0}%`,
      onClick: () => {
        setTraceFilter({ status: "error" });
        document.getElementById("request-trace-panel")?.scrollIntoView({ behavior: "smooth" });
      },
    },
    {
      id: "tokens",
      title: t("metrics.totalTokens"),
      value: hasData ? formatNumber(data?.total_tokens || 0) : "--",
      accentColor: colors.purple,
      trend: tokensTrend,
      isNewData,
      noData: !hasData,
      ariaLabel: `${t("metrics.totalTokens")} ${hasData ? data?.total_tokens : 0}`,
    },
  ];

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: `repeat(${LAYOUT.KPI_COLUMNS}, 1fr)`,
        gap: LAYOUT.CARD_GAP,
      }}
    >
      {kpiData.map((kpi) => (
        <KPICard key={kpi.id} {...kpi} loading={isLoading} />
      ))}
    </div>
  );
}
