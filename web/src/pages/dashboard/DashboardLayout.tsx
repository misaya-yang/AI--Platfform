// web/src/pages/dashboard/DashboardLayout.tsx

import { useState, useCallback, useMemo } from "react";
import GridLayout from "react-grid-layout";
import type { Layout } from "react-grid-layout";
import { useAppStore } from "@/store/useAppStore";
import { DEFAULT_LAYOUTS, DEFAULT_PANELS } from "./types";
import type { PanelType } from "./types";
import { LAYOUT, getColors } from "./styles";
import {
  ServiceHealthPanel,
  PerformancePanel,
  TokenUsagePanel,
  CostAnalysisPanel,
  UserQuotaPanel,
  SecurityEventsPanel,
  RequestTracePanel,
} from "./components/panels";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";

// Storage key for layout persistence
const LAYOUT_STORAGE_KEY = "dashboard-layout";

// Panel component mapping
const PANEL_COMPONENTS: Record<PanelType, React.ComponentType> = {
  "service-health": ServiceHealthPanel,
  "performance": PerformancePanel,
  "token-usage": TokenUsagePanel,
  "cost-analysis": CostAnalysisPanel,
  "user-quota": UserQuotaPanel,
  "security-events": SecurityEventsPanel,
  "request-trace": RequestTracePanel,
};

function loadSavedLayout(): Layout[] {
  try {
    const saved = localStorage.getItem(LAYOUT_STORAGE_KEY);
    if (saved) {
      return JSON.parse(saved);
    }
  } catch (e) {
    console.warn("Failed to load saved layout:", e);
  }
  return DEFAULT_LAYOUTS;
}

function saveLayout(layout: Layout[]) {
  try {
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(layout));
  } catch (e) {
    console.warn("Failed to save layout:", e);
  }
}

interface DashboardLayoutProps {
  width?: number;
}

export function DashboardLayout({ width = 1200 }: DashboardLayoutProps) {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const [layouts, setLayouts] = useState<Layout[]>(loadSavedLayout);

  const onLayoutChange = useCallback((newLayout: Layout[]) => {
    setLayouts(newLayout);
    saveLayout(newLayout);
  }, []);

  // Calculate actual grid width
  const gridWidth = useMemo(() => {
    return Math.max(width, 600); // min 600px
  }, [width]);

  // Row height based on container
  const rowHeight = 40;

  return (
    <div
      style={{
        minHeight: "100%",
      }}
    >
      <GridLayout
        className="layout"
        layout={layouts}
        cols={12}
        rowHeight={rowHeight}
        width={gridWidth}
        onLayoutChange={onLayoutChange}
        draggableHandle=".panel-drag-handle"
        isDraggable
        isResizable
        margin={[LAYOUT.GRID_GAP, LAYOUT.GRID_GAP]}
        containerPadding={[0, 0]}
        useCSSTransforms
      >
        {DEFAULT_PANELS.filter((panel) => panel.visible).map((panel) => {
          const PanelComponent = PANEL_COMPONENTS[panel.type];
          return (
            <div
              key={panel.id}
              className="panel-container"
              style={{
                background: "transparent",
              }}
            >
              <PanelComponent />
            </div>
          );
        })}
      </GridLayout>
    </div>
  );
}

// Reset layout to default
export function resetDashboardLayout() {
  localStorage.removeItem(LAYOUT_STORAGE_KEY);
  window.location.reload();
}
