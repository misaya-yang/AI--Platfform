// web/src/pages/dashboard/components/PanelWrapper.tsx

import { type ReactNode } from "react";
import { Tooltip } from "antd";
import { SyncOutlined } from "@ant-design/icons";
import { useAppStore } from "@/store/useAppStore";
import { LAYOUT, getColors } from "../styles";

interface PanelWrapperProps {
  title: string;
  children: ReactNode;
  loading?: boolean;
  onRefresh?: () => void;
  extra?: ReactNode;
  className?: string;
  noPadding?: boolean;
}

export function PanelWrapper({
  title,
  children,
  loading = false,
  onRefresh,
  extra,
  className = "",
  noPadding = false,
}: PanelWrapperProps) {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);

  return (
    <div
      className={`h-full flex flex-col ${className}`}
      style={{
        borderRadius: LAYOUT.CARD_RADIUS,
        border: `1px solid ${colors.border}`,
        background: colors.cardBg,
        boxShadow: colors.shadowSm,
        overflow: "hidden",
        height: "100%",
      }}
    >
      {/* Header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "0 20px",
          height: 56, // Increased header height
          borderBottom: `1px solid ${colors.border}`,
          background: darkMode ? "rgba(255,255,255,0.03)" : "rgba(0,0,0,0.015)",
          flexShrink: 0,
        }}
      >
        <span
          style={{
            fontSize: 15, // Slightly larger
            fontWeight: 700, // Bolder
            color: colors.textPrimary,
            letterSpacing: "-0.01em",
          }}
        >
          {title}
        </span>

        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {extra}
          {onRefresh && (
            <Tooltip title="刷新">
              <div
                onClick={onRefresh}
                style={{
                  width: 32, // Slightly larger
                  height: 32,
                  borderRadius: 8,
                  background: darkMode ? "#334155" : "#f1f5f9",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  cursor: "pointer",
                  transition: "all 0.2s cubic-bezier(0.4, 0, 0.2, 1)",
                  border: `1px solid ${colors.border}`,
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = darkMode ? "#475569" : "#e2e8f0";
                  e.currentTarget.style.transform = "rotate(180deg)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = darkMode ? "#334155" : "#f1f5f9";
                  e.currentTarget.style.transform = "rotate(0deg)";
                }}
              >
                <SyncOutlined
                  spin={loading}
                  style={{
                    fontSize: 14,
                    color: colors.textPrimary,
                  }}
                />
              </div>
            </Tooltip>
          )}
        </div>
      </div>

      {/* Body */}
      <div
        style={{
          padding: noPadding ? 0 : 20, // Match header padding
          flex: 1,
          overflow: "auto",
        }}
      >
        {children}
      </div>
    </div>
  );
}
