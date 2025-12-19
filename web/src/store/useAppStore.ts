import { create } from "zustand";
import { persist } from "zustand/middleware";

type AppState = {
  selectedServiceId?: string;
  setSelectedServiceId: (id?: string) => void;
  activeSessionId?: string;
  setActiveSessionId: (id?: string) => void;
  localTitles: Record<string, string>;
  setLocalTitles: (titles: Record<string, string> | ((prev: Record<string, string>) => Record<string, string>)) => void;
  darkMode: boolean;
  toggleDarkMode: () => void;
};

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      selectedServiceId: undefined,
      setSelectedServiceId: (id) => set({ selectedServiceId: id }),
      activeSessionId: undefined,
      setActiveSessionId: (id) => set({ activeSessionId: id }),
      localTitles: {},
      setLocalTitles: (updater) => 
        set((state) => ({
          localTitles: typeof updater === 'function' ? updater(state.localTitles) : updater
        })),
      darkMode: false,
      toggleDarkMode: () =>
        set((s) => ({
          darkMode: !s.darkMode,
        })),
    }),
    {
      name: "agent-gateway-storage",
    }
  )
);

