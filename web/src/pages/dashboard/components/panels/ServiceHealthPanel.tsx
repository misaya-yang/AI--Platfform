// web/src/pages/dashboard/components/panels/ServiceHealthPanel.tsx

import { Row, Col, Statistic } from "antd";
import { CheckCircleOutlined, WarningOutlined, CloseCircleOutlined } from "@ant-design/icons";
import { PanelWrapper } from "../PanelWrapper";
import { useAppStore } from "@/store/useAppStore";
import { useServices, useHealth } from "@/hooks/useServices";
import { getColors } from "../../styles";
import { useTranslation } from "react-i18next";

interface ServiceCardProps {
  name: string;
  status: "healthy" | "degraded" | "down";
  qps: number;
  latency: number;
  errorRate: number;
}

function ServiceStatusCard({ name, status, qps, latency, errorRate }: ServiceCardProps) {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);

  const statusConfig = {
    healthy: { color: colors.success, bg: `${colors.success}10`, icon: <CheckCircleOutlined />, text: t("dashboard.serviceHealth.status.healthy") },
    degraded: { color: colors.warning, bg: `${colors.warning}10`, icon: <WarningOutlined />, text: t("dashboard.serviceHealth.status.degraded") },
    down: { color: colors.error, bg: `${colors.error}10`, icon: <CloseCircleOutlined />, text: t("dashboard.serviceHealth.status.down") },
  };

  const config = statusConfig[status];

  return (
    <div
      style={{
        padding: "18px",
        borderRadius: 12,
        background: colors.innerBg,
        border: `1px solid ${colors.border}`,
        transition: "all 0.3s cubic-bezier(0.4, 0, 0.2, 1)",
        position: "relative",
        overflow: "hidden",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = colors.accent;
        e.currentTarget.style.boxShadow = colors.shadowMd;
        e.currentTarget.style.transform = "translateY(-2px)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = colors.border;
        e.currentTarget.style.boxShadow = "none";
        e.currentTarget.style.transform = "translateY(0)";
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ 
            width: 8, 
            height: 8, 
            borderRadius: "50%", 
            background: config.color,
            boxShadow: `0 0 6px ${config.color}`,
          }} />
          <span style={{ fontSize: 15, fontWeight: 700, color: colors.textPrimary }}>
            {name}
          </span>
        </div>
        <div style={{ 
          fontSize: 11, 
          fontWeight: 600, 
          color: config.color, 
          padding: "2px 8px", 
          borderRadius: 20, 
          background: config.bg,
          display: "flex",
          alignItems: "center",
          gap: 4,
        }}>
          {config.icon}
          {config.text}
        </div>
      </div>
      
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 11, fontWeight: 500, color: colors.textMuted, marginBottom: 4 }}>QPS</div>
          <div style={{ fontSize: 18, fontWeight: 800, color: colors.textPrimary, letterSpacing: "-0.02em" }}>
            {qps.toFixed(1)}
          </div>
        </div>
        <div style={{ width: 1, background: colors.border, alignSelf: "stretch" }} />
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 11, fontWeight: 500, color: colors.textMuted, marginBottom: 4 }}>{t("metrics.avgLatency")}</div>
          <div style={{ fontSize: 18, fontWeight: 800, color: colors.textPrimary, letterSpacing: "-0.02em" }}>
            {latency}<span style={{ fontSize: 11, fontWeight: 500, marginLeft: 1 }}>ms</span>
          </div>
        </div>
        <div style={{ width: 1, background: colors.border, alignSelf: "stretch" }} />
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 11, fontWeight: 500, color: colors.textMuted, marginBottom: 4 }}>{t("dashboard.serviceHealth.errorRateLabel")}</div>
          <div
            style={{
              fontSize: 18,
              fontWeight: 800,
              letterSpacing: "-0.02em",
              color: errorRate > 5 ? colors.error : errorRate > 1 ? colors.warning : colors.success,
            }}
          >
            {errorRate.toFixed(1)}<span style={{ fontSize: 11, fontWeight: 500, marginLeft: 1 }}>%</span>
          </div>
        </div>
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

  // Map services to display format
  const serviceCards: ServiceCardProps[] = services.map((s) => {
    const h = health[s.service_id] || {};
    return {
      name: s.display_name || s.service_id,
      status: h.status === "healthy" ? "healthy" : h.status === "degraded" ? "degraded" : "down",
      qps: h.qps || 0,
      latency: h.avg_latency_ms || 0,
      errorRate: h.error_rate || 0,
    };
  });

  // Calculate summary
  const totalServices = serviceCards.length;
  const healthyCount = serviceCards.filter((s) => s.status === "healthy").length;
  const avgErrorRate =
    serviceCards.length > 0
      ? serviceCards.reduce((sum, s) => sum + s.errorRate, 0) / serviceCards.length
      : 0;

  return (
    <PanelWrapper
      title={t("dashboard.serviceHealth.title")}
      onRefresh={refetch}
      loading={servicesQuery.isLoading || healthQuery.isLoading}
    >
      {/* Summary row */}
      <div
        style={{
          display: "flex",
          gap: 24,
          marginBottom: 20,
          padding: "12px 16px",
          background: colors.innerBg,
          borderRadius: 8,
          border: `1px solid ${colors.border}`,
        }}
      >
        <Statistic
          title={<span style={{ fontSize: 12, color: colors.textSecondary }}>{t("dashboard.serviceHealth.availability")}</span>}
          value={totalServices > 0 ? ((healthyCount / totalServices) * 100).toFixed(1) : 0}
          suffix="%"
          styles={{ content: { color: colors.success, fontSize: 24, fontWeight: 700 } }}
        />
        <div style={{ width: 1, background: colors.border, margin: "8px 0" }} />
        <Statistic
          title={<span style={{ fontSize: 12, color: colors.textSecondary }}>{t("dashboard.serviceHealth.errorRate")}</span>}
          value={avgErrorRate.toFixed(2)}
          suffix="%"
          styles={{
            content: {
              color: avgErrorRate > 5 ? colors.error : avgErrorRate > 1 ? colors.warning : colors.success,
              fontSize: 24,
              fontWeight: 700,
            },
          }}
        />
        <div style={{ width: 1, background: colors.border, margin: "8px 0" }} />
        <Statistic
          title={<span style={{ fontSize: 12, color: colors.textSecondary }}>{t("dashboard.serviceHealth.totalServices")}</span>}
          value={totalServices}
          styles={{ content: { fontSize: 24, fontWeight: 700, color: colors.textPrimary } }}
        />
      </div>

      {/* Service cards */}
      <Row gutter={[12, 12]}>
        {serviceCards.map((service, index) => (
          <Col xs={24} sm={12} lg={12} xl={8} key={index}>
            <ServiceStatusCard {...service} />
          </Col>
        ))}
      </Row>
    </PanelWrapper>
  );
}
