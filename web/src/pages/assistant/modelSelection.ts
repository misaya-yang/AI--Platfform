import type { ModelInfo } from "@/api/assistant";

export const PRIMARY_ASSISTANT_MODEL_ID = "qwen3.8-flash";

// The Assistant catalog already excludes disabled models. A model is usable
// in the composer only when its provider is configured for this tenant too.
export function activeAssistantModels(
  models: ModelInfo[],
  availableProviders: string[],
): ModelInfo[] {
  const activeProviders = new Set(availableProviders);
  return models.filter((model) => activeProviders.has(model.provider));
}

export function preferredAssistantModelId(
  models: ModelInfo[],
  serverDefaultId: string,
  userChoice = "",
): string {
  if (userChoice && models.some((model) => model.id === userChoice)) return userChoice;
  return models.find((model) => model.id === PRIMARY_ASSISTANT_MODEL_ID)?.id
    ?? models.find((model) => model.id === serverDefaultId)?.id
    ?? models[0]?.id
    ?? "";
}
