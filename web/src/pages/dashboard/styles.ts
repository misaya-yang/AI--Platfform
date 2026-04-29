// Dashboard design tokens for the AI Gateway operations console.
// The palette intentionally stays quiet and operational: graphite canvas,
// green status accents, warm gold for attention, and steel for charts.

export const SPACING = {
  xs: 4, sm: 8, md: 12, lg: 16, xl: 20, '2xl': 24, '3xl': 32, '4xl': 40, '5xl': 56, '6xl': 72,
} as const;

export const RADIUS = { sm: 5, md: 8, lg: 10 } as const;

export const TYPOGRAPHY = {
  pageTitle:    { fontSize: 17, fontWeight: 600, letterSpacing: '0', lineHeight: 1.2 },
  sectionTitle: { fontSize: 13.5, fontWeight: 600, letterSpacing: '0' },
  eyebrow:      { fontSize: 11, fontWeight: 500, letterSpacing: '0' },
  cardLabel:    { fontSize: 12.5, fontWeight: 500 },
  kpiValue:     { fontSize: 26, fontWeight: 600, fontFeatureSettings: '"tnum"', letterSpacing: '0' },
  kpiUnit:      { fontSize: 13, fontWeight: 500 },
  body:         { fontSize: 13, fontWeight: 400 },
  caption:      { fontSize: 11.5, fontWeight: 500 },
  mono:         { fontFamily: '"IBM Plex Mono", "JetBrains Mono", ui-monospace, SFMono-Regular, Consolas, monospace', fontFeatureSettings: '"tnum"' },
} as const;

export const FONT_FAMILY = {
  sans: '"Geist Sans", "Geist", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
  mono: '"IBM Plex Mono", "JetBrains Mono", ui-monospace, SFMono-Regular, Consolas, monospace',
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
  CARD_GAP: 12,
  CARD_PADDING: 14,
  CARD_RADIUS: 8,
  PAGE_PADDING: 0,
  KPI_HEIGHT: 'auto' as unknown as number,
  PANEL_MIN_HEIGHT: 320,
  DASHBOARD_MIN_CONTENT_WIDTH: 1020,
  GRID_GAP: 12,
  SECTION_GAP: 12,
  KPI_COLUMNS: 5,
  PROVIDER_COLUMNS: 5,
  FIVE_COL_MIN_ITEM_WIDTH: 190,
  BREAKPOINTS: { sm: 640, md: 768, lg: 1024, xl: 1280 },
} as const;

export const getColors = (darkMode: boolean) => ({
  // Canvas
  pageBg:    darkMode ? '#0f1411' : '#f3f5f2',
  cardBg:    darkMode ? '#171d19' : '#fbfcfb',
  cardHover: darkMode ? '#1d241f' : '#eef2ef',
  innerBg:   darkMode ? '#121713' : '#f6f8f6',

  // Borders
  border:      darkMode ? '#2f3a33' : '#dfe5e0',
  borderSoft:  darkMode ? '#263029' : '#e9eeea',
  divider:     darkMode ? '#222b25' : '#edf1ee',
  borderHover: darkMode ? '#425047' : '#cbd6ce',
  borderStrong: darkMode ? '#536157' : '#b8c4bc',

  // Ink
  textPrimary:   darkMode ? '#edf1ec' : '#17211b',
  textSecondary: darkMode ? '#aeb9b0' : '#536159',
  textMuted:     darkMode ? '#838f86' : '#7b8780',
  textFaint:     darkMode ? '#5d6961' : '#a9b3ad',

  // Brand and operations accents
  accent:       darkMode ? '#78c89c' : '#1a6a45',
  accentBright: darkMode ? '#95d7b2' : '#248457',
  accentDeep:   darkMode ? '#4ba774' : '#14543c',
  accentSoft:   darkMode ? 'rgba(120,200,156,0.14)' : '#e8f4ee',
  operator:     darkMode ? '#78c89c' : '#14543c',
  operatorSoft: darkMode ? 'rgba(120,200,156,0.12)' : '#edf7f2',
  navy:         darkMode ? '#a8b8c9' : '#25394b',

  // Semantic
  success:      darkMode ? '#78c89c' : '#18945c',
  successSoft:  darkMode ? 'rgba(120,200,156,0.13)' : '#e8f7ef',
  error:        darkMode ? '#ee7d78' : '#d64545',
  errorSoft:    darkMode ? 'rgba(238,125,120,0.13)' : '#fff0ef',
  warning:      darkMode ? '#d9ba6a' : '#b7842e',
  warningSoft:  darkMode ? 'rgba(217,186,106,0.14)' : '#fbf4df',
  danger:       darkMode ? '#ee7d78' : '#d64545',

  // Back-compat aliases
  gold:   darkMode ? '#d9ba6a' : '#c9a84c',
  purple: darkMode ? '#b6a9c9' : '#786b92',
  info:   darkMode ? '#89a9c5' : '#3f708e',

  // Tinted fills
  successBg: darkMode ? 'rgba(120,200,156,0.15)' : '#e8f7ef',
  warningBg: darkMode ? 'rgba(217,186,106,0.15)' : '#fbf4df',
  errorBg:   darkMode ? 'rgba(238,125,120,0.15)' : '#fff0ef',
  accentBg:  darkMode ? 'rgba(120,200,156,0.15)' : '#e8f4ee',
  purpleBg:  darkMode ? 'rgba(182,169,201,0.14)' : '#f2eef6',
  infoBg:    darkMode ? 'rgba(137,169,197,0.14)' : '#edf4f8',

  shadowSm: darkMode ? 'none' : '0 1px 2px rgba(16,18,24,0.04)',
  shadowMd: darkMode ? 'none' : '0 4px 16px rgba(16,18,24,0.05)',
  shadowLg: darkMode ? 'none' : '0 8px 32px rgba(16,18,24,0.08)',

  // Legacy gradient keys
  accentGradient:  darkMode ? 'linear-gradient(135deg,#78c89c,#4ba774)' : 'linear-gradient(135deg,#1a6a45,#14543c)',
  successGradient: darkMode ? 'linear-gradient(135deg,#78c89c,#4ba774)' : 'linear-gradient(135deg,#18945c,#116a42)',
  warningGradient: darkMode ? 'linear-gradient(135deg,#d9ba6a,#b8934f)' : 'linear-gradient(135deg,#c9a84c,#a77e2a)',
  errorGradient:   darkMode ? 'linear-gradient(135deg,#ee7d78,#d66761)' : 'linear-gradient(135deg,#d64545,#b83535)',
  purpleGradient:  darkMode ? 'linear-gradient(135deg,#b6a9c9,#8f84a4)' : 'linear-gradient(135deg,#786b92,#5f5577)',
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
    '#78c89c', '#89a9c5', '#d9ba6a', '#ee7d78', '#b6a9c9', c.textFaint,
  ] : [
    '#1a6a45',
    '#3f708e',
    '#c9a84c',
    '#d64545',
    '#786b92',
    '#c6cec8',
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
