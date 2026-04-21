/**
 * activityTheme — design tokens for the Claude-design Activity system.
 *
 * The light palette is ported verbatim from `chat.jsx` (Claude Design
 * handoff). Dark-mode values are our own — same hue/chroma, inverted
 * lightness — keyed to `html.dark` (which is how `useThemeSync` toggles
 * Tailwind dark mode in this app).
 *
 * Tokens are exposed as CSS variables so consumers can keep using simple
 * inline styles (`background: T.bg`) while still reacting to the theme
 * toggle live. Don't inline raw oklch values anywhere except this file.
 */

const LIGHT = {
  bg: "oklch(0.985 0.003 80)",
  bgSoft: "oklch(0.97 0.004 80)",
  panel: "oklch(1 0 0)",
  border: "oklch(0.92 0.005 80)",
  borderSoft: "oklch(0.95 0.004 80)",
  text: "oklch(0.23 0.008 80)",
  textMute: "oklch(0.52 0.008 80)",
  textDim: "oklch(0.68 0.006 80)",
  accent: "oklch(0.55 0.13 250)",
  accentSoft: "oklch(0.95 0.03 250)",
  accentText: "oklch(0.4 0.13 250)",
  rail: "oklch(0.98 0.004 80)",
  userBubble: "oklch(0.96 0.01 250)",
  error: "oklch(0.55 0.2 25)",
  errorSoft: "oklch(0.97 0.05 25)",
  shadow: "0 1px 2px rgba(20,14,8,.04), 0 4px 24px rgba(20,14,8,.04)",
  shadowLift: "0 2px 8px rgba(20,14,8,.06), 0 12px 40px rgba(20,14,8,.08)",
};

const DARK = {
  bg: "oklch(0.18 0.008 80)",
  bgSoft: "oklch(0.22 0.008 80)",
  panel: "oklch(0.23 0.008 80)",
  border: "oklch(0.32 0.01 80)",
  borderSoft: "oklch(0.28 0.008 80)",
  text: "oklch(0.94 0.006 80)",
  textMute: "oklch(0.72 0.008 80)",
  textDim: "oklch(0.55 0.008 80)",
  accent: "oklch(0.72 0.15 250)",
  accentSoft: "oklch(0.32 0.08 250)",
  accentText: "oklch(0.85 0.13 250)",
  rail: "oklch(0.24 0.008 80)",
  userBubble: "oklch(0.3 0.05 250)",
  error: "oklch(0.72 0.18 25)",
  errorSoft: "oklch(0.35 0.1 25)",
  shadow: "0 1px 2px rgba(0,0,0,.3), 0 4px 24px rgba(0,0,0,.35)",
  shadowLift: "0 2px 8px rgba(0,0,0,.4), 0 12px 40px rgba(0,0,0,.55)",
};

// Tokens are referenced through CSS variables, so theme toggles take
// effect without re-rendering React trees.
export const T = {
  bg: "var(--act-bg)",
  bgSoft: "var(--act-bgSoft)",
  panel: "var(--act-panel)",
  border: "var(--act-border)",
  borderSoft: "var(--act-borderSoft)",
  text: "var(--act-text)",
  textMute: "var(--act-textMute)",
  textDim: "var(--act-textDim)",
  accent: "var(--act-accent)",
  accentSoft: "var(--act-accentSoft)",
  accentText: "var(--act-accentText)",
  rail: "var(--act-rail)",
  userBubble: "var(--act-userBubble)",
  shadow: "var(--act-shadow)",
  shadowLift: "var(--act-shadowLift)",
} as const;

export const ui = {
  sans:
    'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif',
  mono: '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
} as const;

function varsBlock(palette: Record<string, string>): string {
  return Object.entries(palette)
    .map(([k, v]) => `--act-${k}: ${v};`)
    .join(" ");
}

// Inject CSS variables (both palettes) + shared keyframes / helper
// classes once per session. Idempotent.
export function ensureActivityStyles(): void {
  if (typeof document === "undefined") return;
  if (document.getElementById("assistant-activity-styles")) return;
  const s = document.createElement("style");
  s.id = "assistant-activity-styles";
  s.textContent = `
    :root { ${varsBlock(LIGHT)} }
    html.dark { ${varsBlock(DARK)} }
    .act-scroll::-webkit-scrollbar{width:8px;height:8px}
    .act-scroll::-webkit-scrollbar-thumb{background:var(--act-border);border-radius:4px}
    .act-scroll::-webkit-scrollbar-track{background:transparent}
    .act-btn{transition:background .12s, color .12s, transform .08s}
    .act-btn:active{transform:scale(0.97)}
    @keyframes act-dot{0%,60%,100%{opacity:.3;transform:translateY(0)}30%{opacity:1;transform:translateY(-2px)}}
    .act-dot{display:inline-block;width:4px;height:4px;border-radius:2px;background:currentColor;margin-right:3px;animation:act-dot 1.2s infinite}
    .act-dot:nth-child(2){animation-delay:.15s}
    .act-dot:nth-child(3){animation-delay:.3s}
    @keyframes act-slide-in{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}
    .act-panel-enter{animation:act-slide-in .28s cubic-bezier(.2,.8,.3,1) both}
    @keyframes act-step-tick{from{opacity:0;transform:translateX(-4px)}to{opacity:1;transform:none}}
    .act-step-new{animation:act-step-tick .35s ease both}
    .act-caret{display:inline-block;width:2px;height:14px;background:currentColor;vertical-align:-2px;margin-left:2px;animation:act-caret 1s infinite}
    @keyframes act-caret{0%,50%{opacity:1}51%,100%{opacity:0}}
    .act-running-dot{width:5px;height:5px;border-radius:3px;background:var(--act-accent);animation:act-dot 1.2s infinite}
    .act-hover:hover{background:var(--act-bgSoft)}
    .act-accent-hover:hover{background:var(--act-accentSoft)}
  `;
  document.head.appendChild(s);
}
