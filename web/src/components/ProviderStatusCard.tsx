// web/src/components/ProviderStatusCard.tsx
// Provider Status — hairline editorial table
// Joins /health/providers with usage breakdown to show requests & cost.

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Modal, Table, Tag } from "antd";
import { MoreHorizontal } from "lucide-react";
import {
  CloudOutlined,
  RobotOutlined,
  ThunderboltOutlined,
  GlobalOutlined,
  ExperimentOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import dayjs from "dayjs";
import { useProvidersHealth } from "@/hooks/useServices";
import { useAppStore } from "@/store/useAppStore";
import { api } from "@/lib/api";
import { getUsageBreakdown, type UsageBreakdownItem } from "@/api/usage";
import { useDashboardContext } from "@/pages/dashboard/DashboardContext";
import { LAYOUT, TYPOGRAPHY, getColors, TRANSITION } from "@/pages/dashboard/styles";

// ── Brand config ───────────────────────────────────────────────────
const PROVIDER_CONFIG: Record<string, { icon: React.ReactNode; color: string }> = {
  openai:     { icon: <RobotOutlined />,       color: "#10a37f" },
  anthropic:  { icon: <ExperimentOutlined />,  color: "#d97706" },
  deepseek:   { icon: <ThunderboltOutlined />, color: "#6366f1" },
  dashscope:  { icon: <CloudOutlined />,       color: "#ff6a00" },
  google:     { icon: <GlobalOutlined />,      color: "#4285f4" },
};

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

// ── Health meter — 6 dots, live-data indicator ─────────────────────
function HealthMeter({ level, active }: { level: number; active: boolean }) {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const filled = Math.max(0, Math.min(6, level));
  return (
    <div style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
      {Array.from({ length: 6 }).map((_, i) => {
        const on = i < filled;
        return (
          <span
            key={i}
            style={{
              width: 5, height: 5, borderRadius: "50%",
              background: on ? colors.success : colors.border,
              opacity: on ? 1 : 1,
              transition: TRANSITION.fast,
              ...(active && i === filled - 1 ? {
                animation: "provider-pulse 2s ease-in-out infinite",
              } : {}),
            }}
          />
        );
      })}
      <style>{`
        @keyframes provider-pulse {
          0%, 100% { transform: scale(1); opacity: 1; }
          50% { transform: scale(1.4); opacity: 0.6; }
        }
      `}</style>
    </div>
  );
}

// ── Detail modal ───────────────────────────────────────────────────
function ProviderDetailModal({
  open, onClose, providerKey, providerName,
}: {
  open: boolean; onClose: () => void; providerKey: string; providerName: string;
}) {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const config = PROVIDER_CONFIG[providerKey] || { icon: <CloudOutlined />, color: colors.accent };

  const { data: models, isLoading } = useQuery({
    queryKey: ["models", providerKey],
    queryFn: async () => {
      const { data } = await api.get<Model[]>("/api/v1/models", { params: { provider_id: providerKey } });
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
          <div style={{ fontWeight: 500, color: colors.textPrimary }}>{text}</div>
          <div style={{ fontSize: 11, color: colors.textMuted, fontFamily: '"IBM Plex Mono", monospace' }}>{record.model_id}</div>
        </div>
      ),
    },
    {
      title: t("services.providersStatus.table.context"),
      dataIndex: "context_window",
      key: "context_window",
      width: 90,
      render: (val: number) => (
        <span style={{ fontFamily: '"IBM Plex Mono", monospace', fontSize: 12 }}>
          {`${(val / 1000).toFixed(0)}K`}
        </span>
      ),
    },
    {
      title: t("services.providersStatus.table.capabilities"),
      key: "capabilities",
      width: 130,
      render: (_: unknown, record: Model) => (
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          {record.supports_vision && <Tag style={{ margin: 0, fontSize: 10, background: colors.accentBg, color: colors.accent, border: "none" }}>{t("services.providersStatus.table.vision")}</Tag>}
          {record.supports_tools && <Tag style={{ margin: 0, fontSize: 10, background: colors.successBg, color: colors.success, border: "none" }}>{t("services.providersStatus.table.tools")}</Tag>}
        </div>
      ),
    },
    {
      title: t("services.providersStatus.table.price"),
      key: "price",
      width: 150,
      render: (_: unknown, record: Model) => (
        <div style={{ fontSize: 11, fontFamily: '"IBM Plex Mono", monospace', color: colors.textSecondary }}>
          <div>in ${record.input_price_per_1k}</div>
          <div>out ${record.output_price_per_1k}</div>
        </div>
      ),
    },
    {
      title: t("services.providersStatus.table.status"),
      dataIndex: "is_enabled",
      key: "is_enabled",
      width: 80,
      render: (enabled: boolean) => (
        <span style={{
          fontSize: 11, fontWeight: 500,
          color: enabled ? colors.success : colors.textMuted,
          display: "inline-flex", alignItems: "center", gap: 6,
        }}>
          <span style={{
            width: 5, height: 5, borderRadius: "50%",
            background: enabled ? colors.success : colors.textMuted,
          }} />
          {enabled ? t("services.providersStatus.table.enabled") : t("services.providersStatus.table.disabled")}
        </span>
      ),
    },
  ];

  return (
    <Modal
      title={
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{
            width: 28, height: 28, borderRadius: 6,
            background: `${config.color}15`, color: config.color,
            display: "flex", alignItems: "center", justifyContent: "center",
          }}>
            {config.icon}
          </div>
          <span style={{ fontWeight: 600, letterSpacing: "-0.01em" }}>
            {t("services.providersStatus.modal.title", { provider: providerName })}
          </span>
        </div>
      }
      open={open}
      onCancel={onClose}
      footer={null}
      width={820}
      styles={{ body: { padding: "16px 0" } }}
    >
      <Table
        dataSource={models || []}
        columns={columns}
        rowKey="model_id"
        loading={isLoading}
        pagination={false}
        size="small"
        scroll={{ y: 420 }}
      />
    </Modal>
  );
}

// ── Public: ProviderStatusCard (table) ──────────────────────────────
export function ProviderStatusCard() {
  const { t, i18n } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const { data: providers, isLoading, refetch } = useProvidersHealth();
  const [selectedProvider, setSelectedProvider] = useState<{ key: string; name: string } | null>(null);

  // Join with usage breakdown to show per-provider activity
  const { dateRange, serviceId, userId, lastRefresh } = useDashboardContext();
  const { data: usageByProvider } = useQuery({
    queryKey: ["provider-usage", dateRange, serviceId, userId, lastRefresh.getTime()],
    queryFn: () => getUsageBreakdown({
      start_date: dateRange[0],
      end_date: dateRange[1],
      dimension: "provider",
      service_id: serviceId !== "all" ? serviceId : undefined,
      user_id: userId !== "all" ? userId : undefined,
    }),
    staleTime: 30000,
  });
  const usageMap = useMemo(() => {
    const m: Record<string, UsageBreakdownItem> = {};
    (usageByProvider?.items ?? []).forEach((it) => {
      const key = (it.provider || "").toLowerCase();
      if (key) m[key] = it;
    });
    return m;
  }, [usageByProvider]);

  if (!providers || Object.keys(providers).length === 0) {
    return null;
  }

  const providerList = Object.entries(providers).sort(([ka, a], [kb, b]) => {
    if (a.configured && !b.configured) return -1;
    if (!a.configured && b.configured) return 1;
    const ua = usageMap[ka.toLowerCase()]?.requests || 0;
    const ub = usageMap[kb.toLowerCase()]?.requests || 0;
    return ub - ua;
  });

  const configuredCount = providerList.filter(([, p]) => p.configured).length;
  const totalModels = providerList.reduce((sum, [, p]) => sum + (p.model_count || 0), 0);

  const COL_PADDING_Y = 14;
  const COL_PADDING_X = 14;

  const fmtNum = (n?: number) => (typeof n === "number" && n > 0 ? n.toLocaleString() : "—");
  const fmtPct = (n?: number) => (typeof n === "number" && n > 0 ? `${n.toFixed(1)}%` : "—");
  const fmtCost = (n?: number) => {
    if (!n || n <= 0) return "—";
    return n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`;
  };

  return (
    <section
      style={{
        background: colors.cardBg,
        border: `1px solid ${colors.border}`,
        borderRadius: LAYOUT.CARD_RADIUS,
        boxShadow: colors.shadowSm,
        overflow: "hidden",
      }}
    >
      {/* Section header */}
      <div style={{
        display: "flex", alignItems: "flex-end", justifyContent: "space-between",
        gap: 16, padding: "16px 18px 14px",
        borderBottom: `1px solid ${colors.border}`,
      }}>
        <div>
          <div style={{
            ...TYPOGRAPHY.eyebrow,
            color: colors.textMuted,
            marginBottom: 4,
          }}>
            Providers
          </div>
          <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
            <h3 style={{
              ...TYPOGRAPHY.sectionTitle,
              color: colors.textPrimary, margin: 0,
            }}>
              {t("services.providersStatus.title")}
            </h3>
            <span style={{
              fontSize: 11, color: colors.textMuted, fontWeight: 500,
              letterSpacing: "0.04em",
            }}>
              {t("services.providersStatus.configuredSummary", { configured: configuredCount, total: providerList.length })}
              {"  ·  "}
              {t("services.providersStatus.modelsTotal", { count: totalModels })}
            </span>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{
            fontSize: 11, color: colors.textMuted, fontVariantNumeric: "tabular-nums",
            fontFamily: '"IBM Plex Mono", monospace',
          }}>
            {t("dashboard.trend.refreshedAt", i18n.language.startsWith("zh") ? "更新于" : "Updated")}
            {" "}{dayjs().format("HH:mm:ss")}
          </span>
          <button
            onClick={() => refetch()}
            className="dash-icon-btn"
            aria-label="refresh providers"
            style={{
              display: "flex", alignItems: "center", justifyContent: "center",
              width: 28, height: 28, borderRadius: 6,
              border: `1px solid ${colors.border}`,
              background: colors.cardBg,
              color: colors.textSecondary,
              cursor: "pointer",
              transition: TRANSITION.fast,
            }}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
              <path d="M3 3v5h5" />
              <path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16" />
              <path d="M16 16h5v5" />
            </svg>
          </button>
        </div>
      </div>

      {/* Table */}
      <div style={{ overflowX: "auto" }}>
        <table
          style={{
            width: "100%", borderCollapse: "collapse",
            fontSize: 13,
          }}
        >
          <thead>
            <tr>
              {[
                { label: t("services.providersStatus.table.provider", i18n.language.startsWith("zh") ? "厂商 / 平台" : "Provider"), align: "left", width: undefined },
                { label: t("services.providersStatus.table.modelCount", i18n.language.startsWith("zh") ? "模型数量" : "Models"), align: "right", width: 100 },
                { label: t("services.providersStatus.table.status", i18n.language.startsWith("zh") ? "状态" : "Status"), align: "left", width: 120 },
                { label: t("services.providersStatus.table.health", i18n.language.startsWith("zh") ? "健康度" : "Health"), align: "left", width: 130 },
                { label: t("services.providersStatus.table.requests", i18n.language.startsWith("zh") ? "请求量" : "Requests"), align: "right", width: 110 },
                { label: t("services.providersStatus.table.share", i18n.language.startsWith("zh") ? "成本占比" : "Cost share"), align: "right", width: 130 },
                { label: t("services.providersStatus.table.actions", i18n.language.startsWith("zh") ? "操作" : ""), align: "right", width: 70 },
              ].map((h) => (
                <th key={h.label} style={{
                  ...TYPOGRAPHY.eyebrow,
                  color: colors.textMuted,
                  textAlign: h.align as "left" | "right",
                  padding: `10px ${COL_PADDING_X}px`,
                  fontWeight: 600,
                  width: h.width,
                  background: colors.innerBg,
                  borderBottom: `1px solid ${colors.border}`,
                }}>
                  {h.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              Array.from({ length: 4 }).map((_, i) => (
                <tr key={`sk-${i}`} style={{ borderBottom: `1px solid ${colors.border}` }}>
                  {Array.from({ length: 7 }).map((_, j) => (
                    <td key={j} style={{ padding: `${COL_PADDING_Y}px ${COL_PADDING_X}px` }}>
                      <div className="animate-shimmer" style={{ height: 12, width: j === 0 ? 160 : 60, borderRadius: 3 }} />
                    </td>
                  ))}
                </tr>
              ))
            ) : providerList.map(([key, provider], idx) => {
              const config = PROVIDER_CONFIG[key.toLowerCase()] || { icon: <CloudOutlined />, color: colors.textMuted };
              const usage = usageMap[key.toLowerCase()];
              const hasUsage = !!usage && usage.requests > 0;
              const healthLevel = !provider.configured ? 0 : hasUsage ? 6 : 4;
              const isLast = idx === providerList.length - 1;

              return (
                <tr
                  key={key}
                  onClick={provider.configured ? () => setSelectedProvider({ key, name: provider.name }) : undefined}
                  className="provider-row"
                  style={{
                    cursor: provider.configured ? "pointer" : "default",
                    transition: TRANSITION.fast,
                    borderBottom: isLast ? "none" : `1px solid ${colors.border}`,
                  }}
                >
                  {/* Provider */}
                  <td style={{ padding: `${COL_PADDING_Y}px ${COL_PADDING_X}px` }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                      <div style={{
                        width: 30, height: 30, borderRadius: 7,
                        background: provider.configured ? `${config.color}14` : colors.innerBg,
                        color: provider.configured ? config.color : colors.textMuted,
                        display: "flex", alignItems: "center", justifyContent: "center",
                        fontSize: 14, flexShrink: 0,
                      }}>
                        {config.icon}
                      </div>
                      <div style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
                        <span style={{
                          fontSize: 13, fontWeight: provider.configured ? 600 : 500,
                          color: provider.configured ? colors.textPrimary : colors.textSecondary,
                          letterSpacing: "-0.005em",
                        }}>
                          {provider.name}
                        </span>
                        <span style={{
                          fontSize: 10, color: colors.textMuted,
                          letterSpacing: "0.06em", textTransform: "uppercase",
                          fontFamily: '"IBM Plex Mono", monospace',
                          marginTop: 1,
                        }}>
                          {key}
                        </span>
                      </div>
                    </div>
                  </td>

                  {/* Model count */}
                  <td style={{
                    padding: `${COL_PADDING_Y}px ${COL_PADDING_X}px`,
                    textAlign: "right",
                    fontVariantNumeric: "tabular-nums",
                    fontFamily: '"IBM Plex Mono", monospace',
                    fontSize: 13,
                    color: provider.model_count > 0 ? colors.textPrimary : colors.textMuted,
                  }}>
                    {provider.model_count > 0 ? provider.model_count : "—"}
                  </td>

                  {/* Status pill */}
                  <td style={{ padding: `${COL_PADDING_Y}px ${COL_PADDING_X}px` }}>
                    <span style={{
                      display: "inline-flex", alignItems: "center", gap: 6,
                      padding: "3px 10px", borderRadius: 999,
                      fontSize: 11, fontWeight: 500,
                      background: provider.configured ? colors.successBg : colors.innerBg,
                      color: provider.configured ? colors.success : colors.textMuted,
                      letterSpacing: "0.01em",
                    }}>
                      <span style={{
                        width: 5, height: 5, borderRadius: "50%",
                        background: provider.configured ? colors.success : colors.textMuted,
                      }} />
                      {provider.configured
                        ? t("services.providersStatus.table.enabled", i18n.language.startsWith("zh") ? "已启用" : "Enabled")
                        : t("services.providersStatus.unconfigured", i18n.language.startsWith("zh") ? "未配置" : "Unconfigured")}
                    </span>
                  </td>

                  {/* Health */}
                  <td style={{ padding: `${COL_PADDING_Y}px ${COL_PADDING_X}px` }}>
                    {provider.configured ? (
                      <div style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                        <HealthMeter level={healthLevel} active={hasUsage} />
                        <span style={{
                          fontSize: 10, color: colors.textMuted,
                          fontVariantNumeric: "tabular-nums",
                        }}>
                          {hasUsage ? "100%" : "—"}
                        </span>
                      </div>
                    ) : (
                      <span style={{ fontSize: 12, color: colors.textMuted }}>—</span>
                    )}
                  </td>

                  {/* Requests */}
                  <td style={{
                    padding: `${COL_PADDING_Y}px ${COL_PADDING_X}px`,
                    textAlign: "right",
                    fontVariantNumeric: "tabular-nums",
                    fontFamily: '"IBM Plex Mono", monospace',
                    fontSize: 13,
                    color: hasUsage ? colors.textPrimary : colors.textMuted,
                  }}>
                    {fmtNum(usage?.requests)}
                  </td>

                  {/* Cost share */}
                  <td style={{
                    padding: `${COL_PADDING_Y}px ${COL_PADDING_X}px`,
                    textAlign: "right",
                  }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 10 }}>
                      {hasUsage && (
                        <div style={{
                          width: 50, height: 4,
                          borderRadius: 2,
                          background: colors.innerBg,
                          overflow: "hidden",
                          flexShrink: 0,
                        }}>
                          <div style={{
                            width: `${Math.min(100, usage!.percentage)}%`,
                            height: "100%",
                            background: colors.accent,
                            borderRadius: 2,
                            transition: TRANSITION.normal,
                          }} />
                        </div>
                      )}
                      <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end" }}>
                        <span style={{
                          fontSize: 12, fontWeight: 500,
                          fontVariantNumeric: "tabular-nums",
                          fontFamily: '"IBM Plex Mono", monospace',
                          color: hasUsage ? colors.textPrimary : colors.textMuted,
                        }}>
                          {fmtCost(usage?.cost_usd)}
                        </span>
                        <span style={{
                          fontSize: 10,
                          fontVariantNumeric: "tabular-nums",
                          color: colors.textMuted,
                        }}>
                          {fmtPct(usage?.percentage)}
                        </span>
                      </div>
                    </div>
                  </td>

                  {/* Actions */}
                  <td style={{
                    padding: `${COL_PADDING_Y}px ${COL_PADDING_X}px`,
                    textAlign: "right",
                  }}>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        if (provider.configured) setSelectedProvider({ key, name: provider.name });
                      }}
                      disabled={!provider.configured}
                      aria-label="open provider"
                      style={{
                        width: 28, height: 28, borderRadius: 6,
                        border: `1px solid ${colors.border}`,
                        background: "transparent",
                        color: provider.configured ? colors.textSecondary : colors.textMuted,
                        cursor: provider.configured ? "pointer" : "not-allowed",
                        display: "inline-flex", alignItems: "center", justifyContent: "center",
                        transition: TRANSITION.fast,
                      }}
                      onMouseEnter={(e) => {
                        if (provider.configured) {
                          e.currentTarget.style.background = colors.cardHover;
                          e.currentTarget.style.color = colors.accent;
                          e.currentTarget.style.borderColor = colors.accent;
                        }
                      }}
                      onMouseLeave={(e) => {
                        e.currentTarget.style.background = "transparent";
                        e.currentTarget.style.color = provider.configured ? colors.textSecondary : colors.textMuted;
                        e.currentTarget.style.borderColor = colors.border;
                      }}
                    >
                      <MoreHorizontal size={14} />
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>

        <style>{`
          .provider-row:hover { background: ${colors.cardHover}; }
        `}</style>
      </div>

      <ProviderDetailModal
        open={!!selectedProvider}
        onClose={() => setSelectedProvider(null)}
        providerKey={selectedProvider?.key || ""}
        providerName={selectedProvider?.name || ""}
      />
    </section>
  );
}
