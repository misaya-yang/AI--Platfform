/**
 * LLM Providers API client.
 *
 * Provides CRUD operations for managing LLM providers.
 */

import { api } from "@/lib/api";

// =============================================================================
// Types
// =============================================================================

export interface Provider {
  provider_id: string;
  tenant_id: string;
  display_name: string;
  api_type: string;
  base_url?: string | null;
  has_api_key: boolean;
  is_enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface ProviderCreate {
  provider_id: string;
  display_name: string;
  api_type?: string;
  base_url?: string;
  api_key?: string;
  is_enabled?: boolean;
}

export interface ProviderUpdate {
  display_name?: string;
  api_type?: string;
  base_url?: string;
  api_key?: string;
  is_enabled?: boolean;
}

export interface ProviderTestResult {
  success: boolean;
  message: string;
  latency_ms?: number;
}

// =============================================================================
// API Functions
// =============================================================================

/**
 * List all providers.
 */
export async function listProviders(includeDisabled = false): Promise<Provider[]> {
  const { data } = await api.get<Provider[]>(
    `/api/v1/providers?include_disabled=${includeDisabled}`
  );
  return data;
}

/**
 * Get a specific provider.
 */
export async function getProvider(providerId: string): Promise<Provider> {
  const { data } = await api.get<Provider>(`/api/v1/providers/${providerId}`);
  return data;
}

/**
 * Create a new provider.
 */
export async function createProvider(provider: ProviderCreate): Promise<Provider> {
  const { data } = await api.post<Provider>("/api/v1/providers", provider);
  return data;
}

/**
 * Update a provider.
 */
export async function updateProvider(
  providerId: string,
  updates: ProviderUpdate
): Promise<Provider> {
  const { data } = await api.put<Provider>(`/api/v1/providers/${providerId}`, updates);
  return data;
}

/**
 * Delete a provider.
 */
export async function deleteProvider(providerId: string): Promise<void> {
  await api.delete(`/api/v1/providers/${providerId}`);
}

/**
 * Test provider API connection.
 */
export async function testProviderConnection(providerId: string): Promise<ProviderTestResult> {
  const { data } = await api.post<ProviderTestResult>(
    `/api/v1/providers/${providerId}/test`
  );
  return data;
}

// =============================================================================
// Helper Functions
// =============================================================================

/**
 * Get API type display name.
 */
export function getApiTypeDisplayName(apiType: string): string {
  const names: Record<string, string> = {
    openai: "OpenAI Compatible",
    anthropic: "Anthropic",
    google: "Google Gemini",
  };
  return names[apiType] || apiType;
}

/**
 * Get default base URL for API type.
 */
export function getDefaultBaseUrl(apiType: string): string {
  const urls: Record<string, string> = {
    openai: "https://api.openai.com",
    anthropic: "https://api.anthropic.com",
    google: "https://generativelanguage.googleapis.com",
  };
  return urls[apiType] || "";
}
