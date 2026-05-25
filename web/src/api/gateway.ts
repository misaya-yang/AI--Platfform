import { api } from "@/lib/api";
import type {
  HealthStatus,
  ServiceDefinition,
  ServiceDetail,
  ServiceModelOverride,
  ServiceUiPreferences,
  Task,
  UnifiedRequest,
  UnifiedResponse,
} from "@/types/gateway";

export async function listServices() {
  const { data } = await api.get<ServiceDefinition[]>("/api/v1/services");
  return data;
}

export interface ProxyServiceSummary {
  service_id: string;
  name: string;
  enabled: boolean;
  service_type?: string;
  upstream_url?: string | null;
  graph_id?: string | null;
  assistant_id?: string | null;
  metadata?: {
    ui_preferences?: ServiceUiPreferences;
    adapter_type?: string;
    proxy_mode?: string;
    model_override?: ServiceModelOverride;
  };
}

export async function listProxyServices(): Promise<ProxyServiceSummary[]> {
  const { data } = await api.get<{
    services: Array<{
      service_id: string;
      service_name: string;
      enabled: boolean;
      service_type?: string;
      upstream_url?: string | null;
      graph_id?: string | null;
      assistant_id?: string | null;
      metadata?: {
        ui_preferences?: ServiceUiPreferences;
        adapter_type?: string;
        proxy_mode?: string;
        model_override?: ServiceModelOverride;
      };
    }>;
  }>("/api/v1/proxy");

  return (data.services || []).map((service) => ({
    service_id: service.service_id,
    name: service.service_name || service.service_id,
    enabled: Boolean(service.enabled),
    service_type: service.service_type || "proxy",
    upstream_url: service.upstream_url ?? null,
    graph_id: service.graph_id ?? null,
    assistant_id: service.assistant_id ?? null,
    metadata: service.metadata || {},
  }));
}

export async function getHealth() {
  const { data } = await api.get<Record<string, HealthStatus>>(
    "/api/v1/health/services"
  );
  return data;
}

export async function registerService(definition: Record<string, unknown>) {
  const { data } = await api.post("/api/v1/services", definition);
  return data;
}

export async function getService(serviceId: string) {
  const { data } = await api.get<ServiceDetail>(`/api/v1/services/${serviceId}`);
  return data;
}

export async function updateService(serviceId: string, patch: Record<string, unknown>) {
  const { data } = await api.put(`/api/v1/services/${serviceId}`, patch);
  return data;
}

export async function deleteService(serviceId: string) {
  const { data } = await api.delete(`/api/v1/services/${serviceId}`);
  return data;
}

export async function invokeService(req: UnifiedRequest) {
  const { data } = await api.post<UnifiedResponse>("/api/v1/invoke", req);
  return data;
}

export async function submitService(req: UnifiedRequest) {
  const { data } = await api.post<UnifiedResponse>("/api/v1/submit", req);
  return data;
}

export async function getTask(taskId: string) {
  const { data } = await api.get<Task>(`/api/v1/tasks/${taskId}`);
  return data;
}

export async function getTaskResult(taskId: string) {
  const { data } = await api.get<UnifiedResponse>(
    `/api/v1/tasks/${taskId}/result`
  );
  return data;
}

// 供应商状态类型
export interface ProviderStatus {
  name: string;
  status: "configured" | "not_configured";
  configured: boolean;
  model_count: number;
  last_check: string;
}

export async function getProvidersHealth() {
  const { data } = await api.get<Record<string, ProviderStatus>>(
    "/api/v1/health/providers"
  );
  return data;
}
