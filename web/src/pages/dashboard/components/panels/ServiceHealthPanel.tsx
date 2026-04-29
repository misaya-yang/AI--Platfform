// web/src/pages/dashboard/components/panels/ServiceHealthPanel.tsx

import { Empty, Tag } from "antd";
import { CheckCircleOutlined, CloseCircleOutlined, WarningOutlined } from "@ant-design/icons";
import { PanelWrapper } from "../PanelWrapper";
import { useAppStore } from "@/store/useAppStore";
import { useHealth, useServices } from "@/hooks/useServices";
import { FONT_FAMILY, getColors } from "../../styles";
import { useTranslation } from "react-i18next";

interface ServiceCardProps {
  id: string;
  name: string;
  status: "healthy" | "degraded" | "down";
  qps: number;
  latency: number;
  errorRate: number;
}

function formatLatency(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  return `${Math.round(ms)}ms`;
}

function ServiceStatusCard({ name, status, qps, latency, errorRate }: ServiceCardProps) {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);

  const statusConfig = {
    healthy: {
      color: colors.success,
      bg: colors.successSoft,
      icon: <CheckCircleOutlined />,
      text: t("dashboard.serviceHealth.status.healthy", "健康"),
    },
    degraded: {
      color: colors.warning,
      bg: colors.warningSoft,
      icon: <WarningOutlined />,
      text: t("dashboard.serviceHealth.status.degraded", "降级"),
    },
    down: {
      color: colors.error,
      bg: colors.errorSoft,
      icon: <CloseCircleOutlined />,
      text: t("dashboard.serviceHealth.status.down", "不可用"),
    },
  };
  const config = statusConfig[status];

  return (
    <div
      style={{
        minHeight: 122,
        padding: 14,
        borderRadius: 9,
        background: colors.cardBg,
        border: `1px solid ${status === "down" ? `${colors.error}44` : colors.borderSoft}`,
        display: "flex",
        flexDirection: "column",
        justifyContent: "space-between",
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: config.color,
                boxShadow: `0 0 0 4px ${config.color}18`,
                flexShrink: 0,
              }}
            />
            <span
              style={{
                color: colors.textPrimary,
                fontSize: 14,
                fontWeight: 650,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {name}
            </span>
          </div>
          <div style={{ marginTop: 6, color: colors.textMuted, fontSize: 11 }}>
            {t("dashboard.ops.routingNode", "Gateway route node")}
          </div>
        </div>
        <Tag
          icon={config.icon}
          style={{
            margin: 0,
            border: "none",
            borderRadius: 6,
            color: config.color,
            background: config.bg,
            fontWeight: 650,
          }}
        >
          {config.text}
        </Tag>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
          gap: 8,
        }}
      >
        {[
          { label: "QPS", value: qps.toFixed(1), color: colors.textPrimary },
          { label: t("metrics.avgLatency", "平均延迟"), value: formatLatency(latency), color: latency > 3000 ? colors.warning : colors.textPrimary },
          { label: t("dashboard.serviceHealth.errorRateLabel", "错误率"), value: `${errorRate.toFixed(1)}%`, color: errorRate > 5 ? colors.error : errorRate > 1 ? colors.warning : colors.success },
        ].map((metric) => (
          <div key={metric.label} style={{ minWidth: 0 }}>
            <div style={{ color: colors.textMuted, fontSize: 11, marginBottom: 3 }}>{metric.label}</div>
            <div
              style={{
                color: metric.color,
                fontSize: 16,
                fontWeight: 700,
                fontFamily: metric.label === "QPS" ? FONT_FAMILY.mono : FONT_FAMILY.sans,
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {metric.value}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function ServiceHealthPanel() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const servicesQuery = useServices();
  const healthQuery = useHealth();

  const services = servicesQuery.data || [];
  const health = healthQuery.data || {};

  const refetch = () => {
    servicesQuery.refetch();
    healthQuery.refetch();
  };

  const serviceCards: ServiceCardProps[] = services.map((service) => {
    const h = health[service.service_id] || {};
    return {
      id: service.service_id,
      name: service.display_name || service.service_id,
      status: h.status === "healthy" ? "healthy" : h.status === "degraded" ? "degraded" : "down",
      qps: h.qps || 0,
      latency: h.avg_latency_ms || 0,
      errorRate: h.error_rate || 0,
    };
  });

  const totalServices = serviceCards.length;
  const healthyCount = serviceCards.filter((service) => service.status === "healthy").length;
  const degradedCount = serviceCards.filter((service) => service.status === "degraded").length;
  const downCount = serviceCards.filter((service) => service.status === "down").length;
  const avgErrorRate = totalServices > 0
    ? serviceCards.reduce((sum, service) => sum + service.errorRate, 0) / totalServices
    : 0;
  const availability = totalServices > 0 ? (healthyCount / totalServices) * 100 : 0;

  const summary = [
    {
      label: t("dashboard.serviceHealth.availability", "可用率"),
      value: `${availability.toFixed(1)}%`,
      color: availability >= 99 ? colors.success : availability >= 90 ? colors.warning : colors.error,
    },
    {
      label: t("dashboard.serviceHealth.errorRate", "错误率"),
      value: `${avgErrorRate.toFixed(2)}%`,
      color: avgErrorRate > 5 ? colors.error : avgErrorRate > 1 ? colors.warning : colors.success,
    },
    {
      label: t("dashboard.serviceHealth.totalServices", "服务总数"),
      value: String(totalServices),
      color: colors.textPrimary,
    },
    {
      label: t("dashboard.serviceHealth.degraded", "降级/不可用"),
      value: `${degradedCount}/${downCount}`,
      color: downCount > 0 ? colors.error : degradedCount > 0 ? colors.warning : colors.textPrimary,
    },
  ];

  return (
    <PanelWrapper
      title={t("dashboard.serviceHealth.title", "服务健康")}
      onRefresh={refetch}
      loading={servicesQuery.isLoading || healthQuery.isLoading}
    >
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
          gap: 10,
          marginBottom: 16,
        }}
      >
        {summary.map((item) => (
          <div
            key={item.label}
            style={{
              minHeight: 74,
              padding: "11px 12px",
              borderRadius: 8,
              border: `1px solid ${colors.borderSoft}`,
              background: colors.innerBg,
            }}
          >
            <div style={{ color: colors.textMuted, fontSize: 12, marginBottom: 8 }}>{item.label}</div>
            <div style={{ color: item.color, fontSize: 22, fontWeight: 750, fontFeatureSettings: '"tnum"' }}>
              {item.value}
            </div>
          </div>
        ))}
      </div>

      {serviceCards.length > 0 ? (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
            gap: 12,
          }}
        >
          {serviceCards.map((service) => (
            <ServiceStatusCard key={service.id} {...service} />
          ))}
        </div>
      ) : (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={<span style={{ color: colors.textMuted }}>{t("dashboard.serviceHealth.empty", "暂无服务健康数据")}</span>}
        />
      )}
    </PanelWrapper>
  );
}
