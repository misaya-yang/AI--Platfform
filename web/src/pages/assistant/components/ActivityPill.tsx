/**
 * ActivityPill — inline affordance that opens the right-side Activity
 * drawer.
 *
 * Restrained chrome: a 1px hairline + surface-bg tint at rest — visible
 * enough to read as a control on a near-black canvas, quiet enough to
 * not scream like an AI-flavor filled pill. Earlier iteration went fully
 * transparent and disappeared on dark mode; this tier restores the
 * findability floor without regressing the quiet aesthetic.
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
        padding: "7px 10px",
        borderRadius: 6,
        border: `1px solid ${T.border}`,
        background: T.panel,
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
