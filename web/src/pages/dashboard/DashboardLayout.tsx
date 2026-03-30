import { useMemo, useState, type ComponentType } from "react";
import { Segmented, Tooltip } from "antd";
import {
  DashboardOutlined,
  FundProjectionScreenOutlined,
  SafetyCertificateOutlined,
  ApartmentOutlined,
} from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import { useAppStore } from "@/store/useAppStore";
import { getColors, LAYOUT, TYPOGRAPHY } from "./styles";
import type { PanelType } from "./types";
import {
  ServiceHealthPanel,
  PerformancePanel,
  TokenUsagePanel,
  CostAnalysisPanel,
  UserQuotaPanel,
  SecurityEventsPanel,
  RequestTracePanel,
  FailureAnalysisPanel,
} from "./components/panels";

type WorkspaceKey = "overview" | "reliability" | "governance" | "tracing";

interface PanelSlot {
  type: PanelType;
  minHeight: number;
  span: 1 | 2;
}

interface WorkspaceConfig {
  title: string;
  subtitle: string;
  panels: PanelSlot[];
}

const WORKSPACE_STORAGE_KEY = "dashboard-workspace-v2";

const PANEL_COMPONENTS: Record<PanelType, ComponentType> = {
  "service-health": ServiceHealthPanel,
  performance: PerformancePanel,
  "token-usage": TokenUsagePanel,
  "cost-analysis": CostAnalysisPanel,
  "user-quota": UserQuotaPanel,
  "security-events": SecurityEventsPanel,
  "request-trace": RequestTracePanel,
  "failure-analysis": FailureAnalysisPanel,
};

function loadWorkspace(): WorkspaceKey {
  try {
    const saved = localStorage.getItem(WORKSPACE_STORAGE_KEY);
    if (
      saved === "overview" ||
      saved === "reliability" ||
      saved === "governance" ||
      saved === "tracing"
    ) {
      return saved;
    }
  } catch {
    // ignored
  }
  return "overview";
}

function saveWorkspace(workspace: WorkspaceKey) {
  try {
    localStorage.setItem(WORKSPACE_STORAGE_KEY, workspace);
  } catch {
    // ignored
  }
}

interface DashboardLayoutProps {
  width?: number;
}

export function DashboardLayout({ width = 1200 }: DashboardLayoutProps) {
  const { t } = useTranslation();
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const [workspace, setWorkspace] = useState<WorkspaceKey>(() => loadWorkspace());

  const workspaceConfigs = useMemo<Record<WorkspaceKey, WorkspaceConfig>>(
    () => ({
      overview: {
        title: t("dashboard.workspace.overview.title", "运营总览"),
        subtitle: t(
          "dashboard.workspace.overview.subtitle",
          "关注服务稳定性、请求吞吐与核心成本走势"
        ),
        panels: [
          { type: "service-health", span: 2, minHeight: 380 },
          { type: "performance", span: 1, minHeight: 460 },
          { type: "token-usage", span: 1, minHeight: 460 },
          { type: "cost-analysis", span: 2, minHeight: 520 },
        ],
      },
      reliability: {
        title: t("dashboard.workspace.reliability.title", "故障与可靠性"),
        subtitle: t(
          "dashboard.workspace.reliability.subtitle",
          "按错误类型拆分成功率，快速定位最可能的故障根因"
        ),
        panels: [
          { type: "failure-analysis", span: 1, minHeight: 500 },
          { type: "security-events", span: 1, minHeight: 500 },
          { type: "request-trace", span: 2, minHeight: 540 },
        ],
      },
      governance: {
        title: t("dashboard.workspace.governance.title", "成本与配额治理"),
        subtitle: t(
          "dashboard.workspace.governance.subtitle",
          "聚焦预算控制、用户配额和超额策略执行状态"
        ),
        panels: [
          { type: "cost-analysis", span: 1, minHeight: 520 },
          { type: "user-quota", span: 1, minHeight: 520 },
          { type: "token-usage", span: 2, minHeight: 460 },
        ],
      },
      tracing: {
        title: t("dashboard.workspace.tracing.title", "请求追踪"),
        subtitle: t(
          "dashboard.workspace.tracing.subtitle",
          "按慢请求/失败请求抽样，查看阶段耗时和故障落点"
        ),
        panels: [
          { type: "request-trace", span: 2, minHeight: 620 },
          { type: "failure-analysis", span: 1, minHeight: 500 },
          { type: "performance", span: 1, minHeight: 500 },
        ],
      },
    }),
    [t]
  );

  const activeConfig = workspaceConfigs[workspace];
  const useSingleColumn = width < 1100;

  return (
    <div style={{ minHeight: "100%" }}>
      <div
        style={{
          borderRadius: LAYOUT.CARD_RADIUS,
          border: `1px solid ${colors.border}`,
          background: darkMode
            ? "linear-gradient(120deg, rgba(15,23,42,0.98), rgba(30,41,59,0.9))"
            : "linear-gradient(120deg, rgba(255,255,255,0.98), rgba(248,250,252,0.92))",
          boxShadow: colors.shadowSm,
          padding: "16px 18px",
          marginBottom: LAYOUT.GRID_GAP,
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 16,
            flexWrap: "wrap",
          }}
        >
          <div style={{ minWidth: 280 }}>
            <div
              style={{
                ...TYPOGRAPHY.sectionTitle,
                color: colors.textPrimary,
                letterSpacing: "-0.02em",
              }}
            >
              {activeConfig.title}
            </div>
            <div
              style={{
                fontSize: 12,
                color: colors.textSecondary,
                marginTop: 4,
              }}
            >
              {activeConfig.subtitle}
            </div>
          </div>

          <Segmented
            size="middle"
            value={workspace}
            onChange={(value) => {
              const next = value as WorkspaceKey;
              setWorkspace(next);
              saveWorkspace(next);
            }}
            options={[
              {
                value: "overview",
                label: (
                  <Tooltip title={t("dashboard.workspace.overview.title", "运营总览")}>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      <DashboardOutlined />
                      {t("dashboard.workspace.overview.short", "总览")}
                    </span>
                  </Tooltip>
                ),
              },
              {
                value: "reliability",
                label: (
                  <Tooltip title={t("dashboard.workspace.reliability.title", "故障与可靠性")}>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      <FundProjectionScreenOutlined />
                      {t("dashboard.workspace.reliability.short", "可靠性")}
                    </span>
                  </Tooltip>
                ),
              },
              {
                value: "governance",
                label: (
                  <Tooltip title={t("dashboard.workspace.governance.title", "成本与配额治理")}>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      <SafetyCertificateOutlined />
                      {t("dashboard.workspace.governance.short", "治理")}
                    </span>
                  </Tooltip>
                ),
              },
              {
                value: "tracing",
                label: (
                  <Tooltip title={t("dashboard.workspace.tracing.title", "请求追踪")}>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      <ApartmentOutlined />
                      {t("dashboard.workspace.tracing.short", "追踪")}
                    </span>
                  </Tooltip>
                ),
              },
            ]}
          />
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: useSingleColumn
            ? "minmax(0, 1fr)"
            : "repeat(2, minmax(0, 1fr))",
          gap: useSingleColumn ? 12 : LAYOUT.GRID_GAP,
          alignItems: "stretch",
        }}
      >
        {activeConfig.panels.map((slot) => {
          const PanelComponent = PANEL_COMPONENTS[slot.type];
          return (
            <div
              key={`${workspace}-${slot.type}`}
              style={{
                minHeight: slot.minHeight,
                gridColumn:
                  useSingleColumn || slot.span === 1 ? "auto" : "1 / -1",
              }}
            >
              <PanelComponent />
            </div>
          );
        })}
      </div>
    </div>
  );
}
