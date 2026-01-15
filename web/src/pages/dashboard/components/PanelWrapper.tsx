// web/src/pages/dashboard/components/PanelWrapper.tsx

import { type ReactNode } from "react";
import { Card, Tooltip } from "antd";
import { SyncOutlined } from "@ant-design/icons";
import { useAppStore } from "@/store/useAppStore";

interface PanelWrapperProps {
  title: string;
  children: ReactNode;
  loading?: boolean;
  onRefresh?: () => void;
  extra?: ReactNode;
  className?: string;
}

export function PanelWrapper({
  title,
  children,
  loading = false,
  onRefresh,
  extra,
  className = "",
}: PanelWrapperProps) {
  const { darkMode } = useAppStore();

  return (
    <Card
      className={`h-full ${className}`}
      style={{
        borderRadius: 12,
        border: darkMode ? "1px solid #334155" : "1px solid #e2e8f0",
        background: darkMode ? "#1e293b" : "#ffffff",
        height: "100%",
        display: "flex",
        flexDirection: "column",
      }}
      styles={{
        header: {
          borderBottom: darkMode ? "1px solid #334155" : "1px solid #e2e8f0",
          padding: "12px 16px",
          minHeight: "auto",
        },
        body: {
          padding: 16,
          flex: 1,
          overflow: "auto",
        },
      }}
      title={
        <span
          style={{
            fontSize: 14,
            fontWeight: 600,
            color: darkMode ? "#f1f5f9" : "#1e293b",
          }}
        >
          {title}
        </span>
      }
      extra={
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {extra}
          {onRefresh && (
            <Tooltip title="刷新">
              <div
                onClick={onRefresh}
                style={{
                  width: 28,
                  height: 28,
                  borderRadius: 6,
                  background: darkMode ? "#334155" : "#f1f5f9",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  cursor: "pointer",
                  transition: "all 0.2s",
                }}
              >
                <SyncOutlined
                  spin={loading}
                  style={{
                    fontSize: 12,
                    color: darkMode ? "#94a3b8" : "#64748b",
                  }}
                />
              </div>
            </Tooltip>
          )}
        </div>
      }
    >
      {children}
    </Card>
  );
}
