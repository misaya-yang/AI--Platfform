/**
 * ActivityPanel — right-side Activity drawer (Claude Design · Layout A).
 *
 * Ported from `gateway/project/chat.jsx` (LayoutGPTDrawer). Mounts at
 * the page level, as a sibling to the Artifacts panel. Page owns the
 * mutex via `useRightPanel()`.
 *
 * Visual:
 *   - Slides in from the right, width 380px.
 *   - Header: sparkle/brain icon, "Activity", mono subtitle
 *     "{status} · {N} steps · {T}s", close button.
 *   - Body: vertical rail with absolute-positioned circle markers
 *     (rendered by ActivityTimeline).
 */

import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import type { ChatMessage as ChatMessageType } from "../types";
import { ActivityTimeline } from "./ActivityTimeline";
import { T, ui, ensureActivityStyles } from "./activityTheme";
import { Icon } from "./activityIcons";
import { buildTimeline } from "./buildTimeline";

interface ActivityPanelProps {
  /** Whether the drawer is open. */
  open: boolean;
  /** Close handler (clears rightPanel on the parent). */
  onClose: () => void;
  /** The message whose activity we're showing. */
  message: ChatMessageType | null;
  /** Drawer width in px. 380 matches the design. */
  width?: number;
  onToolApproval?: (
    messageId: string,
    toolId: string,
    approvalId: string,
    approved: boolean,
  ) => void | Promise<void>;
}

function formatTotal(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

export function ActivityPanel({
  open,
  onClose,
  message,
  width = 380,
  onToolApproval,
}: ActivityPanelProps) {
  const { t } = useTranslation();
  ensureActivityStyles();

  const { steps, totalDurationMs } = useMemo(() => {
    if (!message) return { steps: [], totalDurationMs: 0 };
    return buildTimeline(message, t);
  }, [message, t]);

  const cancelled =
    message?.status === "cancelled" || message?.processSummary?.status === "cancelled";
  const failed =
    !cancelled &&
    (message?.status === "failed" || message?.processSummary?.status === "failed");
  const running = !cancelled && !failed && !!message?.isStreaming;
  const stepCount = steps.length;
  const durationLabel = formatTotal(totalDurationMs);
  const pendingApprovals =
    message?.processSummary?.tools.filter(
      (tool) => tool.status === "approval_required" && tool.approvalId,
    ) ?? [];

  const statusWord = running
    ? t("playground.activity.running", { defaultValue: "running" })
    : failed
      ? t("assistant.processSummary.failed", { defaultValue: "Execution failed" })
      : cancelled
        ? t("playground.activity.statusCancelled", { defaultValue: "cancelled" })
        : t("playground.activity.statusCompleted", { defaultValue: "completed" });

  const stepsText = t("playground.activity.steps", {
    count: stepCount,
    defaultValue: "{{count}} steps",
  });

  const subtitle = [statusWord, stepsText, durationLabel]
    .filter(Boolean)
    .join(" · ");

  return (
    <div
      aria-hidden={!open}
      style={{
        width: open ? width : 0,
        flexShrink: 0,
        transition: "width .28s cubic-bezier(.2,.8,.3,1)",
        overflow: "hidden",
        background: T.bg,
        borderLeft: open ? `1px solid ${T.border}` : "none",
        display: "flex",
        flexDirection: "column",
        height: "100%",
        fontFamily: ui.sans,
      }}
    >
      <div
        style={{
          width,
          display: "flex",
          flexDirection: "column",
          height: "100%",
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: "14px 16px 10px",
            display: "flex",
            alignItems: "center",
            gap: 10,
            borderBottom: `1px solid ${T.borderSoft}`,
            flexShrink: 0,
          }}
        >
          {/* Monochrome glyph — restrained, no colored halo at rest. */}
          <span
            style={{
              width: 16,
              height: 16,
              color: T.textMute,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <Icon name={running ? "brain" : "sparkle"} size={14} />
          </span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: T.text }}>
              {t("playground.activity.title", { defaultValue: "Activity" })}
            </div>
            <div
              style={{
                fontSize: 11,
                color: T.textMute,
                fontFamily: ui.mono,
                marginTop: 1,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {subtitle}
            </div>
          </div>
          <button
            type="button"
            className="act-btn act-hover"
            onClick={onClose}
            aria-label={t("common.close", { defaultValue: "Close" })}
            style={{
              width: 26,
              height: 26,
              borderRadius: 6,
              border: "none",
              background: "transparent",
              color: T.textMute,
              cursor: "pointer",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <Icon name="x" size={15} />
          </button>
        </div>

        {/* Body */}
        <div
          className="act-scroll"
          style={{
            flex: 1,
            overflowY: "auto",
            padding: "16px 20px 60px",
          }}
        >
          {message && steps.length > 0 ? (
            <ActivityTimeline steps={steps} running={running} />
          ) : (
            <div
              style={{
                fontSize: 13,
                color: T.textMute,
                padding: "8px 0",
              }}
            >
              {t("playground.activity.empty", {
                defaultValue: "No activity recorded.",
              })}
            </div>
          )}

          {message && pendingApprovals.length > 0 && onToolApproval && (
            <div
              style={{
                marginTop: 16,
                paddingTop: 12,
                borderTop: `1px solid ${T.borderSoft}`,
                display: "flex",
                flexDirection: "column",
                gap: 10,
              }}
            >
              {pendingApprovals.map((tool) => (
                <div
                  key={tool.id}
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: 8,
                    fontSize: 12,
                    color: T.text,
                  }}
                >
                  <div>
                    {t("assistant.activity.approvalRequired", {
                      defaultValue: "Approval required",
                    })}
                    {": "}
                    <span style={{ fontFamily: ui.mono }}>{tool.name}</span>
                  </div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <button
                      type="button"
                      className="act-btn act-hover"
                      onClick={() =>
                        void onToolApproval(
                          message.id,
                          tool.id,
                          tool.approvalId as string,
                          true,
                        )
                      }
                      style={{
                        padding: "6px 10px",
                        borderRadius: 6,
                        border: `1px solid ${T.border}`,
                        background: T.bg,
                        cursor: "pointer",
                      }}
                    >
                      {t("assistant.activity.approve", { defaultValue: "Approve" })}
                    </button>
                    <button
                      type="button"
                      className="act-btn act-hover"
                      onClick={() =>
                        void onToolApproval(
                          message.id,
                          tool.id,
                          tool.approvalId as string,
                          false,
                        )
                      }
                      style={{
                        padding: "6px 10px",
                        borderRadius: 6,
                        border: `1px solid ${T.border}`,
                        background: T.bg,
                        cursor: "pointer",
                      }}
                    >
                      {t("assistant.activity.reject", { defaultValue: "Reject" })}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
