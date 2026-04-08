// Dashboard Design Tokens — Hejaz green/gold alignment

export const SPACING = {
  xs: 4, sm: 8, md: 12, lg: 16, xl: 20, '2xl': 24, '3xl': 32, '4xl': 40,
} as const;

export const RADIUS = { sm: 6, md: 8, lg: 12 } as const;

export const TYPOGRAPHY = {
  pageTitle:    { fontSize: 22, fontWeight: 600 },
  sectionTitle: { fontSize: 16, fontWeight: 600 },
  cardLabel:    { fontSize: 12, fontWeight: 500, letterSpacing: '0.03em', textTransform: 'uppercase' as const },
  kpiValue:     { fontSize: 28, fontWeight: 600, fontFeatureSettings: '"tnum"' },
  kpiUnit:      { fontSize: 13, fontWeight: 500 },
  body:         { fontSize: 14, fontWeight: 400 },
  caption:      { fontSize: 12, fontWeight: 500 },
} as const;

export const TRANSITION = {
  fast:   'all 150ms cubic-bezier(0.16, 1, 0.3, 1)',
  normal: 'all 200ms cubic-bezier(0.16, 1, 0.3, 1)',
  slow:   'all 300ms cubic-bezier(0.16, 1, 0.3, 1)',
} as const;

export const REDUCED_MOTION_CSS = `@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:0.01ms!important;transition-duration:0.01ms!important}}`;

export const ELEVATION = (dark: boolean) => ({
  none: 'none',
  sm: dark ? 'none' : '0 1px 2px rgba(0,0,0,0.04)',
  md: dark ? 'none' : '0 2px 6px rgba(0,0,0,0.05)',
  lg: dark ? 'none' : '0 4px 16px rgba(0,0,0,0.06)',
});

export const LAYOUT = {
  CARD_GAP: 12,
  CARD_PADDING: 16,
  CARD_RADIUS: 8,
  PAGE_PADDING: 0,
  KPI_HEIGHT: 'auto' as unknown as number,
  PANEL_MIN_HEIGHT: 400,
  DASHBOARD_MIN_CONTENT_WIDTH: 1060,
  GRID_GAP: 12,
  SECTION_GAP: 16,
  KPI_COLUMNS: 5,
  PROVIDER_COLUMNS: 5,
  FIVE_COL_MIN_ITEM_WIDTH: 200,
  BREAKPOINTS: { sm: 640, md: 768, lg: 1024, xl: 1280 },
} as const;

// ── Hejaz green/gold palette ──────────────────────────────────────────
export const getColors = (darkMode: boolean) => ({
  pageBg:    darkMode ? '#0c100d' : '#f0f2f0',
  cardBg:    darkMode ? '#1a211c' : '#ffffff',
  cardHover: darkMode ? '#212923' : '#f7f8f7',
  innerBg:   darkMode ? '#141a16' : '#f0f2f0',

  border:      darkMode ? '#2e3830' : '#dde1de',
  borderHover: darkMode ? '#3a443c' : '#c4cac6',

  textPrimary:   darkMode ? '#eef0ef' : '#161b17',
  textSecondary: darkMode ? '#98a29b' : '#4d574f',
  textMuted:     darkMode ? '#6d786f' : '#98a29b',

  accent:  darkMode ? '#3daa73' : '#1a4731',
  success: darkMode ? '#4ade80' : '#22c55e',
  warning: darkMode ? '#e5c472' : '#d9ab44',
  error:   darkMode ? '#f87171' : '#dc3545',
  gold:    darkMode ? '#e5c472' : '#c9a84c',

  successBg: darkMode ? 'rgba(74,222,128,0.08)' : 'rgba(34,197,94,0.06)',
  warningBg: darkMode ? 'rgba(229,196,114,0.08)' : 'rgba(217,171,68,0.06)',
  errorBg:   darkMode ? 'rgba(248,113,113,0.08)' : 'rgba(220,53,69,0.06)',
  accentBg:  darkMode ? 'rgba(61,170,115,0.08)' : 'rgba(26,71,49,0.06)',

  shadowSm: darkMode ? 'none' : '0 1px 2px rgba(0,0,0,0.04)',
  shadowMd: darkMode ? 'none' : '0 2px 6px rgba(0,0,0,0.05)',
  shadowLg: darkMode ? 'none' : '0 4px 16px rgba(0,0,0,0.06)',

  accentGradient:  darkMode ? 'linear-gradient(135deg,#3daa73,#1a4731)' : 'linear-gradient(135deg,#1a4731,#123322)',
  successGradient: darkMode ? 'linear-gradient(135deg,#4ade80,#22c55e)' : 'linear-gradient(135deg,#22c55e,#16a34a)',
  warningGradient: darkMode ? 'linear-gradient(135deg,#e5c472,#d9ab44)' : 'linear-gradient(135deg,#d9ab44,#b08f3a)',
  errorGradient:   darkMode ? 'linear-gradient(135deg,#f87171,#dc3545)' : 'linear-gradient(135deg,#dc3545,#b02a37)',
  purpleGradient:  darkMode ? 'linear-gradient(135deg,#6cc899,#3daa73)' : 'linear-gradient(135deg,#3daa73,#1a4731)',
});

export const commonStyles = { transition: TRANSITION.normal, cardHeaderHeight: 44 };

export const gridStyles = {
  fiveColumn: { display: 'grid' as const, gridTemplateColumns: 'repeat(5, 1fr)', gap: LAYOUT.GRID_GAP },
  fiveColumnResponsive: { display: 'grid' as const, gridTemplateColumns: `repeat(auto-fit, minmax(${LAYOUT.FIVE_COL_MIN_ITEM_WIDTH}px, 1fr))`, gap: LAYOUT.GRID_GAP },
};

export const getCardStyles = (darkMode: boolean) => {
  const c = getColors(darkMode);
  return { background: c.cardBg, border: `1px solid ${c.border}`, borderRadius: LAYOUT.CARD_RADIUS, padding: LAYOUT.CARD_PADDING };
};

export const getSectionStyles = (darkMode: boolean) => {
  const c = getColors(darkMode);
  return { marginBottom: LAYOUT.SECTION_GAP, background: c.cardBg, border: `1px solid ${c.border}`, borderRadius: LAYOUT.CARD_RADIUS, padding: LAYOUT.CARD_PADDING };
};
