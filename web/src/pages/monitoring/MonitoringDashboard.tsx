/**
 * LangSmith-style Enterprise Monitoring Dashboard
 *
 * A production-grade, real-time monitoring interface with:
 * - Live RPS and throughput metrics
 * - Latency percentiles (P50/P95/P99)
 * - Error rate visualization
 * - Active users and threads tracking
 * - Token consumption and cost
 * - Alert status indicators
 * - WebSocket-powered real-time updates
 */

import { useEffect, useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowUpRight,
  ArrowDownRight,
  BarChart3,
  Bell,
  CheckCircle,
  Clock,
  Coins,
  Cpu,
  Layers,
  RefreshCw,
  Server,
  Sparkles,
  TrendingUp,
  Users,
  Wifi,
  WifiOff,
  Zap,
  XCircle,
} from "lucide-react";
import {
  getRealtimeDashboard,
  getDashboardSummary,
  getDashboardWebSocket,
  type RealtimeDashboard,
  type AlertStatus,
  type WebSocketMessage,
} from "@/api/dashboard";

// ============ Utility Functions ============

function formatNumber(num: number, decimals: number = 1): string {
  if (num >= 1000000) return (num / 1000000).toFixed(decimals) + "M";
  if (num >= 1000) return (num / 1000).toFixed(decimals) + "K";
  return num.toFixed(decimals);
}

function formatDuration(ms: number): string {
  if (ms >= 1000) return (ms / 1000).toFixed(1) + "s";
  return Math.round(ms) + "ms";
}

function formatCurrency(usd: number): string {
  if (usd >= 1000) return "$" + (usd / 1000).toFixed(2) + "K";
  if (usd >= 1) return "$" + usd.toFixed(2);
  return "$" + usd.toFixed(4);
}

function getStatusColor(level: "ok" | "warning" | "critical"): string {
  switch (level) {
    case "ok":
      return "text-emerald-400";
    case "warning":
      return "text-amber-400";
    case "critical":
      return "text-rose-400";
    default:
      return "text-slate-400";
  }
}

function getStatusBg(level: "ok" | "warning" | "critical"): string {
  switch (level) {
    case "ok":
      return "bg-emerald-500/20 border-emerald-500/30";
    case "warning":
      return "bg-amber-500/20 border-amber-500/30";
    case "critical":
      return "bg-rose-500/20 border-rose-500/30";
    default:
      return "bg-slate-500/20 border-slate-500/30";
  }
}

// ============ Components ============

function ConnectionStatus({ connected }: { connected: boolean }) {
  return (
    <div
      className={`flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-medium transition-all ${
        connected
          ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/30"
          : "bg-rose-500/10 text-rose-400 border border-rose-500/30"
      }`}
    >
      {connected ? (
        <>
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
            <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
          </span>
          <Wifi className="h-3 w-3" />
          <span>Live</span>
        </>
      ) : (
        <>
          <WifiOff className="h-3 w-3" />
          <span>Disconnected</span>
        </>
      )}
    </div>
  );
}

interface MetricCardProps {
  title: string;
  value: string | number;
  subtitle?: string;
  icon: React.ElementType;
  trend?: "up" | "down" | "neutral";
  trendValue?: string;
  status?: "ok" | "warning" | "critical";
  gradient: string;
  delay?: number;
}

function MetricCard({
  title,
  value,
  subtitle,
  icon: Icon,
  trend,
  trendValue,
  status,
  gradient,
  delay = 0,
}: MetricCardProps) {
  return (
    <div
      className="metric-card group"
      style={{ animationDelay: `${delay}ms` }}
    >
      {/* Background glow */}
      <div
        className={`absolute inset-0 rounded-2xl bg-gradient-to-br ${gradient} opacity-0 group-hover:opacity-5 blur-xl transition-opacity duration-500`}
      />

      <div className="relative z-10">
        <div className="flex items-start justify-between mb-3">
          <span className="text-xs font-medium text-slate-400 uppercase tracking-wider">
            {title}
          </span>
          <div
            className={`p-2 rounded-xl bg-gradient-to-br ${gradient} bg-opacity-10`}
          >
            <Icon className="h-4 w-4 text-white/80" />
          </div>
        </div>

        <div className="flex items-baseline gap-2 mb-1">
          <span className="text-2xl font-bold text-slate-100 font-mono tracking-tight">
            {value}
          </span>
          {trend && trendValue && (
            <span
              className={`flex items-center text-xs font-medium ${
                trend === "up"
                  ? "text-emerald-400"
                  : trend === "down"
                  ? "text-rose-400"
                  : "text-slate-400"
              }`}
            >
              {trend === "up" ? (
                <ArrowUpRight className="h-3 w-3" />
              ) : trend === "down" ? (
                <ArrowDownRight className="h-3 w-3" />
              ) : null}
              {trendValue}
            </span>
          )}
          {status && (
            <span
              className={`px-1.5 py-0.5 rounded text-[10px] font-bold uppercase ${getStatusBg(
                status
              )} ${getStatusColor(status)}`}
            >
              {status}
            </span>
          )}
        </div>

        {subtitle && (
          <span className="text-xs text-slate-500 font-mono">{subtitle}</span>
        )}
      </div>
    </div>
  );
}

interface GaugeChartProps {
  value: number;
  max: number;
  label: string;
  unit?: string;
  thresholds?: { warning: number; critical: number };
}

function GaugeChart({
  value,
  max,
  label,
  unit = "",
  thresholds,
}: GaugeChartProps) {
  const percentage = Math.min((value / max) * 100, 100);

  let status: "ok" | "warning" | "critical" = "ok";
  if (thresholds) {
    if (value >= thresholds.critical) status = "critical";
    else if (value >= thresholds.warning) status = "warning";
  }

  const colors = {
    ok: "from-emerald-500 to-teal-500",
    warning: "from-amber-500 to-orange-500",
    critical: "from-rose-500 to-red-500",
  };

  return (
    <div className="gauge-container">
      <div className="gauge-label">{label}</div>
      <div
        className={`gauge-value bg-gradient-to-r ${colors[status]} bg-clip-text text-transparent`}
      >
        {formatNumber(value, 0)}
        <span className="gauge-unit">{unit}</span>
      </div>
      <div className="gauge-bar">
        <div
          className={`gauge-fill bg-gradient-to-r ${colors[status]}`}
          style={{ width: `${percentage}%` }}
        />
      </div>
    </div>
  );
}

interface AlertBadgeProps {
  alert: AlertStatus;
}

function AlertBadge({ alert }: AlertBadgeProps) {
  const icons = {
    ok: CheckCircle,
    warning: AlertTriangle,
    critical: XCircle,
  };
  const Icon = icons[alert.level] || AlertTriangle;

  return (
    <div
      className={`flex items-center gap-2 px-3 py-2 rounded-lg border ${getStatusBg(
        alert.level
      )}`}
    >
      <Icon className={`h-4 w-4 ${getStatusColor(alert.level)}`} />
      <div className="flex-1 min-w-0">
        <div className="text-xs font-medium text-slate-200 truncate">
          {alert.name.replace(/_/g, " ").toUpperCase()}
        </div>
        <div className="text-[10px] text-slate-400 truncate">
          {alert.message}
        </div>
      </div>
    </div>
  );
}

interface ThreadsChartProps {
  threads: Record<string, number>;
}

function ThreadsChart({ threads }: ThreadsChartProps) {
  const entries = Object.entries(threads)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5);

  const max = Math.max(...entries.map(([, v]) => v), 1);

  return (
    <div className="space-y-2">
      {entries.map(([user, count], i) => (
        <div key={user} className="flex items-center gap-3">
          <div className="w-20 truncate text-xs text-slate-400 font-mono">
            {user.slice(0, 8)}...
          </div>
          <div className="flex-1 h-4 bg-slate-800/50 rounded-full overflow-hidden">
            <div
              className="h-full bg-gradient-to-r from-indigo-500 to-purple-500 rounded-full transition-all duration-500"
              style={{
                width: `${(count / max) * 100}%`,
                animationDelay: `${i * 50}ms`,
              }}
            />
          </div>
          <div className="w-8 text-right text-xs font-mono text-slate-300">
            {count}
          </div>
        </div>
      ))}
      {entries.length === 0 && (
        <div className="text-xs text-slate-500 text-center py-4">
          No active threads
        </div>
      )}
    </div>
  );
}

interface HourlyTrendProps {
  data: Array<{ hour: string; count: number }>;
}

function HourlyTrend({ data }: HourlyTrendProps) {
  const max = Math.max(...data.map((d) => d.count), 1);

  return (
    <div className="flex items-end gap-1 h-32">
      {data.map((item, index) => {
        const height = (item.count / max) * 100;
        return (
          <div
            key={index}
            className="flex-1 flex flex-col items-center gap-1 group"
          >
            <div className="relative w-full flex items-end justify-center h-24">
              <div
                className="w-full max-w-[16px] bg-gradient-to-t from-indigo-600 to-indigo-400 rounded-t transition-all hover:from-indigo-500 hover:to-indigo-300"
                style={{
                  height: `${Math.max(height, 2)}%`,
                  animationDelay: `${index * 20}ms`,
                }}
              />
              {/* Tooltip */}
              <div className="absolute bottom-full mb-2 px-2 py-1 bg-slate-800 border border-slate-700 rounded text-xs opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none whitespace-nowrap z-10">
                <div className="font-mono text-slate-200">
                  {formatNumber(item.count, 0)}
                </div>
                <div className="text-slate-400">{item.hour}</div>
              </div>
            </div>
            {index % 4 === 0 && (
              <span className="text-[9px] text-slate-500 font-mono">
                {item.hour.split(":")[0]}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ============ Main Dashboard Component ============

export function MonitoringDashboard() {
  const [isConnected, setIsConnected] = useState(false);
  const [liveData, setLiveData] = useState<Partial<RealtimeDashboard>>({});
  const [alerts, setAlerts] = useState<AlertStatus[]>([]);

  // Initial data fetch
  const { data: dashboardData, refetch } = useQuery({
    queryKey: ["dashboard-realtime"],
    queryFn: getRealtimeDashboard,
    refetchInterval: 30000, // Fallback polling
    staleTime: 5000,
  });

  const { data: summaryData } = useQuery({
    queryKey: ["dashboard-summary"],
    queryFn: () => getDashboardSummary("today"),
    refetchInterval: 60000,
    staleTime: 30000,
  });

  // WebSocket connection
  useEffect(() => {
    const ws = getDashboardWebSocket();

    const unsubscribe = ws.subscribe((message: WebSocketMessage) => {
      if (message.type === "metrics") {
        setIsConnected(true);
        setLiveData({
          rps: message.rps,
          rps_1m: message.rps_1m,
          rps_5m: message.rps_5m,
          latency: message.latency,
          errors: message.errors,
          users: message.users,
          capacity: message.capacity,
          tokens: message.tokens,
          runs: message.runs,
          timestamp: message.timestamp,
        });
        if (message.alerts) {
          setAlerts(message.alerts);
        }
      }
    });

    return () => {
      unsubscribe();
    };
  }, []);

  // Merge live data with fetched data
  const metrics = useMemo(() => {
    return {
      ...dashboardData,
      ...liveData,
    } as RealtimeDashboard;
  }, [dashboardData, liveData]);

  // Determine alert status for metrics
  const latencyStatus = useMemo(() => {
    if (!metrics.latency) return "ok";
    if (metrics.latency.p95 >= 5000) return "critical";
    if (metrics.latency.p95 >= 2000) return "warning";
    return "ok";
  }, [metrics.latency]);

  const errorStatus = useMemo(() => {
    if (!metrics.errors) return "ok";
    if (metrics.errors.rate >= 10) return "critical";
    if (metrics.errors.rate >= 5) return "warning";
    return "ok";
  }, [metrics.errors]);

  return (
    <div className="monitoring-dashboard min-h-screen bg-slate-950 p-6">
      <style>{dashboardStyles}</style>

      {/* Header */}
      <header className="flex items-center justify-between mb-8">
        <div className="flex items-center gap-4">
          <div className="p-3 rounded-2xl bg-gradient-to-br from-indigo-600 to-purple-600 shadow-lg shadow-indigo-500/25">
            <BarChart3 className="h-6 w-6 text-white" />
          </div>
          <div>
            <h1 className="text-2xl font-bold text-slate-100 tracking-tight">
              Monitoring Dashboard
            </h1>
            <p className="text-sm text-slate-400">
              Real-time gateway metrics and alerts
            </p>
          </div>
        </div>

        <div className="flex items-center gap-4">
          <ConnectionStatus connected={isConnected} />
          <button
            onClick={() => refetch()}
            className="p-2 rounded-lg bg-slate-800/50 border border-slate-700/50 hover:bg-slate-700/50 transition-colors"
            title="Refresh"
          >
            <RefreshCw className="h-4 w-4 text-slate-400" />
          </button>
        </div>
      </header>

      {/* Primary Metrics Row */}
      <section className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-6 gap-4 mb-6">
        <MetricCard
          title="RPS"
          value={metrics.rps?.toFixed(1) || "0"}
          subtitle={`1m: ${metrics.rps_1m?.toFixed(1) || "0"} / 5m: ${metrics.rps_5m?.toFixed(1) || "0"}`}
          icon={Zap}
          gradient="from-yellow-500 to-orange-500"
          delay={0}
        />
        <MetricCard
          title="Latency P95"
          value={formatDuration(metrics.latency?.p95 || 0)}
          subtitle={`P50: ${formatDuration(metrics.latency?.p50 || 0)} / P99: ${formatDuration(metrics.latency?.p99 || 0)}`}
          icon={Clock}
          status={latencyStatus}
          gradient="from-blue-500 to-cyan-500"
          delay={50}
        />
        <MetricCard
          title="Error Rate"
          value={`${(metrics.errors?.rate || 0).toFixed(1)}%`}
          subtitle={`4xx: ${(metrics.errors?.rate_4xx || 0).toFixed(1)}% / 5xx: ${(metrics.errors?.rate_5xx || 0).toFixed(1)}%`}
          icon={AlertTriangle}
          status={errorStatus}
          gradient="from-rose-500 to-pink-500"
          delay={100}
        />
        <MetricCard
          title="Active Users"
          value={metrics.users?.active || 0}
          subtitle={`${metrics.users?.threads_total || 0} threads`}
          icon={Users}
          gradient="from-emerald-500 to-teal-500"
          delay={150}
        />
        <MetricCard
          title="Token Cost"
          value={formatCurrency(metrics.tokens?.cost_usd || 0)}
          subtitle={`${formatNumber(metrics.tokens?.total || 0)} tokens`}
          icon={Coins}
          gradient="from-amber-500 to-yellow-500"
          delay={200}
        />
        <MetricCard
          title="Agent Runs"
          value={formatNumber(metrics.runs?.total || 0, 0)}
          subtitle={`${(metrics.runs?.success_rate || 100).toFixed(1)}% success`}
          icon={Sparkles}
          gradient="from-violet-500 to-purple-500"
          delay={250}
        />
      </section>

      {/* Secondary Row - Detailed Metrics */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-6">
        {/* Capacity Gauges */}
        <div className="dashboard-card">
          <div className="card-header">
            <Cpu className="h-4 w-4 text-slate-400" />
            <h3 className="text-sm font-medium text-slate-300">
              System Capacity
            </h3>
          </div>
          <div className="grid grid-cols-3 gap-4">
            <GaugeChart
              value={metrics.capacity?.concurrent || 0}
              max={metrics.capacity?.max_concurrent || 100}
              label="Concurrent"
              thresholds={{ warning: 80, critical: 95 }}
            />
            <GaugeChart
              value={metrics.capacity?.queue_depth || 0}
              max={100}
              label="Queue"
              thresholds={{ warning: 50, critical: 80 }}
            />
            <GaugeChart
              value={metrics.capacity?.utilization || 0}
              max={100}
              label="Utilization"
              unit="%"
              thresholds={{ warning: 80, critical: 95 }}
            />
          </div>
        </div>

        {/* Threads by User */}
        <div className="dashboard-card">
          <div className="card-header">
            <Layers className="h-4 w-4 text-slate-400" />
            <h3 className="text-sm font-medium text-slate-300">
              Threads by User
            </h3>
          </div>
          <ThreadsChart threads={metrics.users?.threads_by_user || {}} />
        </div>

        {/* Alerts */}
        <div className="dashboard-card">
          <div className="card-header">
            <Bell className="h-4 w-4 text-slate-400" />
            <h3 className="text-sm font-medium text-slate-300">
              Alert Status
            </h3>
            <span className="ml-auto text-xs text-slate-500">
              {alerts.filter((a) => a.level !== "ok").length} active
            </span>
          </div>
          <div className="space-y-2 max-h-40 overflow-y-auto">
            {alerts.map((alert, i) => (
              <AlertBadge key={i} alert={alert} />
            ))}
            {alerts.length === 0 && (
              <div className="text-xs text-slate-500 text-center py-4">
                No alerts configured
              </div>
            )}
          </div>
        </div>
      </div>

      {/* 24-Hour Trend */}
      <div className="dashboard-card">
        <div className="card-header">
          <TrendingUp className="h-4 w-4 text-slate-400" />
          <h3 className="text-sm font-medium text-slate-300">
            24-Hour Request Trend
          </h3>
          <span className="ml-auto text-xs text-slate-500 font-mono">
            Total: {formatNumber(summaryData?.overview?.total_requests || 0, 0)}
          </span>
        </div>
        <HourlyTrend data={summaryData?.hourly_trend || []} />
      </div>

      {/* Footer Stats */}
      <footer className="mt-6 pt-6 border-t border-slate-800/50 flex items-center justify-between text-xs text-slate-500">
        <div className="flex items-center gap-4">
          <span>
            Last updated:{" "}
            {metrics.timestamp
              ? new Date(metrics.timestamp).toLocaleTimeString()
              : "--:--"}
          </span>
          <span>•</span>
          <span>
            Success Rate:{" "}
            {summaryData?.overview?.success_rate?.toFixed(1) || "100"}%
          </span>
        </div>
        <div className="flex items-center gap-2">
          <Server className="h-3 w-3" />
          <span>AI Gateway v2.0</span>
        </div>
      </footer>
    </div>
  );
}

// ============ Styles ============

const dashboardStyles = `
  @keyframes fadeInUp {
    from {
      opacity: 0;
      transform: translateY(10px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }

  .monitoring-dashboard {
    --card-bg: rgba(15, 23, 42, 0.6);
    --card-border: rgba(51, 65, 85, 0.5);
  }

  .metric-card {
    position: relative;
    padding: 1rem;
    border-radius: 1rem;
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    backdrop-filter: blur(12px);
    animation: fadeInUp 0.4s ease-out forwards;
    opacity: 0;
    transition: all 0.2s ease;
  }

  .metric-card:hover {
    border-color: rgba(99, 102, 241, 0.3);
    transform: translateY(-2px);
  }

  .dashboard-card {
    padding: 1.25rem;
    border-radius: 1rem;
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    backdrop-filter: blur(12px);
  }

  .card-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 1rem;
    padding-bottom: 0.75rem;
    border-bottom: 1px solid rgba(51, 65, 85, 0.3);
  }

  .gauge-container {
    text-align: center;
  }

  .gauge-label {
    font-size: 0.65rem;
    color: rgb(148, 163, 184);
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 0.5rem;
  }

  .gauge-value {
    font-size: 1.5rem;
    font-weight: 700;
    font-family: ui-monospace, monospace;
    margin-bottom: 0.5rem;
  }

  .gauge-unit {
    font-size: 0.75rem;
    opacity: 0.7;
    margin-left: 2px;
  }

  .gauge-bar {
    height: 4px;
    background: rgba(51, 65, 85, 0.5);
    border-radius: 2px;
    overflow: hidden;
  }

  .gauge-fill {
    height: 100%;
    border-radius: 2px;
    transition: width 0.5s ease;
  }
`;

export default MonitoringDashboard;
