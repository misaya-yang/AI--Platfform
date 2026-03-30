// web/src/pages/dashboard/components/PanelWrapper.tsx

import { type ReactNode } from "react";
import { Tooltip } from "antd";
import { SyncOutlined } from "@ant-design/icons";
import { useAppStore } from "@/store/useAppStore";
import { LAYOUT, getColors, TRANSITION } from "../styles";
import { useTranslation } from "react-i18next";
import { DataStatusBadge } from "./DataStatusBadge";

interface PanelWrapperProps {
  title: string;
  children: ReactNode;
  loading?: boolean;
  onRefresh?: () => void;
  extra?: ReactNode;
  className?: string;
  noPadding?: boolean;
  dataStatus?: string;
  dataFreshnessMinutes?: number;
}

export function PanelWrapper({
  title,
  children,
  loading = false,
  onRefresh,
  extra,
  className = "",
  noPadding = false,
  dataStatus,
  dataFreshnessMinutes,
}: PanelWrapperProps) {
  const { t } = useTranslation();
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
          padding: `0 ${LAYOUT.CARD_PADDING}px`,
          height: 48,
          borderBottom: `1px solid ${colors.border}`,
          background: darkMode ? "rgba(255,255,255,0.03)" : "rgba(0,0,0,0.015)",
          flexShrink: 0,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span
            style={{
              fontSize: 15,
              fontWeight: 700,
              color: colors.textPrimary,
              letterSpacing: "-0.01em",
            }}
          >
            {title}
          </span>
          {dataStatus !== undefined && (
            <DataStatusBadge dataStatus={dataStatus} dataFreshnessMinutes={dataFreshnessMinutes} />
          )}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {extra}
          {onRefresh && (
            <Tooltip title={t("common.refresh")}>
              <div
                onClick={onRefresh}
                style={{
                  width: 32, // Slightly larger
                  height: 32,
                  borderRadius: 8,
                  background: colors.innerBg,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  cursor: "pointer",
                  transition: TRANSITION.normal,
                  border: `1px solid ${colors.border}`,
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = darkMode ? colors.borderHover : colors.border;
                  e.currentTarget.style.transform = "rotate(180deg)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = colors.innerBg;
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
          padding: noPadding ? 0 : LAYOUT.CARD_PADDING,
          flex: 1,
          overflow: "auto",
        }}
      >
        {children}
      </div>
    </div>
  );
}
