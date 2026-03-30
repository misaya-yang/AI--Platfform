// web/src/pages/dashboard/index.tsx
// Enterprise Dashboard — Compact tab-based layout

import { useEffect, useRef, useState, useCallback } from "react";
import { DashboardProvider, useDashboardContext } from "./DashboardContext";
import { KPICards } from "./components/KPICards";
import { DashboardLayout } from "./DashboardLayout";
import { ProviderStatusCard } from "@/components/ProviderStatusCard";
import { useAppStore } from "@/store/useAppStore";
import { LAYOUT, getColors, TYPOGRAPHY } from "./styles";
import { Select, DatePicker, Tooltip } from "antd";
import { SyncOutlined, ExpandOutlined, AppstoreOutlined, UserOutlined, CalendarOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { useTranslation } from "react-i18next";
import { useDashboardEntityLabels } from "./hooks/useDashboardEntityLabels";
import type { SourceFilter, RefreshInterval } from "./types";

const { RangePicker } = DatePicker;

type DashTab = "summary" | "operations" | "reliability" | "governance" | "tracing";

function loadTab(): DashTab {
  try {
    const s = localStorage.getItem("dashboard-tab-v1");
    if (s === "summary" || s === "operations" || s === "reliability" || s === "governance" || s === "tracing") return s;
  } catch { /* */ }
  return "summary";
}

function DashboardContent() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const containerRef = useRef<HTMLDivElement>(null);
  const [containerWidth, setContainerWidth] = useState(1200);
  const [activeTab, setActiveTab] = useState<DashTab>(loadTab);
  const effectiveWidth = Math.max(containerWidth, LAYOUT.DASHBOARD_MIN_CONTENT_WIDTH);

  const {
    dateRange, granularity, source, serviceId, userId, refreshInterval, lastRefresh,
    setDateRange, setGranularity, setSource, setServiceId, setUserId, setRefreshInterval, triggerRefresh,
  } = useDashboardContext();

  const { serviceOptions, userOptions } = useDashboardEntityLabels();

  const resizeTimeout = useRef<ReturnType<typeof setTimeout>>();
  const handleResize = useCallback(() => {
    clearTimeout(resizeTimeout.current);
    resizeTimeout.current = setTimeout(() => {
      if (containerRef.current) setContainerWidth(containerRef.current.offsetWidth);
    }, 150);
  }, []);

  useEffect(() => {
    if (containerRef.current) setContainerWidth(containerRef.current.offsetWidth);
    const ro = new ResizeObserver(handleResize);
    if (containerRef.current) ro.observe(containerRef.current);
    return () => { ro.disconnect(); clearTimeout(resizeTimeout.current); };
  }, [handleResize]);

  const handleTabChange = (tab: DashTab) => {
    setActiveTab(tab);
    try { localStorage.setItem("dashboard-tab-v1", tab); } catch { /* */ }
  };

  const tabs: { key: DashTab; label: string }[] = [
    { key: "summary", label: "总览" },
    { key: "operations", label: "运营" },
    { key: "reliability", label: "可靠性" },
    { key: "governance", label: "治理" },
    { key: "tracing", label: "追踪" },
  ];

  const workspaceMap: Record<string, string> = {
    operations: "overview", reliability: "reliability", governance: "governance", tracing: "tracing",
  };

  const refreshOptions: { label: string; value: RefreshInterval }[] = [
    { label: "手动", value: 0 }, { label: "30秒", value: 30 },
    { label: "1分钟", value: 60 }, { label: "5分钟", value: 300 },
  ];

  const P = LAYOUT.PAGE_PADDING;

  return (
    <div ref={containerRef} style={{ minHeight: "100%", background: colors.pageBg, padding: `${P}px 0` }}>
      <div style={{ minWidth: LAYOUT.DASHBOARD_MIN_CONTENT_WIDTH }}>

        {/* ─── Row 1: Title + Time Range + Refresh ─── */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <h1 style={{ ...TYPOGRAPHY.pageTitle, margin: 0, color: colors.textPrimary, letterSpacing: "-0.02em" }}>
              {t("metrics.title")}
            </h1>
            <span style={{ fontSize: 11, padding: "3px 10px", borderRadius: 12, background: colors.accentBg, color: colors.accent, fontWeight: 600 }}>
              {dayjs(lastRefresh).format("HH:mm:ss")}
            </span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Select size="small" value={refreshInterval} onChange={setRefreshInterval}
              options={refreshOptions} style={{ width: 80 }} />
            <Tooltip title="刷新"><button onClick={triggerRefresh} className="dash-icon-btn"><SyncOutlined /></button></Tooltip>
            <Tooltip title="全屏"><button onClick={() => document.documentElement.requestFullscreen?.()} className="dash-icon-btn"><ExpandOutlined /></button></Tooltip>
          </div>
        </div>

        {/* ─── Row 2: Inline Filters ─── */}
        <div style={{
          display: "flex", alignItems: "center", gap: 8, marginBottom: 10, flexWrap: "wrap",
          padding: "6px 10px", borderRadius: 8,
          background: darkMode ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.02)',
        }}>
          <AppstoreOutlined style={{ color: colors.textMuted, fontSize: 13 }} />
          <Select size="small" value={serviceId} onChange={setServiceId} options={serviceOptions} style={{ minWidth: 120 }} />
          <UserOutlined style={{ color: colors.textMuted, fontSize: 13 }} />
          <Select size="small" value={userId} onChange={setUserId} options={userOptions} style={{ minWidth: 110 }} />
          <Select size="small" value={source} onChange={(v: string) => setSource(v as SourceFilter)}
            options={[{ label: "全部来源", value: "all" }, { label: "内部", value: "internal" }, { label: "外部", value: "external" }]}
            style={{ minWidth: 90 }} />
          <div style={{ width: 1, height: 18, background: colors.border, margin: "0 2px" }} />
          <CalendarOutlined style={{ color: colors.textMuted, fontSize: 13 }} />
          <RangePicker size="small"
            value={[dayjs(dateRange[0]), dayjs(dateRange[1])]}
            onChange={(d) => d && d[0] && d[1] && setDateRange([d[0].format("YYYY-MM-DD"), d[1].format("YYYY-MM-DD")])}
            format="YYYY-MM-DD" allowClear={false} style={{ width: 220 }} />
          <Select size="small" value={granularity} onChange={setGranularity}
            options={[{ label: "按天", value: "day" }, { label: "按小时", value: "hour" }]}
            style={{ width: 75 }} />
        </div>

        {/* ─── Row 3: Tab Bar ─── */}
        <div style={{ marginBottom: 10 }}>
          <div className="dash-tabs">
            {tabs.map((tab) => (
              <button key={tab.key} className={`dash-tab ${activeTab === tab.key ? "active" : ""}`}
                onClick={() => handleTabChange(tab.key)}>{tab.label}</button>
            ))}
          </div>
        </div>

        {/* ─── Tab Content (all content shares same container padding) ─── */}
        {activeTab === "summary" && (
          <div>
            <div style={{ marginBottom: LAYOUT.CARD_GAP }}><KPICards /></div>
            <div style={{ margin: 0 }}><ProviderStatusCard /></div>
          </div>
        )}
        {activeTab !== "summary" && (
          <div style={{ margin: `0 -${LAYOUT.GRID_GAP}px` }}>
            <DashboardLayout
              width={effectiveWidth}
              forceWorkspace={workspaceMap[activeTab] as "overview" | "reliability" | "governance" | "tracing"}
            />
          </div>
        )}
      </div>

      <style>{`
        .dash-tabs { display: flex; gap: 2px; padding: 3px; border-radius: 8px; background: ${darkMode ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.04)'}; width: fit-content; }
        .dash-tab { padding: 5px 14px; border-radius: 6px; border: none; background: transparent; color: ${darkMode ? '#8B8B8E' : '#6B7280'}; font-size: 13px; font-weight: 500; cursor: pointer; transition: all 0.15s; white-space: nowrap; }
        .dash-tab:hover { color: ${darkMode ? '#EDEDEF' : '#111'}; background: ${darkMode ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.03)'}; }
        .dash-tab.active { color: ${darkMode ? '#EDEDEF' : '#111'}; background: ${darkMode ? 'rgba(255,255,255,0.08)' : '#fff'}; box-shadow: ${darkMode ? 'none' : '0 1px 2px rgba(0,0,0,0.06)'}; }
        .dash-icon-btn { display: flex; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: 6px; border: 1px solid ${darkMode ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}; background: transparent; color: ${darkMode ? '#8B8B8E' : '#6B7280'}; cursor: pointer; transition: all 0.15s; font-size: 13px; }
        .dash-icon-btn:hover { background: ${darkMode ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.04)'}; color: ${darkMode ? '#EDEDEF' : '#111'}; }
      `}</style>
    </div>
  );
}

export function EnterpriseDashboard() {
  return (
    <DashboardProvider>
      <DashboardContent />
    </DashboardProvider>
  );
}

export default EnterpriseDashboard;
