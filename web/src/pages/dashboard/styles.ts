// Dashboard design tokens for the AI Gateway operations console.
// The palette intentionally stays quiet and operational: graphite slate,
// steel product chrome, muted success, and restrained amber for attention.

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
  CARD_GAP: 16,
  CARD_PADDING: 16,
  CARD_RADIUS: 8,
  PAGE_PADDING: 0,
  KPI_HEIGHT: 'auto' as unknown as number,
  PANEL_MIN_HEIGHT: 320,
  DASHBOARD_MIN_CONTENT_WIDTH: 1180,
  GRID_GAP: 16,
  SECTION_GAP: 16,
  KPI_COLUMNS: 5,
  PROVIDER_COLUMNS: 5,
  FIVE_COL_MIN_ITEM_WIDTH: 190,
  BREAKPOINTS: { sm: 640, md: 768, lg: 1024, xl: 1280 },
} as const;

export const getColors = (darkMode: boolean) => ({
  // Canvas
  pageBg:    darkMode ? '#11141a' : '#f4f6f8',
  cardBg:    darkMode ? '#181d25' : '#ffffff',
  cardHover: darkMode ? '#202734' : '#eef2f7',
  innerBg:   darkMode ? '#151922' : '#f7f9fb',

  // Borders
  border:      darkMode ? '#303948' : '#d9dee6',
  borderSoft:  darkMode ? '#252d39' : '#e8edf3',
  divider:     darkMode ? '#222936' : '#edf1f5',
  borderHover: darkMode ? '#3b4657' : '#c9d3df',
  borderStrong: darkMode ? '#4c5a6d' : '#aebdce',

  // Ink
  textPrimary:   darkMode ? '#eef2f6' : '#1a202b',
  textSecondary: darkMode ? '#a6b0bf' : '#505c6e',
  textMuted:     darkMode ? '#7f8b9b' : '#6f7c8e',
  textFaint:     darkMode ? '#596578' : '#a0aab8',

  // Brand and operations accents
  accent:       darkMode ? '#8fa9cf' : '#37577c',
  accentBright: darkMode ? '#b7c9e4' : '#4f6f96',
  accentDeep:   darkMode ? '#6d8bb3' : '#29435f',
  accentSoft:   darkMode ? 'rgba(143,169,207,0.14)' : '#e8eef6',
  operator:     darkMode ? '#8fa9cf' : '#37577c',
  operatorSoft: darkMode ? 'rgba(143,169,207,0.12)' : '#e9eff7',
  navy:         darkMode ? '#b7c9e4' : '#29435f',

  // Semantic
  success:      darkMode ? '#69b58d' : '#2f8f68',
  successSoft:  darkMode ? 'rgba(105,181,141,0.13)' : '#e7f4ee',
  error:        darkMode ? '#ee7d78' : '#d64545',
  errorSoft:    darkMode ? 'rgba(238,125,120,0.13)' : '#fff0ef',
  warning:      darkMode ? '#d0a96b' : '#ae7c32',
  warningSoft:  darkMode ? 'rgba(208,169,107,0.14)' : '#f7eddb',
  danger:       darkMode ? '#ee7d78' : '#d64545',

  // Back-compat aliases
  gold:   darkMode ? '#d0a96b' : '#ae7c32',
  purple: darkMode ? '#a7a0ba' : '#746f86',
  info:   darkMode ? '#8fa9cf' : '#4f6f96',

  // Tinted fills
  successBg: darkMode ? 'rgba(105,181,141,0.15)' : '#e7f4ee',
  warningBg: darkMode ? 'rgba(208,169,107,0.15)' : '#f7eddb',
  errorBg:   darkMode ? 'rgba(238,125,120,0.15)' : '#fff0ef',
  accentBg:  darkMode ? 'rgba(143,169,207,0.15)' : '#e9eff7',
  purpleBg:  darkMode ? 'rgba(167,160,186,0.14)' : '#f1eff5',
  infoBg:    darkMode ? 'rgba(143,169,207,0.14)' : '#e9eff7',

  shadowSm: darkMode ? 'none' : '0 1px 2px rgba(16,18,24,0.04)',
  shadowMd: darkMode ? 'none' : '0 4px 16px rgba(16,18,24,0.05)',
  shadowLg: darkMode ? 'none' : '0 8px 32px rgba(16,18,24,0.08)',

  // Legacy gradient keys
  accentGradient:  darkMode ? 'linear-gradient(135deg,#8fa9cf,#6d8bb3)' : 'linear-gradient(135deg,#37577c,#29435f)',
  successGradient: darkMode ? 'linear-gradient(135deg,#69b58d,#4d9470)' : 'linear-gradient(135deg,#2f8f68,#237251)',
  warningGradient: darkMode ? 'linear-gradient(135deg,#d0a96b,#b88f55)' : 'linear-gradient(135deg,#ae7c32,#8e6329)',
  errorGradient:   darkMode ? 'linear-gradient(135deg,#ee7d78,#d66761)' : 'linear-gradient(135deg,#d64545,#b83535)',
  purpleGradient:  darkMode ? 'linear-gradient(135deg,#a7a0ba,#827b96)' : 'linear-gradient(135deg,#746f86,#595566)',
});

// ── KPI per-metric color map (dashboard.jsx KPIS) ─────────────────────
export const getKpiAccents = (darkMode: boolean) => {
  const c = getColors(darkMode);
  return {
    requests: { fg: c.accent, bg: c.accentBg },
    cost:     { fg: c.success, bg: c.successBg },
    latency:  { fg: c.info,   bg: c.infoBg },
    success:  { fg: c.success, bg: c.successBg },
    tokens:   { fg: c.purple, bg: c.purpleBg },
  };
};

// ── Chart series palette (exact from dashboard.jsx) ───────────────────
export const getChartPalette = (darkMode: boolean) => {
  const c = getColors(darkMode);
  return darkMode ? [
    '#8fa9cf', '#d0a96b', '#69b58d', '#ee7d78', '#a7a0ba', c.textFaint,
  ] : [
    '#37577c',
    '#ae7c32',
    '#2f8f68',
    '#d64545',
    '#746f86',
    '#c1c9d4',
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
