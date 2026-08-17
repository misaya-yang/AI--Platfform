/**
 * Last-selected model cache.
 *
 * The composer unlocks with the last model the user picked instead of waiting
 * for `listModels` / `getConfig` on every page load. The catalog still
 * validates the cached id once it arrives (see AssistantPage loadData).
 */

const LAST_MODEL_STORAGE_KEY = "assistant.lastModelId.v1";

export function readLastModelId(): string {
  try {
    return window.localStorage.getItem(LAST_MODEL_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function writeLastModelId(modelId: string): void {
  if (!modelId) return;
  try {
    window.localStorage.setItem(LAST_MODEL_STORAGE_KEY, modelId);
  } catch {
    // Storage unavailable (private mode / quota) — cache is best-effort only.
  }
}
