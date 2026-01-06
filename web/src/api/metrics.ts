/**
 * Metrics API - 系统指标统计接口
 */
import { api } from "@/lib/api";

export interface HourlyMetric {
  hour: string;
  count: number;
}

export interface MetricsSummary {
  total_requests: number;
  success_rate: number;
  avg_latency_ms: number;
  active_services: number;
  requests_by_hour: HourlyMetric[];
  last_updated: string;
}

/**
 * 获取系统指标摘要
 */
export async function getMetricsSummary(): Promise<MetricsSummary> {
  const response = await api.get<MetricsSummary>("/api/v1/metrics/summary");
  return response.data;
}
