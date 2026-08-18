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
    if (!userId) {
      return window.localStorage.getItem(LAST_MODEL_STORAGE_PREFIX) ?? "";
    }

    const scopedKey = lastModelStorageKey(userId);
    const scoped = window.localStorage.getItem(scopedKey);
    if (scoped) return scoped;

    // One-time migration from the pre-user-scoped cache. Moving (rather than
    // repeatedly falling back to) the legacy value prevents the next account
    // using this browser from inheriting another user's model selection.
    const legacy = window.localStorage.getItem(LAST_MODEL_STORAGE_PREFIX) ?? "";
    if (legacy) {
      window.localStorage.setItem(scopedKey, legacy);
      window.localStorage.removeItem(LAST_MODEL_STORAGE_PREFIX);
    }
    return legacy;
  } catch {
    return "";
  }
}

export function readHydratedLastModelId(
  hydrated: boolean,
  userId?: string,
): string {
  return hydrated ? readLastModelId(userId) : "";
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
