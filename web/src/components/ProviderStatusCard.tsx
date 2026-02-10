// web/src/components/ProviderStatusCard.tsx
// Provider Status Card - Uses Dashboard Unified Layout System

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Spin, Modal, Table, Tag, Tooltip } from "antd";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  CloudOutlined,
  RobotOutlined,
  ThunderboltOutlined,
  GlobalOutlined,
  ExperimentOutlined,
  InfoCircleOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { useProvidersHealth } from "@/hooks/useServices";
import { useAppStore } from "@/store/useAppStore";
import { api } from "@/lib/api";

// Import unified layout from dashboard styles
import { LAYOUT, getColors, gridStyles } from "@/pages/dashboard/styles";
import { PanelWrapper } from "@/pages/dashboard/components/PanelWrapper";

// Provider configuration
// ... (keep PROVIDER_CONFIG)
const PROVIDER_CONFIG: Record<
  string,
  {
    icon: React.ReactNode;
    color: string;
    gradient: string;
  }
> = {
  openai: {
    icon: <RobotOutlined />,
    color: "#10a37f",
    gradient: "linear-gradient(135deg, #10a37f 0%, #1a7f64 100%)",
  },
  anthropic: {
    icon: <ExperimentOutlined />,
    color: "#d97706",
    gradient: "linear-gradient(135deg, #d97706 0%, #b45309 100%)",
  },
  deepseek: {
    icon: <ThunderboltOutlined />,
    color: "#6366f1",
    gradient: "linear-gradient(135deg, #6366f1 0%, #4f46e5 100%)",
  },
  dashscope: {
    icon: <CloudOutlined />,
    color: "#ff6a00",
    gradient: "linear-gradient(135deg, #ff6a00 0%, #ee0979 100%)",
  },
  google: {
    icon: <GlobalOutlined />,
    color: "#4285f4",
    gradient: "linear-gradient(135deg, #4285f4 0%, #34a853 50%, #fbbc04 100%)",
  },
};

// ... (keep Model and ProviderCardProps interfaces)

interface Model {
  model_id: string;
  display_name: string;
  context_window: number;
  max_output_tokens: number;
  supports_vision: boolean;
  supports_tools: boolean;
  input_price_per_1k: string;
  output_price_per_1k: string;
  is_enabled: boolean;
}

interface ProviderCardProps {
  providerKey: string;
  name: string;
  configured: boolean;
  modelCount: number;
  onClick: () => void;
}

function ProviderCard({
  providerKey,
  name,
  configured,
  modelCount,
  onClick,
}: ProviderCardProps) {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);

  const config = PROVIDER_CONFIG[providerKey] || {
    icon: <CloudOutlined />,
    color: "#64748b",
    gradient: "linear-gradient(135deg, #64748b 0%, #475569 100%)",
  };

  const borderColor = configured
    ? darkMode
      ? `${config.color}40`
      : `${config.color}30`
    : colors.border;

  return (
    <div
      onClick={configured ? onClick : undefined}
      style={{
        padding: "18px",
        borderRadius: 14,
        background: configured ? colors.cardBg : colors.innerBg,
        border: `1.5px solid ${borderColor}`,
        position: "relative",
        overflow: "hidden",
        cursor: configured ? "pointer" : "default",
        transition: "all 0.3s cubic-bezier(0.4, 0, 0.2, 1)",
        minWidth: 0,
        boxShadow: colors.shadowSm,
      }}
      onMouseEnter={(e) => {
        if (configured) {
          e.currentTarget.style.transform = "translateY(-4px)";
          e.currentTarget.style.boxShadow = colors.shadowLg;
          e.currentTarget.style.borderColor = `${config.color}60`;
        }
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.transform = "translateY(0)";
        e.currentTarget.style.boxShadow = colors.shadowSm;
        e.currentTarget.style.borderColor = borderColor;
      }}
    >
      {/* Decorative accent blob */}
      {configured && (
        <div 
          style={{
            position: "absolute",
            bottom: -15,
            right: -15,
            width: 60,
            height: 60,
            background: config.gradient,
            opacity: 0.08,
            filter: "blur(15px)",
            borderRadius: "50%",
            pointerEvents: "none",
          }}
        />
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
        <div
          style={{
            width: 44,
            height: 44,
            borderRadius: 12,
            background: configured ? config.gradient : colors.border,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 20,
            color: configured ? "#ffffff" : colors.textMuted,
            flexShrink: 0,
            boxShadow: configured ? `0 4px 10px ${config.color}30` : "none",
          }}
        >
          {config.icon}
        </div>

        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 6 }}>
            <span
              style={{
                fontSize: 15,
                fontWeight: 700,
                color: colors.textPrimary,
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {name}
            </span>
            {configured ? (
              <CheckCircleOutlined style={{ color: colors.success, fontSize: 14 }} />
            ) : (
              <CloseCircleOutlined style={{ color: colors.textMuted, fontSize: 14 }} />
            )}
          </div>

          <div
            style={{
              fontSize: 12,
              fontWeight: 500,
              color: configured ? colors.textSecondary : colors.textMuted,
              marginTop: 2,
              display: "flex",
              alignItems: "center",
              gap: 4,
              flexWrap: "nowrap",
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {configured && modelCount > 0 ? (
              <>
                <span>{t("services.providersStatus.modelsCount", { count: modelCount })}</span>
                <span style={{ fontSize: 10, opacity: 0.5 }}>•</span>
                <span style={{ color: config.color, fontSize: 11 }}>{t("services.providersStatus.viewDetails")}</span>
              </>
            ) : (
              t("services.providersStatus.unconfigured")
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ... (keep ProviderDetailModal)
function ProviderDetailModal({
  open,
  onClose,
  providerKey,
  providerName,
}: {
  open: boolean;
  onClose: () => void;
  providerKey: string;
  providerName: string;
}) {
  const { t } = useTranslation();
  const config = PROVIDER_CONFIG[providerKey] || PROVIDER_CONFIG.openai;

  const { data: models, isLoading } = useQuery({
    queryKey: ["models", providerKey],
    queryFn: async () => {
      const { data } = await api.get<Model[]>("/api/v1/models", {
        params: { provider_id: providerKey },
      });
      return data;
    },
    enabled: open,
  });

  const columns = [
    {
      title: t("services.providersStatus.table.name"),
      dataIndex: "display_name",
      key: "display_name",
      render: (text: string, record: Model) => (
        <div>
          <div style={{ fontWeight: 500 }}>{text}</div>
          <div style={{ fontSize: 11, color: "#94a3b8" }}>{record.model_id}</div>
        </div>
      ),
    },
    {
      title: t("services.providersStatus.table.context"),
      dataIndex: "context_window",
      key: "context_window",
      width: 100,
      render: (val: number) => `${(val / 1000).toFixed(0)}K`,
    },
    {
      title: t("services.providersStatus.table.capabilities"),
      key: "capabilities",
      width: 120,
      render: (_: unknown, record: Model) => (
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          {record.supports_vision && (
            <Tag color="blue" style={{ margin: 0, fontSize: 10 }}>
              {t("services.providersStatus.table.vision")}
            </Tag>
          )}
          {record.supports_tools && (
            <Tag color="green" style={{ margin: 0, fontSize: 10 }}>
              {t("services.providersStatus.table.tools")}
            </Tag>
          )}
        </div>
      ),
    },
    {
      title: t("services.providersStatus.table.price"),
      key: "price",
      width: 140,
      render: (_: unknown, record: Model) => (
        <div style={{ fontSize: 12 }}>
          <div>{t("services.providersStatus.table.input")}: ${record.input_price_per_1k}</div>
          <div>{t("services.providersStatus.table.output")}: ${record.output_price_per_1k}</div>
        </div>
      ),
    },
    {
      title: t("services.providersStatus.table.status"),
      dataIndex: "is_enabled",
      key: "is_enabled",
      width: 80,
      render: (enabled: boolean) => (
        <Tag color={enabled ? "success" : "default"}>
          {enabled ? t("services.providersStatus.table.enabled") : t("services.providersStatus.table.disabled")}
        </Tag>
      ),
    },
  ];

  return (
    <Modal
      title={
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div
            style={{
              width: 28,
              height: 28,
              borderRadius: 6,
              background: `${config.color}15`,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: config.color,
            }}
          >
            {config.icon}
          </div>
          <span>{t("services.providersStatus.modal.title", { provider: providerName })}</span>
        </div>
      }
      open={open}
      onCancel={onClose}
      footer={null}
      width={800}
      styles={{
        body: { padding: "16px 0" },
      }}
    >
      <Table
        dataSource={models || []}
        columns={columns}
        rowKey="model_id"
        loading={isLoading}
        pagination={false}
        size="small"
        scroll={{ y: 400 }}
      />
    </Modal>
  );
}

export function ProviderStatusCard() {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const { data: providers, isLoading, refetch } = useProvidersHealth();
  const [selectedProvider, setSelectedProvider] = useState<{
    key: string;
    name: string;
  } | null>(null);

  if (isLoading) {
    return (
      <div style={{ marginBottom: LAYOUT.SECTION_GAP }}>
        <PanelWrapper title={t("services.providersStatus.title")} loading={true}>
          <div style={{ textAlign: "center", padding: "40px 0" }}>
            <Spin />
          </div>
        </PanelWrapper>
      </div>
    );
  }

  if (!providers || Object.keys(providers).length === 0) {
    return null;
  }

  const providerList = Object.entries(providers).sort(([, a], [, b]) => {
    if (a.configured && !b.configured) return -1;
    if (!a.configured && b.configured) return 1;
    return 0;
  });

  const configuredCount = providerList.filter(([, p]) => p.configured).length;
  const totalModels = providerList.reduce((sum, [, p]) => sum + (p.model_count || 0), 0);

  return (
    <div style={{ marginBottom: LAYOUT.SECTION_GAP, padding: `0 ${LAYOUT.GRID_GAP}px` }}>
      <PanelWrapper
        title={t("services.providersStatus.title")}
        onRefresh={refetch}
        extra={
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span
              style={{
                padding: "2px 8px",
                borderRadius: 4,
                background: colors.innerBg,
                color: colors.textSecondary,
                fontSize: 11,
                fontWeight: 500,
              }}
              >
              {t("services.providersStatus.configuredSummary", { configured: configuredCount, total: providerList.length })}
            </span>
            <Tooltip title={t("services.providersStatus.tooltip")}>
              <div
                style={{
                  fontSize: 12,
                  color: colors.textSecondary,
                  display: "flex",
                  alignItems: "center",
                  gap: 4,
                }}
              >
                <InfoCircleOutlined />
                {t("services.providersStatus.modelsTotal", { count: totalModels })}
              </div>
            </Tooltip>
          </div>
        }
      >
        <div style={gridStyles.fiveColumnResponsive}>
          {providerList.map(([key, provider]) => (
            <ProviderCard
              key={key}
              providerKey={key}
              name={provider.name}
              configured={provider.configured}
              modelCount={provider.model_count}
              onClick={() => setSelectedProvider({ key, name: provider.name })}
            />
          ))}
        </div>
      </PanelWrapper>

      <ProviderDetailModal
        open={!!selectedProvider}
        onClose={() => setSelectedProvider(null)}
        providerKey={selectedProvider?.key || ""}
        providerName={selectedProvider?.name || ""}
      />
    </div>
  );
}
