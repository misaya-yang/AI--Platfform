import { create } from "zustand";
import { persist } from "zustand/middleware";

type AppState = {
  // === Playground (智能对话) 状态 ===
  selectedServiceId?: string;
  setSelectedServiceId: (id?: string) => void;
  activeSessionId?: string;  // Playground 的活动会话
  setActiveSessionId: (id?: string) => void;
  localTitles: Record<string, string>;  // Playground 的会话标题缓存
  setLocalTitles: (titles: Record<string, string> | ((prev: Record<string, string>) => Record<string, string>)) => void;
  
  // === Assistant (AI助手) 独立状态 ===
  assistantActiveSessionId?: string;  // AI助手 的活动会话（与 Playground 完全分离）
  setAssistantActiveSessionId: (id?: string) => void;
  assistantLocalTitles: Record<string, string>;  // AI助手 的会话标题缓存
  setAssistantLocalTitles: (titles: Record<string, string> | ((prev: Record<string, string>) => Record<string, string>)) => void;
  
  // === 全局状态 ===
  darkMode: boolean;
  toggleDarkMode: () => void;
};

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      // Playground (智能对话)
      selectedServiceId: undefined,
      setSelectedServiceId: (id) => set({ selectedServiceId: id }),
      activeSessionId: undefined,
      setActiveSessionId: (id) => set({ activeSessionId: id }),
      localTitles: {},
      setLocalTitles: (updater) => 
        set((state) => ({
          localTitles: typeof updater === 'function' ? updater(state.localTitles) : updater
        })),
      
      // Assistant (AI助手) - 完全独立的状态
      assistantActiveSessionId: undefined,
      setAssistantActiveSessionId: (id) => set({ assistantActiveSessionId: id }),
      assistantLocalTitles: {},
      setAssistantLocalTitles: (updater) =>
        set((state) => ({
          assistantLocalTitles: typeof updater === 'function' ? updater(state.assistantLocalTitles) : updater
        })),
      
      // 全局
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

