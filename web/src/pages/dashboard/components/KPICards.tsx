// web/src/pages/dashboard/components/KPICards.tsx

import { Row, Col, Spin } from "antd";
import {
  ApiOutlined,
  DollarOutlined,
  ThunderboltOutlined,
  CheckCircleOutlined,
  UserOutlined,
  ArrowUpOutlined,
  ArrowDownOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { useDashboardContext } from "../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getUsageSummary } from "@/api/usage";

function formatNumber(num: number): string {
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`;
  if (num >= 1_000) return `${(num / 1_000).toFixed(1)}K`;
  return num.toLocaleString();
}

function formatCurrency(value: number): string {
  if (value >= 1) return `$${value.toFixed(2)}`;
  return `$${value.toFixed(4)}`;
}

interface KPICardProps {
  title: string;
  value: string | number;
  icon: React.ReactNode;
  iconColor: string;
  iconBg: string;
  change?: number;
  suffix?: string;
  loading?: boolean;
}

function KPICard({ title, value, icon, iconColor, iconBg, change, suffix, loading }: KPICardProps) {
  const { darkMode } = useAppStore();

  return (
    <div
      style={{
        padding: 20,
        borderRadius: 12,
        background: darkMode ? "#1e293b" : "#ffffff",
        border: darkMode ? "1px solid #334155" : "1px solid #e2e8f0",
        height: "100%",
      }}
    >
      {loading ? (
        <div style={{ textAlign: "center", padding: 20 }}>
          <Spin />
        </div>
      ) : (
        <div style={{ display: "flex", alignItems: "flex-start", gap: 16 }}>
          <div
            style={{
              width: 48,
              height: 48,
              borderRadius: 12,
              background: iconBg,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 20,
              color: iconColor,
              flexShrink: 0,
            }}
          >
            {icon}
          </div>
          <div style={{ flex: 1 }}>
            <div
              style={{
                fontSize: 13,
                color: darkMode ? "#94a3b8" : "#64748b",
                marginBottom: 4,
              }}
            >
              {title}
            </div>
            <div
              style={{
                fontSize: 24,
                fontWeight: 700,
                color: darkMode ? "#f1f5f9" : "#1e293b",
                lineHeight: 1.2,
              }}
            >
              {value}
              {suffix && (
                <span style={{ fontSize: 14, fontWeight: 400, marginLeft: 2 }}>
                  {suffix}
                </span>
              )}
            </div>
            {change !== undefined && (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 4,
                  marginTop: 4,
                  fontSize: 12,
                  color: change >= 0 ? "#10b981" : "#ef4444",
                }}
              >
                {change >= 0 ? <ArrowUpOutlined /> : <ArrowDownOutlined />}
                {Math.abs(change).toFixed(1)}% vs 上周期
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export function KPICards() {
  const { dateRange, source, lastRefresh } = useDashboardContext();

  const { data, isLoading } = useQuery({
    queryKey: ["dashboard-kpi", dateRange, source, lastRefresh.getTime()],
    queryFn: () =>
      getUsageSummary({
        start_date: dateRange[0],
        end_date: dateRange[1],
      }),
    staleTime: 30000,
  });

  const kpiData = [
    {
      title: "总请求数",
      value: formatNumber(data?.total_requests || 0),
      icon: <ApiOutlined />,
      iconColor: "#3b82f6",
      iconBg: "rgba(59, 130, 246, 0.1)",
    },
    {
      title: "总成本",
      value: formatCurrency(data?.total_cost_usd || 0),
      icon: <DollarOutlined />,
      iconColor: "#10b981",
      iconBg: "rgba(16, 185, 129, 0.1)",
    },
    {
      title: "平均延迟",
      value: data?.avg_latency_ms || 0,
      suffix: "ms",
      icon: <ThunderboltOutlined />,
      iconColor: "#f59e0b",
      iconBg: "rgba(245, 158, 11, 0.1)",
    },
    {
      title: "成功率",
      value: (data?.success_rate || 0).toFixed(1),
      suffix: "%",
      icon: <CheckCircleOutlined />,
      iconColor: data?.success_rate && data.success_rate >= 95 ? "#10b981" : "#f59e0b",
      iconBg:
        data?.success_rate && data.success_rate >= 95
          ? "rgba(16, 185, 129, 0.1)"
          : "rgba(245, 158, 11, 0.1)",
    },
    {
      title: "Token 总量",
      value: formatNumber(data?.total_tokens || 0),
      icon: <UserOutlined />,
      iconColor: "#8b5cf6",
      iconBg: "rgba(139, 92, 246, 0.1)",
    },
  ];

  return (
    <Row gutter={[16, 16]} style={{ marginBottom: 20 }}>
      {kpiData.map((kpi, index) => (
        <Col xs={24} sm={12} md={8} lg={4} xl={4} key={index} style={{ minWidth: 180 }}>
          <KPICard {...kpi} loading={isLoading} />
        </Col>
      ))}
    </Row>
  );
}
