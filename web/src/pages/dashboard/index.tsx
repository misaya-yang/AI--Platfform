// web/src/pages/dashboard/index.tsx
// Enterprise Dashboard — Tab-based layout (no scrolling between sections)

import { useEffect, useRef, useState, useCallback } from "react";
import { DashboardProvider } from "./DashboardContext";
import { DashboardHeader } from "./components/DashboardHeader";
import { KPICards } from "./components/KPICards";
import { DashboardLayout } from "./DashboardLayout";
import { ProviderStatusCard } from "@/components/ProviderStatusCard";
import { useAppStore } from "@/store/useAppStore";
import { LAYOUT, getColors } from "./styles";

type DashTab = "summary" | "operations" | "reliability" | "governance" | "tracing";

const TAB_STORAGE_KEY = "dashboard-tab-v1";

function loadTab(): DashTab {
  try {
    const s = localStorage.getItem(TAB_STORAGE_KEY);
    if (s === "summary" || s === "operations" || s === "reliability" || s === "governance" || s === "tracing") return s;
  } catch { /* */ }
  return "summary";
}

function DashboardContent() {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const containerRef = useRef<HTMLDivElement>(null);
  const [containerWidth, setContainerWidth] = useState(1200);
  const [activeTab, setActiveTab] = useState<DashTab>(loadTab);
  const effectiveDashboardWidth = Math.max(containerWidth, LAYOUT.DASHBOARD_MIN_CONTENT_WIDTH);

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
    try { localStorage.setItem(TAB_STORAGE_KEY, tab); } catch { /* */ }
  };

  const tabs: { key: DashTab; label: string }[] = [
    { key: "summary", label: "总览" },
    { key: "operations", label: "运营" },
    { key: "reliability", label: "可靠性" },
    { key: "governance", label: "治理" },
    { key: "tracing", label: "追踪" },
  ];

  // Map tab to DashboardLayout workspace key
  const workspaceMap: Record<string, string> = {
    operations: "overview",
    reliability: "reliability",
    governance: "governance",
    tracing: "tracing",
  };

  return (
    <div
      ref={containerRef}
      style={{ minHeight: "100%", padding: `${LAYOUT.PAGE_PADDING}px 0`, background: colors.pageBg }}
    >
      <div style={{ minWidth: LAYOUT.DASHBOARD_MIN_CONTENT_WIDTH }}>
        {/* Header */}
        <div style={{ padding: `0 ${LAYOUT.PAGE_PADDING}px`, marginBottom: 8 }}>
          <DashboardHeader />
        </div>

        {/* ─── Tab Bar ─── */}
        <div style={{ padding: `0 ${LAYOUT.PAGE_PADDING}px`, marginBottom: 10 }}>
          <div className="dash-tabs">
            {tabs.map((tab) => (
              <button
                key={tab.key}
                className={`dash-tab ${activeTab === tab.key ? "active" : ""}`}
                onClick={() => handleTabChange(tab.key)}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>

        {/* ─── Tab Content ─── */}
        {activeTab === "summary" && (
          <>
            <div style={{ padding: `0 ${LAYOUT.PAGE_PADDING}px`, marginBottom: LAYOUT.GRID_GAP }}>
              <KPICards />
            </div>
            <div>
              <ProviderStatusCard />
            </div>
          </>
        )}

        {activeTab !== "summary" && (
          <div style={{ padding: `0 ${LAYOUT.PAGE_PADDING - LAYOUT.GRID_GAP}px` }}>
            <DashboardLayout
              width={effectiveDashboardWidth - (LAYOUT.PAGE_PADDING * 2) + (LAYOUT.GRID_GAP * 2)}
              forceWorkspace={workspaceMap[activeTab] as "overview" | "reliability" | "governance" | "tracing"}
            />
          </div>
        )}
      </div>

      <style>{`
        .dash-tabs {
          display: flex;
          gap: 2px;
          padding: 3px;
          border-radius: 8px;
          background: ${darkMode ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.04)'};
          width: fit-content;
        }
        .dash-tab {
          padding: 6px 16px;
          border-radius: 6px;
          border: none;
          background: transparent;
          color: ${darkMode ? '#8B8B8E' : '#6B7280'};
          font-size: 13px;
          font-weight: 500;
          cursor: pointer;
          transition: all 0.15s;
          white-space: nowrap;
        }
        .dash-tab:hover {
          color: ${darkMode ? '#EDEDEF' : '#111'};
          background: ${darkMode ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.03)'};
        }
        .dash-tab.active {
          color: ${darkMode ? '#EDEDEF' : '#111'};
          background: ${darkMode ? 'rgba(255,255,255,0.08)' : '#fff'};
          box-shadow: ${darkMode ? 'none' : '0 1px 2px rgba(0,0,0,0.06)'};
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
