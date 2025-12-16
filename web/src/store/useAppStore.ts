import { create } from "zustand";

type AppState = {
  selectedServiceId?: string;
  setSelectedServiceId: (id?: string) => void;
  darkMode: boolean;
  toggleDarkMode: () => void;
};

export const useAppStore = create<AppState>((set) => ({
  selectedServiceId: undefined,
  setSelectedServiceId: (id) => set({ selectedServiceId: id }),
  darkMode: false,
  toggleDarkMode: () =>
    set((s) => ({
      darkMode: !s.darkMode,
    })),
}));

