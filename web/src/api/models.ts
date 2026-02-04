/**
 * LLM Models API client.
 *
 * Provides CRUD operations for managing LLM models.
 */

import { api } from "@/lib/api";
import i18n from "@/i18n";

// =============================================================================
// Types
// =============================================================================

export type ModelAccessLevel = "public" | "premium" | "admin";

export interface LLMModel {
  model_id: string;
  tenant_id: string;
  provider_id: string;
  display_name: string;
  context_window: number;
  max_output_tokens: number;
  supports_vision: boolean;
  supports_tools: boolean;
  input_price_per_1k: number;
  output_price_per_1k: number;
  access_level: ModelAccessLevel;
  is_enabled: boolean;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface ModelCreate {
  model_id: string;
  provider_id: string;
  display_name: string;
  context_window?: number;
  max_output_tokens?: number;
  supports_vision?: boolean;
  supports_tools?: boolean;
  input_price_per_1k?: number;
  output_price_per_1k?: number;
  access_level?: ModelAccessLevel;
  is_enabled?: boolean;
  sort_order?: number;
}

export interface ModelUpdate {
  display_name?: string;
  context_window?: number;
  max_output_tokens?: number;
  supports_vision?: boolean;
  supports_tools?: boolean;
  input_price_per_1k?: number;
  output_price_per_1k?: number;
  access_level?: ModelAccessLevel;
  is_enabled?: boolean;
  sort_order?: number;
}

// =============================================================================
// API Functions
// =============================================================================

/**
 * List all models.
 */
export async function listModels(
  providerId?: string,
  includeDisabled = false
): Promise<LLMModel[]> {
  let url = `/api/v1/models?include_disabled=${includeDisabled}`;
  if (providerId) {
    url += `&provider_id=${providerId}`;
  }
  const { data } = await api.get<LLMModel[]>(url);
  return data;
}

/**
 * Get a specific model.
 */
export async function getModel(modelId: string): Promise<LLMModel> {
  const { data } = await api.get<LLMModel>(`/api/v1/models/${modelId}`);
  return data;
}

/**
 * Create a new model.
 */
export async function createModel(model: ModelCreate): Promise<LLMModel> {
  const { data } = await api.post<LLMModel>("/api/v1/models", model);
  return data;
}

/**
 * Update a model.
 */
export async function updateModel(
  modelId: string,
  updates: ModelUpdate
): Promise<LLMModel> {
  const { data } = await api.put<LLMModel>(`/api/v1/models/${modelId}`, updates);
  return data;
}

/**
 * Delete a model.
 */
export async function deleteModel(modelId: string): Promise<void> {
  await api.delete(`/api/v1/models/${modelId}`);
}

/**
 * Toggle model enabled state.
 */
export async function toggleModel(
  modelId: string,
  isEnabled: boolean
): Promise<LLMModel> {
  const { data } = await api.patch<LLMModel>(
    `/api/v1/models/${modelId}/toggle?is_enabled=${isEnabled}`
  );
  return data;
}

// =============================================================================
// Helper Functions
// =============================================================================

/**
 * Get access level display name.
 */
export function getAccessLevelDisplayName(level: ModelAccessLevel): string {
  const names: Record<ModelAccessLevel, string> = {
    public: i18n.t("models.access.public"),
    premium: i18n.t("models.access.premium"),
    admin: i18n.t("models.access.admin"),
  };
  return names[level] || level;
}

/**
 * Get access level badge color.
 */
export function getAccessLevelColor(level: ModelAccessLevel): string {
  const colors: Record<ModelAccessLevel, string> = {
    public: "green",
    premium: "blue",
    admin: "orange",
  };
  return colors[level] || "default";
}

/**
 * Format price for display.
 * Handles string, number, null, and undefined inputs safely.
 */
export function formatPrice(price: number | string | null | undefined): string {
  // Handle null/undefined
  if (price === null || price === undefined) return "-";

  // Convert string to number if needed (PostgreSQL DECIMAL returns as string)
  const numPrice = typeof price === "string" ? parseFloat(price) : price;

  // Handle NaN or invalid values
  if (isNaN(numPrice)) return "-";

  if (numPrice === 0) return i18n.t("models.price.free");
  if (numPrice < 0.001) return `$${numPrice.toFixed(6)}`;
  if (numPrice < 0.01) return `$${numPrice.toFixed(5)}`;
  return `$${numPrice.toFixed(4)}`;
}

/**
 * Format context window for display.
 * Handles string, number, null, and undefined inputs safely.
 */
export function formatContextWindow(tokens: number | string | null | undefined): string {
  // Handle null/undefined
  if (tokens === null || tokens === undefined) return "-";

  // Convert string to number if needed
  const numTokens = typeof tokens === "string" ? parseInt(tokens, 10) : tokens;

  // Handle NaN or invalid values
  if (isNaN(numTokens)) return "-";

  if (numTokens >= 1000000) return `${(numTokens / 1000000).toFixed(1)}M`;
  if (numTokens >= 1000) return `${(numTokens / 1000).toFixed(0)}K`;
  return numTokens.toString();
}

/**
 * Group models by provider.
 */
export function groupModelsByProvider(models: LLMModel[]): Record<string, LLMModel[]> {
  const grouped: Record<string, LLMModel[]> = {};
  for (const model of models) {
    if (!grouped[model.provider_id]) {
      grouped[model.provider_id] = [];
    }
    grouped[model.provider_id].push(model);
  }
  return grouped;
}
