// web/src/pages/dashboard/components/KPICards.tsx
// KPI Cards — 1:1 port of design-handoff dashboard.jsx KPI block.
// Uses hand-drawn SVG icons + smooth cardinal sparkline (no recharts).

import { useId } from "react";
import { useQuery } from "@tanstack/react-query";
import { useDashboardContext } from "../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getUsageSummary, getUsageTimeSeries, type UsageTimeSeriesPoint } from "@/api/usage";
import { FONT_FAMILY, LAYOUT, TRANSITION, getColors, getKpiAccents } from "../styles";
import { useTranslation } from "react-i18next";
import dayjs from "dayjs";

// ── Design-handoff SVG icons ────────────────────────────────────────
const ICON = {
  requests: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
      <path d="M6 3h9l-3 6h3l-9 9 3-8H6l0-7z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  ),
  cost: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
      <path d="M9 3v12M12 6H7.5a2 2 0 100 4h3a2 2 0 110 4H6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  ),
  latency: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
      <circle cx="9" cy="9" r="6.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M9 5v4l2.5 2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  success: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
      <circle cx="9" cy="9" r="6.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M6 9.2l2.2 2.2L12.2 7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  tokens: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
      <rect x="2.5" y="5" width="13" height="8" rx="2" stroke="currentColor" strokeWidth="1.5" />
      <path d="M6 8.5h1M9 8.5h1M12 8.5h1M6 11h6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  ),
  trendUp: (
    <svg width="11" height="11" viewBox="0 0 11 11" fill="none">
      <path d="M2 7.5l3-3 2 2 3-4M6 2.5h3v3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  trendDown: (
    <svg width="11" height="11" viewBox="0 0 11 11" fill="none">
      <path d="M2 3.5l3 3 2-2 3 4M6 8.5h3v-3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
};

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

// ── Smooth cardinal spline sparkline (exact design-handoff algorithm)
function smoothPath(points: { x: number; y: number }[]) {
  if (!points.length) return "";
  let d = `M ${points[0].x} ${points[0].y}`;
  for (let i = 0; i < points.length - 1; i++) {
    const p0 = points[i - 1] || points[i];
    const p1 = points[i];
    const p2 = points[i + 1];
    const p3 = points[i + 2] || p2;
    const cp1x = p1.x + (p2.x - p0.x) / 6;
    const cp1y = p1.y + (p2.y - p0.y) / 6;
    const cp2x = p2.x - (p3.x - p1.x) / 6;
    const cp2y = p2.y - (p3.y - p1.y) / 6;
    d += ` C ${cp1x.toFixed(2)} ${cp1y.toFixed(2)}, ${cp2x.toFixed(2)} ${cp2y.toFixed(2)}, ${p2.x} ${p2.y}`;
  }
  return d;
}

function Sparkline({ data, color, w = 220, h = 36 }: { data: number[]; color: string; w?: number; h?: number }) {
  const id = useId();
  if (!data || data.length < 2) return <div style={{ height: h }} />;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const pad = 2;
  const range = max - min || 1;
  const pts = data.map((v, i) => ({
    x: pad + (i * (w - pad * 2)) / (data.length - 1),
    y: h - pad - ((v - min) / range) * (h - pad * 2),
  }));
  const d = smoothPath(pts);
  const areaD = `${d} L ${pts[pts.length - 1].x} ${h} L ${pts[0].x} ${h} Z`;
  return (
    <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ display: "block" }}>
      <defs>
        <linearGradient id={id} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.22" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={areaD} fill={`url(#${id})`} />
      <path d={d} fill="none" stroke={color} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// ── KPI Card ────────────────────────────────────────────────────────
interface KPICardProps {
  id: string;
  label: string;
  value: string | number;
  unit?: string;
  icon: React.ReactNode;
  iconBg: string;
  iconColor: string;
  sparkColor: string;
  delta: number | null;
  isPositiveGood?: boolean;
  isNewData?: boolean;
  sparklineData?: number[];
  onClick?: () => void;
  noData?: boolean;
  ariaLabel?: string;
}

function KPICard({
  id, label, value, unit, icon, iconBg, iconColor, sparkColor,
  delta, isPositiveGood = true, isNewData,
  sparklineData, onClick, noData, ariaLabel,
}: KPICardProps) {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const c = getColors(darkMode);

  const isUp = (delta ?? 0) > 0;
  const isGood = isUp === isPositiveGood;
  const deltaColor = isGood ? c.success : c.danger;
  const deltaLabel = isUp
    ? t("dashboard.trend.up", "增长")
    : t("dashboard.trend.down", "下降");

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
        flex: 1,
        minWidth: 0,
        background: c.cardBg,
        borderRadius: LAYOUT.CARD_RADIUS,
        border: `1px solid ${c.borderSoft}`,
        padding: "16px 18px 14px",
        display: "flex",
        flexDirection: "column",
        gap: 10,
        transition: "transform .15s, box-shadow .15s, border-color .15s",
        cursor: onClick ? "pointer" : "default",
        outline: "none",
        fontFamily: FONT_FAMILY.sans,
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = c.border;
        e.currentTarget.style.boxShadow = c.shadowMd;
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = c.borderSoft;
        e.currentTarget.style.boxShadow = "none";
      }}
    >
      {/* Top row: rounded-square icon + label */}
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{
          width: 32,
          height: 32,
          borderRadius: 9,
          background: iconBg,
          color: iconColor,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          flexShrink: 0,
        }}>
          {icon}
        </div>
        <span
          id={`kpi-${id}`}
          style={{
            fontSize: 12.5,
            color: c.textSecondary,
            fontWeight: 500,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {label}
        </span>
      </div>

      {/* Value */}
      <div style={{ display: "flex", alignItems: "baseline", gap: 5 }}>
        <span style={{
          fontSize: 26,
          fontWeight: 600,
          color: c.textPrimary,
          letterSpacing: "-0.5px",
          fontFeatureSettings: '"tnum"',
          lineHeight: 1.1,
        }}>
          {value}
        </span>
        {unit && (
          <span style={{ fontSize: 13, color: c.textSecondary, fontWeight: 500 }}>
            {unit}
          </span>
        )}
      </div>

      {/* Trend */}
      {noData ? (
        <div style={{ fontSize: 11.5, color: c.textMuted, minHeight: 13 }}>
          {t("dashboard.kpi.noRealData", "暂无数据")}
        </div>
      ) : (
        <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11.5, minHeight: 13 }}>
          {delta === null ? (
            <span style={{ color: c.textMuted, fontWeight: 500 }}>—</span>
          ) : isNewData ? (
            <>
              <span style={{
                display: "inline-flex", alignItems: "center", gap: 4,
                color: c.accent, fontWeight: 600,
              }}>
                <span style={{ width: 5, height: 5, borderRadius: "50%", background: c.accent }} />
                NEW
              </span>
              <span style={{ color: c.textMuted }}>{t("dashboard.trend.noBaseline", "无基线")}</span>
            </>
          ) : (
            <>
              <span style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 3,
                color: deltaColor,
                fontWeight: 600,
                fontFeatureSettings: '"tnum"',
              }}>
                <span style={{ display: "flex" }}>{isUp ? ICON.trendUp : ICON.trendDown}</span>
                {Math.abs(delta).toFixed(1)}%
              </span>
              <span style={{ color: c.textSecondary }}>{deltaLabel}</span>
              <span style={{ color: c.textMuted }}>
                {t("dashboard.trend.vsPrevious", "较上期")}
              </span>
            </>
          )}
        </div>
      )}

      {/* Sparkline */}
      <div style={{ marginTop: "auto", marginLeft: -4, marginRight: -4 }}>
        {noData ? (
          <div style={{ height: 36 }} />
        ) : (
          <Sparkline data={sparklineData ?? []} color={sparkColor} w={220} h={36} />
        )}
      </div>
    </article>
  );
}

// ── KPI Skeleton ────────────────────────────────────────────────────
function KPICardSkeleton({ darkMode }: { darkMode: boolean }) {
  const c = getColors(darkMode);
  return (
    <div style={{
      flex: 1, minWidth: 0,
      background: c.cardBg,
      borderRadius: LAYOUT.CARD_RADIUS,
      border: `1px solid ${c.borderSoft}`,
      padding: "16px 18px 14px",
      minHeight: 154,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <div className="animate-shimmer" style={{ width: 32, height: 32, borderRadius: 9 }} />
        <div className="animate-shimmer" style={{ height: 11, width: 70, borderRadius: 3 }} />
      </div>
      <div className="animate-shimmer" style={{ height: 26, width: "55%", borderRadius: 4, marginBottom: 10 }} />
      <div className="animate-shimmer" style={{ height: 11, width: "60%", borderRadius: 3, marginBottom: 14 }} />
      <div className="animate-shimmer" style={{ height: 36, width: "100%", borderRadius: 4 }} />
    </div>
  );
}

// ── KPI Row Container ───────────────────────────────────────────────
export function KPICards() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const { dateRange, granularity, serviceId, userId, lastRefresh, setTraceFilter } = useDashboardContext();
  const kpi = getKpiAccents(darkMode);

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
  const successSeries = series.map(() => data?.success_rate || 0);

  const cards: KPICardProps[] = [
    {
      id: "requests",
      label: t("metrics.totalRequests"),
      value: hasData ? formatNumber(data?.total_requests || 0) : "—",
      icon: ICON.requests,
      iconBg: kpi.requests.bg,
      iconColor: kpi.requests.fg,
      sparkColor: kpi.requests.fg,
      delta: requestsTrend,
      isNewData,
      noData: !hasData,
      sparklineData: sparkOf("requests"),
      ariaLabel: `${t("metrics.totalRequests")} ${hasData ? data?.total_requests : 0}`,
    },
    {
      id: "cost",
      label: t("dashboard.kpi.totalCostUsd"),
      value: hasData ? formatCurrency(data?.total_cost_usd || 0) : "—",
      icon: ICON.cost,
      iconBg: kpi.cost.bg,
      iconColor: kpi.cost.fg,
      sparkColor: kpi.cost.fg,
      delta: costTrend,
      isPositiveGood: false,
      isNewData,
      noData: !hasData,
      sparklineData: sparkOf("cost_usd"),
      ariaLabel: `${t("dashboard.kpi.totalCostUsd")} ${hasData ? data?.total_cost_usd?.toFixed(2) : 0} USD`,
    },
    {
      id: "latency",
      label: t("metrics.avgLatency"),
      value: hasData ? Math.round(data?.avg_latency_ms || 0).toLocaleString() : "—",
      unit: hasData ? "ms" : undefined,
      icon: ICON.latency,
      iconBg: kpi.latency.bg,
      iconColor: kpi.latency.fg,
      sparkColor: kpi.latency.fg,
      delta: latencyTrend,
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
      label: t("metrics.successRate"),
      value: hasData ? (data?.success_rate || 0).toFixed(1) : "—",
      unit: hasData ? "%" : undefined,
      icon: ICON.success,
      iconBg: kpi.success.bg,
      iconColor: kpi.success.fg,
      sparkColor: kpi.success.fg,
      delta: successRateTrend,
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
      label: t("metrics.totalTokens"),
      value: hasData ? formatNumber(data?.total_tokens || 0) : "—",
      icon: ICON.tokens,
      iconBg: kpi.tokens.bg,
      iconColor: kpi.tokens.fg,
      sparkColor: kpi.tokens.fg,
      delta: tokensTrend,
      isNewData,
      noData: !hasData,
      sparklineData: sparkOf("total_tokens"),
      ariaLabel: `${t("metrics.totalTokens")} ${hasData ? data?.total_tokens : 0}`,
    },
  ];

  return (
    <div className="kpi-row" style={{ display: "flex", gap: 14 }}>
      {isLoading
        ? Array.from({ length: 5 }).map((_, i) => <KPICardSkeleton key={i} darkMode={darkMode} />)
        : cards.map((k, i) => (
            <div key={k.id} className="stagger-item" style={{ flex: 1, minWidth: 0, "--stagger-i": i } as React.CSSProperties}>
              <KPICard {...k} />
            </div>
          ))}
      <style>{`
        @media (max-width: 1080px) {
          .kpi-row { flex-wrap: wrap; }
          .kpi-row > div { flex: 1 1 calc(50% - 7px) !important; min-width: calc(50% - 7px); }
        }
        @media (max-width: 640px) {
          .kpi-row > div { flex: 1 1 100% !important; }
        }
      `}</style>
      {/* swallow unused transitions */}
      <span style={{ display: "none" }}>{TRANSITION.normal}</span>
    </div>
  );
}
