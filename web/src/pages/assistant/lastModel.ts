/**
 * Last-selected model cache.
 *
 * The composer unlocks with the last model the user picked instead of waiting
 * for `listModels` / `getConfig` on every page load. The catalog still
 * validates the cached id once it arrives (see AssistantPage loadData).
 */

const LAST_MODEL_STORAGE_PREFIX = "assistant.lastModelId.v1";

function lastModelStorageKey(userId?: string): string {
  return userId ? `${LAST_MODEL_STORAGE_PREFIX}:${userId}` : LAST_MODEL_STORAGE_PREFIX;
}

export function readLastModelId(userId?: string): string {
  try {
    const scoped = userId ? window.localStorage.getItem(lastModelStorageKey(userId)) : null;
    if (scoped) return scoped;
    return window.localStorage.getItem(LAST_MODEL_STORAGE_PREFIX) ?? "";
  } catch {
    return "";
  }
}

export function writeLastModelId(modelId: string, userId?: string): void {
  if (!modelId) return;
  try {
    window.localStorage.setItem(lastModelStorageKey(userId), modelId);
  } catch {
    // Storage unavailable (private mode / quota) — cache is best-effort only.
  }
}

export function clearLastModelId(): void {
  try {
    const keys: string[] = [];
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const key = window.localStorage.key(i);
      if (key && (key === LAST_MODEL_STORAGE_PREFIX || key.startsWith(`${LAST_MODEL_STORAGE_PREFIX}:`))) {
        keys.push(key);
      }
    }
    keys.forEach((key) => window.localStorage.removeItem(key));
  } catch {
    // Storage unavailable — nothing to clear.
  }
}
