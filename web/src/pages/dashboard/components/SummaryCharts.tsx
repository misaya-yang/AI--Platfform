// web/src/pages/dashboard/components/SummaryCharts.tsx
// Summary tab — 3-up row: request trend · latency trend · cost composition
// All three charts share the dashboard palette (one hue, tonal ramp).

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import dayjs from "dayjs";
import {
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip as ReTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useAppStore } from "@/store/useAppStore";
import { useDashboardContext } from "../DashboardContext";
import {
  getUsageBreakdown,
  getUsageTimeSeries,
  type UsageBreakdownItem,
  type UsageTimeSeriesPoint,
} from "@/api/usage";
import { LAYOUT, TYPOGRAPHY, getColors, getChartPalette } from "../styles";

// ── Panel chrome ───────────────────────────────────────────────────
interface PanelProps {
  title: string;
  unit?: string;
  legend?: React.ReactNode;
  children: React.ReactNode;
  minHeight?: number;
}

function Panel({ title, unit, legend, children, minHeight = 240 }: PanelProps) {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  return (
    <div
      style={{
        background: colors.cardBg,
        border: `1px solid ${colors.border}`,
        borderRadius: LAYOUT.CARD_RADIUS,
        boxShadow: colors.shadowSm,
        padding: LAYOUT.CARD_PADDING,
        minHeight,
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, marginBottom: 12 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 6, minWidth: 0 }}>
          <h3 style={{
            ...TYPOGRAPHY.sectionTitle,
            color: colors.textPrimary,
            margin: 0,
          }}>
            {title}
          </h3>
          {unit && (
            <span style={{ fontSize: 11, color: colors.textMuted, fontWeight: 500 }}>
              {unit}
            </span>
          )}
        </div>
        {legend && (
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {legend}
          </div>
        )}
      </div>
      <div style={{ flex: 1, minHeight: 0 }}>{children}</div>
    </div>
  );
}

function LegendDot({ color, label, dashed }: { color: string; label: string; dashed?: boolean }) {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 5,
      fontSize: 11, color: colors.textSecondary, fontWeight: 500,
    }}>
      <span style={{
        width: 14, height: 2,
        background: dashed ? "transparent" : color,
        borderTop: dashed ? `1.5px dashed ${color}` : "none",
      }} />
      {label}
    </span>
  );
}

// ── Tooltip helper — warm-neutral card with hairline ─────────────────
function useTooltipStyle() {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  return {
    contentStyle: {
      background: colors.cardBg,
      border: `1px solid ${colors.border}`,
      borderRadius: 6,
      boxShadow: darkMode ? "0 4px 18px rgba(0,0,0,0.4)" : "0 4px 18px rgba(30,18,14,0.08)",
      fontSize: 11,
      padding: "8px 10px",
    } as React.CSSProperties,
    labelStyle: {
      color: colors.textMuted,
      fontSize: 10,
      letterSpacing: "0.06em",
      textTransform: "uppercase" as const,
      fontWeight: 600,
      marginBottom: 4,
    },
    itemStyle: {
      color: colors.textPrimary,
      fontSize: 12,
      fontVariantNumeric: "tabular-nums" as const,
    },
  };
}

// ── Request trend line ───────────────────────────────────────────────
function RequestTrend() {
  const { t } = useTranslation();
  const { dateRange, granularity, serviceId, userId, lastRefresh } = useDashboardContext();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const tooltip = useTooltipStyle();

  const { data } = useQuery({
    queryKey: ["dashboard-series-req", dateRange, granularity, serviceId, userId, lastRefresh.getTime()],
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

  const series: UsageTimeSeriesPoint[] = data?.data ?? [];
  const formatted = series.map((p) => ({
    label: dayjs(p.date).format(granularity === "hour" ? "HH:mm" : "MM-DD"),
    value: p.requests,
  }));

  return (
    <Panel
      title={t("dashboard.trend.requestTrend", "请求趋势")}
      unit={`(${t("dashboard.trend.count", "次")})`}
      legend={
        <>
          <LegendDot color={colors.accent} label={t("dashboard.trend.current", "本期")} />
          <LegendDot color={colors.textMuted} label={t("dashboard.trend.previous", "上期")} dashed />
        </>
      }
    >
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={formatted} margin={{ top: 8, right: 8, left: -8, bottom: 0 }}>
          <CartesianGrid stroke={colors.border} strokeDasharray="3 4" vertical={false} />
          <XAxis
            dataKey="label"
            stroke={colors.textMuted}
            tick={{ fill: colors.textMuted, fontSize: 10 }}
            tickLine={false}
            axisLine={{ stroke: colors.border }}
            interval="preserveStartEnd"
          />
          <YAxis
            stroke={colors.textMuted}
            tick={{ fill: colors.textMuted, fontSize: 10 }}
            tickLine={false}
            axisLine={false}
            width={36}
          />
          <ReTooltip
            contentStyle={tooltip.contentStyle}
            labelStyle={tooltip.labelStyle}
            itemStyle={tooltip.itemStyle}
            cursor={{ stroke: colors.borderHover, strokeWidth: 1, strokeDasharray: "2 4" }}
          />
          <Line
            type="monotone"
            dataKey="value"
            name={t("dashboard.trend.current", "本期")}
            stroke={colors.accent}
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 3, fill: colors.accent, stroke: colors.cardBg, strokeWidth: 2 }}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </Panel>
  );
}

// ── Latency trend line ────────────────────────────────────────────────
function LatencyTrend() {
  const { t } = useTranslation();
  const { dateRange, granularity, serviceId, userId, lastRefresh } = useDashboardContext();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const tooltip = useTooltipStyle();

  const { data } = useQuery({
    queryKey: ["dashboard-series-lat", dateRange, granularity, serviceId, userId, lastRefresh.getTime()],
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

  const series: UsageTimeSeriesPoint[] = data?.data ?? [];
  const formatted = series.map((p) => ({
    label: dayjs(p.date).format(granularity === "hour" ? "HH:mm" : "MM-DD"),
    value: Math.round(p.avg_latency_ms || 0),
  }));

  return (
    <Panel
      title={t("dashboard.trend.latencyTrend", "延迟趋势")}
      unit="(ms)"
      legend={
        <>
          <LegendDot color={colors.info} label={t("dashboard.trend.current", "本期")} />
          <LegendDot color={colors.textMuted} label={t("dashboard.trend.previous", "上期")} dashed />
        </>
      }
    >
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={formatted} margin={{ top: 6, right: 6, left: -10, bottom: 0 }}>
          <CartesianGrid stroke={colors.border} strokeDasharray="2 4" vertical={false} />
          <XAxis
            dataKey="label"
            stroke={colors.textMuted}
            tick={{ fill: colors.textMuted, fontSize: 10 }}
            tickLine={false}
            axisLine={{ stroke: colors.border }}
            interval="preserveStartEnd"
          />
          <YAxis
            stroke={colors.textMuted}
            tick={{ fill: colors.textMuted, fontSize: 10 }}
            tickLine={false}
            axisLine={false}
            width={40}
          />
          <ReTooltip
            contentStyle={tooltip.contentStyle}
            labelStyle={tooltip.labelStyle}
            itemStyle={tooltip.itemStyle}
            cursor={{ stroke: colors.borderHover, strokeWidth: 1, strokeDasharray: "2 4" }}
            formatter={(v: unknown) => [`${Number(v).toLocaleString()} ms`, t("metrics.avgLatency")]}
          />
          <Line
            type="monotone"
            dataKey="value"
            name={t("metrics.avgLatency")}
            stroke={colors.info}
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 3, fill: colors.info, stroke: colors.cardBg, strokeWidth: 2 }}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </Panel>
  );
}

// ── Cost composition donut ─────────────────────────────────────────────
function CostComposition() {
  const { t, i18n } = useTranslation();
  const { dateRange, serviceId, userId, lastRefresh } = useDashboardContext();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const palette = getChartPalette(darkMode);
  const tooltip = useTooltipStyle();

  const { data } = useQuery({
    queryKey: ["dashboard-cost-split", dateRange, serviceId, userId, lastRefresh.getTime()],
    queryFn: () =>
      getUsageBreakdown({
        start_date: dateRange[0],
        end_date: dateRange[1],
        dimension: "provider",
        service_id: serviceId !== "all" ? serviceId : undefined,
        user_id: userId !== "all" ? userId : undefined,
      }),
    staleTime: 30000,
  });

  const items: UsageBreakdownItem[] = data?.items ?? [];
  const totalCost = data?.total_cost_usd ?? 0;

  // Merge long tail into "Others"
  const top = items.slice(0, 5);
  const rest = items.slice(5);
  const restSum = rest.reduce((s, i) => s + (i.cost_usd || 0), 0);
  const pieData = useMemo(() => {
    const base = top.map((i) => ({
      name: i.provider || i.label || "—",
      value: i.cost_usd || 0,
      percentage: i.percentage,
    }));
    if (restSum > 0) {
      base.push({
        name: t("dashboard.trend.others", i18n.language.startsWith("zh") ? "其他" : "Others"),
        value: restSum,
        percentage: totalCost > 0 ? (restSum / totalCost) * 100 : 0,
      });
    }
    return base;
  }, [top, restSum, totalCost, t, i18n.language]);

  const formatCost = (v: number) => v >= 1 ? `$${v.toFixed(2)}` : `$${v.toFixed(4)}`;

  return (
    <Panel
      title={t("dashboard.trend.costComposition", "成本构成")}
      unit="(USD)"
    >
      <div style={{ display: "flex", alignItems: "center", gap: 16, height: "100%", minHeight: 200 }}>
        <div style={{ width: 150, height: 150, flexShrink: 0, position: "relative" }}>
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={pieData.length ? pieData : [{ name: "—", value: 1, percentage: 0 }]}
                dataKey="value"
                innerRadius={46}
                outerRadius={66}
                paddingAngle={pieData.length > 1 ? 1.5 : 0}
                stroke={colors.cardBg}
                strokeWidth={2}
                isAnimationActive={false}
              >
                {(pieData.length ? pieData : [{ name: "—", value: 1, percentage: 0 }]).map((_, idx) => (
                  <Cell key={idx} fill={pieData.length ? palette[idx % palette.length] : colors.border} />
                ))}
              </Pie>
              {pieData.length > 0 && (
                <ReTooltip
                  contentStyle={tooltip.contentStyle}
                  labelStyle={tooltip.labelStyle}
                  itemStyle={tooltip.itemStyle}
                  formatter={(v: unknown) => [formatCost(Number(v)), ""]}
                />
              )}
            </PieChart>
          </ResponsiveContainer>
          {/* Center label */}
          <div style={{
            position: "absolute", inset: 0,
            display: "flex", flexDirection: "column",
            alignItems: "center", justifyContent: "center",
            pointerEvents: "none",
          }}>
            <div style={{
              fontSize: 16, fontWeight: 600,
              color: colors.textPrimary,
              letterSpacing: "-0.02em",
              fontVariantNumeric: "tabular-nums",
            }}>
              {formatCost(totalCost)}
            </div>
            <div style={{
              fontSize: 10, color: colors.textMuted,
              marginTop: 2, letterSpacing: "0.02em",
            }}>
              {t("dashboard.trend.totalCost", "总成本")}
            </div>
          </div>
        </div>

        {/* Legend — clean rows matching GPT mockup */}
        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 10 }}>
          {pieData.length === 0 ? (
            <div style={{ color: colors.textMuted, fontSize: 12 }}>
              {t("dashboard.kpi.noRealData", "暂无数据")}
            </div>
          ) : pieData.map((p, idx) => (
            <div key={p.name} style={{
              display: "grid",
              gridTemplateColumns: "auto 1fr auto auto",
              alignItems: "center",
              gap: 8,
              fontSize: 12,
            }}>
              <span style={{
                width: 8, height: 8, borderRadius: "50%",
                background: palette[idx % palette.length],
                flexShrink: 0,
              }} />
              <span style={{
                color: colors.textPrimary,
                fontWeight: 500,
                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
              }}>
                {p.name}
              </span>
              <span style={{
                color: colors.textSecondary,
                fontSize: 11,
                fontVariantNumeric: "tabular-nums",
              }}>
                {p.percentage.toFixed(1)}%
              </span>
              <span style={{
                color: colors.textPrimary,
                fontSize: 12,
                fontVariantNumeric: "tabular-nums",
                fontWeight: 500,
                minWidth: 56,
                textAlign: "right",
              }}>
                {formatCost(p.value)}
              </span>
            </div>
          ))}
        </div>
      </div>
    </Panel>
  );
}

// ── Public: 3-column layout ────────────────────────────────────────
export function SummaryCharts() {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1.1fr)",
        gap: LAYOUT.CARD_GAP,
      }}
      className="summary-charts-grid"
    >
      <RequestTrend />
      <LatencyTrend />
      <CostComposition />
      <style>{`
        @media (max-width: 1080px) {
          .summary-charts-grid {
            grid-template-columns: 1fr !important;
          }
        }
      `}</style>
    </div>
  );
}

export default SummaryCharts;
