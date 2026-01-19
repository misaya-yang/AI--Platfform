import axios, { AxiosError } from "axios";
import { toast } from "@/hooks/use-toast";

const baseURL = import.meta.env.VITE_API_BASE_URL || "";
const AUTH_STORAGE_KEY = "agent-gateway-auth";

// Rate limit state tracking to avoid toast spam
let lastRateLimitToast = 0;
const RATE_LIMIT_TOAST_COOLDOWN = 5000; // Only show toast once per 5 seconds

export const api = axios.create({
  baseURL,
});

/**
 * Get the API base URL for external references (e.g., code examples).
 * Uses configured VITE_API_BASE_URL or falls back to current origin.
 */
export function getApiBaseUrl(): string {
  return baseURL || window.location.origin;
}

/**
 * Get auth data from storage (checks both localStorage and sessionStorage)
 * localStorage is used when rememberMe=true, sessionStorage when rememberMe=false
 */
function getAuthFromStorage(): { token: string | null } {
  // First check localStorage (for rememberMe=true)
  let authStorage = localStorage.getItem(AUTH_STORAGE_KEY);

  // If not in localStorage, check sessionStorage (for rememberMe=false)
  if (!authStorage) {
    authStorage = sessionStorage.getItem(AUTH_STORAGE_KEY);
  }

  if (authStorage) {
    try {
      const authState = JSON.parse(authStorage);
      return { token: authState?.state?.token || null };
    } catch {
      // Ignore parse errors
    }
  }

  return { token: null };
}

/**
 * Clear auth from both storages
 */
function clearAuthStorage(): void {
  localStorage.removeItem(AUTH_STORAGE_KEY);
  sessionStorage.removeItem(AUTH_STORAGE_KEY);
}

// Request interceptor to add JWT token
api.interceptors.request.use(
  (config) => {
    const { token } = getAuthFromStorage();
    if (token && config.headers) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// Response interceptor to handle auth and rate limit errors
api.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    if (error.response?.status === 401) {
      // Clear auth from BOTH storages on 401
      clearAuthStorage();
      // Redirect to login if not already there
      if (!window.location.pathname.includes("/login")) {
        window.location.href = "/login";
      }
    }

    // Handle rate limit errors gracefully
    if (error.response?.status === 429) {
      const now = Date.now();
      // Only show toast if cooldown has passed (prevent toast spam)
      if (now - lastRateLimitToast > RATE_LIMIT_TOAST_COOLDOWN) {
        lastRateLimitToast = now;

        // Extract retry info from response
        const retryAfter = error.response.headers["retry-after"];
        const errorData = error.response.data as {
          error?: { retry_after?: number; dimension?: string };
        };
        const dimension = errorData?.error?.dimension || "unknown";
        const retrySeconds = retryAfter
          ? parseInt(retryAfter, 10)
          : errorData?.error?.retry_after || 60;

        toast.warning(
          "请求过于频繁",
          `请等待 ${retrySeconds} 秒后重试 (${dimension})`
        );
      }

      // Attach retry info to error for react-query retry logic
      (error as AxiosError & { retryAfter?: number }).retryAfter =
        parseInt(error.response.headers["retry-after"] || "5", 10) * 1000;
    }

    return Promise.reject(error);
  }
);

