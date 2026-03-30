/**
 * 服务成本分析组件
 *
 * 按服务类型独立展示用量和成本分析：
 * - AI 助手服务
 * - Agent / LangGraph 服务
 * - 其他代理服务
 */

import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Card, Row, Col, Statistic, Empty, Spin, Progress, Tooltip } from "antd";
import {
  DollarOutlined,
  ThunderboltOutlined,
  ApiOutlined,
  RobotOutlined,
  CloudServerOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import { useMemo } from "react";

import { getUsageBreakdown, getUsageSummary } from "@/api/usage";
import type { UsageBreakdownItem } from "@/api/usage";
import { useAppStore } from "@/store/useAppStore";

// 服务类型配置
const SERVICE_CONFIG: Record<string, { icon: React.ReactNode; color: string; gradient: string }> = {
  assistant: {
    icon: <RobotOutlined />,
    color: "#3b82f6",
    gradient: "linear-gradient(135deg, #3b82f6 0%, #60a5fa 100%)",
  },
  langgraph: {
    icon: <ThunderboltOutlined />,
    color: "#8b5cf6",
    gradient: "linear-gradient(135deg, #8b5cf6 0%, #a78bfa 100%)",
  },
  proxy: {
    icon: <CloudServerOutlined />,
    color: "#10b981",
    gradient: "linear-gradient(135deg, #10b981 0%, #34d399 100%)",
  },
  default: {
    icon: <ApiOutlined />,
    color: "#64748b",
    gradient: "linear-gradient(135deg, #64748b 0%, #94a3b8 100%)",
  },
};

// 格式化数字
function formatNumber(num: number): string {
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`;
  if (num >= 1_000) return `${(num / 1_000).toFixed(1)}K`;
  return num.toLocaleString();
}

// 获取服务配置
function getServiceConfig(serviceName: string) {
  const name = serviceName?.toLowerCase() || "";
  if (name.includes("assistant")) return SERVICE_CONFIG.assistant;
  if (name.includes("langgraph") || name.includes("agent")) return SERVICE_CONFIG.langgraph;
  if (name.includes("proxy")) return SERVICE_CONFIG.proxy;
  return SERVICE_CONFIG.default;
}

// 获取服务显示名称
function getServiceDisplayName(serviceName: string, t: ReturnType<typeof useTranslation>["t"]): string {
  const name = serviceName?.toLowerCase() || "";
  if (name.includes("assistant")) return t("cost.service.assistant");
  if (name.includes("langgraph") || name.includes("agent")) return t("cost.service.agent");
  if (name.includes("proxy")) return t("cost.service.proxy");
  return serviceName || t("cost.service.unknown");
}

// 服务卡片组件
interface ServiceCardProps {
  service: UsageBreakdownItem;
  totalCost: number;
}

function ServiceCard({ service }: ServiceCardProps) {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const config = getServiceConfig(service.service || "");
  const displayName = getServiceDisplayName(service.service || "", t);

  return (
    <Card
      className="service-cost-card"
      style={{
        borderRadius: 16,
        border: darkMode ? "1px solid rgba(255,255,255,0.08)" : "1px solid #e2e8f0",
        background: darkMode ? "#1e293b" : "#ffffff",
        overflow: "hidden",
      }}
      styles={{ body: { padding: 0 } }}
    >
      {/* 头部渐变条 */}
      <div
        style={{
          height: 4,
          background: config.gradient,
        }}
      />

      <div style={{ padding: 20 }}>
        {/* 服务标识 */}
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
          <div
            style={{
              width: 44,
              height: 44,
              borderRadius: 12,
              background: config.gradient,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 20,
              color: "#fff",
              boxShadow: `0 4px 12px ${config.color}40`,
            }}
          >
            {config.icon}
          </div>
          <div>
            <div style={{ fontSize: 16, fontWeight: 600, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
              {displayName}
            </div>
            <div style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>
              {service.requests.toLocaleString()} {t("cost.requests")}
            </div>
          </div>
        </div>

        {/* 主要指标 */}
        <Row gutter={[16, 16]}>
          <Col span={12}>
            <Statistic
              title={<span style={{ color: darkMode ? "#94a3b8" : "#64748b" }}>{t("cost.totalCost")}</span>}
              value={service.cost_usd}
              precision={4}
              prefix={<DollarOutlined style={{ color: config.color }} />}
              styles={{ content: { fontSize: 20, fontWeight: 700, color: darkMode ? "#f1f5f9" : "#1e293b" } }}
            />
          </Col>
          <Col span={12}>
            <Statistic
              title={<span style={{ color: darkMode ? "#94a3b8" : "#64748b" }}>{t("cost.totalTokens")}</span>}
              value={service.total_tokens}
              formatter={(v) => formatNumber(v as number)}
              styles={{ content: { fontSize: 20, fontWeight: 700, color: darkMode ? "#f1f5f9" : "#1e293b" } }}
            />
          </Col>
        </Row>

        {/* Token 分解 */}
        <div style={{ marginTop: 16, paddingTop: 16, borderTop: `1px solid ${darkMode ? "rgba(255,255,255,0.08)" : "#e2e8f0"}` }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
            <span style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>
              {t("cost.inputTokens")}: {formatNumber(service.input_tokens)}
            </span>
            <span style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>
              {t("cost.outputTokens")}: {formatNumber(service.output_tokens)}
            </span>
          </div>

          {/* 成本占比进度条 */}
          <Tooltip title={`${service.percentage.toFixed(1)}% ${t("cost.ofTotal")}`}>
            <Progress
              percent={service.percentage}
              strokeColor={config.gradient}
              railColor={darkMode ? "rgba(255,255,255,0.08)" : "#e2e8f0"}
              showInfo={false}
              size="small"
            />
          </Tooltip>
          <div style={{ textAlign: "right", fontSize: 11, color: darkMode ? "#64748b" : "#94a3b8", marginTop: 4 }}>
            {service.percentage.toFixed(1)}% {t("cost.ofTotal")}
          </div>
        </div>
      </div>
    </Card>
  );
}

// 空状态组件
function EmptyState({ darkMode, statusLabel }: { darkMode: boolean; statusLabel?: string }) {
  const { t } = useTranslation();
  return (
    <Card
      style={{
        borderRadius: 16,
        border: darkMode ? "1px solid rgba(255,255,255,0.08)" : "1px solid #e2e8f0",
        background: darkMode ? "#1e293b" : "#ffffff",
      }}
    >
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={
          <span style={{ color: darkMode ? "#94a3b8" : "#64748b" }}>
            {statusLabel || t("cost.noData")}
          </span>
        }
      />
    </Card>
  );
}

// 主组件
export function ServiceCostAnalysis({
  dateRange,
  granularity,
}: {
  dateRange: [dayjs.Dayjs, dayjs.Dayjs];
  granularity: "day" | "hour";
}) {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();

  const startDate = dateRange[0].format("YYYY-MM-DD");
  const endDate = dateRange[1].format("YYYY-MM-DD");

  // 获取按服务分解的数据
  const { data: breakdown, isLoading, refetch, isFetching } = useQuery({
    queryKey: ["usage-breakdown-service", startDate, endDate, granularity],
    queryFn: () =>
      getUsageBreakdown({
        dimension: "service",
        start_date: startDate,
        end_date: endDate,
        limit: 20,
      }),
    staleTime: 60000,
  });

  // 获取总体摘要
  const { data: summary } = useQuery({
    queryKey: ["usage-summary", startDate, endDate, granularity],
    queryFn: () =>
      getUsageSummary({
        start_date: startDate,
        end_date: endDate,
      }),
    staleTime: 60000,
  });

  // 处理服务列表
  const services = useMemo(() => {
    if (!breakdown?.items) return [];
    return breakdown.items.filter((item) => item.service);
  }, [breakdown]);

  const totalCost = breakdown?.total_cost_usd || 0;
  const dataStatus = summary?.data_status || breakdown?.data_status;
  const freshness = summary?.data_freshness_minutes ?? breakdown?.data_freshness_minutes;
  const statusLabel = dataStatus
    ? t(`dashboard.dataStatus.${dataStatus}`, dataStatus)
    : undefined;

  return (
    <div className="service-cost-analysis">
      {/* 头部 */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 20,
          flexWrap: "wrap",
          gap: 12,
        }}
      >
        <div>
          <h2
            style={{
              fontSize: 18,
              fontWeight: 600,
              margin: 0,
              color: darkMode ? "#f1f5f9" : "#1e293b",
            }}
          >
            {t("cost.title")}
          </h2>
          <p style={{ fontSize: 13, color: darkMode ? "#94a3b8" : "#64748b", margin: "4px 0 0 0" }}>
            {t("cost.subtitle")}
          </p>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {dataStatus && (
            <span
              style={{
                fontSize: 12,
                padding: "4px 10px",
                borderRadius: 999,
                background: darkMode ? "#0f172a" : "#f1f5f9",
                color: darkMode ? "#e2e8f0" : "#475569",
                border: `1px solid ${darkMode ? "rgba(255,255,255,0.08)" : "#e2e8f0"}`,
              }}
            >
              {statusLabel}
              {typeof freshness === "number" ? ` · ${freshness}m` : ""}
            </span>
          )}
          <Tooltip title={t("common.refresh")}>
            <div
              onClick={() => refetch()}
              style={{
                width: 32,
                height: 32,
                borderRadius: 8,
                background: darkMode ? "rgba(255,255,255,0.08)" : "#f1f5f9",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                cursor: "pointer",
                transition: "all 0.2s",
              }}
            >
              <SyncOutlined spin={isFetching} style={{ color: darkMode ? "#94a3b8" : "#64748b" }} />
            </div>
          </Tooltip>
        </div>
      </div>

      {/* 总体摘要 */}
      {summary && (
        <Card
          style={{
            borderRadius: 16,
            marginBottom: 20,
            border: darkMode ? "1px solid rgba(255,255,255,0.08)" : "1px solid #e2e8f0",
            background: darkMode
              ? "linear-gradient(135deg, #1e293b 0%, #0f172a 100%)"
              : "linear-gradient(135deg, #ffffff 0%, #f8fafc 100%)",
          }}
        >
          <Row gutter={[24, 16]}>
            <Col xs={12} sm={6}>
              <Statistic
                title={<span style={{ color: darkMode ? "#94a3b8" : "#64748b" }}>{t("cost.totalRequests")}</span>}
                value={summary.total_requests}
                prefix={<ApiOutlined style={{ color: "#3b82f6" }} />}
                styles={{ content: { color: darkMode ? "#f1f5f9" : "#1e293b" } }}
              />
            </Col>
            <Col xs={12} sm={6}>
              <Statistic
                title={<span style={{ color: darkMode ? "#94a3b8" : "#64748b" }}>{t("cost.totalCostAll")}</span>}
                value={summary.total_cost_usd}
                precision={4}
                prefix={<DollarOutlined style={{ color: "#10b981" }} />}
                styles={{ content: { color: darkMode ? "#f1f5f9" : "#1e293b" } }}
              />
            </Col>
            <Col xs={12} sm={6}>
              <Statistic
                title={<span style={{ color: darkMode ? "#94a3b8" : "#64748b" }}>{t("cost.totalTokensAll")}</span>}
                value={summary.total_tokens}
                formatter={(v) => formatNumber(v as number)}
                prefix={<ThunderboltOutlined style={{ color: "#f59e0b" }} />}
                styles={{ content: { color: darkMode ? "#f1f5f9" : "#1e293b" } }}
              />
            </Col>
            <Col xs={12} sm={6}>
              <Statistic
                title={<span style={{ color: darkMode ? "#94a3b8" : "#64748b" }}>{t("cost.successRate")}</span>}
                value={summary.success_rate}
                precision={1}
                suffix="%"
                styles={{ content: { color: summary.success_rate >= 95 ? "#10b981" : "#f59e0b" } }}
              />
            </Col>
          </Row>
        </Card>
      )}

      {/* 服务列表 */}
      {isLoading ? (
        <div style={{ textAlign: "center", padding: 60 }}>
          <Spin size="large" />
        </div>
      ) : services.length > 0 ? (
        <Row gutter={[16, 16]}>
          {services.map((service, index) => (
            <Col xs={24} sm={12} lg={8} key={service.service || index}>
              <ServiceCard service={service} totalCost={totalCost} />
            </Col>
          ))}
        </Row>
      ) : (
        <EmptyState darkMode={darkMode} statusLabel={statusLabel} />
      )}
    </div>
  );
}

export default ServiceCostAnalysis;
