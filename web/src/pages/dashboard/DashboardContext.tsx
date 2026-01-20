// web/src/pages/dashboard/DashboardContext.tsx

import { createContext, useContext, useState, useCallback, useEffect, useMemo, type ReactNode } from "react";
import dayjs from "dayjs";
import type { DashboardContext, SourceFilter, ServiceFilter, UserFilter, RefreshInterval } from "./types";

interface DashboardContextValue extends DashboardContext {
  setDateRange: (range: [string, string]) => void;
  setGranularity: (granularity: "hour" | "day") => void;
  setSource: (source: SourceFilter) => void;
  setServiceId: (serviceId: ServiceFilter) => void;
  setUserId: (userId: UserFilter) => void;
  setRefreshInterval: (interval: RefreshInterval) => void;
  triggerRefresh: () => void;
}

const Context = createContext<DashboardContextValue | null>(null);

export function DashboardProvider({ children }: { children: ReactNode }) {
  const [dateRange, setDateRange] = useState<[string, string]>([
    dayjs().subtract(7, "day").format("YYYY-MM-DD"),
    dayjs().format("YYYY-MM-DD"),
  ]);
  const [granularity, setGranularity] = useState<"hour" | "day">("day");
  const [source, setSource] = useState<SourceFilter>("all");
  const [serviceId, setServiceId] = useState<ServiceFilter>("all");
  const [userId, setUserId] = useState<UserFilter>("all");
  const [refreshInterval, setRefreshInterval] = useState<RefreshInterval>(60);
  const [lastRefresh, setLastRefresh] = useState<Date>(new Date());

  const triggerRefresh = useCallback(() => {
    setLastRefresh(new Date());
  }, []);

  // Auto-refresh effect
  useEffect(() => {
    if (refreshInterval === 0) return;

    const timer = setInterval(() => {
      triggerRefresh();
    }, refreshInterval * 1000);

    return () => clearInterval(timer);
  }, [refreshInterval, triggerRefresh]);

  // Memoize context value to prevent unnecessary re-renders of consumers
  const value = useMemo<DashboardContextValue>(
    () => ({
      dateRange,
      granularity,
      source,
      serviceId,
      userId,
      refreshInterval,
      lastRefresh,
      setDateRange,
      setGranularity,
      setSource,
      setServiceId,
      setUserId,
      setRefreshInterval,
      triggerRefresh,
    }),
    [dateRange, granularity, source, serviceId, userId, refreshInterval, lastRefresh, triggerRefresh]
  );

  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useDashboardContext() {
  const context = useContext(Context);
  if (!context) {
    throw new Error("useDashboardContext must be used within DashboardProvider");
  }
  return context;
}
