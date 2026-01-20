// web/src/pages/dashboard/components/DashboardHeader.tsx
// Enterprise Dashboard Header - Unified Layout System

import { DatePicker, Select, Tooltip, Badge, Button } from "antd";
import {
  SyncOutlined,
  ExpandOutlined,
  CompressOutlined,
  AppstoreOutlined,
  UserOutlined,
  FilterOutlined,
  CalendarOutlined,
  DownOutlined,
  UpOutlined,
  ClearOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import { useState, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { useDashboardContext } from "../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getUsageBreakdown } from "@/api/usage";
import { LAYOUT, getColors } from "../styles";
import type { SourceFilter, RefreshInterval } from "../types";

const { RangePicker } = DatePicker;

export function DashboardHeader() {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const {
    dateRange,
    granularity,
    source,
    serviceId,
    userId,
    refreshInterval,
    lastRefresh,
    setDateRange,
    setGranularity,
    setSource,
    setServiceId,
    setUserId,
    setRefreshInterval,
    triggerRefresh,
  } = useDashboardContext();

  const [isFullscreen, setIsFullscreen] = useState(false);
  const [filterCollapsed, setFilterCollapsed] = useState(false);

  // Auto-collapse on smaller screens
  useEffect(() => {
    const handleResize = () => {
      if (window.innerWidth < 1400) {
        setFilterCollapsed(true);
      }
    };
    handleResize();
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  // Reset all filters
  const handleResetFilters = () => {
    setServiceId("all");
    setUserId("all");
    setSource("all");
  };

  // Fetch services and users from usage breakdown
  const { data: serviceBreakdown } = useQuery({
    queryKey: ["usage-services", dateRange],
    queryFn: () =>
      getUsageBreakdown({
        dimension: "service",
        start_date: dateRange[0],
        end_date: dateRange[1],
      }),
    staleTime: 60000,
  });

  const { data: userBreakdown } = useQuery({
    queryKey: ["usage-users", dateRange],
    queryFn: () =>
      getUsageBreakdown({
        dimension: "user",
        start_date: dateRange[0],
        end_date: dateRange[1],
      }),
    staleTime: 60000,
  });

  // Build service options
  const serviceOptions = [
    { label: "全部服务", value: "all" },
    ...(serviceBreakdown?.items || []).map((item) => ({
      label: item.service || "unknown",
      value: item.service || "unknown",
    })),
  ];

  // Build user options
  const userOptions = [
    { label: "全部用户", value: "all" },
    ...(userBreakdown?.items || []).map((item) => ({
      label: item.user?.startsWith("anon:")
        ? `匿名-${item.user.slice(-6)}`
        : item.user || "unknown",
      value: item.user || "unknown",
    })),
  ];

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
    { label: "全部来源", value: "all" },
    { label: "内部调用", value: "internal" },
    { label: "外部API", value: "external" },
  ];

  const refreshOptions: { label: string; value: RefreshInterval }[] = [
    { label: "手动刷新", value: 0 },
    { label: "30秒", value: 30 },
    { label: "1分钟", value: 60 },
    { label: "5分钟", value: 300 },
  ];

  // Check if any filter is active
  const hasActiveFilters =
    serviceId !== "all" || userId !== "all" || source !== "all";
  const activeFilterCount =
    (serviceId !== "all" ? 1 : 0) +
    (userId !== "all" ? 1 : 0) +
    (source !== "all" ? 1 : 0);

  return (
    <div style={{ marginBottom: LAYOUT.SECTION_GAP }}>
      {/* Title Row */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: LAYOUT.GRID_GAP,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <h1
            style={{
              fontSize: 26, // Increased size
              fontWeight: 800, // Bolder
              margin: 0,
              color: colors.textPrimary,
              letterSpacing: "-0.03em",
            }}
          >
            监控仪表盘
          </h1>
          <div
            style={{
              fontSize: 11,
              padding: "4px 12px",
              borderRadius: 20, // Pill style
              background: darkMode ? "rgba(99, 102, 241, 0.15)" : "#EEF2FF",
              color: colors.accent,
              fontWeight: 600,
              border: `1px solid ${darkMode ? "rgba(99, 102, 241, 0.2)" : "#DDE6FF"}`,
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <div 
              style={{ 
                width: 6, 
                height: 6, 
                borderRadius: "50%", 
                background: colors.accent,
                boxShadow: `0 0 8px ${colors.accent}`,
              }} 
            />
            上次更新: {dayjs(lastRefresh).format("HH:mm:ss")}
          </div>
        </div>

        {/* Action buttons */}
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Tooltip title="立即刷新">
            <button
              onClick={triggerRefresh}
              style={{
                width: 36,
                height: 36,
                borderRadius: 8,
                background: colors.cardBg,
                border: `1px solid ${colors.border}`,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                cursor: "pointer",
                color: colors.textSecondary,
                transition: "all 0.2s",
              }}
            >
              <SyncOutlined style={{ fontSize: 14 }} />
            </button>
          </Tooltip>

          <Tooltip title={isFullscreen ? "退出全屏" : "全屏模式"}>
            <button
              onClick={handleFullscreen}
              style={{
                width: 36,
                height: 36,
                borderRadius: 8,
                background: colors.cardBg,
                border: `1px solid ${colors.border}`,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                cursor: "pointer",
                color: colors.textSecondary,
                transition: "all 0.2s",
              }}
            >
              {isFullscreen ? (
                <CompressOutlined style={{ fontSize: 14 }} />
              ) : (
                <ExpandOutlined style={{ fontSize: 14 }} />
              )}
            </button>
          </Tooltip>
        </div>
      </div>

      {/* Filter Bar - Collapsible */}
      <div
        style={{
          borderRadius: 16,
          background: darkMode ? "rgba(30, 41, 59, 0.7)" : "rgba(255, 255, 255, 0.8)",
          backdropFilter: "blur(12px)",
          border: `1px solid ${darkMode ? "rgba(255, 255, 255, 0.08)" : "rgba(0, 0, 0, 0.05)"}`,
          boxShadow: colors.shadowSm,
          overflow: "hidden",
          transition: "all 0.3s ease",
        }}
      >
        {/* Collapsed Header - Always visible */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "12px 20px",
            cursor: "pointer",
            borderBottom: filterCollapsed ? "none" : `1px solid ${darkMode ? "rgba(255, 255, 255, 0.05)" : "rgba(0, 0, 0, 0.03)"}`,
          }}
          onClick={() => setFilterCollapsed(!filterCollapsed)}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                color: colors.textSecondary,
                fontSize: 13,
                fontWeight: 600,
              }}
            >
              <FilterOutlined />
              <span>筛选条件</span>
              {hasActiveFilters && (
                <Badge
                  count={activeFilterCount}
                  size="small"
                  style={{
                    backgroundColor: colors.accent,
                    fontSize: 10,
                    height: 16,
                    minWidth: 16,
                    lineHeight: "16px",
                  }}
                />
              )}
            </div>

            {/* Show active filter summary when collapsed */}
            {filterCollapsed && hasActiveFilters && (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  marginLeft: 8,
                }}
              >
                {serviceId !== "all" && (
                  <span
                    style={{
                      fontSize: 11,
                      padding: "2px 8px",
                      borderRadius: 12,
                      background: darkMode ? "rgba(99, 102, 241, 0.15)" : "#EEF2FF",
                      color: colors.accent,
                    }}
                  >
                    {serviceId}
                  </span>
                )}
                {userId !== "all" && (
                  <span
                    style={{
                      fontSize: 11,
                      padding: "2px 8px",
                      borderRadius: 12,
                      background: darkMode ? "rgba(16, 185, 129, 0.15)" : "#ECFDF5",
                      color: "#10B981",
                    }}
                  >
                    {userId.startsWith("anon:") ? `匿名-${userId.slice(-6)}` : userId}
                  </span>
                )}
                {source !== "all" && (
                  <span
                    style={{
                      fontSize: 11,
                      padding: "2px 8px",
                      borderRadius: 12,
                      background: darkMode ? "rgba(245, 158, 11, 0.15)" : "#FEF3C7",
                      color: "#F59E0B",
                    }}
                  >
                    {source === "internal" ? "内部" : "外部"}
                  </span>
                )}
              </div>
            )}
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            {/* Reset filters button */}
            {hasActiveFilters && (
              <Tooltip title="重置筛选">
                <Button
                  type="text"
                  size="small"
                  icon={<ClearOutlined />}
                  onClick={(e) => {
                    e.stopPropagation();
                    handleResetFilters();
                  }}
                  style={{ color: colors.textMuted }}
                >
                  重置
                </Button>
              </Tooltip>
            )}

            {/* Auto refresh - always visible */}
            <div
              style={{ display: "flex", alignItems: "center", gap: 8 }}
              onClick={(e) => e.stopPropagation()}
            >
              <span style={{ fontSize: 12, fontWeight: 500, color: colors.textSecondary }}>
                自动刷新
              </span>
              <Select
                value={refreshInterval}
                onChange={setRefreshInterval}
                options={refreshOptions}
                style={{ width: 100 }}
                variant="filled"
                suffixIcon={<SyncOutlined spin={refreshInterval > 0} />}
              />
            </div>

            {/* Collapse toggle */}
            <button
              style={{
                width: 28,
                height: 28,
                borderRadius: 6,
                border: "none",
                background: darkMode ? "rgba(255, 255, 255, 0.05)" : "rgba(0, 0, 0, 0.03)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                cursor: "pointer",
                color: colors.textMuted,
                transition: "all 0.2s",
              }}
            >
              {filterCollapsed ? <DownOutlined style={{ fontSize: 10 }} /> : <UpOutlined style={{ fontSize: 10 }} />}
            </button>
          </div>
        </div>

        {/* Expandable Filter Content */}
        <div
          style={{
            maxHeight: filterCollapsed ? 0 : 200,
            overflow: "hidden",
            transition: "max-height 0.3s ease",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "flex-start",
              flexWrap: "wrap",
              gap: LAYOUT.GRID_GAP,
              padding: "14px 20px",
            }}
          >
            {/* Filter Group: Data Filters */}
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  color: colors.textSecondary,
                  fontSize: 12,
                  fontWeight: 600,
                  marginRight: 4,
                }}
              >
                <AppstoreOutlined />
                <span>数据筛选</span>
              </div>

              {/* Service Filter */}
              <Select
                value={serviceId}
                onChange={setServiceId}
                options={serviceOptions}
                style={{ minWidth: 140 }}
                suffixIcon={<AppstoreOutlined />}
                placeholder="选择服务"
                popupMatchSelectWidth={false}
                variant="filled"
              />

              {/* User Filter */}
              <Select
                value={userId}
                onChange={setUserId}
                options={userOptions}
                style={{ minWidth: 140 }}
                suffixIcon={<UserOutlined />}
                placeholder="选择用户"
                popupMatchSelectWidth={false}
                variant="filled"
              />

              {/* Source Filter */}
              <Select
                value={source}
                onChange={(v) => setSource(v as SourceFilter)}
                options={sourceOptions}
                style={{ minWidth: 110 }}
                placeholder="来源"
                popupMatchSelectWidth={false}
                variant="filled"
              />
            </div>

            {/* Divider */}
            <div
              style={{
                width: 1,
                height: 24,
                background: darkMode ? "rgba(255, 255, 255, 0.1)" : "rgba(0, 0, 0, 0.1)",
              }}
            />

            {/* Filter Group: Time Range */}
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  color: colors.textSecondary,
                  fontSize: 12,
                  fontWeight: 600,
                  marginRight: 4,
                }}
              >
                <CalendarOutlined />
                <span>时间范围</span>
              </div>

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
                allowClear={false}
                variant="filled"
              />

              {/* Granularity */}
              <Select
                value={granularity}
                onChange={setGranularity}
                options={[
                  { value: "day", label: "天" },
                  { value: "hour", label: "时" },
                ]}
                style={{ width: 70 }}
                variant="filled"
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
