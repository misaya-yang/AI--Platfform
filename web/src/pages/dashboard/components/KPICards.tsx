// web/src/pages/dashboard/components/KPICards.tsx
// KPI Summary Cards — round icon badge · large figure · inline trend · sparkline
// Matches GPT dashboard mockup (per-metric color coding).

import {
  ArrowUp,
  ArrowDown,
  Minus,
  FileText,
  DollarSign,
  Clock,
  CheckCircle2,
  Box,
  type LucideIcon,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { Area, AreaChart, ResponsiveContainer } from "recharts";
import { useDashboardContext } from "../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getUsageSummary, getUsageTimeSeries, type UsageTimeSeriesPoint } from "@/api/usage";
import { LAYOUT, TRANSITION, TYPOGRAPHY, getColors, getKpiAccents } from "../styles";
import { useTranslation } from "react-i18next";
import dayjs from "dayjs";

// ── Formatters ──────────────────────────────────────────────────────
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

// ── Sparkline (area under KPI value) ───────────────────────────────
function Sparkline({
  data, color, gradientId,
}: { data: number[]; color: string; gradientId: string }) {
  if (!data || data.length < 2) return <div style={{ height: 32 }} />;
  const series = data.map((v, i) => ({ i, v }));
  return (
    <div style={{ height: 32, marginTop: 10, marginLeft: -2, marginRight: -2 }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={series} margin={{ top: 3, right: 2, bottom: 0, left: 2 }}>
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.32} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <Area
            type="monotone"
            dataKey="v"
            stroke={color}
            strokeWidth={1.75}
            fill={`url(#${gradientId})`}
            isAnimationActive={false}
            dot={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

// ── Trend chip ─────────────────────────────────────────────────────
function TrendChip({
  value, isPositiveGood = true, isNewData = false,
}: { value: number | null; isPositiveGood?: boolean; isNewData?: boolean }) {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);

  if (isNewData) {
    return (
      <span style={{
        display: "inline-flex", alignItems: "center", gap: 4,
        fontSize: 12, fontWeight: 600, color: colors.accent,
      }}>
        <span style={{
          width: 5, height: 5, borderRadius: "50%",
          background: colors.accent,
        }} />
        <span>NEW</span>
        <span style={{ color: colors.textMuted, fontWeight: 400, marginLeft: 2 }}>
          {t("dashboard.trend.noBaseline", "无基线")}
        </span>
      </span>
    );
  }

  if (value === null || value === undefined) {
    return (
      <span style={{
        display: "inline-flex", alignItems: "center", gap: 4,
        fontSize: 12, fontWeight: 500, color: colors.textMuted,
      }}>
        <Minus size={12} strokeWidth={2} />
        <span>—</span>
      </span>
    );
  }

  const isUp = value > 0;
  const isGood = isUp === isPositiveGood;
  const color = isGood ? colors.success : colors.error;
  const trendLabel = isUp ? t("dashboard.trend.up", "增长") : t("dashboard.trend.down", "下降");

  return (
    <span
      style={{
        display: "inline-flex", alignItems: "center", gap: 3,
        fontSize: 12, fontWeight: 600, color,
        fontVariantNumeric: "tabular-nums",
      }}
      aria-label={`${trendLabel} ${Math.abs(value)}%`}
    >
      {isUp ? <ArrowUp size={12} strokeWidth={2.25} /> : <ArrowDown size={12} strokeWidth={2.25} />}
      <span>{Math.abs(value).toFixed(1)}%</span>
      <span style={{ fontWeight: 500, marginLeft: 1 }}>{trendLabel}</span>
      <span style={{ color: colors.textMuted, fontWeight: 400, marginLeft: 4 }}>
        {t("dashboard.trend.vsPrevious", "较上期")}
      </span>
    </span>
  );
}

// ── Skeleton ────────────────────────────────────────────────────────
function KPICardSkeleton({ darkMode }: { darkMode: boolean }) {
  const colors = getColors(darkMode);
  return (
    <div style={{
      padding: LAYOUT.CARD_PADDING,
      borderRadius: LAYOUT.CARD_RADIUS,
      background: colors.cardBg,
      border: `1px solid ${colors.border}`,
      boxShadow: colors.shadowSm,
      minHeight: 150,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 14 }}>
        <div className="animate-shimmer" style={{ width: 40, height: 40, borderRadius: "50%" }} />
        <div className="animate-shimmer" style={{ height: 12, width: 70, borderRadius: 3 }} />
      </div>
      <div className="animate-shimmer" style={{ height: 28, width: "55%", borderRadius: 4, marginBottom: 10 }} />
      <div className="animate-shimmer" style={{ height: 32, width: "100%", borderRadius: 4 }} />
    </div>
  );
}

// ── KPI Card ────────────────────────────────────────────────────────
interface KPICardProps {
  id: string;
  title: string;
  value: string | number;
  suffix?: string;
  icon: LucideIcon;
  accentFg: string;
  accentBg: string;
  trend?: number | null;
  isPositiveGood?: boolean;
  isNewData?: boolean;
  sparklineData?: number[];
  onClick?: () => void;
  noData?: boolean;
  ariaLabel?: string;
}

function KPICard({
  id, title, value, suffix, icon: Icon,
  accentFg, accentBg,
  trend, isPositiveGood, isNewData,
  sparklineData, onClick, noData, ariaLabel,
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
      className="kpi-card"
      style={{
        padding: LAYOUT.CARD_PADDING,
        borderRadius: LAYOUT.CARD_RADIUS,
        background: colors.cardBg,
        border: `1px solid ${colors.border}`,
        boxShadow: colors.shadowSm,
        minHeight: 150,
        display: "flex",
        flexDirection: "column",
        minWidth: 0,
        transition: TRANSITION.normal,
        position: "relative",
        overflow: "hidden",
        cursor: onClick ? "pointer" : "default",
        outline: "none",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = colors.borderHover;
        e.currentTarget.style.boxShadow = colors.shadowMd;
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = colors.border;
        e.currentTarget.style.boxShadow = colors.shadowSm;
      }}
    >
      {/* Row: round icon + title */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
        <div style={{
          width: 40, height: 40, borderRadius: "50%",
          background: accentBg,
          display: "flex", alignItems: "center", justifyContent: "center",
          color: accentFg,
          flexShrink: 0,
        }}>
          <Icon size={18} strokeWidth={2} />
        </div>
        <span
          id={`kpi-${id}`}
          style={{
            ...TYPOGRAPHY.cardLabel,
            color: colors.textSecondary,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {title}
        </span>
      </div>

      {/* Value */}
      <div style={{ display: "flex", alignItems: "baseline", gap: 4, marginBottom: 2 }}>
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
          <span style={{
            ...TYPOGRAPHY.kpiUnit,
            color: colors.textMuted,
          }}>
            {suffix}
          </span>
        )}
      </div>

      {/* Trend */}
      {noData ? (
        <div style={{ fontSize: 12, color: colors.textMuted, marginTop: 6 }}>
          {t("dashboard.kpi.noRealData", "暂无数据")}
        </div>
      ) : (
        <div style={{ marginTop: 4 }}>
          <TrendChip value={trend ?? null} isPositiveGood={isPositiveGood} isNewData={isNewData} />
        </div>
      )}

      {/* Sparkline — uses this metric's accent */}
      {!noData && (
        <Sparkline
          data={sparklineData ?? []}
          color={accentFg}
          gradientId={`kpi-spark-${id}`}
        />
      )}
    </article>
  );
}

// ── KPI Cards Container ─────────────────────────────────────────────
export function KPICards() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const { dateRange, granularity, serviceId, userId, lastRefresh, setTraceFilter } = useDashboardContext();
  const kpiAccent = getKpiAccents(darkMode);

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

  const { data: timeseries } = useQuery({
    queryKey: ["dashboard-kpi-ts", dateRange, granularity, serviceId, userId, lastRefresh.getTime()],
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

  const series: UsageTimeSeriesPoint[] = timeseries?.data ?? [];
  const sparkOf = (key: keyof UsageTimeSeriesPoint) => series.map((p) => Number(p[key] ?? 0));

  const hasData = (data?.total_requests || 0) > 0;
  const prevHasData = (prevData?.total_requests || 0) > 0;
  const isNewData = hasData && !prevHasData;

  const requestsTrend = computeTrend(data?.total_requests || 0, prevData?.total_requests || 0);
  const costTrend = computeTrend(data?.total_cost_usd || 0, prevData?.total_cost_usd || 0);
  const latencyTrend = computeTrend(data?.avg_latency_ms || 0, prevData?.avg_latency_ms || 0);
  const successRateTrend = computeTrend(data?.success_rate || 0, prevData?.success_rate || 0);
  const tokensTrend = computeTrend(data?.total_tokens || 0, prevData?.total_tokens || 0);

  const successSeries = series.map(() => (data?.success_rate || 0));

  const kpiData: KPICardProps[] = [
    {
      id: "requests",
      title: t("metrics.totalRequests"),
      value: hasData ? formatNumber(data?.total_requests || 0) : "—",
      icon: FileText,
      accentFg: kpiAccent.requests.fg,
      accentBg: kpiAccent.requests.bg,
      trend: requestsTrend,
      isNewData,
      noData: !hasData,
      sparklineData: sparkOf("requests"),
      ariaLabel: `${t("metrics.totalRequests")} ${hasData ? data?.total_requests : 0}`,
    },
    {
      id: "cost",
      title: t("dashboard.kpi.totalCostUsd"),
      value: hasData ? formatCurrency(data?.total_cost_usd || 0) : "—",
      icon: DollarSign,
      accentFg: kpiAccent.cost.fg,
      accentBg: kpiAccent.cost.bg,
      trend: costTrend,
      isPositiveGood: false,
      isNewData,
      noData: !hasData,
      sparklineData: sparkOf("cost_usd"),
      ariaLabel: `${t("dashboard.kpi.totalCostUsd")} ${hasData ? data?.total_cost_usd?.toFixed(2) : 0} USD`,
    },
    {
      id: "latency",
      title: t("metrics.avgLatency"),
      value: hasData ? Math.round(data?.avg_latency_ms || 0).toLocaleString() : "—",
      suffix: hasData ? "ms" : undefined,
      icon: Clock,
      accentFg: kpiAccent.latency.fg,
      accentBg: kpiAccent.latency.bg,
      trend: latencyTrend,
      isPositiveGood: false,
      isNewData,
      noData: !hasData,
      sparklineData: sparkOf("avg_latency_ms"),
      ariaLabel: `${t("metrics.avgLatency")} ${hasData ? Math.round(data?.avg_latency_ms || 0) : 0} ms`,
      onClick: () => {
        setTraceFilter({ sample_reason: "slow_request" });
        document.getElementById("request-trace-panel")?.scrollIntoView({ behavior: "smooth" });
      },
    },
    {
      id: "success",
      title: t("metrics.successRate"),
      value: hasData ? (data?.success_rate || 0).toFixed(1) : "—",
      suffix: hasData ? "%" : undefined,
      icon: CheckCircle2,
      accentFg: kpiAccent.success.fg,
      accentBg: kpiAccent.success.bg,
      trend: successRateTrend,
      isNewData,
      noData: !hasData,
      sparklineData: successSeries,
      ariaLabel: `${t("metrics.successRate")} ${hasData ? (data?.success_rate || 0).toFixed(1) : 0}%`,
      onClick: () => {
        setTraceFilter({ status: "error" });
        document.getElementById("request-trace-panel")?.scrollIntoView({ behavior: "smooth" });
      },
    },
    {
      id: "tokens",
      title: t("metrics.totalTokens"),
      value: hasData ? formatNumber(data?.total_tokens || 0) : "—",
      icon: Box,
      accentFg: kpiAccent.tokens.fg,
      accentBg: kpiAccent.tokens.bg,
      trend: tokensTrend,
      isNewData,
      noData: !hasData,
      sparklineData: sparkOf("total_tokens"),
      ariaLabel: `${t("metrics.totalTokens")} ${hasData ? data?.total_tokens : 0}`,
    },
  ];

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: `repeat(auto-fit, minmax(200px, 1fr))`,
        gap: LAYOUT.CARD_GAP,
      }}
    >
      {isLoading
        ? Array.from({ length: 5 }).map((_, i) => <KPICardSkeleton key={i} darkMode={darkMode} />)
        : kpiData.map((kpi, i) => (
            <div key={kpi.id} className="stagger-item" style={{ "--stagger-i": i } as React.CSSProperties}>
              <KPICard {...kpi} />
            </div>
          ))}
    </div>
  );
}
