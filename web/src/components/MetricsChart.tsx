import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { getMetricsSummary } from "@/api/metrics";
import type { HourlyMetric } from "@/api/metrics";
import { useAppStore } from "@/store/useAppStore";
import {
  Activity,
  CheckCircle2,
  Clock,
  Server,
  RefreshCw,
  AlertTriangle,
  Coins,
  Zap,
  MessageSquare,
  Timer,
  TrendingUp,
  TrendingDown,
  Minus,
} from "lucide-react";
import { useEffect, useState } from "react";

// ============================================================================
// 监控面板 - 支持浅色/深色主题切换
// ============================================================================

function formatNumber(num: number): string {
  if (num >= 1000000) return (num / 1000000).toFixed(1) + "M";
  if (num >= 1000) return (num / 1000).toFixed(1) + "K";
  return num.toLocaleString();
}

function formatTime(isoString: string): string {
  try {
    const date = new Date(isoString);
    return date.toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit"
    });
  } catch {
    return "--:--:--";
  }
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}m`;
}

// ============================================================================
// 主组件
// ============================================================================

export function MetricsChart() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const [mounted, setMounted] = useState(false);
  const [hoveredBar, setHoveredBar] = useState<number | null>(null);

  useEffect(() => {
    setMounted(true);
  }, []);

  const metricsQuery = useQuery({
    queryKey: ["metrics-summary"],
    queryFn: getMetricsSummary,
    refetchInterval: 30000,
    staleTime: 15000,
  });

  const { data: metrics, isLoading, isError, refetch, isFetching } = metricsQuery;
  const themeClass = darkMode ? "metrics-dark" : "metrics-light";
  const themeCSS = darkMode ? darkThemeCSS : lightThemeCSS;

  if (isLoading) {
    return (
      <div className={themeClass}>
        <style>{themeCSS}</style>
        <LoadingState />
      </div>
    );
  }

  if (isError) {
    return (
      <div className={themeClass}>
        <style>{themeCSS}</style>
        <ErrorState onRetry={refetch} t={t} />
      </div>
    );
  }

  if (!metrics) return null;

  const systemStatus = getSystemStatus(metrics.success_rate);

  return (
    <div className={`${themeClass} ${mounted ? "ml-mounted" : ""}`}>
      <style>{themeCSS}</style>

      {/* 头部 */}
      <header className="ml-header">
        <div className="ml-header-left">
          <div className="ml-logo">
            <Activity className="ml-logo-icon" />
          </div>
          <div className="ml-header-text">
            <h1 className="ml-title">{t("metrics.title")}</h1>
            <div className="ml-status-row">
              <StatusIndicator status={systemStatus} />
              <span className="ml-status-text">
                {t(`metrics.systemStatus.${systemStatus}`)}
              </span>
            </div>
          </div>
        </div>

        <div className="ml-header-right">
          <span className="ml-update-time">
            {t("metrics.lastUpdated")} {formatTime(metrics.last_updated)}
          </span>
          <button
            onClick={() => refetch()}
            disabled={isFetching}
            className="ml-refresh-btn"
            title={t("metrics.refreshData")}
          >
            <RefreshCw className={`ml-refresh-icon ${isFetching ? "ml-spinning" : ""}`} />
          </button>
        </div>
      </header>

      {/* 主要指标 */}
      <section className="ml-metrics-grid">
        <MetricCard
          label={t("metrics.totalRequests")}
          value={formatNumber(metrics.total_requests)}
          icon={<Activity />}
          color="blue"
        />
        <MetricCard
          label={t("metrics.successRate")}
          value={`${metrics.success_rate.toFixed(1)}%`}
          icon={<CheckCircle2 />}
          color={metrics.success_rate >= 95 ? "green" : metrics.success_rate >= 90 ? "yellow" : "red"}
          trend={metrics.success_rate >= 95 ? "up" : metrics.success_rate >= 90 ? "neutral" : "down"}
        />
        <MetricCard
          label={t("metrics.avgLatency")}
          value={formatDuration(metrics.avg_latency_ms)}
          icon={<Clock />}
          color="amber"
          subtitle={metrics.latency_p95 > 0 ? t("metrics.p95Label", { value: formatDuration(metrics.latency_p95) }) : undefined}
        />
        <MetricCard
          label={t("metrics.activeServices")}
          value={metrics.active_services.toString()}
          icon={<Server />}
          color="violet"
        />
      </section>

      {/* Token & Agent 指标 */}
      <section className="ml-metrics-grid">
        <MetricCard
          label={t("metrics.totalTokens")}
          value={metrics.total_tokens > 0 ? formatNumber(metrics.total_tokens) : t("metrics.noData")}
          icon={<MessageSquare />}
          color="cyan"
          subtitle={metrics.prompt_tokens > 0
            ? t("metrics.tokenBreakdown", {
                input: formatNumber(metrics.prompt_tokens),
                output: formatNumber(metrics.completion_tokens)
              })
            : t("metrics.waitingForData")}
          isEmpty={metrics.total_tokens === 0}
        />
        <MetricCard
          label={t("metrics.estimatedCost")}
          value={metrics.estimated_cost_usd > 0 ? `$${metrics.estimated_cost_usd.toFixed(4)}` : "$0.00"}
          icon={<Coins />}
          color="yellow"
          isEmpty={metrics.estimated_cost_usd === 0}
        />
        <MetricCard
          label={t("metrics.totalRuns")}
          value={metrics.total_runs > 0 ? formatNumber(metrics.total_runs) : t("metrics.noData")}
          icon={<Zap />}
          color="purple"
          subtitle={metrics.total_runs > 0
            ? t("metrics.runSuccessRate", { rate: metrics.run_success_rate.toFixed(1) })
            : t("metrics.waitingForRun")}
          isEmpty={metrics.total_runs === 0}
        />
        <MetricCard
          label={t("metrics.avgRunDuration")}
          value={metrics.avg_run_duration_ms > 0 ? formatDuration(metrics.avg_run_duration_ms) : t("metrics.noData")}
          icon={<Timer />}
          color="rose"
          isEmpty={metrics.avg_run_duration_ms === 0}
        />
      </section>

      {/* 延迟分布 */}
      {(metrics.latency_p50 > 0 || metrics.latency_p95 > 0 || metrics.latency_p99 > 0) && (
        <section className="ml-section">
          <div className="ml-section-header">
            <Clock className="ml-section-icon" />
            <h2 className="ml-section-title">{t("metrics.latencyDistribution")}</h2>
          </div>
          <div className="ml-latency-grid">
            <LatencyBar label="P50" value={metrics.latency_p50} maxValue={Math.max(metrics.latency_p99, 1000)} />
            <LatencyBar label="P95" value={metrics.latency_p95} maxValue={Math.max(metrics.latency_p99, 1000)} />
            <LatencyBar label="P99" value={metrics.latency_p99} maxValue={Math.max(metrics.latency_p99, 1000)} />
          </div>
        </section>
      )}

      {/* 24小时趋势 */}
      <section className="ml-section">
        <div className="ml-section-header">
          <TrendingUp className="ml-section-icon" />
          <h2 className="ml-section-title">{t("metrics.hourlyTrend")}</h2>
          <span className="ml-chart-peak">
            {t("metrics.peakValue", {
              value: formatNumber(Math.max(...metrics.requests_by_hour.map(h => h.count), 0))
            })}
          </span>
        </div>
        <TrendChart
          data={metrics.requests_by_hour}
          hoveredBar={hoveredBar}
          setHoveredBar={setHoveredBar}
        />
      </section>
    </div>
  );
}

// ============================================================================
// 子组件
// ============================================================================

function StatusIndicator({ status }: { status: "healthy" | "degraded" | "critical" }) {
  return (
    <span className={`ml-status-dot ml-status-${status}`}>
      <span className="ml-status-ping" />
    </span>
  );
}

function getSystemStatus(successRate: number): "healthy" | "degraded" | "critical" {
  if (successRate >= 95) return "healthy";
  if (successRate >= 90) return "degraded";
  return "critical";
}

interface MetricCardProps {
  label: string;
  value: string;
  icon: React.ReactNode;
  color: "blue" | "green" | "amber" | "violet" | "cyan" | "yellow" | "purple" | "rose" | "red";
  subtitle?: string;
  trend?: "up" | "down" | "neutral" | null;
  isEmpty?: boolean;
}

function MetricCard({ label, value, icon, color, subtitle, trend, isEmpty }: MetricCardProps) {
  return (
    <div className={`ml-card ml-card-${color} ${isEmpty ? "ml-card-empty" : ""}`}>
      <div className="ml-card-header">
        <span className="ml-card-label">{label}</span>
        <div className={`ml-card-icon ml-icon-${color}`}>
          {icon}
        </div>
      </div>
      <div className="ml-card-body">
        <span className={`ml-card-value ${isEmpty ? "ml-card-value-empty" : ""}`}>
          {value}
        </span>
        {trend && (
          <span className={`ml-trend ml-trend-${trend}`}>
            {trend === "up" && <TrendingUp className="ml-trend-icon" />}
            {trend === "down" && <TrendingDown className="ml-trend-icon" />}
            {trend === "neutral" && <Minus className="ml-trend-icon" />}
          </span>
        )}
      </div>
      {subtitle && (
        <span className="ml-card-subtitle">{subtitle}</span>
      )}
    </div>
  );
}

function LatencyBar({ label, value, maxValue }: { label: string; value: number; maxValue: number }) {
  const percentage = Math.min((value / maxValue) * 100, 100);
  const status = value < 100 ? "good" : value < 500 ? "medium" : "slow";

  return (
    <div className="ml-latency-item">
      <div className="ml-latency-header">
        <span className="ml-latency-label">{label}</span>
        <span className={`ml-latency-value ml-latency-${status}`}>{formatDuration(value)}</span>
      </div>
      <div className="ml-latency-track">
        <div
          className={`ml-latency-fill ml-latency-fill-${status}`}
          style={{ width: `${percentage}%` }}
        />
      </div>
    </div>
  );
}

interface TrendChartProps {
  data: HourlyMetric[];
  hoveredBar: number | null;
  setHoveredBar: (index: number | null) => void;
}

function TrendChart({ data, hoveredBar, setHoveredBar }: TrendChartProps) {
  const maxCount = Math.max(...data.map(h => h.count), 1);

  return (
    <div className="ml-chart-container">
      <div className="ml-chart-grid">
        {[0.75, 0.5, 0.25].map((ratio) => (
          <div key={ratio} className="ml-chart-gridline" style={{ bottom: `${ratio * 100}%` }}>
            <span className="ml-chart-gridlabel">{formatNumber(Math.round(maxCount * ratio))}</span>
          </div>
        ))}
      </div>
      <div className="ml-chart-bars">
        {data.map((item, index) => {
          const height = (item.count / maxCount) * 100;
          const isHovered = hoveredBar === index;

          return (
            <div
              key={index}
              className={`ml-chart-bar-wrapper ${isHovered ? "ml-bar-hovered" : ""}`}
              onMouseEnter={() => setHoveredBar(index)}
              onMouseLeave={() => setHoveredBar(null)}
            >
              <div className="ml-chart-bar-container">
                <div
                  className="ml-chart-bar"
                  style={{
                    height: `${Math.max(height, 2)}%`,
                    animationDelay: `${index * 20}ms`
                  }}
                />
                {isHovered && (
                  <div className="ml-chart-tooltip">
                    <div className="ml-tooltip-value">{formatNumber(item.count)}</div>
                    <div className="ml-tooltip-time">{item.hour}</div>
                  </div>
                )}
              </div>
              {index % 4 === 0 && (
                <span className="ml-chart-xlabel">{item.hour.split(":")[0]}:00</span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function LoadingState() {
  return (
    <div className="ml-loading">
      <div className="ml-loading-header">
        <div className="ml-skeleton ml-skeleton-logo" />
        <div className="ml-skeleton ml-skeleton-title" />
      </div>
      <div className="ml-loading-grid">
        {[...Array(4)].map((_, i) => (
          <div key={i} className="ml-skeleton ml-skeleton-card" style={{ animationDelay: `${i * 100}ms` }} />
        ))}
      </div>
      <div className="ml-loading-grid">
        {[...Array(4)].map((_, i) => (
          <div key={i} className="ml-skeleton ml-skeleton-card" style={{ animationDelay: `${(i + 4) * 100}ms` }} />
        ))}
      </div>
      <div className="ml-skeleton ml-skeleton-chart" />
    </div>
  );
}

interface ErrorStateProps {
  onRetry: () => void;
  t: (key: string) => string;
}

function ErrorState({ onRetry, t }: ErrorStateProps) {
  return (
    <div className="ml-error">
      <div className="ml-error-icon">
        <AlertTriangle />
      </div>
      <h3 className="ml-error-title">{t("metrics.error.title")}</h3>
      <p className="ml-error-desc">{t("metrics.error.description")}</p>
      <button onClick={onRetry} className="ml-error-btn">
        <RefreshCw className="ml-btn-icon" />
        {t("metrics.reload")}
      </button>
    </div>
  );
}

// ============================================================================
// 共享样式（两个主题通用）- 必须首先定义
// ============================================================================

const sharedStyles = `
.metrics-light *, .metrics-dark * {
  box-sizing: border-box;
}

.ml-mounted {
  animation: ml-fadeIn 0.3s ease-out;
}

@keyframes ml-fadeIn {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

@keyframes ml-shimmer {
  0% { background-position: -200% 0; }
  100% { background-position: 200% 0; }
}

@keyframes ml-ping {
  75%, 100% { transform: scale(2); opacity: 0; }
}

@keyframes ml-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

@keyframes ml-barGrow {
  from { transform: scaleY(0); }
  to { transform: scaleY(1); }
}

/* 头部 */
.ml-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--ml-border);
}

.ml-header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.ml-logo {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  background: linear-gradient(135deg, var(--ml-accent-blue), #1d4ed8);
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 2px 8px rgba(59, 130, 246, 0.3);
}

.ml-logo-icon {
  width: 20px;
  height: 20px;
  color: white;
}

.ml-header-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.ml-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--ml-text-primary);
  margin: 0;
}

.ml-status-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.ml-status-dot {
  position: relative;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.ml-status-healthy { background: var(--ml-accent-green); }
.ml-status-degraded { background: var(--ml-accent-amber); }
.ml-status-critical { background: var(--ml-accent-red); }

.ml-status-ping {
  position: absolute;
  inset: 0;
  border-radius: 50%;
  animation: ml-ping 1.5s cubic-bezier(0, 0, 0.2, 1) infinite;
}

.ml-status-healthy .ml-status-ping { background: var(--ml-accent-green); }
.ml-status-degraded .ml-status-ping { background: var(--ml-accent-amber); }
.ml-status-critical .ml-status-ping { background: var(--ml-accent-red); }

.ml-status-text {
  font-size: 12px;
  color: var(--ml-text-secondary);
}

.ml-header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.ml-update-time {
  font-size: 12px;
  color: var(--ml-text-muted);
}

.ml-refresh-btn {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: var(--ml-bg-card);
  border: 1px solid var(--ml-border);
  color: var(--ml-text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s ease;
}

.ml-refresh-btn:hover {
  background: var(--ml-bg-hover);
  border-color: var(--ml-border-hover);
  color: var(--ml-text-primary);
}

.ml-refresh-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.ml-refresh-icon {
  width: 16px;
  height: 16px;
}

.ml-spinning {
  animation: ml-spin 1s linear infinite;
}

/* 指标网格 */
.ml-metrics-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}

@media (max-width: 1024px) {
  .ml-metrics-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 640px) {
  .ml-metrics-grid {
    grid-template-columns: 1fr;
  }
}

/* 指标卡片 */
.ml-card {
  background: var(--ml-bg-card);
  border: 1px solid var(--ml-border);
  border-radius: 14px;
  padding: 16px;
  transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  box-shadow: var(--ml-shadow);
}

.ml-card:hover {
  border-color: var(--ml-border-hover);
  box-shadow: var(--ml-shadow-hover);
  transform: translateY(-2px);
}

.ml-card-empty {
  opacity: 0.7;
}

.ml-card-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 12px;
}

.ml-card-label {
  font-size: 13px;
  color: var(--ml-text-secondary);
  font-weight: 500;
}

.ml-card-icon {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
}

.ml-card-icon svg {
  width: 18px;
  height: 18px;
}

/* 渐变图标背景 */
.ml-icon-blue { background: linear-gradient(135deg, #3b82f6 0%, #60a5fa 100%); color: white; }
.ml-icon-green { background: linear-gradient(135deg, #10b981 0%, #34d399 100%); color: white; }
.ml-icon-amber { background: linear-gradient(135deg, #f59e0b 0%, #fbbf24 100%); color: white; }
.ml-icon-violet { background: linear-gradient(135deg, #8b5cf6 0%, #a78bfa 100%); color: white; }
.ml-icon-cyan { background: linear-gradient(135deg, #06b6d4 0%, #22d3ee 100%); color: white; }
.ml-icon-yellow { background: linear-gradient(135deg, #eab308 0%, #facc15 100%); color: white; }
.ml-icon-purple { background: linear-gradient(135deg, #a855f7 0%, #c084fc 100%); color: white; }
.ml-icon-rose { background: linear-gradient(135deg, #f43f5e 0%, #fb7185 100%); color: white; }
.ml-icon-red { background: linear-gradient(135deg, #ef4444 0%, #f87171 100%); color: white; }

.ml-card-body {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}

.ml-card-value {
  font-size: 24px;
  font-weight: 700;
  color: var(--ml-text-primary);
  font-variant-numeric: tabular-nums;
}

.ml-card-value-empty {
  font-size: 14px;
  font-weight: 500;
  color: var(--ml-text-muted);
}

.ml-trend {
  display: flex;
  align-items: center;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 12px;
}

.ml-trend-icon {
  width: 14px;
  height: 14px;
}

.ml-trend-up { background: rgba(34, 197, 94, 0.15); color: var(--ml-accent-green); }
.ml-trend-down { background: rgba(239, 68, 68, 0.15); color: var(--ml-accent-red); }
.ml-trend-neutral { background: rgba(148, 163, 184, 0.15); color: var(--ml-text-secondary); }

.ml-card-subtitle {
  font-size: 12px;
  color: var(--ml-text-muted);
}

/* 区块 */
.ml-section {
  background: var(--ml-bg-card);
  border: 1px solid var(--ml-border);
  border-radius: 12px;
  padding: 16px;
  margin-bottom: 16px;
  box-shadow: var(--ml-shadow);
}

.ml-section-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--ml-border);
}

.ml-section-icon {
  width: 16px;
  height: 16px;
  color: var(--ml-text-muted);
}

.ml-section-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--ml-text-primary);
  margin: 0;
}

/* 延迟分布 */
.ml-latency-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 20px;
}

@media (max-width: 640px) {
  .ml-latency-grid {
    grid-template-columns: 1fr;
    gap: 12px;
  }
}

.ml-latency-item {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ml-latency-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.ml-latency-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--ml-text-secondary);
  letter-spacing: 0.02em;
}

.ml-latency-value {
  font-size: 16px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.ml-latency-good { color: var(--ml-accent-green); }
.ml-latency-medium { color: var(--ml-accent-amber); }
.ml-latency-slow { color: var(--ml-accent-red); }

.ml-latency-track {
  height: 8px;
  background: var(--ml-latency-track);
  border-radius: 9999px;
  overflow: hidden;
}

.ml-latency-fill {
  height: 100%;
  border-radius: 9999px;
  transition: width 0.5s ease;
}

.ml-latency-fill-good { background: linear-gradient(90deg, var(--ml-accent-green), #4ade80); }
.ml-latency-fill-medium { background: linear-gradient(90deg, var(--ml-accent-amber), #fbbf24); }
.ml-latency-fill-slow { background: linear-gradient(90deg, var(--ml-accent-red), #f87171); }

/* 趋势图 */
.ml-chart-peak {
  margin-left: auto;
  font-size: 12px;
  color: var(--ml-text-muted);
}

.ml-chart-container {
  position: relative;
  height: 180px;
  margin-top: 12px;
}

.ml-chart-grid {
  position: absolute;
  inset: 0;
  bottom: 24px;
  pointer-events: none;
}

.ml-chart-gridline {
  position: absolute;
  left: 36px;
  right: 0;
  height: 1px;
  background: var(--ml-chart-gridline);
}

.ml-chart-gridlabel {
  position: absolute;
  right: 100%;
  margin-right: 6px;
  font-size: 10px;
  color: var(--ml-text-muted);
  transform: translateY(-50%);
}

.ml-chart-bars {
  position: relative;
  display: flex;
  align-items: flex-end;
  height: 100%;
  padding-left: 36px;
  padding-bottom: 24px;
  gap: 2px;
  z-index: 1;
}

.ml-chart-bar-wrapper {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  height: 100%;
  cursor: pointer;
}

.ml-chart-bar-container {
  flex: 1;
  width: 100%;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  position: relative;
}

.ml-chart-bar {
  width: 100%;
  max-width: 16px;
  min-width: 4px;
  background: linear-gradient(180deg, var(--ml-accent-blue), #1d4ed8);
  border-radius: 3px 3px 0 0;
  transform-origin: bottom;
  animation: ml-barGrow 0.4s ease-out forwards;
  opacity: 0.7;
  transition: opacity 0.15s ease;
}

.ml-bar-hovered .ml-chart-bar {
  opacity: 1;
}

.ml-chart-tooltip {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 50%;
  transform: translateX(-50%);
  background: var(--ml-tooltip-bg);
  color: var(--ml-tooltip-text);
  border-radius: 6px;
  padding: 6px 10px;
  white-space: nowrap;
  z-index: 10;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  animation: ml-fadeIn 0.15s ease-out;
}

.ml-tooltip-value {
  font-size: 13px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.ml-tooltip-time {
  font-size: 11px;
  opacity: 0.8;
  margin-top: 2px;
}

.ml-chart-xlabel {
  font-size: 10px;
  color: var(--ml-text-muted);
  margin-top: 6px;
}

/* 加载状态 */
.ml-loading {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.ml-loading-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--ml-border);
}

.ml-loading-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

@media (max-width: 1024px) {
  .ml-loading-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

.ml-skeleton {
  background: linear-gradient(90deg, var(--ml-skeleton-base) 0%, var(--ml-skeleton-highlight) 50%, var(--ml-skeleton-base) 100%);
  background-size: 200% 100%;
  animation: ml-shimmer 1.5s ease-in-out infinite;
  border-radius: 8px;
}

.ml-skeleton-logo {
  width: 40px;
  height: 40px;
  border-radius: 10px;
}

.ml-skeleton-title {
  width: 120px;
  height: 20px;
}

.ml-skeleton-card {
  height: 100px;
  border-radius: 12px;
}

.ml-skeleton-chart {
  height: 200px;
  border-radius: 12px;
}

/* 错误状态 */
.ml-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 48px 20px;
  text-align: center;
}

.ml-error-icon {
  width: 56px;
  height: 56px;
  border-radius: 12px;
  background: rgba(239, 68, 68, 0.15);
  color: var(--ml-accent-red);
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 16px;
}

.ml-error-icon svg {
  width: 28px;
  height: 28px;
}

.ml-error-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--ml-text-primary);
  margin: 0 0 8px 0;
}

.ml-error-desc {
  font-size: 14px;
  color: var(--ml-text-secondary);
  margin: 0 0 20px 0;
  max-width: 280px;
}

.ml-error-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border-radius: 8px;
  background: rgba(239, 68, 68, 0.15);
  border: 1px solid rgba(239, 68, 68, 0.3);
  color: var(--ml-accent-red);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s ease;
}

.ml-error-btn:hover {
  background: rgba(239, 68, 68, 0.2);
  border-color: rgba(239, 68, 68, 0.4);
}

.ml-btn-icon {
  width: 16px;
  height: 16px;
}
`;

// ============================================================================
// 浅色主题样式 - 蓝白色调
// ============================================================================

const lightThemeCSS = `
/* 基础变量 - 蓝白色浅色主题 */
.metrics-light {
  --ml-bg: #ffffff;
  --ml-bg-card: #ffffff;
  --ml-bg-hover: #f8fafc;
  --ml-border: #e2e8f0;
  --ml-border-hover: #cbd5e1;
  --ml-text-primary: #1e293b;
  --ml-text-secondary: #64748b;
  --ml-text-muted: #94a3b8;
  --ml-accent-blue: #3b82f6;
  --ml-accent-green: #22c55e;
  --ml-accent-amber: #f59e0b;
  --ml-accent-violet: #8b5cf6;
  --ml-accent-cyan: #06b6d4;
  --ml-accent-yellow: #eab308;
  --ml-accent-purple: #a855f7;
  --ml-accent-rose: #f43f5e;
  --ml-accent-red: #ef4444;
  --ml-shadow: 0 1px 3px rgba(0, 0, 0, 0.1), 0 1px 2px rgba(0, 0, 0, 0.06);
  --ml-shadow-hover: 0 4px 6px rgba(0, 0, 0, 0.1), 0 2px 4px rgba(0, 0, 0, 0.06);
  --ml-skeleton-base: #f1f5f9;
  --ml-skeleton-highlight: #e2e8f0;
  --ml-latency-track: #f1f5f9;
  --ml-chart-gridline: #f1f5f9;
  --ml-tooltip-bg: #1e293b;
  --ml-tooltip-text: #ffffff;

  background: var(--ml-bg);
  border-radius: 12px;
  padding: 20px;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  color: var(--ml-text-primary);
}

${sharedStyles}
`;

// ============================================================================
// 深色主题样式
// ============================================================================

const darkThemeCSS = `
/* 基础变量 - 深色主题 */
.metrics-dark {
  --ml-bg: #0f172a;
  --ml-bg-card: #1e293b;
  --ml-bg-hover: #334155;
  --ml-border: #334155;
  --ml-border-hover: #475569;
  --ml-text-primary: #f1f5f9;
  --ml-text-secondary: #94a3b8;
  --ml-text-muted: #64748b;
  --ml-accent-blue: #60a5fa;
  --ml-accent-green: #4ade80;
  --ml-accent-amber: #fbbf24;
  --ml-accent-violet: #a78bfa;
  --ml-accent-cyan: #22d3ee;
  --ml-accent-yellow: #facc15;
  --ml-accent-purple: #c084fc;
  --ml-accent-rose: #fb7185;
  --ml-accent-red: #f87171;
  --ml-shadow: 0 1px 3px rgba(0, 0, 0, 0.3), 0 1px 2px rgba(0, 0, 0, 0.2);
  --ml-shadow-hover: 0 4px 6px rgba(0, 0, 0, 0.3), 0 2px 4px rgba(0, 0, 0, 0.2);
  --ml-skeleton-base: #1e293b;
  --ml-skeleton-highlight: #334155;
  --ml-latency-track: #334155;
  --ml-chart-gridline: #334155;
  --ml-tooltip-bg: #f1f5f9;
  --ml-tooltip-text: #0f172a;

  background: var(--ml-bg);
  border-radius: 12px;
  padding: 20px;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  color: var(--ml-text-primary);
}

${sharedStyles}
`;
