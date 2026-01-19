// web/src/pages/dashboard/components/KPICards.tsx
// KPI Summary Cards - Unified Layout System

import { Spin } from "antd";
import {
  ApiOutlined,
  DollarOutlined,
  ThunderboltOutlined,
  CheckCircleOutlined,
  DatabaseOutlined,
  ArrowUpOutlined,
  ArrowDownOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { useDashboardContext } from "../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { getUsageSummary } from "@/api/usage";
import { LAYOUT, getColors, gridStyles } from "../styles";

function formatNumber(num: number): string {
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`;
  if (num >= 1_000) return `${(num / 1_000).toFixed(1)}K`;
  return num.toLocaleString();
}

function formatCurrency(value: number): string {
  if (value >= 1) return `$${value.toFixed(2)}`;
  return `$${value.toFixed(4)}`;
}

interface TrendProps {
  value: number;
  isPositiveGood?: boolean;
}

function Trend({ value, isPositiveGood = true }: TrendProps) {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const isUp = value > 0;
  const isGood = isUp === isPositiveGood;
  const color = isGood ? colors.success : colors.error;

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 4,
        fontSize: 11,
        fontWeight: 600,
        color: color,
        marginTop: 4,
      }}
    >
      {isUp ? <ArrowUpOutlined /> : <ArrowDownOutlined />}
      <span>{Math.abs(value)}%</span>
      <span style={{ color: colors.textMuted, fontWeight: 400, marginLeft: 2 }}>较上期</span>
    </div>
  );
}

interface KPICardProps {
  title: string;
  value: string | number;
  icon: React.ReactNode;
  iconColor: string;
  iconGradient: string;
  suffix?: string;
  loading?: boolean;
  trend?: number;
  isPositiveGood?: boolean;
}

function KPICard({ 
  title, 
  value, 
  icon, 
  iconColor, 
  iconGradient,
  suffix, 
  loading,
  trend,
  isPositiveGood 
}: KPICardProps) {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);

  return (
    <div
      style={{
        padding: LAYOUT.CARD_PADDING,
        borderRadius: LAYOUT.CARD_RADIUS,
        background: colors.cardBg,
        border: `1px solid ${colors.border}`,
        boxShadow: colors.shadowSm,
        height: "100%",
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
        minWidth: 0,
        transition: "all 0.3s cubic-bezier(0.4, 0, 0.2, 1)",
        position: "relative",
        overflow: "hidden",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.transform = "translateY(-4px)";
        e.currentTarget.style.boxShadow = colors.shadowLg;
        e.currentTarget.style.borderColor = `${iconColor}40`;
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.transform = "translateY(0)";
        e.currentTarget.style.boxShadow = colors.shadowSm;
        e.currentTarget.style.borderColor = colors.border;
      }}
    >
      {/* Decorative gradient blur */}
      <div 
        style={{
          position: "absolute",
          top: -20,
          right: -20,
          width: 80,
          height: 80,
          background: iconGradient,
          opacity: 0.05,
          filter: "blur(20px)",
          borderRadius: "50%",
          pointerEvents: "none",
        }}
      />

      {loading ? (
        <div style={{ textAlign: "center", padding: 16 }}>
          <Spin size="small" />
        </div>
      ) : (
        <div style={{ display: "flex", alignItems: "flex-start", gap: 16 }}>
          {/* Icon */}
          <div
            style={{
              width: 44,
              height: 44,
              borderRadius: 12,
              background: iconGradient,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 20,
              color: "#ffffff",
              flexShrink: 0,
              boxShadow: `0 4px 10px ${iconColor}30`,
            }}
          >
            {icon}
          </div>

          {/* Content */}
          <div style={{ flex: 1, minWidth: 0 }}>
            <div
              style={{
                fontSize: 12,
                fontWeight: 500,
                color: colors.textSecondary,
                marginBottom: 2,
                whiteSpace: "nowrap",
                textTransform: "uppercase",
                letterSpacing: "0.025em",
              }}
            >
              {title}
            </div>
            <div
              style={{
                display: "flex",
                alignItems: "baseline",
                gap: 2,
              }}
            >
              <span
                style={{
                  fontSize: 26,
                  fontWeight: 700,
                  color: colors.textPrimary,
                  lineHeight: 1.1,
                  letterSpacing: "-0.02em",
                }}
              >
                {value}
              </span>
              {suffix && (
                <span
                  style={{
                    fontSize: 14,
                    fontWeight: 600,
                    color: colors.textSecondary,
                  }}
                >
                  {suffix}
                </span>
              )}
            </div>
            {trend !== undefined && <Trend value={trend} isPositiveGood={isPositiveGood} />}
          </div>
        </div>
      )}
    </div>
  );
}

export function KPICards() {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const { dateRange, serviceId, userId, lastRefresh } = useDashboardContext();

  const { data, isLoading } = useQuery({
    queryKey: ["dashboard-kpi", dateRange, serviceId, userId, lastRefresh.getTime()],
    queryFn: () =>
      getUsageSummary({
        start_date: dateRange[0],
        end_date: dateRange[1],
        service_id: serviceId !== "all" ? serviceId : undefined,
        user_id: userId !== "all" ? userId : undefined,
      }),
    staleTime: 30000,
  });

  const kpiData = [
    {
      title: "总请求数",
      value: formatNumber(data?.total_requests || 0),
      icon: <ApiOutlined />,
      iconColor: colors.accent,
      iconGradient: colors.accentGradient,
      trend: 12.5,
    },
    {
      title: "总成本",
      value: formatCurrency(data?.total_cost_usd || 0),
      icon: <DollarOutlined />,
      iconColor: colors.success,
      iconGradient: colors.successGradient,
      trend: -5.2,
      isPositiveGood: false,
    },
    {
      title: "平均延迟",
      value: Math.round(data?.avg_latency_ms || 0),
      suffix: "ms",
      icon: <ThunderboltOutlined />,
      iconColor: colors.warning,
      iconGradient: colors.warningGradient,
      trend: -8.4,
      isPositiveGood: false,
    },
    {
      title: "成功率",
      value: (data?.success_rate || 0).toFixed(1),
      suffix: "%",
      icon: <CheckCircleOutlined />,
      iconColor: data?.success_rate && data.success_rate >= 95 ? colors.success : colors.warning,
      iconGradient: data?.success_rate && data.success_rate >= 95 ? colors.successGradient : colors.warningGradient,
      trend: 0.2,
    },
    {
      title: "Token 总量",
      value: formatNumber(data?.total_tokens || 0),
      icon: <DatabaseOutlined />,
      iconColor: colors.purple,
      iconGradient: colors.purpleGradient,
      trend: 15.8,
    },
  ];

  return (
    <div
      style={{
        ...gridStyles.fiveColumnResponsive,
      }}
    >
      {kpiData.map((kpi, index) => (
        <KPICard key={index} {...kpi} loading={isLoading} />
      ))}
    </div>
  );
}
