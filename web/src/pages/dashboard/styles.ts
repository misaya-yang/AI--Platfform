// Dashboard Design Tokens — shadcn/ui zinc alignment

export const SPACING = {
  xs: 4, sm: 8, md: 12, lg: 16, xl: 20, '2xl': 24, '3xl': 32, '4xl': 40,
} as const;

export const RADIUS = { sm: 6, md: 8, lg: 12, xl: 16 } as const;

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
  fast:   'all 150ms ease',
  normal: 'all 200ms ease',
  slow:   'all 300ms ease',
} as const;

export const REDUCED_MOTION_CSS = `@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:0.01ms!important;transition-duration:0.01ms!important}}`;

export const ELEVATION = (dark: boolean) => ({
  none: 'none',
  sm: dark ? 'none' : '0 1px 2px rgba(0,0,0,0.05)',
  md: dark ? 'none' : '0 2px 6px rgba(0,0,0,0.06)',
  lg: dark ? 'none' : '0 4px 16px rgba(0,0,0,0.08)',
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

// ── shadcn zinc palette ────────────────────────────────────────────────
export const getColors = (darkMode: boolean) => ({
  pageBg:    darkMode ? '#09090b' : '#f5f5f5',
  cardBg:    darkMode ? '#18181b' : '#ffffff',
  cardHover: darkMode ? '#1e1e22' : '#fafafa',
  innerBg:   darkMode ? '#0f0f12' : '#f5f5f5',

  border:      darkMode ? '#27272a' : '#e5e5e5',
  borderHover: darkMode ? '#3f3f46' : '#d4d4d4',

  textPrimary:   darkMode ? '#fafafa' : '#09090b',
  textSecondary: darkMode ? '#a3a3a3' : '#525252',
  textMuted:     darkMode ? '#737373' : '#a3a3a3',

  accent:  darkMode ? '#a78bfa' : '#7c3aed',
  success: darkMode ? '#4ade80' : '#22c55e',
  warning: darkMode ? '#fbbf24' : '#f59e0b',
  error:   darkMode ? '#f87171' : '#ef4444',
  purple:  darkMode ? '#c4b5fd' : '#8b5cf6',

  successBg: darkMode ? 'rgba(74,222,128,0.08)' : 'rgba(34,197,94,0.08)',
  warningBg: darkMode ? 'rgba(251,191,36,0.08)' : 'rgba(245,158,11,0.08)',
  errorBg:   darkMode ? 'rgba(248,113,113,0.08)' : 'rgba(239,68,68,0.08)',
  accentBg:  darkMode ? 'rgba(167,139,250,0.08)' : 'rgba(124,58,237,0.06)',

  shadowSm: darkMode ? 'none' : '0 1px 2px rgba(0,0,0,0.05)',
  shadowMd: darkMode ? 'none' : '0 2px 6px rgba(0,0,0,0.06)',
  shadowLg: darkMode ? 'none' : '0 4px 16px rgba(0,0,0,0.08)',

  accentGradient:  darkMode ? 'linear-gradient(135deg,#a78bfa,#7c3aed)' : 'linear-gradient(135deg,#7c3aed,#6d28d9)',
  successGradient: darkMode ? 'linear-gradient(135deg,#4ade80,#22c55e)' : 'linear-gradient(135deg,#22c55e,#16a34a)',
  warningGradient: darkMode ? 'linear-gradient(135deg,#fbbf24,#f59e0b)' : 'linear-gradient(135deg,#f59e0b,#d97706)',
  errorGradient:   darkMode ? 'linear-gradient(135deg,#f87171,#ef4444)' : 'linear-gradient(135deg,#ef4444,#dc2626)',
  purpleGradient:  darkMode ? 'linear-gradient(135deg,#c4b5fd,#8b5cf6)' : 'linear-gradient(135deg,#8b5cf6,#7c3aed)',
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
