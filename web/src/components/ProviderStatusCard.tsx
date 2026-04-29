// web/src/components/ProviderStatusCard.tsx
// Provider table — 1:1 port of design-handoff dashboard.jsx ProviderTable.
// Uses letter avatars, 10-bar health meter, status pills, mono numerics.

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Modal, Table, Tag } from "antd";
import { useQuery } from "@tanstack/react-query";
import dayjs from "dayjs";
import { useProvidersHealth } from "@/hooks/useServices";
import { useAppStore } from "@/store/useAppStore";
import { api } from "@/lib/api";
import { getUsageBreakdown, type UsageBreakdownItem } from "@/api/usage";
import { useDashboardContext } from "@/pages/dashboard/DashboardContext";
import { FONT_FAMILY, LAYOUT, getColors } from "@/pages/dashboard/styles";

// ── Design-handoff brand avatars — letter tint ────────────────────
const PROVIDER_META: Record<string, { color: string; letter: string }> = {
  openai:     { color: "#10a37f", letter: "O" },
  anthropic:  { color: "#c76a3a", letter: "A" },
  deepseek:   { color: "#6366f1", letter: "D" },
  dashscope:  { color: "#ff6a00", letter: "A" }, // 阿里云
  alibaba:    { color: "#ff6a00", letter: "A" },
  qwen:       { color: "#ff6a00", letter: "Q" },
  doubao:     { color: "#2f7d5a", letter: "D" },
  volcengine: { color: "#2f7d5a", letter: "V" },
  moonshot:   { color: "#3f708e", letter: "M" },
  zhipu:      { color: "#786b92", letter: "Z" },
  siliconflow:{ color: "#14543c", letter: "S" },
  minimax:    { color: "#b7842e", letter: "M" },
  xai:        { color: "#536159", letter: "X" },
  mistral:    { color: "#d64545", letter: "M" },
  cohere:     { color: "#3f708e", letter: "C" },
  google:     { color: "#4285f4", letter: "G" },
  gemini:     { color: "#4285f4", letter: "G" },
  vertex:     { color: "#34a853", letter: "V" },
  "google-vertex": { color: "#34a853", letter: "V" },
};

const EXPECTED_PROVIDER_KEYS = [
  "openai",
  "anthropic",
  "google",
  "deepseek",
  "dashscope",
  "doubao",
  "moonshot",
  "zhipu",
  "siliconflow",
];

const ICON = {
  refresh: (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M12 7a5 5 0 11-1.5-3.5M12 2v2h-2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  mini: (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M2 10l3-4 2.5 2 3-4 1.5 2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  more: (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <circle cx="3" cy="7" r="1.1" fill="currentColor" />
      <circle cx="7" cy="7" r="1.1" fill="currentColor" />
      <circle cx="11" cy="7" r="1.1" fill="currentColor" />
    </svg>
  ),
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

// ── HealthBar: 10 small vertical bars ──────────────────────────────
function HealthBar({ score }: { score: number | null }) {
  const { darkMode } = useAppStore();
  const c = getColors(darkMode);
  if (score === null) return <span style={{ color: c.textFaint }}>—</span>;
  const dots = Math.round(score / 10);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <div style={{ display: "flex", gap: 2.5 }}>
        {Array.from({ length: 10 }).map((_, i) => (
          <span
            key={i}
            style={{
              width: 4,
              height: 12,
              borderRadius: 1,
              background: i < dots ? c.success : c.divider,
            }}
          />
        ))}
      </div>
      <span style={{
        fontSize: 12,
        color: c.textPrimary,
        fontWeight: 500,
        fontFeatureSettings: '"tnum"',
      }}>
        {score.toFixed(0)}%
      </span>
    </div>
  );
}

// ── Status badge ───────────────────────────────────────────────────
function StatusBadge({ on, tOn, tOff }: { on: boolean; tOn: string; tOff: string }) {
  const { darkMode } = useAppStore();
  const c = getColors(darkMode);
  if (on) {
    return (
      <span style={{
        display: "inline-flex", alignItems: "center", gap: 5,
        padding: "3px 9px", borderRadius: 5,
        background: c.successBg, color: c.success,
        fontSize: 11.5, fontWeight: 600,
      }}>
        <span style={{ width: 5, height: 5, borderRadius: "50%", background: c.success }} />
        {tOn}
      </span>
    );
  }
  return (
    <span style={{
      display: "inline-flex", alignItems: "center",
      padding: "3px 9px", borderRadius: 5,
      background: c.cardHover, color: c.textMuted,
      fontSize: 11.5, fontWeight: 500,
    }}>
      {tOff}
    </span>
  );
}

function ReadinessBadge({ state, label }: { state: "ready" | "warning" | "off"; label: string }) {
  const { darkMode } = useAppStore();
  const c = getColors(darkMode);
  const tone = state === "ready"
    ? { fg: c.success, bg: c.successBg }
    : state === "warning"
    ? { fg: c.warning, bg: c.warningBg }
    : { fg: c.textMuted, bg: c.cardHover };

  return (
    <span style={{
      display: "inline-flex",
      alignItems: "center",
      gap: 5,
      padding: "3px 9px",
      borderRadius: 5,
      background: tone.bg,
      color: tone.fg,
      fontSize: 11.5,
      fontWeight: 650,
      whiteSpace: "nowrap",
    }}>
      <span style={{ width: 5, height: 5, borderRadius: "50%", background: tone.fg }} />
      {label}
    </span>
  );
}

// ── Provider avatar — letter in a tinted rounded square ───────────
function ProviderAvatar({ color, letter }: { color: string; letter: string }) {
  return (
    <div style={{
      width: 26, height: 26, borderRadius: 7,
      background: `${color}18`,
      border: `1px solid ${color}38`,
      display: "flex", alignItems: "center", justifyContent: "center",
      color, fontSize: 11, fontWeight: 700, flexShrink: 0,
    }}>
      {letter}
    </div>
  );
}

// ── Detail Modal ───────────────────────────────────────────────────
function ProviderDetailModal({
  open, onClose, providerKey, providerName,
}: {
  open: boolean; onClose: () => void; providerKey: string; providerName: string;
}) {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const c = getColors(darkMode);
  const meta = PROVIDER_META[providerKey.toLowerCase()] || { color: c.accent, letter: providerKey[0]?.toUpperCase() || "?" };

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
          <div style={{ fontWeight: 500, color: c.textPrimary }}>{text}</div>
          <div style={{ fontSize: 11, color: c.textMuted, fontFamily: FONT_FAMILY.mono }}>{record.model_id}</div>
        </div>
      ),
    },
    {
      title: t("services.providersStatus.table.context"),
      dataIndex: "context_window",
      key: "context_window",
      width: 90,
      render: (val: number) => (
        <span style={{ fontFamily: FONT_FAMILY.mono, fontSize: 12 }}>{`${(val / 1000).toFixed(0)}K`}</span>
      ),
    },
    {
      title: t("services.providersStatus.table.capabilities"),
      key: "capabilities",
      width: 130,
      render: (_: unknown, record: Model) => (
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          {record.supports_vision && <Tag style={{ margin: 0, fontSize: 10, background: c.accentBg, color: c.accent, border: "none" }}>{t("services.providersStatus.table.vision")}</Tag>}
          {record.supports_tools && <Tag style={{ margin: 0, fontSize: 10, background: c.successBg, color: c.success, border: "none" }}>{t("services.providersStatus.table.tools")}</Tag>}
        </div>
      ),
    },
    {
      title: t("services.providersStatus.table.price"),
      key: "price",
      width: 150,
      render: (_: unknown, record: Model) => (
        <div style={{ fontSize: 11, fontFamily: FONT_FAMILY.mono, color: c.textSecondary }}>
          <div>in ${record.input_price_per_1k}</div>
          <div>out ${record.output_price_per_1k}</div>
        </div>
      ),
    },
    {
      title: t("services.providersStatus.table.status"),
      dataIndex: "is_enabled",
      key: "is_enabled",
      width: 90,
      render: (enabled: boolean) => (
        <StatusBadge on={enabled}
          tOn={t("services.providersStatus.table.enabled", "已启用")}
          tOff={t("services.providersStatus.table.disabled", "已停用")}
        />
      ),
    },
  ];

  return (
    <Modal
      title={
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <ProviderAvatar color={meta.color} letter={meta.letter} />
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

// ── Public table ───────────────────────────────────────────────────
export function ProviderStatusCard() {
  const { t, i18n } = useTranslation();
  const { darkMode } = useAppStore();
  const c = getColors(darkMode);
  const { data: providers, isLoading, refetch, dataUpdatedAt } = useProvidersHealth();
  const [selected, setSelected] = useState<{ key: string; name: string } | null>(null);

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

  const usageItems = usageByProvider?.items ?? [];
  const unattributedUsage = usageItems.find((item) => {
    const provider = (item.provider || "").toLowerCase();
    return provider.startsWith("unattributed") || provider === "unknown" || provider === "none";
  });

  if (!providers || Object.keys(providers).length === 0) {
    const isZh = i18n.language.startsWith("zh");
    return (
      <section style={{
        background: c.cardBg,
        borderRadius: LAYOUT.CARD_RADIUS,
        border: `1px solid ${c.borderSoft}`,
        padding: "14px 16px",
        fontFamily: FONT_FAMILY.sans,
        overflow: "hidden",
        minHeight: "100%",
      }}>
        <div style={{ display: "flex", alignItems: "center", marginBottom: 12 }}>
          <span style={{ fontSize: 13.5, fontWeight: 650, color: c.textPrimary }}>
            {t("services.providersStatus.title", "厂商状态")}
          </span>
          <div style={{ flex: 1 }} />
          <button
            onClick={() => refetch()}
            aria-label="refresh"
            style={rowBtn(c)}
            onMouseEnter={(e) => { e.currentTarget.style.background = c.cardHover; e.currentTarget.style.borderColor = c.border; }}
            onMouseLeave={(e) => { e.currentTarget.style.background = c.cardBg; e.currentTarget.style.borderColor = c.borderSoft; }}
          >
            {ICON.refresh}
          </button>
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(260px, 1.1fr) minmax(320px, 1.4fr)",
            gap: 12,
          }}
        >
          <div style={{ padding: 14, borderRadius: 8, background: c.innerBg, border: `1px solid ${c.warning}33` }}>
            <div style={{ color: c.warning, fontSize: 12, fontWeight: 700, marginBottom: 6 }}>
              {isZh ? "模型厂商健康接口未返回数据" : "Provider health returned no records"}
            </div>
            <div style={{ color: c.textSecondary, fontSize: 12, lineHeight: 1.6 }}>
              {isZh
                ? "前端已调用 /api/v1/health/providers，但没有拿到任何厂商状态。这里不会伪造模型列表，请先在后端完成厂商与模型配置。"
                : "The frontend called /api/v1/health/providers, but the backend returned no provider records. The UI will not invent model data; configure providers and models in the backend first."}
            </div>
            {unattributedUsage && (
              <div style={{ marginTop: 10, padding: "8px 10px", borderRadius: 6, background: c.warningBg, color: c.warning, fontSize: 12, fontWeight: 650 }}>
                {isZh ? "已检测到未归因请求" : "Unattributed usage detected"} · {unattributedUsage.requests} req · ${Number(unattributedUsage.cost_usd || 0).toFixed(4)}
              </div>
            )}
          </div>

          <div style={{ padding: 14, borderRadius: 8, background: c.innerBg, border: `1px solid ${c.borderSoft}` }}>
            <div style={{ color: c.textPrimary, fontSize: 12, fontWeight: 700, marginBottom: 10 }}>
              {isZh ? "建议接入清单" : "Suggested provider coverage"}
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {EXPECTED_PROVIDER_KEYS.map((key) => {
                const meta = PROVIDER_META[key] || { color: c.textMuted, letter: key[0]?.toUpperCase() || "?" };
                return (
                  <span
                    key={key}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 7,
                      padding: "6px 9px",
                      borderRadius: 7,
                      border: `1px solid ${c.borderSoft}`,
                      background: c.cardBg,
                      color: c.textSecondary,
                      fontSize: 12,
                    }}
                  >
                    <ProviderAvatar color={meta.color} letter={meta.letter} />
                    {key}
                  </span>
                );
              })}
            </div>
          </div>
        </div>
      </section>
    );
  }

  const list = Object.entries(providers).sort(([ka, a], [kb, b]) => {
    if (a.configured && !b.configured) return -1;
    if (!a.configured && b.configured) return 1;
    const ua = usageMap[ka.toLowerCase()]?.requests || 0;
    const ub = usageMap[kb.toLowerCase()]?.requests || 0;
    return ub - ua;
  });

  const totalModels = list.reduce((sum, [, p]) => sum + (p.model_count || 0), 0);
  const configuredProviders = list.filter(([, p]) => p.configured).length;
  const readyProviders = list.filter(([, p]) => p.configured && p.model_count > 0).length;
  const integrationGaps = list.length - readyProviders + (unattributedUsage ? 1 : 0);
  const cols = "2fr 0.9fr 0.9fr 1fr 1.6fr 1.1fr 0.9fr 0.9fr";
  const pad = "13px 18px";

  return (
    <section style={{
      background: c.cardBg,
      borderRadius: LAYOUT.CARD_RADIUS,
      border: `1px solid ${c.borderSoft}`,
      padding: "16px 18px",
      fontFamily: FONT_FAMILY.sans,
      overflow: "hidden",
      height: "100%",
      display: "flex",
      flexDirection: "column",
    }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", marginBottom: 8 }}>
        <span style={{ fontSize: 13.5, fontWeight: 600, color: c.textPrimary }}>
          {t("services.providersStatus.title", "厂商状态")}
        </span>
        <div style={{ flex: 1 }} />
        <div style={{ display: "flex", alignItems: "center", gap: 12, fontSize: 11.5, color: c.textSecondary }}>
          <span style={{ fontFamily: FONT_FAMILY.mono, fontFeatureSettings: '"tnum"' }}>
            {t("dashboard.trend.refreshedAt", i18n.language.startsWith("zh") ? "更新于" : "Updated")}
            {" "}
            {dayjs(dataUpdatedAt || undefined).format("HH:mm:ss")}
          </span>
          <span style={{
            display: "inline-flex", alignItems: "center", gap: 5,
            padding: "3px 9px", borderRadius: 5,
            background: c.successBg, color: c.success,
            fontWeight: 600, fontSize: 11,
          }}>
            <span style={{ width: 5, height: 5, borderRadius: "50%", background: c.success }} />
            {configuredProviders}/{list.length} {i18n.language.startsWith("zh") ? "已配置" : "configured"}
          </span>
          <span style={{
            display: "inline-flex", alignItems: "center", gap: 5,
            padding: "3px 9px", borderRadius: 5,
            background: c.accentBg, color: c.accent,
            fontWeight: 600, fontSize: 11,
          }}>
            <span style={{ width: 5, height: 5, borderRadius: "50%", background: c.accent }} />
            {totalModels} {t("services.providersStatus.modelUnit", i18n.language.startsWith("zh") ? "个模型" : "models")}
          </span>
          {integrationGaps > 0 && (
            <span style={{
              display: "inline-flex", alignItems: "center", gap: 5,
              padding: "3px 9px", borderRadius: 5,
              background: c.warningBg, color: c.warning,
              fontWeight: 650, fontSize: 11,
            }}>
              <span style={{ width: 5, height: 5, borderRadius: "50%", background: c.warning }} />
              {integrationGaps} {i18n.language.startsWith("zh") ? "个接入缺口" : "gaps"}
            </span>
          )}
          <button
            onClick={() => refetch()}
            aria-label="refresh"
            style={{
              width: 30, height: 30, borderRadius: 6,
              border: `1px solid ${c.borderSoft}`,
              background: c.cardBg,
              color: c.textSecondary,
              display: "flex", alignItems: "center", justifyContent: "center",
              cursor: "pointer", transition: "all .12s",
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = c.cardHover;
              e.currentTarget.style.borderColor = c.border;
              e.currentTarget.style.color = c.textPrimary;
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = c.cardBg;
              e.currentTarget.style.borderColor = c.borderSoft;
              e.currentTarget.style.color = c.textSecondary;
            }}
          >
            {ICON.refresh}
          </button>
        </div>
      </div>

      {/* Table grid — negative margin so lines go edge-to-edge */}
      <div style={{ margin: "0 -18px -16px", borderTop: `1px solid ${c.divider}`, flex: 1, overflow: "auto", minHeight: 0 }}>
        {/* Header row */}
        <div style={{
          display: "grid", gridTemplateColumns: cols,
          padding: "10px 18px", borderBottom: `1px solid ${c.divider}`,
          fontSize: 11, color: c.textMuted, fontWeight: 500,
        }}>
          <span>{t("services.providersStatus.col.provider", i18n.language.startsWith("zh") ? "厂商 / 平台" : "Provider")}</span>
          <span>{t("services.providersStatus.col.models", i18n.language.startsWith("zh") ? "模型数量" : "Models")}</span>
          <span>{t("services.providersStatus.col.enabled", i18n.language.startsWith("zh") ? "已启用" : "Enabled")}</span>
          <span>{t("services.providersStatus.col.status", i18n.language.startsWith("zh") ? "状态" : "Status")}</span>
          <span>{t("services.providersStatus.col.health", i18n.language.startsWith("zh") ? "健康度" : "Health")}</span>
          <span>{t("services.providersStatus.col.latency", i18n.language.startsWith("zh") ? "平均延迟 (ms)" : "Avg Latency (ms)")}</span>
          <span>{t("services.providersStatus.col.success", i18n.language.startsWith("zh") ? "成功率" : "Success")}</span>
          <span style={{ textAlign: "right" }}>{t("services.providersStatus.col.actions", i18n.language.startsWith("zh") ? "操作" : "Actions")}</span>
        </div>

        {isLoading ? (
          Array.from({ length: 4 }).map((_, i) => (
            <div key={`sk-${i}`} style={{
              display: "grid", gridTemplateColumns: cols,
              padding: pad, alignItems: "center",
              borderBottom: `1px solid ${c.divider}`,
            }}>
              {Array.from({ length: 8 }).map((_, j) => (
                <div key={j} className="animate-shimmer" style={{ height: 12, width: j === 0 ? 160 : 60, borderRadius: 3 }} />
              ))}
            </div>
          ))
        ) : list.map(([key, p], i) => {
          const meta = PROVIDER_META[key.toLowerCase()] || {
            color: c.textMuted,
            letter: (p.name?.[0] || key[0] || "?").toUpperCase(),
          };
          const usage = usageMap[key.toLowerCase()];
          const hasUsage = !!usage && usage.requests > 0;
          const on = p.configured;
          const ready = p.configured && p.model_count > 0;
          const health = ready ? 100 : p.configured ? 35 : null;
          const isLast = i === list.length - 1;

          return (
            <div
              key={key}
              onClick={on ? () => setSelected({ key, name: p.name }) : undefined}
              className="provider-row"
              style={{
                display: "grid", gridTemplateColumns: cols,
                padding: pad, alignItems: "center",
                borderBottom: isLast ? "none" : `1px solid ${c.divider}`,
                fontSize: 12.5, color: c.textPrimary,
                cursor: on ? "pointer" : "default",
                transition: "background .12s",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <ProviderAvatar color={meta.color} letter={meta.letter} />
                <span style={{ fontWeight: 500 }}>{p.name}</span>
              </div>
              <span style={{
                color: p.model_count > 0 ? c.textPrimary : c.textFaint,
                fontFeatureSettings: '"tnum"',
              }}>
                {p.model_count > 0 ? p.model_count : "—"}
              </span>
              <span style={{
                color: p.model_count > 0 ? c.textPrimary : c.textFaint,
                fontFeatureSettings: '"tnum"',
              }}>
                {p.model_count > 0 ? p.model_count : "—"}
              </span>
              <span>
                <ReadinessBadge
                  state={ready ? "ready" : on ? "warning" : "off"}
                  label={
                    ready
                      ? t("services.providersStatus.table.enabled", "已启用")
                      : on
                      ? (i18n.language.startsWith("zh") ? "无模型" : "No models")
                      : t("services.providersStatus.unconfigured", i18n.language.startsWith("zh") ? "未配置" : "Unconfigured")
                  }
                />
              </span>
              <span><HealthBar score={health} /></span>
              <span style={{
                color: c.textFaint,
                fontFamily: FONT_FAMILY.mono,
                fontSize: 12,
              }}>
                —
              </span>
              <span style={{
                color: hasUsage ? c.textPrimary : c.textFaint,
                fontFeatureSettings: '"tnum"',
              }}>
                {hasUsage ? "100.0%" : "—"}
              </span>
              <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                {on && (
                  <button
                    onClick={(e) => { e.stopPropagation(); setSelected({ key, name: p.name }); }}
                    aria-label="trend"
                    style={rowBtn(c)}
                    onMouseEnter={(e) => { e.currentTarget.style.borderColor = c.border; e.currentTarget.style.color = c.accent; }}
                    onMouseLeave={(e) => { e.currentTarget.style.borderColor = c.borderSoft; e.currentTarget.style.color = c.textSecondary; }}
                  >
                    {ICON.mini}
                  </button>
                )}
                <button
                  onClick={(e) => { e.stopPropagation(); if (on) setSelected({ key, name: p.name }); }}
                  aria-label="more"
                  style={rowBtn(c)}
                  onMouseEnter={(e) => { e.currentTarget.style.borderColor = c.border; e.currentTarget.style.color = c.textPrimary; }}
                  onMouseLeave={(e) => { e.currentTarget.style.borderColor = c.borderSoft; e.currentTarget.style.color = c.textSecondary; }}
                >
                  {ICON.more}
                </button>
              </div>
            </div>
          );
        })}
      </div>

      <style>{`
        .provider-row:hover { background: ${c.innerBg}; }
      `}</style>

      <ProviderDetailModal
        open={!!selected}
        onClose={() => setSelected(null)}
        providerKey={selected?.key || ""}
        providerName={selected?.name || ""}
      />
    </section>
  );
}

function rowBtn(c: ReturnType<typeof getColors>) {
  return {
    width: 28, height: 28, borderRadius: 6,
    border: `1px solid ${c.borderSoft}`,
    background: c.cardBg,
    display: "flex", alignItems: "center", justifyContent: "center",
    color: c.textSecondary, cursor: "pointer",
    transition: "all .12s",
  } as React.CSSProperties;
}
