// web/src/pages/dashboard/components/panels/ServiceHealthPanel.tsx

import { Row, Col, Tag, Statistic } from "antd";
import { CheckCircleOutlined, WarningOutlined, CloseCircleOutlined } from "@ant-design/icons";
import { PanelWrapper } from "../PanelWrapper";
import { useDashboardContext } from "../../DashboardContext";
import { useAppStore } from "@/store/useAppStore";
import { useServices, useHealth } from "@/hooks/useServices";

interface ServiceCardProps {
  name: string;
  status: "healthy" | "degraded" | "down";
  qps: number;
  latency: number;
  errorRate: number;
}

function ServiceStatusCard({ name, status, qps, latency, errorRate }: ServiceCardProps) {
  const { darkMode } = useAppStore();

  const statusConfig = {
    healthy: { color: "#10b981", icon: <CheckCircleOutlined />, text: "正常" },
    degraded: { color: "#f59e0b", icon: <WarningOutlined />, text: "降级" },
    down: { color: "#ef4444", icon: <CloseCircleOutlined />, text: "异常" },
  };

  const config = statusConfig[status];

  return (
    <div
      style={{
        padding: 16,
        borderRadius: 8,
        background: darkMode ? "#0f172a" : "#f8fafc",
        border: `1px solid ${darkMode ? "#334155" : "#e2e8f0"}`,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
        <span style={{ fontSize: 14, fontWeight: 600, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
          {name}
        </span>
        <Tag color={config.color} icon={config.icon}>
          {config.text}
        </Tag>
      </div>
      <Row gutter={8}>
        <Col span={8}>
          <div style={{ fontSize: 11, color: darkMode ? "#64748b" : "#94a3b8" }}>QPS</div>
          <div style={{ fontSize: 16, fontWeight: 600, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
            {qps.toFixed(1)}
          </div>
        </Col>
        <Col span={8}>
          <div style={{ fontSize: 11, color: darkMode ? "#64748b" : "#94a3b8" }}>延迟</div>
          <div style={{ fontSize: 16, fontWeight: 600, color: darkMode ? "#f1f5f9" : "#1e293b" }}>
            {latency}ms
          </div>
        </Col>
        <Col span={8}>
          <div style={{ fontSize: 11, color: darkMode ? "#64748b" : "#94a3b8" }}>错误率</div>
          <div
            style={{
              fontSize: 16,
              fontWeight: 600,
              color: errorRate > 5 ? "#ef4444" : errorRate > 1 ? "#f59e0b" : "#10b981",
            }}
          >
            {errorRate.toFixed(1)}%
          </div>
        </Col>
      </Row>
    </div>
  );
}

export function ServiceHealthPanel() {
  const { darkMode } = useAppStore();
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
      title="服务健康状态"
      onRefresh={refetch}
      loading={servicesQuery.isLoading || healthQuery.isLoading}
    >
      {/* Summary row */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={8}>
          <Statistic
            title={<span style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>可用性</span>}
            value={totalServices > 0 ? ((healthyCount / totalServices) * 100).toFixed(1) : 0}
            suffix="%"
            valueStyle={{ color: "#10b981", fontSize: 20 }}
          />
        </Col>
        <Col span={8}>
          <Statistic
            title={<span style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>错误率</span>}
            value={avgErrorRate.toFixed(2)}
            suffix="%"
            valueStyle={{
              color: avgErrorRate > 5 ? "#ef4444" : avgErrorRate > 1 ? "#f59e0b" : "#10b981",
              fontSize: 20,
            }}
          />
        </Col>
        <Col span={8}>
          <Statistic
            title={<span style={{ fontSize: 12, color: darkMode ? "#94a3b8" : "#64748b" }}>服务数</span>}
            value={totalServices}
            valueStyle={{ fontSize: 20, color: darkMode ? "#f1f5f9" : "#1e293b" }}
          />
        </Col>
      </Row>

      {/* Service cards */}
      <Row gutter={[12, 12]}>
        {serviceCards.map((service, index) => (
          <Col xs={24} sm={12} lg={8} key={index}>
            <ServiceStatusCard {...service} />
          </Col>
        ))}
      </Row>
    </PanelWrapper>
  );
}
