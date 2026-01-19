// web/src/pages/dashboard/index.tsx
// Enterprise Dashboard - Unified Layout System

import { useEffect, useRef, useState } from "react";
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

  useEffect(() => {
    const updateWidth = () => {
      if (containerRef.current) {
        setContainerWidth(containerRef.current.offsetWidth);
      }
    };

    updateWidth();

    // Watch for resize
    const resizeObserver = new ResizeObserver(updateWidth);
    if (containerRef.current) {
      resizeObserver.observe(containerRef.current);
    }

    return () => resizeObserver.disconnect();
  }, []);

  return (
    <div
      ref={containerRef}
      style={{
        minHeight: "100%",
        padding: `${LAYOUT.PAGE_PADDING}px 0`, // Vertical padding only
        background: colors.pageBg,
      }}
    >
      {/* Header with controls */}
      <div style={{ padding: `0 ${LAYOUT.PAGE_PADDING}px`, marginBottom: LAYOUT.SECTION_GAP }}>
        <DashboardHeader />
      </div>

      {/* KPI Summary Cards */}
      <div style={{ padding: `0 ${LAYOUT.PAGE_PADDING}px`, marginBottom: LAYOUT.SECTION_GAP }}>
        <KPICards />
      </div>

      {/* Provider Status */}
      <ProviderStatusCard />

      {/* Draggable Panel Grid */}
      <div style={{ padding: `0 ${LAYOUT.PAGE_PADDING - LAYOUT.GRID_GAP}px` }}>
        <DashboardLayout width={containerWidth - (LAYOUT.PAGE_PADDING * 2) + (LAYOUT.GRID_GAP * 2)} />
      </div>
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
