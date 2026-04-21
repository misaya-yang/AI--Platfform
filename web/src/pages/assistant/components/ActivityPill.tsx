/**
 * ActivityPill — inline affordance that opens the right-side Activity
 * drawer. Ported verbatim from chat.jsx.
 *
 * Two variants:
 *   - "pill"  (default) — bordered rounded rect used inline above the
 *                         assistant's answer.
 *   - "chip"            — compact variant used in the top bar when the
 *                         panel is collapsed.
 *
 * While `running`, shows animated three-dot + "Thinking" label; when
 * idle, shows the sparkle icon + "Activity" label. Subtitle is
 * "· N steps · Ts" in monospace.
 */

import { T, ui, ensureActivityStyles } from "./activityTheme";
import { Icon } from "./activityIcons";

interface ActivityPillProps {
  steps: number;
  durationLabel: string;
  running: boolean;
  onOpen: () => void;
  variant?: "pill" | "chip";
  label: string; // localized "Thinking" | "Activity"
}

export function ActivityPill({
  steps,
  durationLabel,
  running,
  onOpen,
  variant = "pill",
  label,
}: ActivityPillProps) {
  ensureActivityStyles();

  const subtitle =
    durationLabel && steps > 0
      ? `· ${steps} steps · ${durationLabel}`
      : steps > 0
        ? `· ${steps} steps`
        : durationLabel
          ? `· ${durationLabel}`
          : "";

  if (variant === "chip") {
    return (
      <button
        type="button"
        onClick={onOpen}
        className="act-btn act-accent-hover"
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 8,
          padding: "5px 10px 5px 8px",
          borderRadius: 14,
          border: `1px solid ${T.border}`,
          background: T.panel,
          color: T.text,
          fontFamily: ui.sans,
          fontSize: 12.5,
          cursor: "pointer",
        }}
      >
        {running ? (
          <span style={{ display: "inline-flex", color: T.accent }}>
            <span className="act-dot" />
            <span className="act-dot" />
            <span className="act-dot" />
          </span>
        ) : (
          <span style={{ color: T.accent, display: "inline-flex" }}>
            <Icon name="sparkle" size={13} />
          </span>
        )}
        <span style={{ fontWeight: 500 }}>{label}</span>
        {subtitle && (
          <span
            style={{
              color: T.textMute,
              fontFamily: ui.mono,
              fontSize: 11.5,
            }}
          >
            {subtitle}
          </span>
        )}
        <Icon name="chevRight" size={12} />
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={onOpen}
      className="act-btn act-hover"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 10,
        padding: "7px 12px 7px 10px",
        borderRadius: 10,
        border: `1px solid ${T.border}`,
        background: T.panel,
        color: T.text,
        fontFamily: ui.sans,
        fontSize: 13,
        cursor: "pointer",
      }}
    >
      <span
        style={{
          width: 22,
          height: 22,
          borderRadius: 6,
          background: T.accentSoft,
          color: T.accent,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          flexShrink: 0,
        }}
      >
        <Icon name={running ? "brain" : "sparkle"} size={13} />
      </span>
      <span style={{ fontWeight: 500 }}>{label}</span>
      {subtitle && (
        <span
          style={{
            color: T.textMute,
            fontFamily: ui.mono,
            fontSize: 12,
          }}
        >
          {subtitle}
        </span>
      )}
      <Icon name="chevRight" size={13} />
    </button>
  );
}
