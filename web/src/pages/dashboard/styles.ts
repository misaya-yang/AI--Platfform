// Dashboard Design Tokens — 1:1 mirror of the design-handoff dashboard.jsx
// Reference: anthropic design handoff bundle · gateway/project/dashboard.jsx `T`

export const SPACING = {
  xs: 4, sm: 8, md: 12, lg: 16, xl: 20, '2xl': 24, '3xl': 32, '4xl': 40, '5xl': 56, '6xl': 72,
} as const;

export const RADIUS = { sm: 5, md: 8, lg: 10 } as const;

export const TYPOGRAPHY = {
  pageTitle:    { fontSize: 17, fontWeight: 600, letterSpacing: '-0.2px', lineHeight: 1.2 },
  sectionTitle: { fontSize: 13.5, fontWeight: 600, letterSpacing: '-0.01em' },
  eyebrow:      { fontSize: 11, fontWeight: 500, letterSpacing: '0' },
  cardLabel:    { fontSize: 12.5, fontWeight: 500 },
  kpiValue:     { fontSize: 26, fontWeight: 600, fontFeatureSettings: '"tnum"', letterSpacing: '-0.5px' },
  kpiUnit:      { fontSize: 13, fontWeight: 500 },
  body:         { fontSize: 13, fontWeight: 400 },
  caption:      { fontSize: 11.5, fontWeight: 500 },
  mono:         { fontFamily: '"JetBrains Mono", ui-monospace, SFMono-Regular, Consolas, monospace', fontFeatureSettings: '"tnum"' },
} as const;

export const FONT_FAMILY = {
  sans: '"Inter", -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif',
  mono: '"JetBrains Mono", ui-monospace, SFMono-Regular, Consolas, monospace',
} as const;

export const TRANSITION = {
  fast:   'all 140ms cubic-bezier(0.16, 1, 0.3, 1)',
  normal: 'all 200ms cubic-bezier(0.16, 1, 0.3, 1)',
  slow:   'all 320ms cubic-bezier(0.16, 1, 0.3, 1)',
} as const;

export const REDUCED_MOTION_CSS = `@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:0.01ms!important;transition-duration:0.01ms!important}}`;

export const ELEVATION = (dark: boolean) => ({
  none: 'none',
  sm: dark ? 'none' : '0 1px 2px rgba(16,18,24,0.04)',
  md: dark ? 'none' : '0 4px 16px rgba(16,18,24,0.05)',
  lg: dark ? 'none' : '0 8px 32px rgba(16,18,24,0.08)',
});

export const LAYOUT = {
  CARD_GAP: 14,
  CARD_PADDING: 16,
  CARD_RADIUS: 10,
  PAGE_PADDING: 0,
  KPI_HEIGHT: 'auto' as unknown as number,
  PANEL_MIN_HEIGHT: 400,
  DASHBOARD_MIN_CONTENT_WIDTH: 1020,
  GRID_GAP: 14,
  SECTION_GAP: 16,
  KPI_COLUMNS: 5,
  PROVIDER_COLUMNS: 5,
  FIVE_COL_MIN_ITEM_WIDTH: 190,
  BREAKPOINTS: { sm: 640, md: 768, lg: 1024, xl: 1280 },
} as const;

// ── Exact design-handoff palette ────────────────────────────────────
// All hex values verbatim from dashboard.jsx `T`. Dark mode values
// derived by inverting luminance on the neutrals while keeping the
// indigo accent stable (with minor brightness lift).
export const getColors = (darkMode: boolean) => ({
  // Canvas
  pageBg:    darkMode ? '#0c0f16' : '#f4f6f5',
  cardBg:    darkMode ? '#161a24' : '#ffffff',
  cardHover: darkMode ? '#1b2130' : '#f1f4f3',   // T.hover
  innerBg:   darkMode ? '#111622' : '#f8faf9',   // T.surfaceAlt

  // Borders
  border:      darkMode ? '#2a3042' : '#e6e8ee',  // T.border
  borderSoft:  darkMode ? '#232938' : '#eef0f4',  // T.borderSoft
  divider:     darkMode ? '#1e2432' : '#f1f2f6',  // T.divider
  borderHover: darkMode ? '#3a4156' : '#cfd2d8',
  borderStrong: darkMode ? '#3f465c' : '#cfd2d8',

  // Ink
  textPrimary:   darkMode ? '#e7e9ef' : '#1a1d24',  // T.text
  textSecondary: darkMode ? '#a2a7b4' : '#565a66',  // T.textMid
  textMuted:     darkMode ? '#787d8a' : '#8b8f9b',  // T.textDim
  textFaint:     darkMode ? '#4e5362' : '#b7bac3',  // T.textFaint

  // Signature indigo — the ONE accent
  accent:       darkMode ? '#9aa6ff' : '#6674f4',   // active nav/tab only
  accentBright: darkMode ? '#b0b9ff' : '#7b87f6',
  accentDeep:   darkMode ? '#7784ff' : '#5663e8',
  accentSoft:   darkMode ? '#232958' : '#eef0fe',
  operator:     darkMode ? '#5dd6a6' : '#14543c',
  operatorSoft: darkMode ? 'rgba(93,214,166,0.12)' : '#edf7f2',
  navy:         darkMode ? '#93a4bc' : '#1f3448',

  // Semantic
  success:      darkMode ? '#34d399' : '#10b981',
  successSoft:  darkMode ? 'rgba(16,185,129,0.12)' : '#ecfdf5',
  error:        darkMode ? '#f87171' : '#ef4444',
  errorSoft:    darkMode ? 'rgba(239,68,68,0.12)' : '#fef2f2',
  warning:      darkMode ? '#fbbf24' : '#d98906',
  warningSoft:  darkMode ? 'rgba(245,158,11,0.12)' : '#fffbeb',
  danger:       darkMode ? '#f87171' : '#ef4444',

  // Back-compat aliases
  gold:   darkMode ? '#fbbf24' : '#f59e0b',
  purple: darkMode ? '#c4b5fd' : '#a78bfa',
  info:   darkMode ? '#22d3ee' : '#06b6d4',

  // Tinted fills
  successBg: darkMode ? 'rgba(52,211,153,0.14)' : '#ecfdf5',
  warningBg: darkMode ? 'rgba(251,191,36,0.14)' : '#fffbeb',
  errorBg:   darkMode ? 'rgba(248,113,113,0.14)' : '#fef2f2',
  accentBg:  darkMode ? 'rgba(135,148,255,0.14)' : '#eef0fe',
  purpleBg:  darkMode ? 'rgba(196,181,253,0.14)' : '#f5f0ff',
  infoBg:    darkMode ? 'rgba(34,211,238,0.14)' : '#ecfeff',

  shadowSm: darkMode ? 'none' : '0 1px 2px rgba(16,18,24,0.04)',
  shadowMd: darkMode ? 'none' : '0 4px 16px rgba(16,18,24,0.05)',
  shadowLg: darkMode ? 'none' : '0 8px 32px rgba(16,18,24,0.08)',

  // Legacy gradient keys
  accentGradient:  darkMode ? 'linear-gradient(135deg,#8794ff,#6674f4)' : 'linear-gradient(135deg,#6674f4,#5663e8)',
  successGradient: darkMode ? 'linear-gradient(135deg,#34d399,#10b981)' : 'linear-gradient(135deg,#10b981,#059669)',
  warningGradient: darkMode ? 'linear-gradient(135deg,#fbbf24,#f59e0b)' : 'linear-gradient(135deg,#f59e0b,#d97706)',
  errorGradient:   darkMode ? 'linear-gradient(135deg,#f87171,#ef4444)' : 'linear-gradient(135deg,#ef4444,#dc2626)',
  purpleGradient:  darkMode ? 'linear-gradient(135deg,#c4b5fd,#a78bfa)' : 'linear-gradient(135deg,#a78bfa,#8b5cf6)',
});

// ── KPI per-metric color map (dashboard.jsx KPIS) ─────────────────────
export const getKpiAccents = (darkMode: boolean) => {
  const c = getColors(darkMode);
  return {
    requests: { fg: c.accent, bg: c.accentBg  }, // indigo
    cost:     { fg: c.success, bg: c.successBg }, // green
    latency:  { fg: c.info,   bg: c.infoBg    }, // cyan
    success:  { fg: c.success, bg: c.successBg }, // green
    tokens:   { fg: c.purple, bg: c.purpleBg  }, // purple
  };
};

// ── Chart series palette (exact from dashboard.jsx) ───────────────────
export const getChartPalette = (darkMode: boolean) => {
  const c = getColors(darkMode);
  return darkMode ? [
    '#8794ff', '#34d399', '#22d3ee', '#c4b5fd', '#fbbf24', c.textFaint,
  ] : [
    '#6674f4', // indigo
    '#10b981', // green
    '#06b6d4', // cyan
    '#a78bfa', // purple
    '#f59e0b', // amber
    '#c9ccd4', // muted
  ];
};

export const commonStyles = { transition: TRANSITION.normal, cardHeaderHeight: 44 };

export const gridStyles = {
  fiveColumn: { display: 'grid' as const, gridTemplateColumns: 'repeat(5, 1fr)', gap: LAYOUT.GRID_GAP },
  fiveColumnResponsive: { display: 'grid' as const, gridTemplateColumns: `repeat(auto-fit, minmax(${LAYOUT.FIVE_COL_MIN_ITEM_WIDTH}px, 1fr))`, gap: LAYOUT.GRID_GAP },
};

export const getCardStyles = (darkMode: boolean) => {
  const c = getColors(darkMode);
  return { background: c.cardBg, border: `1px solid ${c.borderSoft}`, borderRadius: LAYOUT.CARD_RADIUS, padding: LAYOUT.CARD_PADDING };
};

export const getSectionStyles = (darkMode: boolean) => {
  const c = getColors(darkMode);
  return { marginBottom: LAYOUT.SECTION_GAP, background: c.cardBg, border: `1px solid ${c.borderSoft}`, borderRadius: LAYOUT.CARD_RADIUS, padding: LAYOUT.CARD_PADDING };
};
