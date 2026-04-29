// web/src/pages/dashboard/index.tsx
// 监控面板 — 1:1 port of design-handoff dashboard.jsx
// Title row · FilterBar · Tabs · KPI row · Charts row · Provider table

import { useEffect, useRef, useState, useCallback } from "react";
import { DashboardProvider, useDashboardContext } from "./DashboardContext";
import { DashboardLayout } from "./DashboardLayout";
import { useAppStore } from "@/store/useAppStore";
import { FONT_FAMILY, LAYOUT, getColors } from "./styles";
import { Select, DatePicker } from "antd";
import dayjs from "dayjs";
import { useTranslation } from "react-i18next";
import { useDashboardEntityLabels } from "./hooks/useDashboardEntityLabels";
import type { SourceFilter, RefreshInterval } from "./types";

const { RangePicker } = DatePicker;

type DashTab = "summary" | "operations" | "reliability" | "governance" | "tracing";
const DASHBOARD_TAB_STORAGE_KEY = "dashboard-tab-v2";

function loadTab(): DashTab {
  try {
    const s = localStorage.getItem(DASHBOARD_TAB_STORAGE_KEY);
    if (s === "summary" || s === "operations" || s === "reliability" || s === "governance" || s === "tracing") return s;
  } catch { /* */ }
  return "operations";
}

// ── Design-handoff SVG icons (clock, refresh, expand, chev, cal) ─────
const ICON = {
  clock: (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <circle cx="6.5" cy="6.5" r="5" stroke="currentColor" strokeWidth="1.3" />
      <path d="M6.5 3.5V6.5L8.5 8" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  refresh: (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M12 7a5 5 0 11-1.5-3.5M12 2v2h-2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  expand: (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M2 5V2h3M12 5V2H9M2 9v3h3M12 9v3H9" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
};

function DashboardContent() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const c = getColors(darkMode);
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
    try { localStorage.setItem(DASHBOARD_TAB_STORAGE_KEY, tab); } catch { /* */ }
  };

  const tabs: { key: DashTab; label: string }[] = [
    { key: "summary", label: t("dashboard.tabs.summary", "总览") },
    { key: "operations", label: t("dashboard.tabs.operations", "运营") },
    { key: "reliability", label: t("dashboard.tabs.reliability", "可靠性") },
    { key: "governance", label: t("dashboard.tabs.governance", "治理") },
    { key: "tracing", label: t("dashboard.tabs.tracing", "追踪") },
  ];

  const workspaceMap: Record<string, string> = {
    summary: "overview",
    operations: "overview", reliability: "reliability", governance: "governance", tracing: "tracing",
  };

  const refreshOptions: { label: string; value: RefreshInterval }[] = [
    { label: t("dashboard.refresh.manual", "手动"), value: 0 },
    { label: t("dashboard.refresh.30s", "30秒"), value: 30 },
    { label: t("dashboard.refresh.1min", "1分钟"), value: 60 },
    { label: t("dashboard.refresh.5min", "5分钟"), value: 300 },
  ];

  return (
    <div
      ref={containerRef}
      style={{
        minHeight: "100%",
        background: c.pageBg,
        fontFamily: FONT_FAMILY.sans,
        color: c.textPrimary,
      }}
    >
      <div style={{ minWidth: LAYOUT.DASHBOARD_MIN_CONTENT_WIDTH }}>

        {/* ── Command header ── */}
        <div style={{
          display: "flex", alignItems: "center", gap: 16,
          padding: "16px 24px 0",
          justifyContent: "space-between",
          flexWrap: "wrap",
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12, minWidth: 0 }}>
            <div style={{
              width: 4,
              height: 28,
              borderRadius: 999,
              background: c.operator,
              flexShrink: 0,
            }} />
            <div>
              <h1 style={{
                fontSize: 18, fontWeight: 700, color: c.textPrimary,
                margin: 0, letterSpacing: "0",
              }}>
                {t("metrics.title", "监控面板")}
              </h1>
              <div style={{ fontSize: 12, color: c.textSecondary, marginTop: 3 }}>
                {t("dashboard.command.subtitle", "AI Gateway operations, reliability, governance and trace observability")}
              </div>
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{
              display: "flex", alignItems: "center", gap: 6,
              padding: "5px 10px", borderRadius: 8,
              background: c.cardBg, border: `1px solid ${c.borderSoft}`,
              fontSize: 12, color: c.textSecondary, fontFamily: FONT_FAMILY.mono,
            }}>
              <span style={{ display: "flex", color: c.textMuted }}>{ICON.clock}</span>
              <span>{dayjs(lastRefresh).format("YYYY-MM-DD HH:mm:ss")}</span>
            </div>
          </div>
        </div>

        {/* ── Filter bar ── */}
        <div style={{
          display: "flex", alignItems: "center", gap: 10,
          padding: "14px 24px 0", flexWrap: "wrap",
        }}>
          <Select
            size="middle"
            value={serviceId}
            onChange={setServiceId}
            options={serviceOptions}
            style={{ minWidth: 130 }}
            className="dash-filter-select"
          />
          <Select
            size="middle"
            value={userId}
            onChange={setUserId}
            options={userOptions}
            style={{ minWidth: 120 }}
            className="dash-filter-select"
          />
          <Select
            size="middle"
            value={source}
            onChange={(v: string) => setSource(v as SourceFilter)}
            options={[
              { label: t("dashboard.filters.allSources", "全部来源"), value: "all" },
              { label: t("dashboard.filters.internal", "内部"), value: "internal" },
              { label: t("dashboard.filters.external", "外部"), value: "external" },
            ]}
            style={{ minWidth: 110 }}
            className="dash-filter-select"
          />
          <RangePicker
            size="middle"
            value={[dayjs(dateRange[0]), dayjs(dateRange[1])]}
            onChange={(d) => d && d[0] && d[1] && setDateRange([d[0].format("YYYY-MM-DD"), d[1].format("YYYY-MM-DD")])}
            format="YYYY-MM-DD"
            allowClear={false}
            style={{ width: 250 }}
            className="dash-filter-range"
          />
          <Select
            size="middle"
            value={granularity}
            onChange={setGranularity}
            options={[
              { label: t("dashboard.filters.byDay", "按天"), value: "day" },
              { label: t("dashboard.filters.byHour", "按小时"), value: "hour" },
            ]}
            style={{ width: 96 }}
            className="dash-filter-select"
          />
          <div style={{ flex: 1 }} />
          <Select
            size="middle"
            value={refreshInterval}
            onChange={setRefreshInterval}
            options={refreshOptions}
            style={{ width: 96 }}
            className="dash-filter-select"
          />
          <button onClick={triggerRefresh} className="dash-icon-btn" aria-label="refresh">{ICON.refresh}</button>
          <button
            onClick={() => document.documentElement.requestFullscreen?.()}
            className="dash-icon-btn"
            aria-label="fullscreen"
          >
            {ICON.expand}
          </button>
        </div>

        {/* ── Tabs ── */}
        <div style={{
          display: "flex", gap: 24,
          padding: "14px 24px 0",
          borderBottom: `1px solid ${c.borderSoft}`,
          margin: "14px 0 0",
        }}>
          {tabs.map((tab) => {
            const active = activeTab === tab.key;
            return (
              <div
                key={tab.key}
                onClick={() => handleTabChange(tab.key)}
                className={`dash-tab ${active ? "active" : ""}`}
                style={{
                  padding: "8px 2px 12px",
                  fontSize: 13.5,
                  fontWeight: active ? 600 : 500,
                  color: active ? c.accent : c.textSecondary,
                  cursor: "pointer",
                  borderBottom: `2px solid ${active ? c.accent : "transparent"}`,
                  marginBottom: -1,
                  transition: ".15s",
                }}
              >
                {tab.label}
              </div>
            );
          })}
        </div>

        {/* ── Tab content ── */}
        <div style={{ padding: "12px 0 18px", margin: `0 -${LAYOUT.GRID_GAP}px` }}>
          <DashboardLayout
            width={effectiveWidth}
            forceWorkspace={workspaceMap[activeTab] as "overview" | "reliability" | "governance" | "tracing"}
          />
        </div>
      </div>

      <style>{`
        /* Filter select — match design's 34px pill */
        .dash-filter-select .ant-select-selector {
          height: 34px !important;
          border-radius: 8px !important;
          border-color: ${c.border} !important;
          background: ${c.cardBg} !important;
          padding: 0 12px !important;
          display: flex;
          align-items: center;
          font-size: 12.5px !important;
        }
        .dash-filter-select .ant-select-selection-item {
          line-height: 32px !important;
          color: ${c.textPrimary};
        }
        .dash-filter-select .ant-select-arrow { color: ${c.textFaint}; }
        .dash-filter-select:hover .ant-select-selector { border-color: ${c.borderHover} !important; }

        /* Range picker — match same 34px pill */
        .dash-filter-range {
          height: 34px !important;
          border-radius: 8px !important;
          border-color: ${c.border} !important;
          background: ${c.cardBg} !important;
          font-family: ${FONT_FAMILY.mono} !important;
          font-size: 12.5px !important;
        }
        .dash-filter-range:hover { border-color: ${c.borderHover} !important; }
        .dash-filter-range .ant-picker-input input {
          font-family: ${FONT_FAMILY.mono} !important;
          font-size: 12.5px !important;
          color: ${c.textPrimary} !important;
        }
        .dash-filter-range .ant-picker-suffix { color: ${c.textFaint} !important; }

        /* Icon buttons in header */
        .dash-icon-btn {
          width: 34px; height: 34px; border-radius: 8px;
          border: none; background: transparent;
          display: inline-flex; align-items: center; justify-content: center;
          color: ${c.textSecondary};
          cursor: pointer;
          transition: all .14s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .dash-icon-btn:hover {
          background: ${c.cardHover};
          color: ${c.textPrimary};
        }

        /* Tabs hover */
        .dash-tab:hover:not(.active) { color: ${c.textPrimary} !important; }
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
