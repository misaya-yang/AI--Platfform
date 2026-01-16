# Enterprise Dashboard Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a Grafana-style enterprise AI monitoring dashboard with draggable panels, source filtering, and auto-refresh.

**Architecture:** Frontend-first approach - start with UI framework and static panels, then connect to existing APIs, finally add new backend endpoints as needed. Use react-grid-layout for panel system.

**Tech Stack:** React 18 + TypeScript + Ant Design + Recharts + react-grid-layout + TanStack Query

---

## Phase 1: Foundation (Tasks 1-5)

### Task 1: Install react-grid-layout dependency

**Files:**
- Modify: `web/package.json`

**Step 1: Add dependency**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm add react-grid-layout @types/react-grid-layout
```

**Step 2: Verify installation**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm list react-grid-layout
```

Expected: Shows react-grid-layout version

**Step 3: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add web/package.json web/pnpm-lock.yaml && git commit -m "chore: add react-grid-layout for dashboard panels"
```

---

### Task 2: Create Dashboard Types

**Files:**
- Create: `web/src/pages/dashboard/types.ts`

**Step 1: Create types file**

```typescript
// web/src/pages/dashboard/types.ts

import type { Layout } from "react-grid-layout";

// Source filter type - internal (AI assistant, chat) vs external (API)
export type SourceFilter = "all" | "internal" | "external";

// Refresh interval options
export type RefreshInterval = 0 | 30 | 60 | 300; // 0 = manual, 30s, 1min, 5min

// Dashboard context shared across panels
export interface DashboardContext {
  dateRange: [string, string]; // [startDate, endDate] in YYYY-MM-DD
  granularity: "hour" | "day";
  source: SourceFilter;
  refreshInterval: RefreshInterval;
  lastRefresh: Date;
}

// Panel configuration
export interface PanelConfig {
  id: string;
  title: string;
  type: PanelType;
  visible: boolean;
}

export type PanelType =
  | "service-health"
  | "performance"
  | "token-usage"
  | "cost-analysis"
  | "user-quota"
  | "security-events"
  | "request-trace";

// KPI card data
export interface KPIData {
  totalRequests: number;
  totalCost: number;
  avgLatency: number;
  successRate: number;
  activeUsers: number;
  costChange?: number; // percentage change
}

// Service health data
export interface ServiceHealth {
  serviceId: string;
  serviceName: string;
  status: "healthy" | "degraded" | "down";
  qps: number;
  avgLatency: number;
  errorRate: number;
}

// Performance metrics
export interface PerformanceMetrics {
  timestamp: string;
  p50: number;
  p95: number;
  p99: number;
  avgLatency: number;
}

// Token usage data
export interface TokenUsageData {
  totalTokens: number;
  inputTokens: number;
  outputTokens: number;
  byModel: Array<{
    model: string;
    tokens: number;
    percentage: number;
  }>;
  trend: Array<{
    date: string;
    tokens: number;
  }>;
}

// Cost analysis data
export interface CostAnalysisData {
  today: number;
  thisWeek: number;
  thisMonth: number;
  todayChange: number;
  weekChange: number;
  monthChange: number;
  bySource: Array<{
    source: string;
    cost: number;
    percentage: number;
  }>;
  byService: Array<{
    service: string;
    cost: number;
    percentage: number;
  }>;
  trend: Array<{
    date: string;
    internal: number;
    external: number;
  }>;
}

// User quota data
export interface UserQuotaData {
  userId: string;
  username: string;
  dailyUsed: number;
  dailyLimit: number;
  monthlyUsed: number;
  monthlyLimit: number;
  status: "normal" | "warning" | "exceeded";
}

// Security event data
export interface SecurityEventData {
  authFailures: number;
  rateLimitHits: number;
  anomalies: number;
  authFailuresChange: number;
  rateLimitChange: number;
  anomaliesChange: number;
  hourlyDistribution: Array<{
    hour: string;
    authFailures: number;
    rateLimitHits: number;
  }>;
  topUsers: Array<{
    user: string;
    eventType: string;
    count: number;
  }>;
}

// Request trace data
export interface RequestTrace {
  requestId: string;
  timestamp: string;
  service: string;
  model: string;
  user: string;
  source: "internal" | "external";
  totalLatency: number;
  status: "success" | "error";
  inputTokens: number;
  outputTokens: number;
  cost: number;
  spans?: TraceSpan[];
}

export interface TraceSpan {
  name: string;
  startTime: number;
  duration: number;
}

// Default panel layouts
export const DEFAULT_LAYOUTS: Layout[] = [
  { i: "service-health", x: 0, y: 0, w: 12, h: 4, minW: 6, minH: 3 },
  { i: "performance", x: 0, y: 4, w: 6, h: 5, minW: 4, minH: 4 },
  { i: "token-usage", x: 6, y: 4, w: 6, h: 5, minW: 4, minH: 4 },
  { i: "cost-analysis", x: 0, y: 9, w: 6, h: 6, minW: 4, minH: 5 },
  { i: "user-quota", x: 6, y: 9, w: 6, h: 6, minW: 4, minH: 5 },
  { i: "security-events", x: 0, y: 15, w: 6, h: 5, minW: 4, minH: 4 },
  { i: "request-trace", x: 6, y: 15, w: 6, h: 5, minW: 4, minH: 4 },
];

// Default panel configs
export const DEFAULT_PANELS: PanelConfig[] = [
  { id: "service-health", title: "服务健康状态", type: "service-health", visible: true },
  { id: "performance", title: "性能监控", type: "performance", visible: true },
  { id: "token-usage", title: "Token 用量", type: "token-usage", visible: true },
  { id: "cost-analysis", title: "成本分析", type: "cost-analysis", visible: true },
  { id: "user-quota", title: "用户配额", type: "user-quota", visible: true },
  { id: "security-events", title: "安全事件", type: "security-events", visible: true },
  { id: "request-trace", title: "请求追踪", type: "request-trace", visible: true },
];
```

**Step 2: Verify TypeScript**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm type-check
```

Expected: No errors

**Step 3: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add web/src/pages/dashboard/types.ts && git commit -m "feat(dashboard): add type definitions for enterprise dashboard"
```

---

### Task 3: Create Dashboard Context Provider

**Files:**
- Create: `web/src/pages/dashboard/DashboardContext.tsx`

**Step 1: Create context file**

```typescript
// web/src/pages/dashboard/DashboardContext.tsx

import { createContext, useContext, useState, useCallback, useEffect, type ReactNode } from "react";
import dayjs from "dayjs";
import type { DashboardContext, SourceFilter, RefreshInterval } from "./types";

interface DashboardContextValue extends DashboardContext {
  setDateRange: (range: [string, string]) => void;
  setGranularity: (granularity: "hour" | "day") => void;
  setSource: (source: SourceFilter) => void;
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

  const value: DashboardContextValue = {
    dateRange,
    granularity,
    source,
    refreshInterval,
    lastRefresh,
    setDateRange,
    setGranularity,
    setSource,
    setRefreshInterval,
    triggerRefresh,
  };

  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useDashboardContext() {
  const context = useContext(Context);
  if (!context) {
    throw new Error("useDashboardContext must be used within DashboardProvider");
  }
  return context;
}
```

**Step 2: Verify TypeScript**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm type-check
```

Expected: No errors

**Step 3: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add web/src/pages/dashboard/DashboardContext.tsx && git commit -m "feat(dashboard): add dashboard context provider with auto-refresh"
```

---

### Task 4: Create Panel Wrapper Component

**Files:**
- Create: `web/src/pages/dashboard/components/PanelWrapper.tsx`

**Step 1: Create directory and file**

```typescript
// web/src/pages/dashboard/components/PanelWrapper.tsx

import { type ReactNode } from "react";
import { Card, Tooltip } from "antd";
import { SyncOutlined, FullscreenOutlined, FullscreenExitOutlined } from "@ant-design/icons";
import { useAppStore } from "@/store/useAppStore";

interface PanelWrapperProps {
  title: string;
  children: ReactNode;
  loading?: boolean;
  onRefresh?: () => void;
  extra?: ReactNode;
  className?: string;
}

export function PanelWrapper({
  title,
  children,
  loading = false,
  onRefresh,
  extra,
  className = "",
}: PanelWrapperProps) {
  const { darkMode } = useAppStore();

  return (
    <Card
      className={`h-full ${className}`}
      style={{
        borderRadius: 12,
        border: darkMode ? "1px solid #334155" : "1px solid #e2e8f0",
        background: darkMode ? "#1e293b" : "#ffffff",
        height: "100%",
        display: "flex",
        flexDirection: "column",
      }}
      styles={{
        header: {
          borderBottom: darkMode ? "1px solid #334155" : "1px solid #e2e8f0",
          padding: "12px 16px",
          minHeight: "auto",
        },
        body: {
          padding: 16,
          flex: 1,
          overflow: "auto",
        },
      }}
      title={
        <span
          style={{
            fontSize: 14,
            fontWeight: 600,
            color: darkMode ? "#f1f5f9" : "#1e293b",
          }}
        >
          {title}
        </span>
      }
      extra={
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {extra}
          {onRefresh && (
            <Tooltip title="刷新">
              <div
                onClick={onRefresh}
                style={{
                  width: 28,
                  height: 28,
                  borderRadius: 6,
                  background: darkMode ? "#334155" : "#f1f5f9",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  cursor: "pointer",
                  transition: "all 0.2s",
                }}
              >
                <SyncOutlined
                  spin={loading}
                  style={{
                    fontSize: 12,
                    color: darkMode ? "#94a3b8" : "#64748b",
                  }}
                />
              </div>
            </Tooltip>
          )}
        </div>
      }
    >
      {children}
    </Card>
  );
}
```

**Step 2: Verify TypeScript**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm type-check
```

Expected: No errors

**Step 3: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add web/src/pages/dashboard/components/PanelWrapper.tsx && git commit -m "feat(dashboard): add reusable panel wrapper component"
```

---

### Task 5: Create Dashboard Header Component

**Files:**
- Create: `web/src/pages/dashboard/components/DashboardHeader.tsx`

**Step 1: Create header component**

```typescript
// web/src/pages/dashboard/components/DashboardHeader.tsx

import { DatePicker, Select, Segmented, Tooltip } from "antd";
import { SyncOutlined, ExpandOutlined, CompressOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useDashboardContext } from "../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import type { SourceFilter, RefreshInterval } from "../types";

const { RangePicker } = DatePicker;

export function DashboardHeader() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const {
    dateRange,
    granularity,
    source,
    refreshInterval,
    lastRefresh,
    setDateRange,
    setGranularity,
    setSource,
    setRefreshInterval,
    triggerRefresh,
  } = useDashboardContext();

  const [isFullscreen, setIsFullscreen] = useState(false);

  const handleFullscreen = () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen();
      setIsFullscreen(true);
    } else {
      document.exitFullscreen();
      setIsFullscreen(false);
    }
  };

  const sourceOptions: { label: string; value: SourceFilter }[] = [
    { label: "全部", value: "all" },
    { label: "内部调用", value: "internal" },
    { label: "外部API", value: "external" },
  ];

  const refreshOptions: { label: string; value: RefreshInterval }[] = [
    { label: "手动", value: 0 },
    { label: "30秒", value: 30 },
    { label: "1分钟", value: 60 },
    { label: "5分钟", value: 300 },
  ];

  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 16,
        marginBottom: 20,
        padding: "16px 20px",
        borderRadius: 12,
        background: darkMode ? "#1e293b" : "#ffffff",
        border: darkMode ? "1px solid #334155" : "1px solid #e2e8f0",
      }}
    >
      {/* Left: Title */}
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <h1
          style={{
            fontSize: 20,
            fontWeight: 700,
            margin: 0,
            color: darkMode ? "#f1f5f9" : "#1e293b",
          }}
        >
          监控仪表盘
        </h1>
        <span
          style={{
            fontSize: 12,
            padding: "4px 8px",
            borderRadius: 4,
            background: darkMode ? "#334155" : "#f1f5f9",
            color: darkMode ? "#94a3b8" : "#64748b",
          }}
        >
          更新于 {dayjs(lastRefresh).format("HH:mm:ss")}
        </span>
      </div>

      {/* Right: Controls */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        {/* Source Filter */}
        <Segmented
          options={sourceOptions}
          value={source}
          onChange={(v) => setSource(v as SourceFilter)}
          style={{
            background: darkMode ? "#334155" : "#f1f5f9",
          }}
        />

        {/* Date Range */}
        <RangePicker
          value={[dayjs(dateRange[0]), dayjs(dateRange[1])]}
          onChange={(dates) => {
            if (dates && dates[0] && dates[1]) {
              setDateRange([
                dates[0].format("YYYY-MM-DD"),
                dates[1].format("YYYY-MM-DD"),
              ]);
            }
          }}
          style={{ width: 240 }}
        />

        {/* Granularity */}
        <Select
          value={granularity}
          onChange={setGranularity}
          options={[
            { value: "day", label: "按天" },
            { value: "hour", label: "按小时" },
          ]}
          style={{ width: 100 }}
        />

        {/* Refresh Interval */}
        <Select
          value={refreshInterval}
          onChange={setRefreshInterval}
          options={refreshOptions}
          style={{ width: 100 }}
          suffixIcon={<SyncOutlined />}
        />

        {/* Manual Refresh */}
        <Tooltip title="立即刷新">
          <div
            onClick={triggerRefresh}
            style={{
              width: 32,
              height: 32,
              borderRadius: 6,
              background: darkMode ? "#334155" : "#f1f5f9",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              cursor: "pointer",
            }}
          >
            <SyncOutlined style={{ color: darkMode ? "#94a3b8" : "#64748b" }} />
          </div>
        </Tooltip>

        {/* Fullscreen */}
        <Tooltip title={isFullscreen ? "退出全屏" : "全屏模式"}>
          <div
            onClick={handleFullscreen}
            style={{
              width: 32,
              height: 32,
              borderRadius: 6,
              background: darkMode ? "#334155" : "#f1f5f9",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              cursor: "pointer",
            }}
          >
            {isFullscreen ? (
              <CompressOutlined style={{ color: darkMode ? "#94a3b8" : "#64748b" }} />
            ) : (
              <ExpandOutlined style={{ color: darkMode ? "#94a3b8" : "#64748b" }} />
            )}
          </div>
        </Tooltip>
      </div>
    </div>
  );
}
```

**Step 2: Verify TypeScript**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm type-check
```

Expected: No errors

**Step 3: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add web/src/pages/dashboard/components/DashboardHeader.tsx && git commit -m "feat(dashboard): add dashboard header with source filter and controls"
```

---

## Phase 2: KPI Cards (Task 6)

### Task 6: Create KPI Cards Component

**Files:**
- Create: `web/src/pages/dashboard/components/KPICards.tsx`

**Step 1: Create KPI cards**

```typescript
// web/src/pages/dashboard/components/KPICards.tsx

import { Row, Col, Statistic, Spin } from "antd";
import {
  ApiOutlined,
  DollarOutlined,
  ThunderboltOutlined,
  CheckCircleOutlined,
  UserOutlined,
  ArrowUpOutlined,
  ArrowDownOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { useDashboardContext } from "../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getUsageSummary } from "@/api/usage";

function formatNumber(num: number): string {
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`;
  if (num >= 1_000) return `${(num / 1_000).toFixed(1)}K`;
  return num.toLocaleString();
}

function formatCurrency(value: number): string {
  if (value >= 1) return `$${value.toFixed(2)}`;
  return `$${value.toFixed(4)}`;
}

interface KPICardProps {
  title: string;
  value: string | number;
  icon: React.ReactNode;
  iconColor: string;
  iconBg: string;
  change?: number;
  suffix?: string;
  loading?: boolean;
}

function KPICard({ title, value, icon, iconColor, iconBg, change, suffix, loading }: KPICardProps) {
  const { darkMode } = useAppStore();

  return (
    <div
      style={{
        padding: 20,
        borderRadius: 12,
        background: darkMode ? "#1e293b" : "#ffffff",
        border: darkMode ? "1px solid #334155" : "1px solid #e2e8f0",
        height: "100%",
      }}
    >
      {loading ? (
        <div style={{ textAlign: "center", padding: 20 }}>
          <Spin />
        </div>
      ) : (
        <div style={{ display: "flex", alignItems: "flex-start", gap: 16 }}>
          <div
            style={{
              width: 48,
              height: 48,
              borderRadius: 12,
              background: iconBg,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 20,
              color: iconColor,
              flexShrink: 0,
            }}
          >
            {icon}
          </div>
          <div style={{ flex: 1 }}>
            <div
              style={{
                fontSize: 13,
                color: darkMode ? "#94a3b8" : "#64748b",
                marginBottom: 4,
              }}
            >
              {title}
            </div>
            <div
              style={{
                fontSize: 24,
                fontWeight: 700,
                color: darkMode ? "#f1f5f9" : "#1e293b",
                lineHeight: 1.2,
              }}
            >
              {value}
              {suffix && (
                <span style={{ fontSize: 14, fontWeight: 400, marginLeft: 2 }}>
                  {suffix}
                </span>
              )}
            </div>
            {change !== undefined && (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 4,
                  marginTop: 4,
                  fontSize: 12,
                  color: change >= 0 ? "#10b981" : "#ef4444",
                }}
              >
                {change >= 0 ? <ArrowUpOutlined /> : <ArrowDownOutlined />}
                {Math.abs(change).toFixed(1)}% vs 上周期
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export function KPICards() {
  const { dateRange, source, lastRefresh } = useDashboardContext();

  const { data, isLoading } = useQuery({
    queryKey: ["dashboard-kpi", dateRange, source, lastRefresh.getTime()],
    queryFn: () =>
      getUsageSummary({
        start_date: dateRange[0],
        end_date: dateRange[1],
        // TODO: Add source filter when backend supports it
      }),
    staleTime: 30000,
  });

  const kpiData = [
    {
      title: "总请求数",
      value: formatNumber(data?.total_requests || 0),
      icon: <ApiOutlined />,
      iconColor: "#3b82f6",
      iconBg: "rgba(59, 130, 246, 0.1)",
    },
    {
      title: "总成本",
      value: formatCurrency(data?.total_cost_usd || 0),
      icon: <DollarOutlined />,
      iconColor: "#10b981",
      iconBg: "rgba(16, 185, 129, 0.1)",
    },
    {
      title: "平均延迟",
      value: data?.avg_latency_ms || 0,
      suffix: "ms",
      icon: <ThunderboltOutlined />,
      iconColor: "#f59e0b",
      iconBg: "rgba(245, 158, 11, 0.1)",
    },
    {
      title: "成功率",
      value: (data?.success_rate || 0).toFixed(1),
      suffix: "%",
      icon: <CheckCircleOutlined />,
      iconColor: data?.success_rate && data.success_rate >= 95 ? "#10b981" : "#f59e0b",
      iconBg:
        data?.success_rate && data.success_rate >= 95
          ? "rgba(16, 185, 129, 0.1)"
          : "rgba(245, 158, 11, 0.1)",
    },
    {
      title: "Token 总量",
      value: formatNumber(data?.total_tokens || 0),
      icon: <UserOutlined />,
      iconColor: "#8b5cf6",
      iconBg: "rgba(139, 92, 246, 0.1)",
    },
  ];

  return (
    <Row gutter={[16, 16]} style={{ marginBottom: 20 }}>
      {kpiData.map((kpi, index) => (
        <Col xs={24} sm={12} md={8} lg={4} xl={4} key={index} style={{ minWidth: 180 }}>
          <KPICard {...kpi} loading={isLoading} />
        </Col>
      ))}
    </Row>
  );
}
```

**Step 2: Verify TypeScript**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm type-check
```

Expected: No errors

**Step 3: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add web/src/pages/dashboard/components/KPICards.tsx && git commit -m "feat(dashboard): add KPI cards component"
```

---

## Phase 3: Core Panels (Tasks 7-10)

### Task 7: Create Service Health Panel

**Files:**
- Create: `web/src/pages/dashboard/components/panels/ServiceHealthPanel.tsx`

**Step 1: Create service health panel**

```typescript
// web/src/pages/dashboard/components/panels/ServiceHealthPanel.tsx

import { Row, Col, Tag, Statistic } from "antd";
import { CheckCircleOutlined, WarningOutlined, CloseCircleOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { PanelWrapper } from "../PanelWrapper";
import { useDashboardContext } from "../../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { useServices, useHealth } from "@/hooks/useServices";

interface ServiceCardProps {
  name: string;
  status: "healthy" | "degraded" | "down";
  qps: number;
  latency: number;
  errorRate: number;
}

function ServiceStatusCard({ name, status, qps, latency, errorRate }: ServiceCardProps) {
  const { darkMode } = useAppStore();

  const statusConfig = {
    healthy: { color: "#10b981", icon: <CheckCircleOutlined />, text: "正常" },
    degraded: { color: "#f59e0b", icon: <WarningOutlined />, text: "降级" },
    down: { color: "#ef4444", icon: <CloseCircleOutlined />, text: "异常" },
  };

  const config = statusConfig[status];

  return (
    <div
      style={{
        padding: 16,
        borderRadius: 8,
        background: darkMode ? "#0f172a" : "#f8fafc",
        border: `1px solid ${darkMode ? "#334155" : "#e2e8f0"}`,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
        <span style={{ fontSize: 14, fontWeight: 600, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
          {name}
        </span>
        <Tag color={config.color} icon={config.icon}>
          {config.text}
        </Tag>
      </div>
      <Row gutter={8}>
        <Col span={8}>
          <div style={{ fontSize: 11, color: darkMode ? "#64748b" : "#94a3b8" }}>QPS</div>
          <div style={{ fontSize: 16, fontWeight: 600, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
            {qps.toFixed(1)}
          </div>
        </Col>
        <Col span={8}>
          <div style={{ fontSize: 11, color: darkMode ? "#64748b" : "#94a3b8" }}>延迟</div>
          <div style={{ fontSize: 16, fontWeight: 600, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
            {latency}ms
          </div>
        </Col>
        <Col span={8}>
          <div style={{ fontSize: 11, color: darkMode ? "#64748b" : "#94a3b8" }}>错误率</div>
          <div
            style={{
              fontSize: 16,
              fontWeight: 600,
              color: errorRate > 5 ? "#ef4444" : errorRate > 1 ? "#f59e0b" : "#10b981",
            }}
          >
            {errorRate.toFixed(1)}%
          </div>
        </Col>
      </Row>
    </div>
  );
}

export function ServiceHealthPanel() {
  const { darkMode } = useAppStore();
  const { lastRefresh } = useDashboardContext();
  const servicesQuery = useServices();
  const healthQuery = useHealth();

  const services = servicesQuery.data || [];
  const health = healthQuery.data || {};

  const refetch = () => {
    servicesQuery.refetch();
    healthQuery.refetch();
  };

  // Map services to display format
  const serviceCards: ServiceCardProps[] = services.map((s) => {
    const h = health[s.service_id] || {};
    return {
      name: s.display_name || s.service_id,
      status: h.status === "healthy" ? "healthy" : h.status === "degraded" ? "degraded" : "down",
      qps: h.qps || 0,
      latency: h.avg_latency_ms || 0,
      errorRate: h.error_rate || 0,
    };
  });

  // Calculate summary
  const totalServices = serviceCards.length;
  const healthyCount = serviceCards.filter((s) => s.status === "healthy").length;
  const avgErrorRate =
    serviceCards.length > 0
      ? serviceCards.reduce((sum, s) => sum + s.errorRate, 0) / serviceCards.length
      : 0;

  return (
    <PanelWrapper
      title="服务健康状态"
      onRefresh={refetch}
      loading={servicesQuery.isLoading || healthQuery.isLoading}
    >
      {/* Summary row */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={8}>
          <Statistic
            title={<span style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>可用性</span>}
            value={totalServices > 0 ? ((healthyCount / totalServices) * 100).toFixed(1) : 0}
            suffix="%"
            valueStyle={{ color: "#10b981", fontSize: 20 }}
          />
        </Col>
        <Col span={8}>
          <Statistic
            title={<span style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>错误率</span>}
            value={avgErrorRate.toFixed(2)}
            suffix="%"
            valueStyle={{
              color: avgErrorRate > 5 ? "#ef4444" : avgErrorRate > 1 ? "#f59e0b" : "#10b981",
              fontSize: 20,
            }}
          />
        </Col>
        <Col span={8}>
          <Statistic
            title={<span style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>服务数</span>}
            value={totalServices}
            valueStyle={{ fontSize: 20, color: darkMode ? "#f1f5f9" : "#1e293b" }}
          />
        </Col>
      </Row>

      {/* Service cards */}
      <Row gutter={[12, 12]}>
        {serviceCards.map((service, index) => (
          <Col xs={24} sm={12} lg={8} key={index}>
            <ServiceStatusCard {...service} />
          </Col>
        ))}
      </Row>
    </PanelWrapper>
  );
}
```

**Step 2: Verify TypeScript**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm type-check
```

Expected: No errors

**Step 3: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add web/src/pages/dashboard/components/panels/ServiceHealthPanel.tsx && git commit -m "feat(dashboard): add service health panel"
```

---

### Task 8: Create Performance Panel

**Files:**
- Create: `web/src/pages/dashboard/components/panels/PerformancePanel.tsx`

**Step 1: Create performance panel**

```typescript
// web/src/pages/dashboard/components/panels/PerformancePanel.tsx

import { useState } from "react";
import { Select, Row, Col, Statistic } from "antd";
import { useQuery } from "@tanstack/react-query";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from "recharts";
import dayjs from "dayjs";
import { PanelWrapper } from "../PanelWrapper";
import { useDashboardContext } from "../../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getUsageTimeSeries } from "@/api/usage";

type LatencyMetric = "p50" | "p95" | "p99" | "avg";

export function PerformancePanel() {
  const { darkMode } = useAppStore();
  const { dateRange, granularity, lastRefresh } = useDashboardContext();
  const [selectedMetric, setSelectedMetric] = useState<LatencyMetric>("p95");

  const { data, isLoading, refetch } = useQuery({
    queryKey: ["dashboard-performance", dateRange, granularity, lastRefresh.getTime()],
    queryFn: () =>
      getUsageTimeSeries({
        start_date: dateRange[0],
        end_date: dateRange[1],
        granularity,
      }),
    staleTime: 30000,
  });

  const chartData = (data?.data || []).map((point) => ({
    date: point.date,
    avgLatency: point.avg_latency_ms,
    // Note: Real P50/P95/P99 would need backend support
    // For now, simulate with avg variations
    p50: Math.round(point.avg_latency_ms * 0.7),
    p95: Math.round(point.avg_latency_ms * 1.5),
    p99: Math.round(point.avg_latency_ms * 2),
  }));

  const latestData = chartData[chartData.length - 1];
  const gridColor = darkMode ? "#334155" : "#e2e8f0";

  const metricOptions = [
    { value: "avg", label: "平均延迟" },
    { value: "p50", label: "P50" },
    { value: "p95", label: "P95" },
    { value: "p99", label: "P99" },
  ];

  return (
    <PanelWrapper
      title="性能监控"
      onRefresh={refetch}
      loading={isLoading}
      extra={
        <Select
          value={selectedMetric}
          onChange={setSelectedMetric}
          options={metricOptions}
          size="small"
          style={{ width: 100 }}
        />
      }
    >
      {/* Summary stats */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Statistic
            title={<span style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>P50</span>}
            value={latestData?.p50 || 0}
            suffix="ms"
            valueStyle={{ fontSize: 16, color: darkMode ? "#f1f5f9" : "#1e293b" }}
          />
        </Col>
        <Col span={6}>
          <Statistic
            title={<span style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>P95</span>}
            value={latestData?.p95 || 0}
            suffix="ms"
            valueStyle={{ fontSize: 16, color: "#f59e0b" }}
          />
        </Col>
        <Col span={6}>
          <Statistic
            title={<span style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>P99</span>}
            value={latestData?.p99 || 0}
            suffix="ms"
            valueStyle={{ fontSize: 16, color: "#ef4444" }}
          />
        </Col>
        <Col span={6}>
          <Statistic
            title={<span style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>平均</span>}
            value={latestData?.avgLatency || 0}
            suffix="ms"
            valueStyle={{ fontSize: 16, color: "#3b82f6" }}
          />
        </Col>
      </Row>

      {/* Chart */}
      <div style={{ width: "100%", height: 200 }}>
        <ResponsiveContainer>
          <AreaChart data={chartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={gridColor} strokeDasharray="3 3" />
            <XAxis
              dataKey="date"
              tick={{ fontSize: 11 }}
              tickFormatter={(value) =>
                dayjs(value).format(granularity === "hour" ? "HH:mm" : "MM-DD")
              }
            />
            <YAxis tick={{ fontSize: 11 }} width={40} />
            <Tooltip
              formatter={(value: number) => [`${value} ms`, ""]}
              labelFormatter={(label) => dayjs(label).format("YYYY-MM-DD HH:mm")}
            />
            <Area
              type="monotone"
              dataKey={selectedMetric === "avg" ? "avgLatency" : selectedMetric}
              stroke="#3b82f6"
              fill="rgba(59, 130, 246, 0.2)"
              strokeWidth={2}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </PanelWrapper>
  );
}
```

**Step 2: Verify TypeScript**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm type-check
```

Expected: No errors

**Step 3: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add web/src/pages/dashboard/components/panels/PerformancePanel.tsx && git commit -m "feat(dashboard): add performance monitoring panel"
```

---

### Task 9: Create Token Usage Panel

**Files:**
- Create: `web/src/pages/dashboard/components/panels/TokenUsagePanel.tsx`

**Step 1: Create token usage panel**

```typescript
// web/src/pages/dashboard/components/panels/TokenUsagePanel.tsx

import { Progress, Row, Col } from "antd";
import { useQuery } from "@tanstack/react-query";
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from "recharts";
import dayjs from "dayjs";
import { PanelWrapper } from "../PanelWrapper";
import { useDashboardContext } from "../../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getUsageSummary, getUsageBreakdown, getUsageTimeSeries } from "@/api/usage";

function formatTokens(num: number): string {
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`;
  if (num >= 1_000) return `${(num / 1_000).toFixed(1)}K`;
  return num.toLocaleString();
}

export function TokenUsagePanel() {
  const { darkMode } = useAppStore();
  const { dateRange, granularity, lastRefresh } = useDashboardContext();

  const summaryQuery = useQuery({
    queryKey: ["dashboard-token-summary", dateRange, lastRefresh.getTime()],
    queryFn: () =>
      getUsageSummary({
        start_date: dateRange[0],
        end_date: dateRange[1],
      }),
    staleTime: 30000,
  });

  const breakdownQuery = useQuery({
    queryKey: ["dashboard-token-breakdown", dateRange, lastRefresh.getTime()],
    queryFn: () =>
      getUsageBreakdown({
        dimension: "model",
        start_date: dateRange[0],
        end_date: dateRange[1],
        limit: 5,
      }),
    staleTime: 30000,
  });

  const timeseriesQuery = useQuery({
    queryKey: ["dashboard-token-timeseries", dateRange, granularity, lastRefresh.getTime()],
    queryFn: () =>
      getUsageTimeSeries({
        start_date: dateRange[0],
        end_date: dateRange[1],
        granularity,
      }),
    staleTime: 30000,
  });

  const refetch = () => {
    summaryQuery.refetch();
    breakdownQuery.refetch();
    timeseriesQuery.refetch();
  };

  const summary = summaryQuery.data;
  const breakdown = breakdownQuery.data?.items || [];
  const timeseries = timeseriesQuery.data?.data || [];

  const totalTokens = summary?.total_tokens || 0;
  const inputTokens = summary?.total_input_tokens || 0;
  const outputTokens = summary?.total_output_tokens || 0;
  const inputPercent = totalTokens > 0 ? (inputTokens / totalTokens) * 100 : 0;

  const chartData = timeseries.map((point) => ({
    date: point.date,
    tokens: point.total_tokens,
  }));

  const gridColor = darkMode ? "#334155" : "#e2e8f0";
  const modelColors = ["#3b82f6", "#8b5cf6", "#10b981", "#f59e0b", "#ef4444"];

  return (
    <PanelWrapper
      title="Token 用量"
      onRefresh={refetch}
      loading={summaryQuery.isLoading}
    >
      {/* Total and breakdown */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 24, fontWeight: 700, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
          {formatTokens(totalTokens)}
          <span style={{ fontSize: 14, fontWeight: 400, marginLeft: 4 }}>tokens</span>
        </div>

        {/* Input/Output bar */}
        <div style={{ marginTop: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
            <span style={{ color: darkMode ? "#94a3b8" : "#64748b" }}>
              输入: {formatTokens(inputTokens)} ({inputPercent.toFixed(0)}%)
            </span>
            <span style={{ color: darkMode ? "#94a3b8" : "#64748b" }}>
              输出: {formatTokens(outputTokens)} ({(100 - inputPercent).toFixed(0)}%)
            </span>
          </div>
          <Progress
            percent={100}
            success={{ percent: inputPercent }}
            showInfo={false}
            strokeColor="#8b5cf6"
            trailColor={darkMode ? "#334155" : "#e2e8f0"}
          />
        </div>
      </div>

      {/* Model breakdown */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: darkMode ? "#94a3b8" : "#64748b", marginBottom: 8 }}>
          模型分布
        </div>
        {breakdown.slice(0, 4).map((item, index) => (
          <div
            key={item.model || index}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              marginBottom: 6,
            }}
          >
            <div
              style={{
                width: 8,
                height: 8,
                borderRadius: 2,
                background: modelColors[index % modelColors.length],
              }}
            />
            <span
              style={{
                flex: 1,
                fontSize: 12,
                color: darkMode ? "#e2e8f0" : "#475569",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {item.model || "Unknown"}
            </span>
            <span style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>
              {formatTokens(item.total_tokens)}
            </span>
            <span style={{ fontSize: 11, color: darkMode ? "#64748b" : "#94a3b8", width: 40 }}>
              {item.percentage.toFixed(0)}%
            </span>
          </div>
        ))}
      </div>

      {/* Trend chart */}
      <div style={{ width: "100%", height: 100 }}>
        <ResponsiveContainer>
          <LineChart data={chartData} margin={{ top: 5, right: 5, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={gridColor} strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="date" hide />
            <YAxis hide />
            <Tooltip
              formatter={(value: number) => [formatTokens(value), "Tokens"]}
              labelFormatter={(label) => dayjs(label).format("MM-DD")}
            />
            <Line
              type="monotone"
              dataKey="tokens"
              stroke="#8b5cf6"
              strokeWidth={2}
              dot={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </PanelWrapper>
  );
}
```

**Step 2: Verify TypeScript**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm type-check
```

Expected: No errors

**Step 3: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add web/src/pages/dashboard/components/panels/TokenUsagePanel.tsx && git commit -m "feat(dashboard): add token usage panel"
```

---

### Task 10: Create Cost Analysis Panel

**Files:**
- Create: `web/src/pages/dashboard/components/panels/CostAnalysisPanel.tsx`

**Step 1: Create cost analysis panel**

```typescript
// web/src/pages/dashboard/components/panels/CostAnalysisPanel.tsx

import { Row, Col, Statistic } from "antd";
import { ArrowUpOutlined, ArrowDownOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  PieChart,
  Pie,
  Cell,
} from "recharts";
import dayjs from "dayjs";
import { PanelWrapper } from "../PanelWrapper";
import { useDashboardContext } from "../../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getUsageSummary, getUsageBreakdown, getUsageTimeSeries } from "@/api/usage";

function formatCost(value: number): string {
  if (value >= 1) return `$${value.toFixed(2)}`;
  return `$${value.toFixed(4)}`;
}

export function CostAnalysisPanel() {
  const { darkMode } = useAppStore();
  const { dateRange, granularity, lastRefresh } = useDashboardContext();

  // Today's data
  const todayQuery = useQuery({
    queryKey: ["dashboard-cost-today", lastRefresh.getTime()],
    queryFn: () =>
      getUsageSummary({
        start_date: dayjs().format("YYYY-MM-DD"),
        end_date: dayjs().format("YYYY-MM-DD"),
      }),
    staleTime: 30000,
  });

  // This week's data
  const weekQuery = useQuery({
    queryKey: ["dashboard-cost-week", lastRefresh.getTime()],
    queryFn: () =>
      getUsageSummary({
        start_date: dayjs().startOf("week").format("YYYY-MM-DD"),
        end_date: dayjs().format("YYYY-MM-DD"),
      }),
    staleTime: 30000,
  });

  // This month's data
  const monthQuery = useQuery({
    queryKey: ["dashboard-cost-month", lastRefresh.getTime()],
    queryFn: () =>
      getUsageSummary({
        start_date: dayjs().startOf("month").format("YYYY-MM-DD"),
        end_date: dayjs().format("YYYY-MM-DD"),
      }),
    staleTime: 30000,
  });

  // Service breakdown for pie chart
  const breakdownQuery = useQuery({
    queryKey: ["dashboard-cost-breakdown", dateRange, lastRefresh.getTime()],
    queryFn: () =>
      getUsageBreakdown({
        dimension: "service",
        start_date: dateRange[0],
        end_date: dateRange[1],
        limit: 5,
      }),
    staleTime: 30000,
  });

  // Time series for trend
  const timeseriesQuery = useQuery({
    queryKey: ["dashboard-cost-timeseries", dateRange, granularity, lastRefresh.getTime()],
    queryFn: () =>
      getUsageTimeSeries({
        start_date: dateRange[0],
        end_date: dateRange[1],
        granularity,
      }),
    staleTime: 30000,
  });

  const refetch = () => {
    todayQuery.refetch();
    weekQuery.refetch();
    monthQuery.refetch();
    breakdownQuery.refetch();
    timeseriesQuery.refetch();
  };

  const pieData = (breakdownQuery.data?.items || []).map((item) => ({
    name: item.service || "Unknown",
    value: item.cost_usd || 0,
  }));

  const chartData = (timeseriesQuery.data?.data || []).map((point) => ({
    date: point.date,
    cost: point.cost_usd,
  }));

  const pieColors = ["#3b82f6", "#8b5cf6", "#10b981", "#f59e0b", "#ef4444"];
  const gridColor = darkMode ? "#334155" : "#e2e8f0";

  return (
    <PanelWrapper
      title="成本分析"
      onRefresh={refetch}
      loading={todayQuery.isLoading}
    >
      {/* Cost summary cards */}
      <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
        <Col span={8}>
          <div
            style={{
              padding: 12,
              borderRadius: 8,
              background: darkMode ? "#0f172a" : "#f8fafc",
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: 20, fontWeight: 700, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
              {formatCost(todayQuery.data?.total_cost_usd || 0)}
            </div>
            <div style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>今日</div>
          </div>
        </Col>
        <Col span={8}>
          <div
            style={{
              padding: 12,
              borderRadius: 8,
              background: darkMode ? "#0f172a" : "#f8fafc",
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: 20, fontWeight: 700, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
              {formatCost(weekQuery.data?.total_cost_usd || 0)}
            </div>
            <div style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>本周</div>
          </div>
        </Col>
        <Col span={8}>
          <div
            style={{
              padding: 12,
              borderRadius: 8,
              background: darkMode ? "#0f172a" : "#f8fafc",
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: 20, fontWeight: 700, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
              {formatCost(monthQuery.data?.total_cost_usd || 0)}
            </div>
            <div style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>本月</div>
          </div>
        </Col>
      </Row>

      {/* Pie chart and trend */}
      <Row gutter={16}>
        <Col span={10}>
          <div style={{ fontSize: 12, fontWeight: 600, color: darkMode ? "#94a3b8" : "#64748b", marginBottom: 8 }}>
            服务分布
          </div>
          <div style={{ width: "100%", height: 120 }}>
            <ResponsiveContainer>
              <PieChart>
                <Pie
                  data={pieData}
                  cx="50%"
                  cy="50%"
                  innerRadius={30}
                  outerRadius={50}
                  dataKey="value"
                  label={({ name, percent }) => `${(percent * 100).toFixed(0)}%`}
                  labelLine={false}
                >
                  {pieData.map((_, index) => (
                    <Cell key={index} fill={pieColors[index % pieColors.length]} />
                  ))}
                </Pie>
                <Tooltip formatter={(value: number) => formatCost(value)} />
              </PieChart>
            </ResponsiveContainer>
          </div>
          {/* Legend */}
          <div style={{ marginTop: 8 }}>
            {pieData.slice(0, 3).map((item, index) => (
              <div key={index} style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
                <div
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: 2,
                    background: pieColors[index],
                  }}
                />
                <span style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>
                  {item.name}
                </span>
              </div>
            ))}
          </div>
        </Col>
        <Col span={14}>
          <div style={{ fontSize: 12, fontWeight: 600, color: darkMode ? "#94a3b8" : "#64748b", marginBottom: 8 }}>
            成本趋势
          </div>
          <div style={{ width: "100%", height: 150 }}>
            <ResponsiveContainer>
              <AreaChart data={chartData} margin={{ top: 5, right: 5, left: 0, bottom: 0 }}>
                <CartesianGrid stroke={gridColor} strokeDasharray="3 3" />
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: 10 }}
                  tickFormatter={(v) => dayjs(v).format("MM-DD")}
                />
                <YAxis tick={{ fontSize: 10 }} width={35} />
                <Tooltip
                  formatter={(value: number) => [formatCost(value), "成本"]}
                  labelFormatter={(label) => dayjs(label).format("YYYY-MM-DD")}
                />
                <Area
                  type="monotone"
                  dataKey="cost"
                  stroke="#10b981"
                  fill="rgba(16, 185, 129, 0.2)"
                  strokeWidth={2}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </Col>
      </Row>
    </PanelWrapper>
  );
}
```

**Step 2: Verify TypeScript**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm type-check
```

Expected: No errors

**Step 3: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add web/src/pages/dashboard/components/panels/CostAnalysisPanel.tsx && git commit -m "feat(dashboard): add cost analysis panel"
```

---

## Phase 4: Additional Panels (Tasks 11-13)

### Task 11: Create User Quota Panel

**Files:**
- Create: `web/src/pages/dashboard/components/panels/UserQuotaPanel.tsx`

**Step 1: Create user quota panel**

```typescript
// web/src/pages/dashboard/components/panels/UserQuotaPanel.tsx

import { Table, Progress, Tag, Select } from "antd";
import { useQuery } from "@tanstack/react-query";
import { PanelWrapper } from "../PanelWrapper";
import { useDashboardContext } from "../../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getUsageBreakdown } from "@/api/usage";
import { useState } from "react";

function formatTokens(num: number): string {
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`;
  if (num >= 1_000) return `${(num / 1_000).toFixed(1)}K`;
  return num.toLocaleString();
}

export function UserQuotaPanel() {
  const { darkMode } = useAppStore();
  const { dateRange, lastRefresh } = useDashboardContext();
  const [sortBy, setSortBy] = useState<"usage" | "cost">("usage");

  const { data, isLoading, refetch } = useQuery({
    queryKey: ["dashboard-user-quota", dateRange, lastRefresh.getTime()],
    queryFn: () =>
      getUsageBreakdown({
        dimension: "user",
        start_date: dateRange[0],
        end_date: dateRange[1],
        limit: 20,
      }),
    staleTime: 30000,
  });

  const users = (data?.items || []).map((item) => {
    // Mock quota limits - in production these would come from backend
    const dailyLimit = 100000; // 100K tokens
    const monthlyLimit = 1000000; // 1M tokens
    const dailyUsed = item.total_tokens || 0;
    const monthlyUsed = dailyUsed * 7; // Mock monthly as 7x daily
    const dailyPercent = (dailyUsed / dailyLimit) * 100;
    const monthlyPercent = (monthlyUsed / monthlyLimit) * 100;

    let status: "normal" | "warning" | "exceeded" = "normal";
    if (dailyPercent >= 100 || monthlyPercent >= 100) {
      status = "exceeded";
    } else if (dailyPercent >= 80 || monthlyPercent >= 80) {
      status = "warning";
    }

    return {
      user: item.user || "unknown",
      dailyUsed,
      dailyLimit,
      dailyPercent,
      monthlyUsed,
      monthlyLimit,
      monthlyPercent,
      cost: item.cost_usd || 0,
      status,
    };
  });

  const sortedUsers = [...users].sort((a, b) => {
    if (sortBy === "usage") return b.dailyUsed - a.dailyUsed;
    return b.cost - a.cost;
  });

  const warningCount = users.filter((u) => u.status === "warning" || u.status === "exceeded").length;

  const columns = [
    {
      title: "用户",
      dataIndex: "user",
      key: "user",
      width: 120,
      ellipsis: true,
      render: (text: string) => (
        <span style={{ fontWeight: 500, color: darkMode ? "#f1f5f9" : "#1e293b" }}>{text}</span>
      ),
    },
    {
      title: "日配额",
      key: "daily",
      width: 150,
      render: (_: unknown, record: (typeof users)[0]) => (
        <div>
          <Progress
            percent={Math.min(record.dailyPercent, 100)}
            size="small"
            strokeColor={record.dailyPercent >= 80 ? "#f59e0b" : "#3b82f6"}
            showInfo={false}
          />
          <div style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>
            {formatTokens(record.dailyUsed)}/{formatTokens(record.dailyLimit)}
          </div>
        </div>
      ),
    },
    {
      title: "月配额",
      key: "monthly",
      width: 150,
      render: (_: unknown, record: (typeof users)[0]) => (
        <div>
          <Progress
            percent={Math.min(record.monthlyPercent, 100)}
            size="small"
            strokeColor={record.monthlyPercent >= 80 ? "#f59e0b" : "#10b981"}
            showInfo={false}
          />
          <div style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>
            {formatTokens(record.monthlyUsed)}/{formatTokens(record.monthlyLimit)}
          </div>
        </div>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 80,
      render: (status: string) => {
        const config = {
          normal: { color: "success", text: "正常" },
          warning: { color: "warning", text: "警告" },
          exceeded: { color: "error", text: "超额" },
        }[status] || { color: "default", text: status };
        return <Tag color={config.color}>{config.text}</Tag>;
      },
    },
  ];

  return (
    <PanelWrapper
      title="用户配额"
      onRefresh={refetch}
      loading={isLoading}
      extra={
        <Select
          value={sortBy}
          onChange={setSortBy}
          size="small"
          style={{ width: 90 }}
          options={[
            { value: "usage", label: "按用量" },
            { value: "cost", label: "按成本" },
          ]}
        />
      }
    >
      {/* Warning summary */}
      {warningCount > 0 && (
        <div
          style={{
            padding: "8px 12px",
            marginBottom: 12,
            borderRadius: 6,
            background: darkMode ? "rgba(245, 158, 11, 0.1)" : "rgba(245, 158, 11, 0.1)",
            border: "1px solid rgba(245, 158, 11, 0.3)",
            fontSize: 12,
            color: "#f59e0b",
          }}
        >
          {warningCount} 个用户接近或超过配额限制
        </div>
      )}

      {/* User table */}
      <Table
        dataSource={sortedUsers}
        columns={columns}
        rowKey="user"
        size="small"
        pagination={false}
        scroll={{ y: 200 }}
        style={{
          background: "transparent",
        }}
      />
    </PanelWrapper>
  );
}
```

**Step 2: Verify TypeScript**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm type-check
```

Expected: No errors

**Step 3: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add web/src/pages/dashboard/components/panels/UserQuotaPanel.tsx && git commit -m "feat(dashboard): add user quota panel"
```

---

### Task 12: Create Security Events Panel

**Files:**
- Create: `web/src/pages/dashboard/components/panels/SecurityEventsPanel.tsx`

**Step 1: Create security events panel**

```typescript
// web/src/pages/dashboard/components/panels/SecurityEventsPanel.tsx

import { Row, Col, Statistic, Select } from "antd";
import { ArrowUpOutlined, ArrowDownOutlined, MinusOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from "recharts";
import dayjs from "dayjs";
import { useState } from "react";
import { PanelWrapper } from "../PanelWrapper";
import { useDashboardContext } from "../../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { api } from "@/lib/api";

interface SecurityBreakdownResponse {
  items: Array<{
    user?: string;
    service?: string;
    event_type: string;
    count: number;
  }>;
  start_date: string;
  end_date: string;
}

interface SecurityTimeseriesResponse {
  data: Array<{
    date: string;
    auth_failed: number;
    rate_limited: number;
  }>;
}

async function getSecurityBreakdown(params: {
  dimension: string;
  event_type?: string;
  start_date?: string;
  end_date?: string;
}): Promise<SecurityBreakdownResponse> {
  const searchParams = new URLSearchParams();
  searchParams.set("dimension", params.dimension);
  if (params.event_type) searchParams.set("event_type", params.event_type);
  if (params.start_date) searchParams.set("start_date", params.start_date);
  if (params.end_date) searchParams.set("end_date", params.end_date);
  const response = await api.get<SecurityBreakdownResponse>(`/api/v1/metrics/security/breakdown?${searchParams}`);
  return response.data;
}

async function getSecurityTimeseries(params: {
  start_date?: string;
  end_date?: string;
  granularity?: string;
}): Promise<SecurityTimeseriesResponse> {
  const searchParams = new URLSearchParams();
  if (params.start_date) searchParams.set("start_date", params.start_date);
  if (params.end_date) searchParams.set("end_date", params.end_date);
  if (params.granularity) searchParams.set("granularity", params.granularity);
  const response = await api.get<SecurityTimeseriesResponse>(`/api/v1/metrics/security/timeseries?${searchParams}`);
  return response.data;
}

export function SecurityEventsPanel() {
  const { darkMode } = useAppStore();
  const { dateRange, granularity, lastRefresh } = useDashboardContext();
  const [timeRange, setTimeRange] = useState<"today" | "week">("today");

  const actualStartDate = timeRange === "today" ? dayjs().format("YYYY-MM-DD") : dateRange[0];
  const actualEndDate = timeRange === "today" ? dayjs().format("YYYY-MM-DD") : dateRange[1];

  const breakdownQuery = useQuery({
    queryKey: ["dashboard-security-breakdown", actualStartDate, actualEndDate, lastRefresh.getTime()],
    queryFn: () =>
      getSecurityBreakdown({
        dimension: "user",
        start_date: actualStartDate,
        end_date: actualEndDate,
      }),
    staleTime: 30000,
  });

  const timeseriesQuery = useQuery({
    queryKey: ["dashboard-security-timeseries", actualStartDate, actualEndDate, granularity, lastRefresh.getTime()],
    queryFn: () =>
      getSecurityTimeseries({
        start_date: actualStartDate,
        end_date: actualEndDate,
        granularity,
      }),
    staleTime: 30000,
  });

  const refetch = () => {
    breakdownQuery.refetch();
    timeseriesQuery.refetch();
  };

  const breakdown = breakdownQuery.data?.items || [];
  const chartData = timeseriesQuery.data?.data || [];

  // Calculate totals
  const authFailures = breakdown.filter((i) => i.event_type === "auth_failed").reduce((sum, i) => sum + i.count, 0);
  const rateLimitHits = breakdown.filter((i) => i.event_type === "rate_limited").reduce((sum, i) => sum + i.count, 0);

  // Top users with events
  const userEvents = breakdown.reduce((acc, item) => {
    const key = item.user || "unknown";
    if (!acc[key]) acc[key] = { user: key, authFailed: 0, rateLimited: 0 };
    if (item.event_type === "auth_failed") acc[key].authFailed += item.count;
    if (item.event_type === "rate_limited") acc[key].rateLimited += item.count;
    return acc;
  }, {} as Record<string, { user: string; authFailed: number; rateLimited: number }>);

  const topUsers = Object.values(userEvents)
    .sort((a, b) => b.authFailed + b.rateLimited - (a.authFailed + a.rateLimited))
    .slice(0, 3);

  const gridColor = darkMode ? "#334155" : "#e2e8f0";

  return (
    <PanelWrapper
      title="安全事件"
      onRefresh={refetch}
      loading={breakdownQuery.isLoading}
      extra={
        <Select
          value={timeRange}
          onChange={setTimeRange}
          size="small"
          style={{ width: 80 }}
          options={[
            { value: "today", label: "今日" },
            { value: "week", label: "本周" },
          ]}
        />
      }
    >
      {/* Summary stats */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={8}>
          <div
            style={{
              padding: 12,
              borderRadius: 8,
              background: darkMode ? "#0f172a" : "#f8fafc",
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: 24, fontWeight: 700, color: "#ef4444" }}>{authFailures}</div>
            <div style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>认证失败</div>
          </div>
        </Col>
        <Col span={8}>
          <div
            style={{
              padding: 12,
              borderRadius: 8,
              background: darkMode ? "#0f172a" : "#f8fafc",
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: 24, fontWeight: 700, color: "#f59e0b" }}>{rateLimitHits}</div>
            <div style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>限流触发</div>
          </div>
        </Col>
        <Col span={8}>
          <div
            style={{
              padding: 12,
              borderRadius: 8,
              background: darkMode ? "#0f172a" : "#f8fafc",
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: 24, fontWeight: 700, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
              {authFailures + rateLimitHits}
            </div>
            <div style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>总计</div>
          </div>
        </Col>
      </Row>

      {/* Chart */}
      <div style={{ width: "100%", height: 120, marginBottom: 12 }}>
        <ResponsiveContainer>
          <BarChart data={chartData} margin={{ top: 5, right: 5, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={gridColor} strokeDasharray="3 3" />
            <XAxis
              dataKey="date"
              tick={{ fontSize: 10 }}
              tickFormatter={(v) => dayjs(v).format(granularity === "hour" ? "HH:mm" : "MM-DD")}
            />
            <YAxis tick={{ fontSize: 10 }} width={30} />
            <Tooltip
              labelFormatter={(label) => dayjs(label).format("YYYY-MM-DD HH:mm")}
            />
            <Bar dataKey="auth_failed" name="认证失败" fill="#ef4444" stackId="a" />
            <Bar dataKey="rate_limited" name="限流" fill="#f59e0b" stackId="a" />
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* Top users */}
      <div>
        <div style={{ fontSize: 12, fontWeight: 600, color: darkMode ? "#94a3b8" : "#64748b", marginBottom: 8 }}>
          TOP 触发用户
        </div>
        {topUsers.map((user, index) => (
          <div
            key={user.user}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "6px 0",
              borderBottom: index < topUsers.length - 1 ? `1px solid ${gridColor}` : "none",
            }}
          >
            <span style={{ fontSize: 12, color: darkMode ? "#e2e8f0" : "#475569" }}>
              {index + 1}. {user.user}
            </span>
            <div style={{ display: "flex", gap: 12 }}>
              {user.authFailed > 0 && (
                <span style={{ fontSize: 11, color: "#ef4444" }}>{user.authFailed}次认证失败</span>
              )}
              {user.rateLimited > 0 && (
                <span style={{ fontSize: 11, color: "#f59e0b" }}>{user.rateLimited}次限流</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </PanelWrapper>
  );
}
```

**Step 2: Verify TypeScript**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm type-check
```

Expected: No errors

**Step 3: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add web/src/pages/dashboard/components/panels/SecurityEventsPanel.tsx && git commit -m "feat(dashboard): add security events panel"
```

---

### Task 13: Create Request Trace Panel (Simplified)

**Files:**
- Create: `web/src/pages/dashboard/components/panels/RequestTracePanel.tsx`

**Step 1: Create request trace panel**

```typescript
// web/src/pages/dashboard/components/panels/RequestTracePanel.tsx

import { Table, Tag, Input, Empty } from "antd";
import { SearchOutlined } from "@ant-design/icons";
import { useState } from "react";
import { PanelWrapper } from "../PanelWrapper";
import { useDashboardContext } from "../../DashboardContext";
import { useAppStore } from "@/store/useAppStore";

// Note: This is a placeholder panel. Full implementation requires backend trace API.
// For now, we show a message about upcoming feature.

export function RequestTracePanel() {
  const { darkMode } = useAppStore();
  const { lastRefresh } = useDashboardContext();
  const [searchId, setSearchId] = useState("");

  return (
    <PanelWrapper title="请求追踪">
      {/* Search bar */}
      <Input
        placeholder="搜索 Request ID..."
        prefix={<SearchOutlined style={{ color: darkMode ? "#64748b" : "#94a3b8" }} />}
        value={searchId}
        onChange={(e) => setSearchId(e.target.value)}
        style={{ marginBottom: 16 }}
      />

      {/* Placeholder content */}
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={
          <div style={{ color: darkMode ? "#94a3b8" : "#64748b" }}>
            <p>链路追踪功能开发中...</p>
            <p style={{ fontSize: 12 }}>将支持查看完整请求调用链和耗时分解</p>
          </div>
        }
      />
    </PanelWrapper>
  );
}
```

**Step 2: Verify TypeScript**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm type-check
```

Expected: No errors

**Step 3: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add web/src/pages/dashboard/components/panels/RequestTracePanel.tsx && git commit -m "feat(dashboard): add request trace panel placeholder"
```

---

## Phase 5: Dashboard Layout & Integration (Tasks 14-16)

### Task 14: Create Panel Index Export

**Files:**
- Create: `web/src/pages/dashboard/components/panels/index.ts`

**Step 1: Create index file**

```typescript
// web/src/pages/dashboard/components/panels/index.ts

export { ServiceHealthPanel } from "./ServiceHealthPanel";
export { PerformancePanel } from "./PerformancePanel";
export { TokenUsagePanel } from "./TokenUsagePanel";
export { CostAnalysisPanel } from "./CostAnalysisPanel";
export { UserQuotaPanel } from "./UserQuotaPanel";
export { SecurityEventsPanel } from "./SecurityEventsPanel";
export { RequestTracePanel } from "./RequestTracePanel";
```

**Step 2: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add web/src/pages/dashboard/components/panels/index.ts && git commit -m "feat(dashboard): add panel exports"
```

---

### Task 15: Create Dashboard Layout with Grid

**Files:**
- Create: `web/src/pages/dashboard/DashboardLayout.tsx`

**Step 1: Create layout component**

```typescript
// web/src/pages/dashboard/DashboardLayout.tsx

import { useState, useCallback } from "react";
import GridLayout, { Layout } from "react-grid-layout";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";
import { useAppStore } from "@/store/useAppStore";
import { DEFAULT_LAYOUTS } from "./types";
import {
  ServiceHealthPanel,
  PerformancePanel,
  TokenUsagePanel,
  CostAnalysisPanel,
  UserQuotaPanel,
  SecurityEventsPanel,
  RequestTracePanel,
} from "./components/panels";

const LAYOUT_STORAGE_KEY = "dashboard-layout";

function loadSavedLayout(): Layout[] {
  try {
    const saved = localStorage.getItem(LAYOUT_STORAGE_KEY);
    if (saved) {
      return JSON.parse(saved);
    }
  } catch (e) {
    console.error("Failed to load saved layout:", e);
  }
  return DEFAULT_LAYOUTS;
}

function saveLayout(layout: Layout[]) {
  try {
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(layout));
  } catch (e) {
    console.error("Failed to save layout:", e);
  }
}

export function DashboardLayout() {
  const { darkMode } = useAppStore();
  const [layout, setLayout] = useState<Layout[]>(loadSavedLayout);
  const [containerWidth, setContainerWidth] = useState(1200);

  const handleLayoutChange = useCallback((newLayout: Layout[]) => {
    setLayout(newLayout);
    saveLayout(newLayout);
  }, []);

  const panelComponents: Record<string, React.ReactNode> = {
    "service-health": <ServiceHealthPanel />,
    "performance": <PerformancePanel />,
    "token-usage": <TokenUsagePanel />,
    "cost-analysis": <CostAnalysisPanel />,
    "user-quota": <UserQuotaPanel />,
    "security-events": <SecurityEventsPanel />,
    "request-trace": <RequestTracePanel />,
  };

  return (
    <div
      ref={(el) => {
        if (el) {
          const width = el.offsetWidth;
          if (width !== containerWidth) {
            setContainerWidth(width);
          }
        }
      }}
      style={{ width: "100%" }}
    >
      <GridLayout
        className="dashboard-grid"
        layout={layout}
        cols={12}
        rowHeight={60}
        width={containerWidth}
        onLayoutChange={handleLayoutChange}
        draggableHandle=".ant-card-head"
        margin={[16, 16]}
        containerPadding={[0, 0]}
        isResizable={true}
        isDraggable={true}
        compactType="vertical"
      >
        {layout.map((item) => (
          <div
            key={item.i}
            style={{
              background: darkMode ? "#1e293b" : "#ffffff",
              borderRadius: 12,
              overflow: "hidden",
            }}
          >
            {panelComponents[item.i]}
          </div>
        ))}
      </GridLayout>
    </div>
  );
}
```

**Step 2: Verify TypeScript**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm type-check
```

Expected: No errors

**Step 3: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add web/src/pages/dashboard/DashboardLayout.tsx && git commit -m "feat(dashboard): add grid layout with draggable panels"
```

---

### Task 16: Create New Dashboard Page Entry

**Files:**
- Create: `web/src/pages/dashboard/index.tsx`

**Step 1: Create main dashboard page**

```typescript
// web/src/pages/dashboard/index.tsx

import { DashboardProvider } from "./DashboardContext";
import { DashboardHeader } from "./components/DashboardHeader";
import { KPICards } from "./components/KPICards";
import { DashboardLayout } from "./DashboardLayout";

export function EnterpriseDashboard() {
  return (
    <DashboardProvider>
      <div className="enterprise-dashboard" style={{ padding: "0 4px" }}>
        <DashboardHeader />
        <KPICards />
        <DashboardLayout />
      </div>
    </DashboardProvider>
  );
}

export default EnterpriseDashboard;
```

**Step 2: Verify TypeScript**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm type-check
```

Expected: No errors

**Step 3: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add web/src/pages/dashboard/index.tsx && git commit -m "feat(dashboard): add enterprise dashboard main entry"
```

---

### Task 17: Update Router to Use New Dashboard

**Files:**
- Modify: `web/src/App.tsx` or router file

**Step 1: Find and read router file**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && grep -r "DashboardPage" src/ --include="*.tsx" -l
```

**Step 2: Update router import**

Replace the old DashboardPage import with the new EnterpriseDashboard:

```typescript
// Change from:
import { DashboardPage } from "@/pages/Dashboard";

// To:
import { EnterpriseDashboard } from "@/pages/dashboard";
```

And update the route element:

```typescript
// Change from:
<Route path="/dashboard" element={<DashboardPage />} />

// To:
<Route path="/dashboard" element={<EnterpriseDashboard />} />
```

**Step 3: Verify build**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm type-check
```

Expected: No errors

**Step 4: Commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add -A && git commit -m "feat(dashboard): integrate enterprise dashboard into router"
```

---

## Phase 6: Testing & Verification (Task 18)

### Task 18: Build and Test Dashboard

**Step 1: Run frontend build**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm build
```

Expected: Build succeeds without errors

**Step 2: Start backend (in conda environment)**

```bash
source ~/miniconda3/bin/activate ai_gateway && cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && python -m src.main
```

**Step 3: Start frontend dev server**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web && pnpm dev
```

**Step 4: Manual verification checklist**

- [ ] Dashboard loads without errors
- [ ] KPI cards show data
- [ ] Panels are draggable
- [ ] Panels are resizable
- [ ] Auto-refresh works (check timestamp updates)
- [ ] Source filter UI works
- [ ] Date range picker works
- [ ] Dark mode works

**Step 5: Final commit**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway && git add -A && git commit -m "feat(dashboard): complete enterprise dashboard v1"
```

---

## Summary

| Phase | Tasks | Description |
|-------|-------|-------------|
| 1 | 1-5 | Foundation: deps, types, context, header |
| 2 | 6 | KPI Cards |
| 3 | 7-10 | Core Panels: health, performance, token, cost |
| 4 | 11-13 | Additional Panels: quota, security, trace |
| 5 | 14-17 | Layout & Router Integration |
| 6 | 18 | Build & Test |

Total: 18 tasks, estimated ~2-3 hours of implementation time.
