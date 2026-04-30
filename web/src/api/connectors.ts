/**
 * Connectors API — OAuth-based third-party integrations.
 */

import { api } from "@/lib/api";

export interface ConnectorInfo {
  provider: string;
  display_name: string;
  description: string | null;
  icon_url: string | null;
  enabled: boolean;
  connected: boolean;
  status: string | null;
}

export interface ConnectorSearchResult {
  id: string;
  title: string;
  excerpt: string;
  url?: string;
  type: string;
  [key: string]: unknown;
}

export interface ConnectedConnector {
  provider: string;
  display_name?: string;
  status?: string | null;
  connected_at?: string | null;
  metadata?: Record<string, unknown> | null;
}

export async function listAvailableConnectors(): Promise<ConnectorInfo[]> {
  const { data } = await api.get<ConnectorInfo[]>("/api/v1/connectors/available");
  return data;
}

export async function listMyConnectors(): Promise<ConnectedConnector[]> {
  const { data } = await api.get<ConnectedConnector[]>("/api/v1/connectors/mine");
  return data;
}

export async function initiateOAuth(provider: string): Promise<{ auth_url: string; state: string }> {
  const { data } = await api.get<{ auth_url: string; state: string }>(
    `/api/v1/connectors/auth/${provider}`
  );
  return data;
}

export async function disconnectConnector(provider: string): Promise<void> {
  await api.delete(`/api/v1/connectors/${provider}`);
}

export async function searchConnector(
  provider: string,
  query: string,
  limit = 10
): Promise<ConnectorSearchResult[]> {
  const { data } = await api.post<ConnectorSearchResult[]>(
    `/api/v1/connectors/${provider}/search`,
    { query, limit }
  );
  return data;
}
