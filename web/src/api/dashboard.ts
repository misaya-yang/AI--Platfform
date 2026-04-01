/**
 * Dashboard API - LangSmith-style Enterprise Monitoring
 *
 * Provides:
 * - Real-time metrics fetching
 * - WebSocket connection for live updates
 * - Time-series data queries
 * - User-specific metrics
 */

import { api } from "@/lib/api";

// ============ Types ============

export interface LatencyMetrics {
  p50: number;
  p95: number;
  p99: number;
  avg: number;
}

export interface ErrorMetrics {
  rate: number;
  rate_4xx: number;
  rate_5xx: number;
}

export interface UserMetrics {
  active: number;
  threads_total: number;
  threads_by_user: Record<string, number>;
}

export interface CapacityMetrics {
  queue_depth: number;
  concurrent: number;
  max_concurrent: number;
  utilization: number;
}

export interface TokenMetrics {
  total: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  per_minute: number;
}

export interface RunMetrics {
  total: number;
  success_rate: number;
  avg_duration_ms: number;
}

export interface RealtimeDashboard {
  rps: number;
  rps_1m: number;
  rps_5m: number;
  latency: LatencyMetrics;
  errors: ErrorMetrics;
  users: UserMetrics;
  capacity: CapacityMetrics;
  tokens: TokenMetrics;
  runs: RunMetrics;
  timestamp: string;
  is_live: boolean;
}

export interface AlertStatus {
  name: string;
  level: "ok" | "warning" | "critical";
  message: string;
  threshold: number;
  current_value: number;
  triggered_at?: string;
}

export interface AlertsResponse {
  alerts: AlertStatus[];
  last_check: string;
}

export interface TimeSeriesDataPoint {
  timestamp: string;
  value: number;
  label?: string;
}

export interface TimeSeriesResponse {
  metric: string;
  granularity: string;
  start: string;
  end: string;
  data: TimeSeriesDataPoint[];
}

export interface HourlyMetric {
  hour: string;
  count: number;
}

export interface DashboardSummary {
  period: string;
  overview: {
    total_requests: number;
    success_rate: number;
    avg_latency_ms: number;
    total_tokens: number;
    estimated_cost_usd: number;
    total_runs: number;
  };
  realtime: {
    rps: number;
    active_users: number;
    concurrent_requests: number;
    queue_depth: number;
  };
  latency: {
    p50: number;
    p95: number;
    p99: number;
  };
  hourly_trend: HourlyMetric[];
  timestamp: string;
}

export interface UserDashboard {
  user_id: string;
  tokens: TokenMetrics;
  active_threads: number;
  requests_today: number;
  avg_latency_ms: number;
  error_rate: number;
  timestamp: string;
}

export interface UsageBreakdown {
  dimension_value: string;
  requests: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  percentage: number;
}

// ============ WebSocket Types ============

export interface WebSocketMessage {
  type: "metrics" | "pong" | "error";
  rps?: number;
  rps_1m?: number;
  rps_5m?: number;
  latency?: LatencyMetrics;
  errors?: ErrorMetrics;
  users?: UserMetrics;
  capacity?: CapacityMetrics;
  tokens?: TokenMetrics;
  runs?: RunMetrics;
  alerts?: AlertStatus[];
  timestamp?: string;
}

// ============ API Functions ============

/**
 * Get real-time dashboard metrics
 */
export async function getRealtimeDashboard(): Promise<RealtimeDashboard> {
  const response = await api.get<RealtimeDashboard>(
    "/api/v1/dashboard/realtime"
  );
  return response.data;
}

/**
 * Get dashboard summary
 */
export async function getDashboardSummary(
  period: "today" | "week" | "month" = "today"
): Promise<DashboardSummary> {
  const response = await api.get<DashboardSummary>(
    `/api/v1/dashboard/summary?period=${period}`
  );
  return response.data;
}

/**
 * Get time-series data for a metric
 */
export async function getTimeSeriesData(
  metric: string,
  start?: Date,
  end?: Date,
  granularity: string = "hour",
  userId?: string
): Promise<TimeSeriesResponse> {
  const params = new URLSearchParams({ granularity });

  if (start) {
    params.set("start", start.toISOString());
  }
  if (end) {
    params.set("end", end.toISOString());
  }
  if (userId) {
    params.set("user_id", userId);
  }

  const response = await api.get<TimeSeriesResponse>(
    `/api/v1/dashboard/timeseries/${metric}?${params}`
  );
  return response.data;
}

/**
 * Get current alerts
 */
export async function getAlerts(): Promise<AlertsResponse> {
  const response = await api.get<AlertsResponse>("/api/v1/dashboard/alerts");
  return response.data;
}

/**
 * Get user-specific dashboard
 */
export async function getUserDashboard(userId: string): Promise<UserDashboard> {
  const response = await api.get<UserDashboard>(
    `/api/v1/dashboard/user/${userId}`
  );
  return response.data;
}

/**
 * Get usage breakdown by dimension
 */
export async function getUsageBreakdown(
  dimension: "model" | "user" | "service" | "provider",
  start?: Date,
  end?: Date,
  limit: number = 10
): Promise<UsageBreakdown[]> {
  const params = new URLSearchParams({ dimension, limit: limit.toString() });

  if (start) {
    params.set("start", start.toISOString());
  }
  if (end) {
    params.set("end", end.toISOString());
  }

  const response = await api.get<UsageBreakdown[]>(
    `/api/v1/dashboard/breakdown?${params}`
  );
  return response.data;
}

// ============ WebSocket Connection ============

export type DashboardWebSocketCallback = (data: WebSocketMessage) => void;

export class DashboardWebSocket {
  private ws: WebSocket | null = null;
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 20;
  private reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
  private callbacks: Set<DashboardWebSocketCallback> = new Set();
  private url: string;

  constructor(baseUrl?: string) {
    const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = baseUrl || window.location.host;
    this.url = `${wsProtocol}//${host}/api/v1/dashboard/ws`;
  }

  connect(): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      return;
    }

    try {
      this.ws = new WebSocket(this.url);

      this.ws.onopen = () => {
        console.log("[Dashboard WS] Connected");
        this.reconnectAttempts = 0;
      };

      this.ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data) as WebSocketMessage;
          this.callbacks.forEach((callback) => callback(data));
        } catch (e) {
          console.error("[Dashboard WS] Parse error:", e);
        }
      };

      this.ws.onclose = () => {
        console.log("[Dashboard WS] Disconnected");
        this.scheduleReconnect();
      };

      this.ws.onerror = (error) => {
        console.error("[Dashboard WS] Error:", error);
      };
    } catch (e) {
      console.error("[Dashboard WS] Connection error:", e);
      this.scheduleReconnect();
    }
  }

  disconnect(): void {
    if (this.reconnectTimeout) {
      clearTimeout(this.reconnectTimeout);
      this.reconnectTimeout = null;
    }

    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }

    this.callbacks.clear();
  }

  subscribe(callback: DashboardWebSocketCallback): () => void {
    this.callbacks.add(callback);

    // Auto-connect on first subscription
    if (this.callbacks.size === 1) {
      this.connect();
    }

    // Return unsubscribe function
    return () => {
      this.callbacks.delete(callback);

      // Auto-disconnect when no subscribers
      if (this.callbacks.size === 0) {
        this.disconnect();
      }
    };
  }

  sendPing(): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send("ping");
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.log("[Dashboard WS] Max reconnect attempts reached");
      return;
    }

    // Exponential backoff with jitter to prevent thundering herd
    const base = Math.min(1000 * Math.pow(2, this.reconnectAttempts), 30000);
    const delay = base + Math.random() * 1000;
    this.reconnectAttempts++;

    console.log(`[Dashboard WS] Reconnecting in ${delay}ms...`);

    this.reconnectTimeout = setTimeout(() => {
      this.connect();
    }, delay);
  }
}

// Global WebSocket instance
let dashboardWs: DashboardWebSocket | null = null;

export function getDashboardWebSocket(): DashboardWebSocket {
  if (!dashboardWs) {
    dashboardWs = new DashboardWebSocket();
  }
  return dashboardWs;
}
