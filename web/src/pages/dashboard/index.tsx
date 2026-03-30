// web/src/pages/dashboard/index.tsx
// Enterprise Dashboard - Unified Layout System

import { useEffect, useRef, useState, useCallback } from "react";
import { DashboardProvider } from "./DashboardContext";
import { DashboardHeader } from "./components/DashboardHeader";
import { KPICards } from "./components/KPICards";
import { DashboardLayout } from "./DashboardLayout";
import { ProviderStatusCard } from "@/components/ProviderStatusCard";
import { useAppStore } from "@/store/useAppStore";
import { LAYOUT, getColors } from "./styles";

function DashboardContent() {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const containerRef = useRef<HTMLDivElement>(null);
  const [containerWidth, setContainerWidth] = useState(1200);
  const effectiveDashboardWidth = Math.max(
    containerWidth,
    LAYOUT.DASHBOARD_MIN_CONTENT_WIDTH
  );

  // Debounced ResizeObserver (150ms) to prevent excessive re-renders
  const resizeTimeout = useRef<ReturnType<typeof setTimeout>>();
  const handleResize = useCallback(() => {
    clearTimeout(resizeTimeout.current);
    resizeTimeout.current = setTimeout(() => {
      if (containerRef.current) {
        setContainerWidth(containerRef.current.offsetWidth);
      }
    }, 150);
  }, []);

  useEffect(() => {
    if (containerRef.current) {
      setContainerWidth(containerRef.current.offsetWidth);
    }
    const resizeObserver = new ResizeObserver(handleResize);
    if (containerRef.current) {
      resizeObserver.observe(containerRef.current);
    }
    return () => {
      resizeObserver.disconnect();
      clearTimeout(resizeTimeout.current);
    };
  }, [handleResize]);

  return (
    <div
      ref={containerRef}
      style={{
        minHeight: "100%",
        padding: `${LAYOUT.PAGE_PADDING}px 0`,
        background: colors.pageBg,
      }}
    >
      <div style={{ minWidth: LAYOUT.DASHBOARD_MIN_CONTENT_WIDTH }}>
        {/* Header with controls */}
        <div
          className="dashboard-section"
          style={{ padding: `0 ${LAYOUT.PAGE_PADDING}px`, marginBottom: LAYOUT.SECTION_GAP }}
        >
          <DashboardHeader />
        </div>

        {/* KPI Summary Cards */}
        <div
          className="dashboard-section"
          style={{ padding: `0 ${LAYOUT.PAGE_PADDING}px`, marginBottom: LAYOUT.SECTION_GAP }}
        >
          <KPICards />
        </div>

        {/* Provider Status */}
        <div className="dashboard-section">
          <ProviderStatusCard />
        </div>

        {/* Panel Grid */}
        <div
          className="dashboard-section"
          style={{ padding: `0 ${LAYOUT.PAGE_PADDING - LAYOUT.GRID_GAP}px` }}
        >
          <DashboardLayout
            width={
              effectiveDashboardWidth - (LAYOUT.PAGE_PADDING * 2) + (LAYOUT.GRID_GAP * 2)
            }
          />
        </div>
      </div>

      {/* Stagger fade-in + prefers-reduced-motion */}
      <style>{`
        .dashboard-section {
          animation: dashFadeIn 400ms ease-out both;
        }
        .dashboard-section:nth-child(1) { animation-delay: 0ms; }
        .dashboard-section:nth-child(2) { animation-delay: 50ms; }
        .dashboard-section:nth-child(3) { animation-delay: 100ms; }
        .dashboard-section:nth-child(4) { animation-delay: 150ms; }

        @keyframes dashFadeIn {
          from { opacity: 0; transform: translateY(8px); }
          to { opacity: 1; transform: translateY(0); }
        }

        @media (prefers-reduced-motion: reduce) {
          .dashboard-section {
            animation: none !important;
          }
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

// Default export for lazy loading
export default EnterpriseDashboard;
