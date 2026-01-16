// web/src/pages/dashboard/index.tsx

import { useEffect, useRef, useState } from "react";
import { DashboardProvider } from "./DashboardContext";
import { DashboardHeader } from "./components/DashboardHeader";
import { KPICards } from "./components/KPICards";
import { DashboardLayout } from "./DashboardLayout";
import { useAppStore } from "@/store/useAppStore";

function DashboardContent() {
  const { darkMode } = useAppStore();
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
        padding: 24,
        background: darkMode ? "#0f172a" : "#f8fafc",
      }}
    >
      {/* Header with controls */}
      <DashboardHeader />

      {/* KPI Summary Cards */}
      <KPICards />

      {/* Draggable Panel Grid */}
      <DashboardLayout width={containerWidth} />
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
