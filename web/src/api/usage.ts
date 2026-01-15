/**
 * Usage API - 用量统计接口
 *
 * 提供按服务、模型、用户、助手分类的用量分析
 */
import { api } from "@/lib/api";

// ============ Types ============

export interface UsageSummary {
  total_requests: number;
  success_rate: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
  total_cost_usd: number;
  avg_latency_ms: number;
  start_date: string;
  end_date: string;
}

export interface UsageBreakdownItem {
  model?: string;
  user?: string;
  assistant?: string;
  service?: string;
  requests: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  percentage: number;
}

export interface UsageBreakdownResponse {
  dimension: string;
  items: UsageBreakdownItem[];
  start_date: string;
  end_date: string;
  total_cost_usd: number;
}

export interface UsageTimeSeriesPoint {
  date: string;
  requests: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  avg_latency_ms: number;
}

export interface UsageTimeSeriesResponse {
  data: UsageTimeSeriesPoint[];
  start_date: string;
  end_date: string;
  granularity: string;
}

// ============ API Functions ============

/**
 * 获取用量摘要
 */
export async function getUsageSummary(params?: {
  start_date?: string;
  end_date?: string;
  user_id?: string;
  model?: string;
  service_id?: string;
  assistant_id?: string;
}): Promise<UsageSummary> {
  const searchParams = new URLSearchParams();
  if (params?.start_date) searchParams.set("start_date", params.start_date);
  if (params?.end_date) searchParams.set("end_date", params.end_date);
  if (params?.user_id) searchParams.set("user_id", params.user_id);
  if (params?.model) searchParams.set("model", params.model);
  if (params?.service_id) searchParams.set("service_id", params.service_id);
  if (params?.assistant_id) searchParams.set("assistant_id", params.assistant_id);

  const query = searchParams.toString();
  const response = await api.get<UsageSummary>(`/api/v1/usage/summary${query ? `?${query}` : ""}`);
  return response.data;
}

/**
 * 获取用量分解 - 按维度分类
 */
export async function getUsageBreakdown(params: {
  dimension: "model" | "user" | "assistant" | "service";
  start_date?: string;
  end_date?: string;
  limit?: number;
}): Promise<UsageBreakdownResponse> {
  const searchParams = new URLSearchParams();
  searchParams.set("dimension", params.dimension);
  if (params.start_date) searchParams.set("start_date", params.start_date);
  if (params.end_date) searchParams.set("end_date", params.end_date);
  if (params.limit) searchParams.set("limit", params.limit.toString());

  const response = await api.get<UsageBreakdownResponse>(`/api/v1/usage/breakdown?${searchParams}`);
  return response.data;
}

/**
 * 获取用量时间序列
 */
export async function getUsageTimeSeries(params?: {
  start_date?: string;
  end_date?: string;
  user_id?: string;
  model?: string;
  service_id?: string;
}): Promise<UsageTimeSeriesResponse> {
  const searchParams = new URLSearchParams();
  if (params?.start_date) searchParams.set("start_date", params.start_date);
  if (params?.end_date) searchParams.set("end_date", params.end_date);
  if (params?.user_id) searchParams.set("user_id", params.user_id);
  if (params?.model) searchParams.set("model", params.model);
  if (params?.service_id) searchParams.set("service_id", params.service_id);

  const query = searchParams.toString();
  const response = await api.get<UsageTimeSeriesResponse>(`/api/v1/usage/timeseries${query ? `?${query}` : ""}`);
  return response.data;
}
