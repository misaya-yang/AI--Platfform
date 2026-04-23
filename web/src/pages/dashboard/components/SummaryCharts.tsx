// web/src/pages/dashboard/components/SummaryCharts.tsx
// 1:1 port of design-handoff dashboard.jsx charts row.
// RequestTrend (indigo, current + prev dashed) · LatencyTrend (cyan) · CostBreakdown donut.
// Pure SVG — smooth cardinal spline, no recharts.

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import dayjs from "dayjs";
import { useAppStore } from "@/store/useAppStore";
import { useDashboardContext } from "../DashboardContext";
import {
  getUsageBreakdown,
  getUsageTimeSeries,
  type UsageBreakdownItem,
  type UsageTimeSeriesPoint,
} from "@/api/usage";
import { FONT_FAMILY, LAYOUT, getColors, getChartPalette } from "../styles";

// ── Shared helpers ─────────────────────────────────────────────────
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

// ── Card chrome ────────────────────────────────────────────────────
interface CardProps {
  title: string;
  unit?: string;
  right?: React.ReactNode;
  children: React.ReactNode;
  minW?: number;
  minHeight?: number;
}

function Card({ title, unit, right, children, minW = 0, minHeight }: CardProps) {
  const { darkMode } = useAppStore();
  const c = getColors(darkMode);
  return (
    <div style={{
      flex: 1,
      minWidth: minW,
      minHeight,
      background: c.cardBg,
      borderRadius: LAYOUT.CARD_RADIUS,
      border: `1px solid ${c.borderSoft}`,
      padding: "16px 18px",
      display: "flex",
      flexDirection: "column",
      fontFamily: FONT_FAMILY.sans,
    }}>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 8 }}>
        <span style={{ fontSize: 13.5, fontWeight: 600, color: c.textPrimary }}>{title}</span>
        {unit && <span style={{ fontSize: 11, color: c.textMuted, marginLeft: 6 }}>{unit}</span>}
        <div style={{ flex: 1 }} />
        {right}
      </div>
      {children}
    </div>
  );
}

function LegendDots({ items }: { items: { label: string; color: string; dashed?: boolean }[] }) {
  const { darkMode } = useAppStore();
  const c = getColors(darkMode);
  return (
    <div style={{ display: "flex", gap: 14, fontSize: 11, color: c.textSecondary }}>
      {items.map((it, i) => (
        <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <span
            style={{
              display: "inline-block",
              width: 14,
              height: it.dashed ? 0 : 2,
              borderRadius: 1,
              background: it.dashed ? "transparent" : it.color,
              borderTop: it.dashed ? `1.5px dashed ${it.color}` : "none",
              opacity: it.dashed ? 0.7 : 1,
            }}
          />
          <span>{it.label}</span>
        </span>
      ))}
    </div>
  );
}

// ── Line chart (responsive via preserveAspectRatio="none") ────────
interface Series {
  data: number[];
  color: string;
  dashed?: boolean;
}

function LineChart({
  series,
  labels,
  yTicks,
  yFormat,
  w = 400,
  h = 180,
}: {
  series: Series[];
  labels: string[];
  yTicks?: number[];
  yFormat?: (v: number) => string;
  w?: number;
  h?: number;
}) {
  const { darkMode } = useAppStore();
  const c = getColors(darkMode);
  const padL = 36, padR = 12, padT = 14, padB = 26;
  const innerW = w - padL - padR;
  const innerH = h - padT - padB;
  const allVals = series.flatMap((s) => s.data);
  const dataMax = Math.max(0, ...allVals);
  const baseTicks = yTicks ?? [0, Math.round(dataMax * 0.25), Math.round(dataMax * 0.5), Math.round(dataMax * 0.75), Math.round(dataMax)];
  const ticks = baseTicks.map((n) => Math.max(0, n));
  const yMax = Math.max(...ticks, 1);

  const toXY = (v: number, i: number, len: number) => ({
    x: padL + (i * innerW) / Math.max(len - 1, 1),
    y: padT + innerH - ((v - 0) / (yMax - 0)) * innerH,
  });

  const hasData = labels.length > 0 && series.some((s) => s.data.some((v) => v !== 0));

  return (
    <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ display: "block" }}>
      {/* Grid */}
      {ticks.map((tick, i) => {
        const y = padT + innerH - ((tick - 0) / (yMax - 0)) * innerH;
        return (
          <g key={i}>
            <line
              x1={padL}
              x2={w - padR}
              y1={y}
              y2={y}
              stroke={c.divider}
              strokeWidth="1"
              strokeDasharray={i === 0 ? "0" : "3 3"}
            />
            <text
              x={padL - 6}
              y={y + 3}
              fontSize="9.5"
              fill={c.textMuted}
              textAnchor="end"
              fontFamily={FONT_FAMILY.sans}
            >
              {yFormat ? yFormat(tick) : tick}
            </text>
          </g>
        );
      })}
      {/* X labels */}
      {labels.map((l, i) => {
        const x = padL + (i * innerW) / Math.max(labels.length - 1, 1);
        return (
          <text key={i} x={x} y={h - 8} fontSize="9.5" fill={c.textMuted} textAnchor="middle" fontFamily={FONT_FAMILY.sans}>
            {l}
          </text>
        );
      })}
      {/* Series */}
      {hasData && series.map((s, idx) => {
        if (s.data.length === 0) return null;
        const pts = s.data.map((v, i) => toXY(v, i, s.data.length));
        const d = smoothPath(pts);
        return (
          <g key={idx}>
            <path
              d={d}
              fill="none"
              stroke={s.color}
              strokeWidth="1.8"
              strokeDasharray={s.dashed ? "4 3" : "0"}
              strokeLinecap="round"
              strokeLinejoin="round"
              opacity={s.dashed ? 0.55 : 1}
            />
            {!s.dashed && pts.map((p, i) => (
              <circle key={i} cx={p.x} cy={p.y} r="2.4" fill={c.cardBg} stroke={s.color} strokeWidth="1.6" />
            ))}
          </g>
        );
      })}
      {!hasData && (
        <text x={w / 2} y={h / 2} textAnchor="middle" fill={c.textMuted} fontSize="11" fontFamily={FONT_FAMILY.sans}>
          暂无数据
        </text>
      )}
    </svg>
  );
}

// ── Donut ─────────────────────────────────────────────────────────
function Donut({
  data,
  size = 144,
  thickness = 20,
}: {
  data: { pct: number; color: string }[];
  size?: number;
  thickness?: number;
}) {
  const { darkMode } = useAppStore();
  const c = getColors(darkMode);
  const r = (size - thickness) / 2;
  const cx = size / 2;
  const cy = size / 2;
  const C = 2 * Math.PI * r;
  const total = data.reduce((a, b) => a + b.pct, 0) || 1;
  let offset = 0;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <circle cx={cx} cy={cy} r={r} fill="none" stroke={c.divider} strokeWidth={thickness - 2} />
      {data.map((d, i) => {
        const frac = d.pct / total;
        const len = C * frac - 2; // small gap
        const dash = `${Math.max(len, 0)} ${C}`;
        const dashOffset = -offset;
        offset += C * frac;
        return (
          <circle
            key={i}
            cx={cx}
            cy={cy}
            r={r}
            fill="none"
            stroke={d.color}
            strokeWidth={thickness}
            strokeDasharray={dash}
            strokeDashoffset={dashOffset}
            strokeLinecap="butt"
            transform={`rotate(-90 ${cx} ${cy})`}
          />
        );
      })}
    </svg>
  );
}

// ── Panel: Request Trend ────────────────────────────────────────────
function RequestTrend() {
  const { t } = useTranslation();
  const { dateRange, granularity, serviceId, userId, lastRefresh } = useDashboardContext();
  const { darkMode } = useAppStore();
  const c = getColors(darkMode);

  // Current period
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

  // Previous period (same length shift)
  const [prevStart, prevEnd] = (() => {
    const s = dayjs(dateRange[0]);
    const e = dayjs(dateRange[1]);
    const days = e.diff(s, "day") + 1;
    const pe = s.subtract(1, "day");
    return [pe.subtract(days - 1, "day").format("YYYY-MM-DD"), pe.format("YYYY-MM-DD")];
  })();
  const { data: prev } = useQuery({
    queryKey: ["dashboard-series-req-prev", prevStart, prevEnd, granularity, serviceId, userId, lastRefresh.getTime()],
    queryFn: () =>
      getUsageTimeSeries({
        start_date: prevStart,
        end_date: prevEnd,
        granularity,
        service_id: serviceId !== "all" ? serviceId : undefined,
        user_id: userId !== "all" ? userId : undefined,
      }),
    staleTime: 60000,
  });

  const cur: UsageTimeSeriesPoint[] = data?.data ?? [];
  const pre: UsageTimeSeriesPoint[] = prev?.data ?? [];
  const labels = cur.map((p) => dayjs(p.date).format(granularity === "hour" ? "HH:mm" : "MM-DD"));
  const reqThis = cur.map((p) => p.requests);
  // Align prev to same length as current
  const reqLast = (() => {
    if (pre.length === 0) return [];
    if (pre.length === reqThis.length) return pre.map((p) => p.requests);
    // Pad / trim to match
    const out = Array(reqThis.length).fill(0);
    for (let i = 0; i < reqThis.length; i++) {
      const src = pre[Math.floor((i / reqThis.length) * pre.length)];
      if (src) out[i] = src.requests;
    }
    return out;
  })();

  return (
    <Card
      title={t("dashboard.trend.requestTrend", "请求趋势")}
      unit={`(${t("dashboard.trend.count", "次")})`}
      right={
        <LegendDots
          items={[
            { label: t("dashboard.trend.current", "本期"), color: c.accent },
            { label: t("dashboard.trend.previous", "上期"), color: c.textFaint, dashed: true },
          ]}
        />
      }
    >
      <LineChart
        series={[
          ...(reqLast.length > 0 ? [{ data: reqLast, color: c.textFaint, dashed: true }] : []),
          { data: reqThis, color: c.accent },
        ]}
        labels={labels}
      />
    </Card>
  );
}

// ── Panel: Latency Trend ────────────────────────────────────────────
function LatencyTrend() {
  const { t } = useTranslation();
  const { dateRange, granularity, serviceId, userId, lastRefresh } = useDashboardContext();
  const { darkMode } = useAppStore();
  const c = getColors(darkMode);

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
  const [prevStart, prevEnd] = (() => {
    const s = dayjs(dateRange[0]);
    const e = dayjs(dateRange[1]);
    const days = e.diff(s, "day") + 1;
    const pe = s.subtract(1, "day");
    return [pe.subtract(days - 1, "day").format("YYYY-MM-DD"), pe.format("YYYY-MM-DD")];
  })();
  const { data: prev } = useQuery({
    queryKey: ["dashboard-series-lat-prev", prevStart, prevEnd, granularity, serviceId, userId, lastRefresh.getTime()],
    queryFn: () =>
      getUsageTimeSeries({
        start_date: prevStart,
        end_date: prevEnd,
        granularity,
        service_id: serviceId !== "all" ? serviceId : undefined,
        user_id: userId !== "all" ? userId : undefined,
      }),
    staleTime: 60000,
  });

  const cur: UsageTimeSeriesPoint[] = data?.data ?? [];
  const pre: UsageTimeSeriesPoint[] = prev?.data ?? [];
  const labels = cur.map((p) => dayjs(p.date).format(granularity === "hour" ? "HH:mm" : "MM-DD"));
  const latThis = cur.map((p) => Math.round(p.avg_latency_ms || 0));
  const latLast = (() => {
    if (pre.length === 0) return [];
    if (pre.length === latThis.length) return pre.map((p) => Math.round(p.avg_latency_ms || 0));
    const out = Array(latThis.length).fill(0);
    for (let i = 0; i < latThis.length; i++) {
      const src = pre[Math.floor((i / latThis.length) * pre.length)];
      if (src) out[i] = Math.round(src.avg_latency_ms || 0);
    }
    return out;
  })();

  return (
    <Card
      title={t("dashboard.trend.latencyTrend", "延迟趋势")}
      unit="(ms)"
      right={
        <LegendDots
          items={[
            { label: t("dashboard.trend.current", "本期"), color: c.info },
            { label: t("dashboard.trend.previous", "上期"), color: c.textFaint, dashed: true },
          ]}
        />
      }
    >
      <LineChart
        series={[
          ...(latLast.length > 0 ? [{ data: latLast, color: c.textFaint, dashed: true }] : []),
          { data: latThis, color: c.info },
        ]}
        labels={labels}
        yFormat={(v) => (v === 0 ? "0" : `${v / 1000}K`)}
      />
    </Card>
  );
}

// ── Panel: Cost composition donut ─────────────────────────────────
function CostComposition() {
  const { t, i18n } = useTranslation();
  const { dateRange, serviceId, userId, lastRefresh } = useDashboardContext();
  const { darkMode } = useAppStore();
  const c = getColors(darkMode);
  const palette = getChartPalette(darkMode);

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

  const top = items.slice(0, 3);
  const rest = items.slice(3);
  const restSum = rest.reduce((s, i) => s + (i.cost_usd || 0), 0);

  const pieData = useMemo(() => {
    const base = top.map((i, idx) => ({
      label: i.provider || i.label || "—",
      pct: i.percentage,
      val: i.cost_usd || 0,
      color: palette[idx % palette.length],
    }));
    if (restSum > 0) {
      base.push({
        label: i18n.language.startsWith("zh") ? "其他" : "Others",
        pct: totalCost > 0 ? (restSum / totalCost) * 100 : 0,
        val: restSum,
        color: palette[4],
      });
    }
    return base;
  }, [top, restSum, totalCost, palette, i18n.language]);

  const formatCost = (v: number) => (v >= 1 ? `$${v.toFixed(2)}` : `$${v.toFixed(4)}`);

  return (
    <Card title={t("dashboard.trend.costComposition", "成本构成")} unit="(USD)" minW={340}>
      <div style={{ display: "flex", alignItems: "center", gap: 18, paddingTop: 4 }}>
        <div style={{ position: "relative", flexShrink: 0, width: 144, height: 144 }}>
          <Donut data={pieData.length ? pieData : [{ pct: 100, color: c.divider }]} size={144} thickness={20} />
          <div style={{
            position: "absolute", inset: 0,
            display: "flex", flexDirection: "column",
            alignItems: "center", justifyContent: "center",
            pointerEvents: "none",
          }}>
            <div style={{
              fontSize: 18, fontWeight: 600, color: c.textPrimary,
              letterSpacing: "-0.3px", fontFeatureSettings: '"tnum"',
            }}>
              {formatCost(totalCost)}
            </div>
            <div style={{ fontSize: 10.5, color: c.textMuted, marginTop: 2 }}>
              {t("dashboard.trend.totalCost", "总成本")}
            </div>
          </div>
        </div>
        <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 8 }}>
          {pieData.length === 0 ? (
            <div style={{ color: c.textMuted, fontSize: 11.5 }}>
              {t("dashboard.kpi.noRealData", "暂无数据")}
            </div>
          ) : pieData.map((d, i) => (
            <div key={i} style={{
              display: "grid",
              gridTemplateColumns: "10px 1fr auto auto",
              alignItems: "center",
              gap: 8,
              fontSize: 11.5,
            }}>
              <span style={{ width: 7, height: 7, borderRadius: "50%", background: d.color }} />
              <span style={{
                color: c.textSecondary,
                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
              }}>
                {d.label}
              </span>
              <span style={{ color: c.textPrimary, fontWeight: 600, fontFeatureSettings: '"tnum"' }}>
                {d.pct.toFixed(1)}%
              </span>
              <span style={{
                color: c.textSecondary,
                fontFamily: FONT_FAMILY.mono,
                fontSize: 11,
              }}>
                {formatCost(d.val)}
              </span>
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
}

// ── 3-up row ──────────────────────────────────────────────────────
export function SummaryCharts() {
  return (
    <div className="charts-row" style={{ display: "flex", gap: 14 }}>
      <RequestTrend />
      <LatencyTrend />
      <CostComposition />
      <style>{`
        @media (max-width: 1080px) {
          .charts-row { flex-direction: column; }
        }
      `}</style>
    </div>
  );
}

export default SummaryCharts;
