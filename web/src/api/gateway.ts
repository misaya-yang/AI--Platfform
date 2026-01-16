import { api } from "@/lib/api";
import type {
  HealthStatus,
  ServiceDefinition,
  ServiceDetail,
  Task,
  UnifiedRequest,
  UnifiedResponse,
} from "@/types/gateway";

export async function listServices() {
  const { data } = await api.get<ServiceDefinition[]>("/api/v1/services");
  return data;
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
