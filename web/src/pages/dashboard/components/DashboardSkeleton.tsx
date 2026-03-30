// web/src/pages/dashboard/components/DashboardSkeleton.tsx
// Full-page skeleton loading state for dashboard first load

import { useAppStore } from "@/store/useAppStore";
import { LAYOUT, getColors } from "../styles";

function ShimmerBlock({ width = "100%", height = 14, radius = 6 }: { width?: string | number; height?: number; radius?: number }) {
  const { darkMode } = useAppStore();
  const bg = darkMode
    ? "linear-gradient(90deg, rgba(255,255,255,0.03) 0%, rgba(255,255,255,0.08) 50%, rgba(255,255,255,0.03) 100%)"
    : "linear-gradient(90deg, rgba(0,0,0,0.03) 0%, rgba(0,0,0,0.06) 50%, rgba(0,0,0,0.03) 100%)";

  return (
    <div
      style={{
        width,
        height,
        borderRadius: radius,
        background: bg,
        backgroundSize: "200px 100%",
        animation: "shimmer 1.5s ease-in-out infinite",
      }}
    />
  );
}

function KPISkeleton() {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);

  return (
    <div
      style={{
        padding: LAYOUT.CARD_PADDING,
        borderRadius: LAYOUT.CARD_RADIUS,
        border: `1px solid ${colors.border}`,
        background: colors.cardBg,
        height: LAYOUT.KPI_HEIGHT,
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
        gap: 10,
      }}
    >
      <ShimmerBlock width="60%" height={12} />
      <ShimmerBlock width="40%" height={28} />
      <ShimmerBlock width="80%" height={10} />
      <ShimmerBlock width="100%" height={32} radius={4} />
    </div>
  );
}

function PanelSkeleton({ span = 1 }: { span?: 1 | 2 }) {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);

  return (
    <div
      style={{
        borderRadius: LAYOUT.CARD_RADIUS,
        border: `1px solid ${colors.border}`,
        background: colors.cardBg,
        minHeight: 380,
        gridColumn: span === 2 ? "1 / -1" : "auto",
        overflow: "hidden",
      }}
    >
      {/* Header */}
      <div style={{ padding: "14px 24px", borderBottom: `1px solid ${colors.border}` }}>
        <ShimmerBlock width="30%" height={16} />
      </div>
      {/* Body */}
      <div style={{ padding: 24, display: "flex", flexDirection: "column", gap: 16 }}>
        <ShimmerBlock width="100%" height={200} radius={8} />
        <ShimmerBlock width="70%" height={14} />
        <ShimmerBlock width="50%" height={14} />
      </div>
    </div>
  );
}

export function DashboardSkeleton() {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);

  return (
    <div style={{ padding: LAYOUT.PAGE_PADDING, background: colors.pageBg, minHeight: "100vh" }}>
      <style>{`@keyframes shimmer { 0% { background-position: -200px 0; } 100% { background-position: calc(200px + 100%) 0; } }`}</style>

      {/* Header skeleton */}
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: LAYOUT.SECTION_GAP }}>
        <ShimmerBlock width={240} height={32} />
        <div style={{ display: "flex", gap: 8 }}>
          <ShimmerBlock width={36} height={36} radius={8} />
          <ShimmerBlock width={36} height={36} radius={8} />
        </div>
      </div>

      {/* KPI skeletons */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
          gap: LAYOUT.CARD_GAP,
          marginBottom: LAYOUT.SECTION_GAP,
        }}
      >
        {Array.from({ length: 5 }).map((_, i) => (
          <KPISkeleton key={i} />
        ))}
      </div>

      {/* Provider skeleton */}
      <div
        style={{
          borderRadius: LAYOUT.CARD_RADIUS,
          border: `1px solid ${colors.border}`,
          background: colors.cardBg,
          padding: LAYOUT.CARD_PADDING,
          marginBottom: LAYOUT.SECTION_GAP,
        }}
      >
        <ShimmerBlock width="20%" height={16} />
        <div style={{ marginTop: 16, display: "flex", flexDirection: "column", gap: 10 }}>
          {Array.from({ length: 5 }).map((_, i) => (
            <ShimmerBlock key={i} width="100%" height={36} radius={8} />
          ))}
        </div>
      </div>

      {/* Panel skeletons */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: LAYOUT.GRID_GAP }}>
        <PanelSkeleton span={2} />
        <PanelSkeleton />
        <PanelSkeleton />
      </div>
    </div>
  );
}
