/**
 * ActivityPill — inline affordance that opens the right-side Activity
 * drawer.
 *
 * Visual target (restrained, Linear/Claude.ai style):
 *   icon + "Activity · N steps · XXs" + chevron   — reads as text, not a button
 *
 * At rest: no background, no border, no colored icon halo. Tap target
 * remains accessible (≥32px via vertical padding). On hover: subtle
 * bg-soft fade + primary text colour.
 *
 * Two variants are kept for API compatibility (pill | chip) but they
 * now render with the same reduced-chrome style; only typography size
 * differs.
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

  // Subtitle: "· N step(s) · XXs" — English plural handled at call-site
  // if it ever changes; here we render whatever the caller computed.
  const subtitle =
    durationLabel && steps > 0
      ? `· ${steps} ${steps === 1 ? "step" : "steps"} · ${durationLabel}`
      : steps > 0
        ? `· ${steps} ${steps === 1 ? "step" : "steps"}`
        : durationLabel
          ? `· ${durationLabel}`
          : "";

  const isChip = variant === "chip";
  const fontSize = isChip ? 12.5 : 13;
  const subtitleSize = isChip ? 11.5 : 12;

  return (
    <button
      type="button"
      onClick={onOpen}
      className="act-btn act-hover"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 7,
        // Vertical padding keeps the 32px click target while the visual
        // at rest reads as plain text.
        padding: "7px 8px",
        borderRadius: 6,
        border: "1px solid transparent",
        background: "transparent",
        color: T.textMute,
        fontFamily: ui.sans,
        fontSize,
        cursor: "pointer",
        lineHeight: 1,
      }}
    >
      {running ? (
        <span
          style={{
            display: "inline-flex",
            color: T.accent,
            // Reserve glyph width so layout doesn't jump between running
            // and idle states.
            width: 14,
            justifyContent: "center",
          }}
        >
          <span className="act-dot" />
          <span className="act-dot" />
          <span className="act-dot" />
        </span>
      ) : (
        <span
          style={{
            color: T.textMute,
            display: "inline-flex",
            width: 14,
            justifyContent: "center",
          }}
        >
          <Icon name="sparkle" size={13} />
        </span>
      )}
      <span style={{ fontWeight: 500, color: T.text }}>{label}</span>
      {subtitle && (
        <span
          style={{
            color: T.textDim,
            fontFamily: ui.mono,
            fontSize: subtitleSize,
          }}
        >
          {subtitle}
        </span>
      )}
      <Icon name="chevRight" size={12} />
    </button>
  );
}
