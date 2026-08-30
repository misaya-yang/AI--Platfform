/**
 * ActivityTimeline — the vertical rail + circle markers container.
 *
 * Ported from chat.jsx `ActivityBody`: a continuous 1px rail on the
 * left, with absolute-positioned 11px circle markers at each step.
 * Each row derives its state from its own lifecycle. This keeps tool markers
 * correct when the separately-streaming reasoning row is rendered last.
 *
 * Step rendering delegates to `TimelineStep`.
 */

import { useTranslation } from "react-i18next";
import type { ChatMessage as ChatMessageType } from "../types";
import { TimelineStep, type TimelineStepData } from "./TimelineStep";
import { T } from "./activityTheme";
import { buildTimeline } from "./buildTimeline";

interface ActivityTimelineProps {
  /** Pre-built steps. Prefer this in the drawer (parent already builds). */
  steps?: TimelineStepData[];
  /** Fallback: build from a ChatMessage. Used by share-page / legacy. */
  message?: ChatMessageType;
  /** Whether the last step is actively running. */
  running?: boolean;
  className?: string;
}

export function ActivityTimeline({
  steps: stepsProp,
  message,
  running,
  className,
}: ActivityTimelineProps) {
  const { t } = useTranslation();

  const steps: TimelineStepData[] =
    stepsProp ?? (message ? buildTimeline(message, t).steps : []);

  if (steps.length === 0) {
    return (
      <div className={className}>
        <p style={{ fontSize: 13, color: T.textMute }}>
          {t("playground.activity.empty", {
            defaultValue: "No activity recorded.",
          })}
        </p>
      </div>
    );
  }

  return (
    <div
      className={className}
      style={{ position: "relative", paddingLeft: 18 }}
    >
      {/* Continuous vertical rail */}
      <div
        style={{
          position: "absolute",
          left: 5,
          top: 6,
          bottom: 20,
          width: 1,
          background: T.border,
        }}
      />
      {steps.map((step) => {
        const stepStatus =
          step.kind === "tool"
            ? step.status
            : step.streaming
              ? "running"
              : "completed";
        const isRunning = !!running && stepStatus === "running";
        const isCompleted =
          stepStatus === "completed" ||
          (stepStatus === "running" && !isRunning);
        const isError = stepStatus === "error";

        return (
          <TimelineStep
            key={step.id}
            step={step}
            isRunning={isRunning}
            isCompleted={isCompleted}
            isError={isError}
          />
        );
      })}
    </div>
  );
}
