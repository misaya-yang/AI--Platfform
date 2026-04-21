/**
 * activityTheme — design tokens for the Claude-design Activity system.
 *
 * Ported verbatim from `chat.jsx` (Claude Design handoff). The values are
 * in oklch space and are usable directly in inline styles or Tailwind
 * arbitrary-value syntax (`bg-[oklch(0.985_0.003_80)]`).
 *
 * Keep this file small and purely data — consumed by ActivityPanel,
 * ActivityTimeline, TimelineStep, ActivityPill.
 */

export const T = {
  // warm neutral palette
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
  shadow: "0 1px 2px rgba(20,14,8,.04), 0 4px 24px rgba(20,14,8,.04)",
  shadowLift: "0 2px 8px rgba(20,14,8,.06), 0 12px 40px rgba(20,14,8,.08)",
} as const;

export const ui = {
  sans:
    'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif',
  mono: '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
} as const;

// Inject shared keyframes / helper classes once per session. Idempotent.
export function ensureActivityStyles(): void {
  if (typeof document === "undefined") return;
  if (document.getElementById("assistant-activity-styles")) return;
  const s = document.createElement("style");
  s.id = "assistant-activity-styles";
  s.textContent = `
    .act-scroll::-webkit-scrollbar{width:8px;height:8px}
    .act-scroll::-webkit-scrollbar-thumb{background:${T.border};border-radius:4px}
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
    .act-running-dot{width:5px;height:5px;border-radius:3px;background:${T.accent};animation:act-dot 1.2s infinite}
    .act-hover:hover{background:${T.bgSoft}}
    .act-accent-hover:hover{background:${T.accentSoft}}
  `;
  document.head.appendChild(s);
}
