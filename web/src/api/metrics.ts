/**
 * Metrics API - 系统指标统计接口
 *
 * Provides dashboard metrics including:
 * - Request metrics (count, success rate, latency)
 * - Token consumption (input/output tokens, cost)
 * - LangGraph run metrics (executions, success rate)
 * - Time-series data with custom date range support
 */
import { api } from "@/lib/api";

// ============ Types ============

export interface HourlyMetric {
  hour: string;
  count: number;
}

export interface MetricsSummary {
  // Basic request metrics
  total_requests: number;
  success_rate: number;
  avg_latency_ms: number;
  active_services: number;
  requests_by_hour: HourlyMetric[];

  // Latency percentiles
  latency_p50: number;
  latency_p95: number;
  latency_p99: number;

  // Token consumption
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  estimated_cost_usd: number;

  // LangGraph Run metrics
  total_runs: number;
  run_success_rate: number;
  avg_run_duration_ms: number;

  // Metadata
  last_updated: string;
  is_simulated: boolean;
}

export interface TimeSeriesPoint {
  timestamp: string;
  value: number;
}

export interface TimeSeriesResponse {
  metric: string;
  granularity: string;
  start: string;
  end: string;
  data: TimeSeriesPoint[];
}

export interface TokenUsagePeriod {
  period: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
}

export interface TokenUsageResponse {
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
  by_period: TokenUsagePeriod[];
}

export interface BreakdownItem {
  name: string;
  count: number;
  percentage: number;
}

export interface BreakdownResponse {
  dimension: string;
  items: BreakdownItem[];
}

export interface SecurityEventBreakdownItem {
  name: string;
  count: number;
  percentage: number;
}

export interface SecurityEventBreakdownResponse {
  dimension: "user" | "service";
  event_type: "auth_failed" | "rate_limited";
  items: SecurityEventBreakdownItem[];
  start_date: string;
  end_date: string;
  data_status: string;
  data_freshness_minutes: number;
  last_ingested_at?: string | null;
}

export interface SecurityEventTimeSeriesPoint {
  date: string;
  count: number;
}

export interface SecurityEventTimeSeriesResponse {
  dimension: "user" | "service";
  event_type: "auth_failed" | "rate_limited";
  data: SecurityEventTimeSeriesPoint[];
  start_date: string;
  end_date: string;
  data_status: string;
  data_freshness_minutes: number;
  last_ingested_at?: string | null;
}

// ============ API Functions ============

/**
 * 获取系统指标摘要
 */
export async function getMetricsSummary(): Promise<MetricsSummary> {
  const response = await api.get<MetricsSummary>("/api/v1/metrics/summary");
  return response.data;
}

/**
 * 获取时间序列数据
 */
export async function getMetricsTimeSeries(
  metric: string,
  start: Date,
  end: Date,
  granularity: string = "hour"
): Promise<TimeSeriesResponse> {
  const params = new URLSearchParams({
    metric,
    start: start.toISOString(),
    end: end.toISOString(),
    granularity,
  });
  const response = await api.get<TimeSeriesResponse>(
    `/api/v1/metrics/timeseries?${params}`
  );
  return response.data;
}

/**
 * 获取 Token 使用统计
 */
export async function getTokenUsage(
  startDate?: Date,
  endDate?: Date
): Promise<TokenUsageResponse> {
  const params = new URLSearchParams();
  if (startDate) {
    params.set("start_date", startDate.toISOString().split("T")[0]);
  }
  if (endDate) {
    params.set("end_date", endDate.toISOString().split("T")[0]);
  }
  const response = await api.get<TokenUsageResponse>(
    `/api/v1/metrics/tokens?${params}`
  );
  return response.data;
}

/**
 * 获取指标分解
 */
export async function getMetricsBreakdown(
  dimension: string,
  limit: number = 10,
  dateStr?: string
): Promise<BreakdownResponse> {
  const params = new URLSearchParams({
    dimension,
    limit: limit.toString(),
  });
  if (dateStr) {
    params.set("date_str", dateStr);
  }
  const response = await api.get<BreakdownResponse>(
    `/api/v1/metrics/breakdown?${params}`
  );
  return response.data;
}

export async function getSecurityEventBreakdown(params: {
  dimension: "user" | "service";
  event_type: "auth_failed" | "rate_limited";
  start_date?: string;
  end_date?: string;
  limit?: number;
}): Promise<SecurityEventBreakdownResponse> {
  const searchParams = new URLSearchParams();
  searchParams.set("dimension", params.dimension);
  searchParams.set("event_type", params.event_type);
  if (params.start_date) searchParams.set("start_date", params.start_date);
  if (params.end_date) searchParams.set("end_date", params.end_date);
  if (params.limit) searchParams.set("limit", params.limit.toString());

  const response = await api.get<SecurityEventBreakdownResponse>(
    `/api/v1/metrics/security/breakdown?${searchParams.toString()}`
  );
  return response.data;
}

export async function getSecurityEventTimeSeries(params: {
  dimension: "user" | "service";
  event_type: "auth_failed" | "rate_limited";
  start_date?: string;
  end_date?: string;
  user_id?: string;
  service_id?: string;
}): Promise<SecurityEventTimeSeriesResponse> {
  const searchParams = new URLSearchParams();
  searchParams.set("dimension", params.dimension);
  searchParams.set("event_type", params.event_type);
  if (params.start_date) searchParams.set("start_date", params.start_date);
  if (params.end_date) searchParams.set("end_date", params.end_date);
  if (params.user_id) searchParams.set("user_id", params.user_id);
  if (params.service_id) searchParams.set("service_id", params.service_id);

  const response = await api.get<SecurityEventTimeSeriesResponse>(
    `/api/v1/metrics/security/timeseries?${searchParams.toString()}`
  );
  return response.data;
}
