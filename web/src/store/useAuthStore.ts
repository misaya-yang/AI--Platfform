import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

export interface User {
  user_id: string;
  email: string | null;
  display_name: string | null;
  department: string | null;  // 部门 - 由管理员分配
  roles: string[];
  permissions: string[];
  tier: string;
}

export interface AuthState {
  // State
  token: string | null;
  user: User | null;
  isAuthenticated: boolean;
  forcePasswordChange: boolean;
  isLoading: boolean;
  rememberMe: boolean;  // 30天免登录

  // Actions
  setAuth: (token: string, user: User, forcePasswordChange: boolean, rememberMe?: boolean) => void;
  clearAuth: () => void;
  setLoading: (loading: boolean) => void;
  updateUser: (user: Partial<User>) => void;
  setForcePasswordChange: (force: boolean) => void;

  // Permission helpers
  hasPermission: (permission: string) => boolean;
  hasRole: (role: string) => boolean;
  hasAnyPermission: (permissions: string[]) => boolean;
  hasAllPermissions: (permissions: string[]) => boolean;
}

const STORAGE_KEY = "agent-gateway-auth";

// Custom storage that switches between localStorage and sessionStorage
const createDynamicStorage = () => {
  return {
    getItem: (name: string): string | null => {
      try {
        // First try localStorage (for rememberMe=true)
        const localData = localStorage.getItem(name);
        if (localData) {
          // Validate JSON structure
          const parsed = JSON.parse(localData);
          if (parsed && typeof parsed === "object") {
            return localData;
          }
          // Invalid data, clear it
          localStorage.removeItem(name);
        }
        // Then try sessionStorage (for rememberMe=false)
        const sessionData = sessionStorage.getItem(name);
        if (sessionData) {
          // Validate JSON structure
          const parsed = JSON.parse(sessionData);
          if (parsed && typeof parsed === "object") {
            return sessionData;
          }
          // Invalid data, clear it
          sessionStorage.removeItem(name);
        }
      } catch {
        // Clear corrupted data on parse error
        localStorage.removeItem(name);
        sessionStorage.removeItem(name);
      }
      return null;
    },
    setItem: (name: string, value: string): void => {
      // Parse the value to check rememberMe flag
      try {
        const parsed = JSON.parse(value);
        const rememberMe = parsed?.state?.rememberMe ?? false;

        if (rememberMe) {
          // Use localStorage for persistent storage
          localStorage.setItem(name, value);
          // Clear from sessionStorage if it exists
          sessionStorage.removeItem(name);
        } else {
          // Use sessionStorage for session-only storage
          sessionStorage.setItem(name, value);
          // Clear from localStorage if it exists
          localStorage.removeItem(name);
        }
      } catch {
        // Fallback to sessionStorage
        sessionStorage.setItem(name, value);
      }
    },
    removeItem: (name: string): void => {
      // Clear from both storages
      localStorage.removeItem(name);
      sessionStorage.removeItem(name);
    },
  };
};

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      // Initial state
      token: null,
      user: null,
      isAuthenticated: false,
      forcePasswordChange: false,
      isLoading: false,
      rememberMe: false,

      // Set authentication data after successful login
      setAuth: (token, user, forcePasswordChange, rememberMe = false) =>
        set({
          token,
          user: {
            ...user,
            roles: Array.isArray(user.roles) ? user.roles : [],
            permissions: Array.isArray(user.permissions) ? user.permissions : [],
          },
          isAuthenticated: true,
          forcePasswordChange,
          isLoading: false,
          rememberMe,
        }),

      // Clear authentication data on logout
      clearAuth: () => {
        // Clear from both storages
        localStorage.removeItem(STORAGE_KEY);
        sessionStorage.removeItem(STORAGE_KEY);
        set({
          token: null,
          user: null,
          isAuthenticated: false,
          forcePasswordChange: false,
          isLoading: false,
          rememberMe: false,
        });
      },

      // Set loading state
      setLoading: (loading) => set({ isLoading: loading }),

      // Update user data
      updateUser: (userData) =>
        set((state) => ({
          user: state.user ? { ...state.user, ...userData } : null,
        })),

      // Update force password change flag
      setForcePasswordChange: (force) => set({ forcePasswordChange: force }),

      // Check if user has a specific permission
      hasPermission: (permission: string) => {
        const { user } = get();
        if (!user) return false;
        const permissions = Array.isArray(user.permissions) ? user.permissions : [];

        // Admin wildcard permission
        if (permissions.includes("admin:*")) return true;

        // Direct permission check
        if (permissions.includes(permission)) return true;

        // Wildcard permission matching (e.g., "console:*" matches "console:dashboard:view")
        const parts = permission.split(":");
        for (let i = parts.length - 1; i > 0; i--) {
          const wildcardPerm = parts.slice(0, i).join(":") + ":*";
          if (permissions.includes(wildcardPerm)) return true;
        }

        return false;
      },

      // Check if user has a specific role
      hasRole: (role: string) => {
        const { user } = get();
        if (!user) return false;
        const roles = Array.isArray(user.roles) ? user.roles : [];
        return roles.includes(role);
      },

      // Check if user has any of the specified permissions
      hasAnyPermission: (permissions: string[]) => {
        const { hasPermission } = get();
        return permissions.some((p) => hasPermission(p));
      },

      // Check if user has all specified permissions
      hasAllPermissions: (permissions: string[]) => {
        const { hasPermission } = get();
        return permissions.every((p) => hasPermission(p));
      },
    }),
    {
      name: STORAGE_KEY,
      storage: createJSONStorage(() => createDynamicStorage()),
      partialize: (state) => ({
        token: state.token,
        user: state.user,
        isAuthenticated: state.isAuthenticated,
        forcePasswordChange: state.forcePasswordChange,
        rememberMe: state.rememberMe,
      }),
      // Normalize data when loading from storage to handle old/corrupted data
      merge: (persistedState, currentState) => {
        const persisted = persistedState as Partial<AuthState> | undefined;
        if (!persisted) return currentState;

        // Normalize user data to ensure arrays exist
        let normalizedUser = persisted.user;
        if (normalizedUser) {
          normalizedUser = {
            ...normalizedUser,
            roles: Array.isArray(normalizedUser.roles) ? normalizedUser.roles : [],
            permissions: Array.isArray(normalizedUser.permissions) ? normalizedUser.permissions : [],
          };
        }

        return {
          ...currentState,
          ...persisted,
          user: normalizedUser ?? null,
        };
      },
    }
  )
);

// Helper hook for common permission checks
export const usePermission = (permission: string) => {
  return useAuthStore((state) => state.hasPermission(permission));
};

// Helper hook for role checks
export const useRole = (role: string) => {
  return useAuthStore((state) => state.hasRole(role));
};

// Helper hook for checking if user is admin
export const useIsAdmin = () => {
  return useAuthStore((state) => state.hasRole("admin"));
};
