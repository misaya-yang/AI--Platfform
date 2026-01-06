import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { getMetricsSummary } from "@/api/metrics";
import type { HourlyMetric } from "@/api/metrics";
import {
  Activity,
  CheckCircle2,
  Clock,
  Server,
  RefreshCw,
  AlertTriangle,
  TrendingUp,
} from "lucide-react";

export function MetricsChart() {
  const { t } = useTranslation();

  const metricsQuery = useQuery({
    queryKey: ["metrics-summary"],
    queryFn: getMetricsSummary,
    refetchInterval: 60000, // 每分钟自动刷新
    staleTime: 30000,
  });

  const { data: metrics, isLoading, isError, refetch } = metricsQuery;

  // Loading State
  if (isLoading) {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-semibold tracking-tight">{t("metrics.title", "系统指标")}</h3>
        </div>
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="rounded-xl border bg-card p-5 animate-pulse">
              <div className="h-4 w-24 bg-muted rounded mb-3" />
              <div className="h-8 w-16 bg-muted rounded" />
            </div>
          ))}
        </div>
        <div className="rounded-xl border bg-card p-5 animate-pulse">
          <div className="h-4 w-32 bg-muted rounded mb-4" />
          <div className="h-40 bg-muted rounded" />
        </div>
      </div>
    );
  }

  // Error State
  if (isError) {
    return (
      <div className="rounded-xl border border-destructive/30 bg-destructive/5 p-8">
        <div className="flex flex-col items-center justify-center text-center">
          <AlertTriangle className="h-10 w-10 text-destructive mb-3" />
          <h4 className="text-lg font-medium text-destructive mb-2">
            {t("metrics.error.title", "无法加载指标数据")}
          </h4>
          <p className="text-sm text-muted-foreground mb-4">
            {t("metrics.error.description", "请检查网络连接或稍后重试")}
          </p>
          <button
            onClick={() => refetch()}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-destructive/10 text-destructive hover:bg-destructive/20 transition-colors text-sm font-medium"
          >
            <RefreshCw className="h-4 w-4" />
            {t("common.retry", "重试")}
          </button>
        </div>
      </div>
    );
  }

  if (!metrics) return null;

  const maxCount = Math.max(...metrics.requests_by_hour.map((h) => h.count), 1);

  const statCards = [
    {
      label: t("metrics.totalRequests", "总请求数"),
      value: formatNumber(metrics.total_requests),
      icon: Activity,
      color: "text-blue-600 dark:text-blue-400",
      bg: "bg-blue-500/10",
    },
    {
      label: t("metrics.successRate", "成功率"),
      value: `${metrics.success_rate}%`,
      icon: CheckCircle2,
      color: "text-emerald-600 dark:text-emerald-400",
      bg: "bg-emerald-500/10",
      trend: (metrics.success_rate >= 95 ? "good" : metrics.success_rate >= 90 ? "warning" : "bad") as "good" | "warning" | "bad",
    },
    {
      label: t("metrics.avgLatency", "平均延迟"),
      value: `${metrics.avg_latency_ms}ms`,
      icon: Clock,
      color: "text-amber-600 dark:text-amber-400",
      bg: "bg-amber-500/10",
    },
    {
      label: t("metrics.activeServices", "活跃服务"),
      value: metrics.active_services.toString(),
      icon: Server,
      color: "text-violet-600 dark:text-violet-400",
      bg: "bg-violet-500/10",
    },
  ];

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <TrendingUp className="h-5 w-5 text-primary" />
          <h3 className="text-lg font-semibold tracking-tight">{t("metrics.title", "系统指标")}</h3>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-xs text-muted-foreground">
            {t("metrics.lastUpdated", "更新于")}: {formatTime(metrics.last_updated)}
          </span>
          <button
            onClick={() => refetch()}
            className="p-1.5 rounded-md hover:bg-muted transition-colors"
            title={t("common.refresh", "刷新")}
          >
            <RefreshCw className="h-4 w-4 text-muted-foreground" />
          </button>
        </div>
      </div>

      {/* Stats Grid */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        {statCards.map((stat, index) => (
          <StatCard key={index} {...stat} />
        ))}
      </div>

      {/* Chart */}
      <div className="rounded-xl border bg-card overflow-hidden">
        <div className="px-5 py-4 border-b bg-muted/30">
          <h4 className="text-sm font-medium">{t("metrics.hourlyTrend", "24小时请求趋势")}</h4>
        </div>
        <div className="p-5">
          <HourlyBarChart data={metrics.requests_by_hour} maxCount={maxCount} />
        </div>
      </div>
    </div>
  );
}

interface StatCardProps {
  label: string;
  value: string;
  icon: React.ComponentType<{ className?: string }>;
  color: string;
  bg: string;
  trend?: "good" | "warning" | "bad";
}

function StatCard({ label, value, icon: Icon, color, bg, trend }: StatCardProps) {
  return (
    <div className="relative overflow-hidden rounded-xl border bg-card p-5 transition-all hover:shadow-md hover:border-primary/20">
      {/* Background decoration */}
      <div className={`absolute -right-4 -top-4 h-20 w-20 rounded-full ${bg} opacity-50 blur-2xl`} />

      <div className="relative">
        <div className="flex items-center justify-between mb-3">
          <span className="text-sm font-medium text-muted-foreground">{label}</span>
          <div className={`p-2 rounded-lg ${bg}`}>
            <Icon className={`h-4 w-4 ${color}`} />
          </div>
        </div>
        <div className="flex items-end gap-2">
          <span className="text-2xl font-bold tracking-tight">{value}</span>
          {trend && (
            <span
              className={`text-xs px-1.5 py-0.5 rounded ${
                trend === "good"
                  ? "bg-emerald-500/10 text-emerald-600"
                  : trend === "warning"
                  ? "bg-amber-500/10 text-amber-600"
                  : "bg-red-500/10 text-red-600"
              }`}
            >
              {trend === "good" ? "Healthy" : trend === "warning" ? "Warning" : "Critical"}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

interface HourlyBarChartProps {
  data: HourlyMetric[];
  maxCount: number;
}

function HourlyBarChart({ data, maxCount }: HourlyBarChartProps) {
  return (
    <div className="flex items-end gap-1 h-40">
      {data.map((item, index) => {
        const heightPercent = (item.count / maxCount) * 100;
        const isHighlight = heightPercent > 70;

        return (
          <div
            key={index}
            className="flex-1 flex flex-col items-center gap-1 group"
          >
            {/* Bar */}
            <div className="w-full relative flex items-end justify-center h-32">
              <div
                className={`w-full rounded transition-all duration-300 ${
                  isHighlight
                    ? "bg-gradient-to-t from-primary to-primary/70"
                    : "bg-gradient-to-t from-primary/60 to-primary/30"
                } group-hover:from-primary group-hover:to-primary/80`}
                style={{ height: `${Math.max(heightPercent, 2)}%` }}
              />
              {/* Tooltip */}
              <div className="absolute -top-8 left-1/2 -translate-x-1/2 opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none z-10">
                <div className="bg-popover text-popover-foreground text-xs px-2 py-1 rounded shadow-lg whitespace-nowrap border">
                  {formatNumber(item.count)}
                </div>
              </div>
            </div>
            {/* Label - show every 3 hours */}
            {index % 3 === 0 && (
              <span className="text-[10px] text-muted-foreground font-medium">
                {item.hour.split(":")[0]}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

function formatNumber(num: number): string {
  if (num >= 1000000) {
    return (num / 1000000).toFixed(1) + "M";
  }
  if (num >= 1000) {
    return (num / 1000).toFixed(1) + "K";
  }
  return num.toString();
}

function formatTime(isoString: string): string {
  try {
    const date = new Date(isoString);
    return date.toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "--:--";
  }
}
