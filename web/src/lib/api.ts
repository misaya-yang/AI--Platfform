import axios, { AxiosError } from "axios";

const baseURL = import.meta.env.VITE_API_BASE_URL || "";
const AUTH_STORAGE_KEY = "agent-gateway-auth";

export const api = axios.create({
  baseURL,
});

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

// Response interceptor to handle auth errors
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
    return Promise.reject(error);
  }
);

