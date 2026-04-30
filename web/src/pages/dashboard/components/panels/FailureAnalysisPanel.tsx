// web/src/pages/dashboard/components/panels/FailureAnalysisPanel.tsx
// 失败根因分析面板 - 消费已有 /api/v1/usage/failure-breakdown API

import { Table, Tag, Segmented } from "antd";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PanelWrapper } from "../PanelWrapper";
import { useDashboardContext } from "../../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getColors } from "../../styles";
import { getFailureBreakdown, type FailureBreakdownItem } from "@/api/usage";
import { useTranslation } from "react-i18next";
import { useDashboardEntityLabels } from "../../hooks/useDashboardEntityLabels";

type FailureDimension = "service" | "model" | "provider";

const ERROR_TYPE_COLORS: Record<string, string> = {
  timeout: "orange",
  provider_error: "red",
  rate_limit: "gold",
  auth_error: "blue",
  content_filter: "purple",
  tool_error: "default",
  unknown: "default",
  quota_exceeded: "volcano",
  validation_error: "cyan",
  network_error: "magenta",
};

function getErrorTypeColor(errorType: string): string {
  return ERROR_TYPE_COLORS[errorType] || "default";
}

export function FailureAnalysisPanel() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const { dateRange, serviceId, userId, lastRefresh } = useDashboardContext();
  const { resolveServiceLabel } = useDashboardEntityLabels();
  const [dimension, setDimension] = useState<FailureDimension>("service");

  const { data, isLoading, refetch } = useQuery({
    queryKey: ["dashboard-failure-breakdown", dimension, dateRange, serviceId, userId, lastRefresh.getTime()],
    queryFn: () =>
      getFailureBreakdown({
        dimension,
        start_date: dateRange[0],
        end_date: dateRange[1],
        service_id: serviceId !== "all" ? serviceId : undefined,
        user_id: userId !== "all" ? userId : undefined,
        limit: 50,
      }),
    staleTime: 30000,
  });

  const items = data?.items || [];

  // 汇总统计
  const totalFailures = items.reduce((sum, item) => sum + item.failure_count, 0);
  const totalRequests = items.reduce((sum, item) => sum + item.total_count, 0);
  const overallSuccessRate = totalRequests > 0 ? ((totalRequests - totalFailures) / totalRequests * 100).toFixed(1) : "--";

  const columns = [
    {
      title: dimension === "service" ? t("common.service", "服务") : dimension === "model" ? t("common.model", "模型") : t("common.provider", "厂商"),
      dataIndex: "dimension_value",
      key: "dimension_value",
      width: 140,
      ellipsis: true,
      render: (text: string) => (
        <span style={{ fontWeight: 500, color: colors.textPrimary }}>
          {dimension === "service"
            ? resolveServiceLabel(text || "")
            : text || "unknown"}
        </span>
      ),
    },
    {
      title: t("dashboard.failure.errorType", "错误类型"),
      dataIndex: "error_type",
      key: "error_type",
      width: 120,
      render: (errorType: string) => (
        <Tag color={getErrorTypeColor(errorType)} style={{ margin: 0 }}>
          {errorType}
        </Tag>
      ),
    },
    {
      title: t("dashboard.failure.failureCount", "失败数"),
      dataIndex: "failure_count",
      key: "failure_count",
      width: 80,
      sorter: (a: FailureBreakdownItem, b: FailureBreakdownItem) => a.failure_count - b.failure_count,
      defaultSortOrder: "descend" as const,
      render: (count: number) => (
        <span style={{ fontWeight: 600, color: count > 0 ? colors.error : colors.textSecondary }}>
          {count}
        </span>
      ),
    },
    {
      title: t("metrics.successRate", "成功率"),
      dataIndex: "success_rate",
      key: "success_rate",
      width: 90,
      render: (rate: number) => {
        const color = rate >= 95 ? colors.success : rate >= 80 ? colors.warning : colors.error;
        return (
          <span style={{ fontWeight: 600, color }}>
            {rate.toFixed(1)}%
          </span>
        );
      },
    },
  ];

  return (
    <PanelWrapper
      title={t("dashboard.failure.title", "失败根因分析")}
      onRefresh={refetch}
      loading={isLoading}
      extra={
        <Segmented
          size="small"
          value={dimension}
          onChange={(val) => setDimension(val as FailureDimension)}
          options={[
            { value: "service", label: t("common.service", "服务") },
            { value: "model", label: t("common.model", "模型") },
            { value: "provider", label: t("common.provider", "厂商") },
          ]}
        />
      }
    >
      {/* 汇总指标 */}
      <div
        style={{
          display: "flex",
          gap: 16,
          marginBottom: 12,
          padding: "8px 12px",
          borderRadius: 8,
          background: totalFailures > 0
            ? colors.errorBg
            : colors.successBg,
          border: `1px solid ${totalFailures > 0
            ? `${colors.error}55`
            : `${colors.success}55`
          }`,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontSize: 11, color: colors.textMuted }}>{t("dashboard.failure.totalFailures", "总失败数")}:</span>
          <span style={{ fontSize: 14, fontWeight: 700, color: totalFailures > 0 ? colors.error : colors.success }}>
            {totalFailures}
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontSize: 11, color: colors.textMuted }}>{t("metrics.successRate", "成功率")}:</span>
          <span style={{ fontSize: 14, fontWeight: 700, color: colors.textPrimary }}>
            {overallSuccessRate}%
          </span>
        </div>
      </div>

      {/* 失败明细表格 */}
      <Table
        dataSource={items}
        columns={columns}
        rowKey={(record) => `${record.dimension_value}-${record.error_type}`}
        size="small"
        pagination={false}
        scroll={{ x: 480, y: 200 }}
        style={{ background: "transparent" }}
        locale={{ emptyText: t("dashboard.failure.noFailures", "当前时间范围内无失败记录") }}
      />
    </PanelWrapper>
  );
}
