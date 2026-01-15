// web/src/pages/dashboard/components/DashboardHeader.tsx

import { DatePicker, Select, Segmented, Tooltip } from "antd";
import { SyncOutlined, ExpandOutlined, CompressOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { useState } from "react";
import { useDashboardContext } from "../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import type { SourceFilter, RefreshInterval } from "../types";

const { RangePicker } = DatePicker;

export function DashboardHeader() {
  const { darkMode } = useAppStore();
  const {
    dateRange,
    granularity,
    source,
    refreshInterval,
    lastRefresh,
    setDateRange,
    setGranularity,
    setSource,
    setRefreshInterval,
    triggerRefresh,
  } = useDashboardContext();

  const [isFullscreen, setIsFullscreen] = useState(false);

  const handleFullscreen = () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen();
      setIsFullscreen(true);
    } else {
      document.exitFullscreen();
      setIsFullscreen(false);
    }
  };

  const sourceOptions: { label: string; value: SourceFilter }[] = [
    { label: "全部", value: "all" },
    { label: "内部调用", value: "internal" },
    { label: "外部API", value: "external" },
  ];

  const refreshOptions: { label: string; value: RefreshInterval }[] = [
    { label: "手动", value: 0 },
    { label: "30秒", value: 30 },
    { label: "1分钟", value: 60 },
    { label: "5分钟", value: 300 },
  ];

  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 16,
        marginBottom: 20,
        padding: "16px 20px",
        borderRadius: 12,
        background: darkMode ? "#1e293b" : "#ffffff",
        border: darkMode ? "1px solid #334155" : "1px solid #e2e8f0",
      }}
    >
      {/* Left: Title */}
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <h1
          style={{
            fontSize: 20,
            fontWeight: 700,
            margin: 0,
            color: darkMode ? "#f1f5f9" : "#1e293b",
          }}
        >
          监控仪表盘
        </h1>
        <span
          style={{
            fontSize: 12,
            padding: "4px 8px",
            borderRadius: 4,
            background: darkMode ? "#334155" : "#f1f5f9",
            color: darkMode ? "#94a3b8" : "#64748b",
          }}
        >
          更新于 {dayjs(lastRefresh).format("HH:mm:ss")}
        </span>
      </div>

      {/* Right: Controls */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        {/* Source Filter */}
        <Segmented
          options={sourceOptions}
          value={source}
          onChange={(v) => setSource(v as SourceFilter)}
          style={{
            background: darkMode ? "#334155" : "#f1f5f9",
          }}
        />

        {/* Date Range */}
        <RangePicker
          value={[dayjs(dateRange[0]), dayjs(dateRange[1])]}
          onChange={(dates) => {
            if (dates && dates[0] && dates[1]) {
              setDateRange([
                dates[0].format("YYYY-MM-DD"),
                dates[1].format("YYYY-MM-DD"),
              ]);
            }
          }}
          style={{ width: 240 }}
        />

        {/* Granularity */}
        <Select
          value={granularity}
          onChange={setGranularity}
          options={[
            { value: "day", label: "按天" },
            { value: "hour", label: "按小时" },
          ]}
          style={{ width: 100 }}
        />

        {/* Refresh Interval */}
        <Select
          value={refreshInterval}
          onChange={setRefreshInterval}
          options={refreshOptions}
          style={{ width: 100 }}
          suffixIcon={<SyncOutlined />}
        />

        {/* Manual Refresh */}
        <Tooltip title="立即刷新">
          <div
            onClick={triggerRefresh}
            style={{
              width: 32,
              height: 32,
              borderRadius: 6,
              background: darkMode ? "#334155" : "#f1f5f9",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              cursor: "pointer",
            }}
          >
            <SyncOutlined style={{ color: darkMode ? "#94a3b8" : "#64748b" }} />
          </div>
        </Tooltip>

        {/* Fullscreen */}
        <Tooltip title={isFullscreen ? "退出全屏" : "全屏模式"}>
          <div
            onClick={handleFullscreen}
            style={{
              width: 32,
              height: 32,
              borderRadius: 6,
              background: darkMode ? "#334155" : "#f1f5f9",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              cursor: "pointer",
            }}
          >
            {isFullscreen ? (
              <CompressOutlined style={{ color: darkMode ? "#94a3b8" : "#64748b" }} />
            ) : (
              <ExpandOutlined style={{ color: darkMode ? "#94a3b8" : "#64748b" }} />
            )}
          </div>
        </Tooltip>
      </div>
    </div>
  );
}
