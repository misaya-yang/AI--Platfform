// web/src/pages/dashboard/components/DataStatusBadge.tsx
// 数据新鲜度指示器 - 显示面板数据来源状态

import { Tooltip } from "antd";
import { useTranslation } from "react-i18next";

export type DataStatus = "live" | "stale" | "no_data";

interface DataStatusBadgeProps {
  dataStatus?: string;
  dataFreshnessMinutes?: number;
}

const STATUS_CONFIG: Record<DataStatus, { color: string; label: string; bgColor: string }> = {
  live: { color: "#10b981", label: "dashboard.dataStatus.live", bgColor: "rgba(16, 185, 129, 0.15)" },
  stale: { color: "#f59e0b", label: "dashboard.dataStatus.stale", bgColor: "rgba(245, 158, 11, 0.15)" },
  no_data: { color: "#94a3b8", label: "dashboard.dataStatus.noData", bgColor: "rgba(148, 163, 184, 0.15)" },
};

function resolveStatus(dataStatus?: string, freshnessMinutes?: number): DataStatus {
  if (dataStatus === "no_data") return "no_data";
  if (dataStatus === "stale") return "stale";
  if (dataStatus === "live") {
    // Double check with freshness
    if (freshnessMinutes !== undefined && freshnessMinutes >= 5) return "stale";
    return "live";
  }
  // Infer from freshness if dataStatus not provided
  if (freshnessMinutes === undefined || freshnessMinutes < 0) return "no_data";
  if (freshnessMinutes < 5) return "live";
  return "stale";
}

export function DataStatusBadge({ dataStatus, dataFreshnessMinutes }: DataStatusBadgeProps) {
  const { t } = useTranslation();
  const status = resolveStatus(dataStatus, dataFreshnessMinutes);
  const config = STATUS_CONFIG[status];

  const freshnessText = dataFreshnessMinutes !== undefined && dataFreshnessMinutes >= 0
    ? t("dashboard.dataStatus.freshnessMinutes", { minutes: Math.round(dataFreshnessMinutes) })
    : t("dashboard.dataStatus.unknown");

  return (
    <Tooltip
      title={
        <div>
          <div>{t(config.label, status)}</div>
          <div style={{ fontSize: 11, opacity: 0.8, marginTop: 2 }}>{freshnessText}</div>
        </div>
      }
    >
      <div
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
          padding: "2px 8px",
          borderRadius: 10,
          background: config.bgColor,
          cursor: "help",
        }}
      >
        <div
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: config.color,
            boxShadow: status === "live" ? `0 0 6px ${config.color}` : "none",
          }}
        />
        <span style={{ fontSize: 10, fontWeight: 500, color: config.color }}>
          {status === "live" ? "LIVE" : status === "stale" ? "STALE" : "N/A"}
        </span>
      </div>
    </Tooltip>
  );
}
