// web/src/pages/dashboard/components/panels/UserQuotaPanel.tsx

import { Table, Progress, Tag, Select } from "antd";
import { useQuery } from "@tanstack/react-query";
import { PanelWrapper } from "../PanelWrapper";
import { useDashboardContext } from "../../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getUsageBreakdown } from "@/api/usage";
import { useState } from "react";

function formatTokens(num: number): string {
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`;
  if (num >= 1_000) return `${(num / 1_000).toFixed(1)}K`;
  return num.toLocaleString();
}

export function UserQuotaPanel() {
  const { darkMode } = useAppStore();
  const { dateRange, lastRefresh } = useDashboardContext();
  const [sortBy, setSortBy] = useState<"usage" | "cost">("usage");

  const { data, isLoading, refetch } = useQuery({
    queryKey: ["dashboard-user-quota", dateRange, lastRefresh.getTime()],
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
      user: item.user || "unknown",
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

  const columns = [
    {
      title: "用户",
      dataIndex: "user",
      key: "user",
      width: 120,
      ellipsis: true,
      render: (text: string) => (
        <span style={{ fontWeight: 500, color: darkMode ? "#f1f5f9" : "#1e293b" }}>{text}</span>
      ),
    },
    {
      title: "日配额",
      key: "daily",
      width: 150,
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
      title: "月配额",
      key: "monthly",
      width: 150,
      render: (_: unknown, record: (typeof users)[0]) => (
        <div>
          <Progress
            percent={Math.min(record.monthlyPercent, 100)}
            size="small"
            strokeColor={record.monthlyPercent >= 80 ? "#f59e0b" : "#10b981"}
            showInfo={false}
          />
          <div style={{ fontSize: 11, color: darkMode ? "#94a3b8" : "#64748b" }}>
            {formatTokens(record.monthlyUsed)}/{formatTokens(record.monthlyLimit)}
          </div>
        </div>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 80,
      render: (status: string) => {
        const config = {
          normal: { color: "success", text: "正常" },
          warning: { color: "warning", text: "警告" },
          exceeded: { color: "error", text: "超额" },
        }[status] || { color: "default", text: status };
        return <Tag color={config.color}>{config.text}</Tag>;
      },
    },
  ];

  return (
    <PanelWrapper
      title="用户配额"
      onRefresh={refetch}
      loading={isLoading}
      extra={
        <Select
          value={sortBy}
          onChange={setSortBy}
          size="small"
          style={{ width: 90 }}
          options={[
            { value: "usage", label: "按用量" },
            { value: "cost", label: "按成本" },
          ]}
        />
      }
    >
      {/* Warning summary */}
      {warningCount > 0 && (
        <div
          style={{
            padding: "8px 12px",
            marginBottom: 12,
            borderRadius: 6,
            background: darkMode ? "rgba(245, 158, 11, 0.1)" : "rgba(245, 158, 11, 0.1)",
            border: "1px solid rgba(245, 158, 11, 0.3)",
            fontSize: 12,
            color: "#f59e0b",
          }}
        >
          {warningCount} 个用户接近或超过配额限制
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
      />
    </PanelWrapper>
  );
}
