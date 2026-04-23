// web/src/pages/dashboard/index.tsx
// Enterprise Dashboard — Compact tab-based layout

import { useEffect, useRef, useState, useCallback } from "react";
import { DashboardProvider, useDashboardContext } from "./DashboardContext";
import { KPICards } from "./components/KPICards";
import { SummaryCharts } from "./components/SummaryCharts";
import { DashboardLayout } from "./DashboardLayout";
import { ProviderStatusCard } from "@/components/ProviderStatusCard";
import { useAppStore } from "@/store/useAppStore";
import { LAYOUT, getColors, TYPOGRAPHY } from "./styles";
import { Select, DatePicker, Tooltip } from "antd";
import { SyncOutlined, ExpandOutlined } from "@ant-design/icons";
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
    { key: "summary", label: t("dashboard.tabs.summary") },
    { key: "operations", label: t("dashboard.tabs.operations") },
    { key: "reliability", label: t("dashboard.tabs.reliability") },
    { key: "governance", label: t("dashboard.tabs.governance") },
    { key: "tracing", label: t("dashboard.tabs.tracing") },
  ];

  const workspaceMap: Record<string, string> = {
    operations: "overview", reliability: "reliability", governance: "governance", tracing: "tracing",
  };

  const refreshOptions: { label: string; value: RefreshInterval }[] = [
    { label: t("dashboard.refresh.manual"), value: 0 }, { label: t("dashboard.refresh.30s"), value: 30 },
    { label: t("dashboard.refresh.1min"), value: 60 }, { label: t("dashboard.refresh.5min"), value: 300 },
  ];

  const P = LAYOUT.PAGE_PADDING;

  return (
    <div ref={containerRef} style={{ minHeight: "100%", background: colors.pageBg, padding: `${P}px 0` }}>
      <div style={{ minWidth: LAYOUT.DASHBOARD_MIN_CONTENT_WIDTH }}>

        {/* ─── Row 1: Editorial headline + live clock + refresh controls ─── */}
        <div style={{
          display: "flex", alignItems: "flex-end",
          justifyContent: "space-between", gap: 16,
          marginBottom: 18,
        }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
            <div style={{
              fontSize: 10, fontWeight: 600, letterSpacing: "0.14em",
              textTransform: "uppercase", color: colors.textMuted,
              marginBottom: 4,
            }}>
              <span style={{
                display: "inline-block", width: 6, height: 6,
                borderRadius: "50%", background: colors.accent,
                marginRight: 8, verticalAlign: "middle",
                boxShadow: `0 0 0 3px ${colors.accentBg}`,
              }} />
              Gateway · Overview
            </div>
            <h1 style={{
              ...TYPOGRAPHY.pageTitle,
              margin: 0,
              color: colors.textPrimary,
            }}>
              {t("metrics.title")}
            </h1>
            <span style={{
              fontFamily: '"IBM Plex Mono", monospace',
              fontSize: 11, color: colors.textMuted,
              letterSpacing: "0.02em", marginTop: 2,
              fontVariantNumeric: "tabular-nums",
            }}>
              {dayjs(lastRefresh).format("YYYY-MM-DD HH:mm:ss")}
            </span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Select size="small" value={refreshInterval} onChange={setRefreshInterval}
              options={refreshOptions} style={{ width: 90 }} />
            <Tooltip title={t("common.refresh")}><button onClick={triggerRefresh} className="dash-icon-btn" aria-label="refresh"><SyncOutlined /></button></Tooltip>
            <Tooltip title={t("dashboard.actions.fullscreen")}><button onClick={() => document.documentElement.requestFullscreen?.()} className="dash-icon-btn" aria-label="fullscreen"><ExpandOutlined /></button></Tooltip>
          </div>
        </div>

        {/* ─── Row 2: Tabs (hairline + signature red underline) ─── */}
        <div style={{ marginBottom: 14 }}>
          <div className="dash-tabs">
            {tabs.map((tab) => (
              <button key={tab.key} className={`dash-tab ${activeTab === tab.key ? "active" : ""}`}
                onClick={() => handleTabChange(tab.key)}>{tab.label}</button>
            ))}
          </div>
        </div>

        {/* ─── Row 3: Filter strip (subtle, above content) ─── */}
        <div className="dash-filters" style={{
          display: "flex", alignItems: "center",
          gap: 10, marginBottom: 20, flexWrap: "wrap",
          paddingBottom: 14,
          borderBottom: `1px solid ${colors.border}`,
        }}>
          <Select size="small" value={serviceId} onChange={setServiceId} options={serviceOptions} style={{ minWidth: 130 }} />
          <Select size="small" value={userId} onChange={setUserId} options={userOptions} style={{ minWidth: 120 }} />
          <Select size="small" value={source} onChange={(v: string) => setSource(v as SourceFilter)}
            options={[{ label: t("dashboard.filters.allSources"), value: "all" }, { label: t("dashboard.filters.internal"), value: "internal" }, { label: t("dashboard.filters.external"), value: "external" }]}
            style={{ minWidth: 100 }} />
          <RangePicker size="small"
            value={[dayjs(dateRange[0]), dayjs(dateRange[1])]}
            onChange={(d) => d && d[0] && d[1] && setDateRange([d[0].format("YYYY-MM-DD"), d[1].format("YYYY-MM-DD")])}
            format="YYYY-MM-DD" allowClear={false} style={{ width: 230 }} />
          <Select size="small" value={granularity} onChange={setGranularity}
            options={[{ label: t("dashboard.filters.byDay"), value: "day" }, { label: t("dashboard.filters.byHour"), value: "hour" }]}
            style={{ width: 80 }} />
        </div>

        {/* ─── Tab Content ─── */}
        {activeTab === "summary" && (
          <div style={{ display: "flex", flexDirection: "column", gap: LAYOUT.SECTION_GAP }}>
            <KPICards />
            <SummaryCharts />
            <ProviderStatusCard />
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
        .dash-tabs {
          display: flex; gap: 2px; width: 100%;
          border-bottom: 1px solid ${colors.border};
        }
        .dash-tab {
          padding: 10px 18px; border: none; background: transparent;
          color: ${colors.textMuted};
          font-size: 13px; font-weight: 500;
          letter-spacing: -0.005em;
          cursor: pointer;
          transition: color 140ms cubic-bezier(0.16, 1, 0.3, 1);
          white-space: nowrap; position: relative;
          margin-bottom: -1px;
        }
        .dash-tab::after {
          content: ""; position: absolute; left: 18px; right: 18px; bottom: -1px;
          height: 1.5px; background: transparent;
          transition: background 180ms cubic-bezier(0.16, 1, 0.3, 1);
        }
        .dash-tab:hover { color: ${colors.textPrimary}; }
        .dash-tab.active { color: ${colors.textPrimary}; font-weight: 600; }
        .dash-tab.active::after { background: ${colors.accent}; }
        .dash-icon-btn {
          display: flex; align-items: center; justify-content: center;
          width: 30px; height: 30px; border-radius: 6px;
          border: 1px solid ${colors.border};
          background: ${colors.cardBg};
          color: ${colors.textSecondary};
          cursor: pointer; font-size: 13px;
          transition: all 160ms cubic-bezier(0.16, 1, 0.3, 1);
        }
        .dash-icon-btn:hover {
          background: ${colors.cardHover};
          border-color: ${colors.borderHover};
          color: ${colors.textPrimary};
        }
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
