// web/src/pages/dashboard/components/panels/UserQuotaPanel.tsx

import { Table, Progress, Tag, Select, Tooltip, Button } from "antd";
import { useQuery } from "@tanstack/react-query";
import { WarningOutlined, ExclamationCircleOutlined, ExpandOutlined } from "@ant-design/icons";
import { PanelWrapper } from "../PanelWrapper";
import { useDashboardContext } from "../../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getUsageBreakdown } from "@/api/usage";
import { useState } from "react";
import { useTranslation } from "react-i18next";

function formatTokens(num: number): string {
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`;
  if (num >= 1_000) return `${(num / 1_000).toFixed(1)}K`;
  return num.toLocaleString();
}

export function UserQuotaPanel() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const { dateRange, serviceId, lastRefresh } = useDashboardContext();
  const [sortBy, setSortBy] = useState<"usage" | "cost">("usage");

  const { data, isLoading, refetch } = useQuery({
    queryKey: ["dashboard-user-quota", dateRange, serviceId, lastRefresh.getTime()],
    queryFn: () =>
      getUsageBreakdown({
        dimension: "user",
        start_date: dateRange[0],
        end_date: dateRange[1],
        limit: 20,
      }),
    staleTime: 30000,
  });

  const users = (data?.items || []).map((item) => {
    // Mock quota limits - in production these would come from backend
    const dailyLimit = 100000; // 100K tokens
    const monthlyLimit = 1000000; // 1M tokens
    const dailyUsed = item.total_tokens || 0;
    const monthlyUsed = dailyUsed * 7; // Mock monthly as 7x daily
    const dailyPercent = (dailyUsed / dailyLimit) * 100;
    const monthlyPercent = (monthlyUsed / monthlyLimit) * 100;

    let status: "normal" | "warning" | "exceeded" = "normal";
    if (dailyPercent >= 100 || monthlyPercent >= 100) {
      status = "exceeded";
    } else if (dailyPercent >= 80 || monthlyPercent >= 80) {
      status = "warning";
    }

    return {
      user: item.user || t("common.unknown"),
      dailyUsed,
      dailyLimit,
      dailyPercent,
      monthlyUsed,
      monthlyLimit,
      monthlyPercent,
      cost: item.cost_usd || 0,
      status,
    };
  });

  const sortedUsers = [...users].sort((a, b) => {
    if (sortBy === "usage") return b.dailyUsed - a.dailyUsed;
    return b.cost - a.cost;
  });

  const warningCount = users.filter((u) => u.status === "warning" || u.status === "exceeded").length;

  // Format user display name
  const formatUserName = (user: string) => {
    if (user.startsWith("anon:")) {
      return t("dashboard.filters.anonymousUser", { id: user.slice(-6) });
    }
    return user;
  };

  const columns = [
    {
      title: t("common.user"),
      dataIndex: "user",
      key: "user",
      width: 100,
      ellipsis: true,
      render: (text: string) => (
        <span style={{ fontWeight: 500, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
          {formatUserName(text)}
        </span>
      ),
    },
    {
      title: t("metrics.totalTokens"),
      key: "daily",
      width: 130,
      render: (_: unknown, record: (typeof users)[0]) => (
        <div>
          <Progress
            percent={Math.min(record.dailyPercent, 100)}
            size="small"
            strokeColor={record.dailyPercent >= 80 ? "#f59e0b" : "#3b82f6"}
            showInfo={false}
          />
          <div style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>
            {formatTokens(record.dailyUsed)}/{formatTokens(record.dailyLimit)}
          </div>
        </div>
      ),
    },
    {
      title: t("analytics.cost"),
      dataIndex: "cost",
      key: "cost",
      width: 80,
      render: (cost: number) => (
        <span style={{ fontWeight: 500, color: "#10b981" }}>
          ${cost >= 1 ? cost.toFixed(2) : cost.toFixed(4)}
        </span>
      ),
    },
    {
      title: t("common.status"),
      dataIndex: "status",
      key: "status",
      width: 70,
      render: (status: string) => {
        const config = {
          normal: { color: "success", text: t("dashboard.userQuota.status.normal") },
          warning: { color: "warning", text: t("dashboard.userQuota.status.warning") },
          exceeded: { color: "error", text: t("dashboard.userQuota.status.exceeded") },
        }[status] || { color: "default", text: status };
        return <Tag color={config.color}>{config.text}</Tag>;
      },
    },
  ];

  return (
    <PanelWrapper
      title={t("dashboard.userQuota.title")}
      onRefresh={refetch}
      loading={isLoading}
      extra={
        <Select
          value={sortBy}
          onChange={setSortBy}
          size="small"
          style={{ width: 90 }}
          options={[
            { value: "usage", label: t("dashboard.userQuota.sort.usage") },
            { value: "cost", label: t("dashboard.userQuota.sort.cost") },
          ]}
        />
      }
    >
      {/* Warning summary - more prominent for exceeded users */}
      {warningCount > 0 && (
        <div
          style={{
            padding: "12px 16px",
            marginBottom: 12,
            borderRadius: 8,
            background: users.some((u) => u.status === "exceeded")
              ? darkMode
                ? "rgba(239, 68, 68, 0.15)"
                : "rgba(239, 68, 68, 0.08)"
              : darkMode
              ? "rgba(245, 158, 11, 0.15)"
              : "rgba(245, 158, 11, 0.08)",
            border: users.some((u) => u.status === "exceeded")
              ? "1px solid rgba(239, 68, 68, 0.4)"
              : "1px solid rgba(245, 158, 11, 0.4)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              {users.some((u) => u.status === "exceeded") ? (
                <ExclamationCircleOutlined style={{ fontSize: 16, color: "#ef4444" }} />
              ) : (
                <WarningOutlined style={{ fontSize: 16, color: "#f59e0b" }} />
              )}
              <div>
                <div
                  style={{
                    fontSize: 13,
                    fontWeight: 600,
                    color: users.some((u) => u.status === "exceeded") ? "#ef4444" : "#f59e0b",
                  }}
                >
                  {users.filter((u) => u.status === "exceeded").length > 0
                    ? t("dashboard.userQuota.alert.exceeded", { count: users.filter((u) => u.status === "exceeded").length })
                    : t("dashboard.userQuota.alert.warning", { count: warningCount })}
                </div>
                <div
                  style={{
                    fontSize: 11,
                    color: darkMode ? "#94a3b8" : "#64748b",
                    marginTop: 2,
                  }}
                >
                  {t("dashboard.userQuota.alert.hint")}
                </div>
              </div>
            </div>
            <Tooltip title={t("dashboard.userQuota.alert.upgradeHint")}>
              <Button size="small" type="text" icon={<ExpandOutlined />}>
                {t("dashboard.userQuota.alert.upgradeAction")}
              </Button>
            </Tooltip>
          </div>
        </div>
      )}

      {/* User table */}
      <Table
        dataSource={sortedUsers}
        columns={columns}
        rowKey="user"
        size="small"
        pagination={false}
        scroll={{ y: 200 }}
        style={{
          background: "transparent",
        }}
        rowClassName={(record) =>
          record.status === "exceeded"
            ? "quota-exceeded-row"
            : record.status === "warning"
            ? "quota-warning-row"
            : ""
        }
      />
      <style>{`
        .quota-exceeded-row {
          background: ${darkMode ? "rgba(239, 68, 68, 0.08)" : "rgba(239, 68, 68, 0.04)"} !important;
        }
        .quota-exceeded-row:hover > td {
          background: ${darkMode ? "rgba(239, 68, 68, 0.12)" : "rgba(239, 68, 68, 0.08)"} !important;
        }
        .quota-warning-row {
          background: ${darkMode ? "rgba(245, 158, 11, 0.06)" : "rgba(245, 158, 11, 0.03)"} !important;
        }
        .quota-warning-row:hover > td {
          background: ${darkMode ? "rgba(245, 158, 11, 0.10)" : "rgba(245, 158, 11, 0.06)"} !important;
        }
      `}</style>
    </PanelWrapper>
  );
}
