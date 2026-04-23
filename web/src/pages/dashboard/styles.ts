// Dashboard Design Tokens — aligned with GPT mockup
// Cool-neutral canvas · violet brand · per-metric accent colors · multi-hue charts

export const SPACING = {
  xs: 4, sm: 8, md: 12, lg: 16, xl: 20, '2xl': 24, '3xl': 32, '4xl': 40, '5xl': 56, '6xl': 72,
} as const;

export const RADIUS = { sm: 6, md: 10, lg: 14 } as const;

export const TYPOGRAPHY = {
  pageTitle:    { fontSize: 22, fontWeight: 600, letterSpacing: '-0.02em', lineHeight: 1.2 },
  sectionTitle: { fontSize: 15, fontWeight: 600, letterSpacing: '-0.01em' },
  eyebrow:      { fontSize: 10, fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase' as const },
  cardLabel:    { fontSize: 13, fontWeight: 500 },
  kpiValue:     { fontSize: 28, fontWeight: 600, fontFeatureSettings: '"tnum"', letterSpacing: '-0.02em' },
  kpiUnit:      { fontSize: 13, fontWeight: 500 },
  body:         { fontSize: 13, fontWeight: 400 },
  caption:      { fontSize: 12, fontWeight: 500 },
  mono:         { fontFamily: '"IBM Plex Mono", "JetBrains Mono", SFMono-Regular, Menlo, monospace', fontFeatureSettings: '"tnum"' },
} as const;

export const TRANSITION = {
  fast:   'all 140ms cubic-bezier(0.16, 1, 0.3, 1)',
  normal: 'all 200ms cubic-bezier(0.16, 1, 0.3, 1)',
  slow:   'all 320ms cubic-bezier(0.16, 1, 0.3, 1)',
} as const;

export const REDUCED_MOTION_CSS = `@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:0.01ms!important;transition-duration:0.01ms!important}}`;

export const ELEVATION = (dark: boolean) => ({
  none: 'none',
  sm: dark ? 'none' : '0 1px 2px rgba(17, 24, 39, 0.05)',
  md: dark ? 'none' : '0 2px 10px rgba(17, 24, 39, 0.06)',
  lg: dark ? 'none' : '0 6px 24px rgba(17, 24, 39, 0.08)',
});

export const LAYOUT = {
  CARD_GAP: 14,
  CARD_PADDING: 18,
  CARD_RADIUS: 12,
  PAGE_PADDING: 0,
  KPI_HEIGHT: 'auto' as unknown as number,
  PANEL_MIN_HEIGHT: 400,
  DASHBOARD_MIN_CONTENT_WIDTH: 1060,
  GRID_GAP: 14,
  SECTION_GAP: 16,
  KPI_COLUMNS: 5,
  PROVIDER_COLUMNS: 5,
  FIVE_COL_MIN_ITEM_WIDTH: 200,
  BREAKPOINTS: { sm: 640, md: 768, lg: 1024, xl: 1280 },
} as const;

// ── Palette — violet brand on cool-neutral canvas ───────────────────
export const getColors = (darkMode: boolean) => ({
  // Surfaces
  pageBg:    darkMode ? '#0b0f17' : '#f5f7fa',
  cardBg:    darkMode ? '#151a24' : '#ffffff',
  cardHover: darkMode ? '#1a202c' : '#f8fafc',
  innerBg:   darkMode ? '#1c2230' : '#f1f3f7',

  // Borders (hairlines)
  border:      darkMode ? '#242b38' : '#e5e7eb',
  borderHover: darkMode ? '#2f3745' : '#d1d5db',
  borderStrong: darkMode ? '#3a4252' : '#cbd5e1',

  // Ink
  textPrimary:   darkMode ? '#e5e7eb' : '#111827',
  textSecondary: darkMode ? '#9ca3af' : '#4b5563',
  textMuted:     darkMode ? '#6b7280' : '#9ca3af',

  // Brand (violet) — active nav, primary CTAs, focus rings
  accent:       darkMode ? '#a78bfa' : '#7c3aed',
  accentBright: darkMode ? '#c4b5fd' : '#8b5cf6',
  accentDeep:   darkMode ? '#8b5cf6' : '#6d28d9',

  // Semantic palette (KPI per-metric color + status)
  success: darkMode ? '#34d399' : '#10b981',
  info:    darkMode ? '#60a5fa' : '#3b82f6',
  warning: darkMode ? '#fbbf24' : '#f59e0b',
  error:   darkMode ? '#f87171' : '#ef4444',

  // Back-compat aliases
  gold:   darkMode ? '#fbbf24' : '#f59e0b',
  purple: darkMode ? '#a78bfa' : '#8b5cf6',

  // Tinted surface fills (KPI badge backgrounds, status chips)
  successBg: darkMode ? 'rgba(52,211,153,0.14)'  : 'rgba(16,185,129,0.10)',
  infoBg:    darkMode ? 'rgba(96,165,250,0.14)'  : 'rgba(59,130,246,0.10)',
  warningBg: darkMode ? 'rgba(251,191,36,0.14)'  : 'rgba(245,158,11,0.10)',
  errorBg:   darkMode ? 'rgba(248,113,113,0.14)' : 'rgba(239,68,68,0.10)',
  accentBg:  darkMode ? 'rgba(167,139,250,0.15)' : 'rgba(124,58,237,0.10)',
  purpleBg:  darkMode ? 'rgba(167,139,250,0.15)' : 'rgba(139,92,246,0.10)',

  shadowSm: darkMode ? 'none' : '0 1px 2px rgba(17,24,39,0.05)',
  shadowMd: darkMode ? 'none' : '0 2px 10px rgba(17,24,39,0.06)',
  shadowLg: darkMode ? 'none' : '0 6px 24px rgba(17,24,39,0.08)',

  // Legacy gradient keys (kept for back-compat with other panels)
  accentGradient:  darkMode ? 'linear-gradient(135deg,#a78bfa,#7c3aed)' : 'linear-gradient(135deg,#8b5cf6,#6d28d9)',
  successGradient: darkMode ? 'linear-gradient(135deg,#34d399,#10b981)' : 'linear-gradient(135deg,#10b981,#059669)',
  warningGradient: darkMode ? 'linear-gradient(135deg,#fbbf24,#f59e0b)' : 'linear-gradient(135deg,#f59e0b,#d97706)',
  errorGradient:   darkMode ? 'linear-gradient(135deg,#f87171,#ef4444)' : 'linear-gradient(135deg,#ef4444,#dc2626)',
  purpleGradient:  darkMode ? 'linear-gradient(135deg,#c4b5fd,#a78bfa)' : 'linear-gradient(135deg,#a78bfa,#7c3aed)',
});

// ── KPI per-metric color map (matches GPT mockup icon badges) ───────
export const getKpiAccents = (darkMode: boolean) => {
  const c = getColors(darkMode);
  return {
    requests: { fg: c.purple,  bg: c.purpleBg  },
    cost:     { fg: c.success, bg: c.successBg },
    latency:  { fg: c.info,    bg: c.infoBg    },
    success:  { fg: c.success, bg: c.successBg },
    tokens:   { fg: c.accent,  bg: c.accentBg  },
  };
};

// ── Chart series palette — multi-hue brand colors (donut, stacks) ───
// Order matches GPT mockup: orange (DashScope-ish), blue (Gemini),
// green (Vertex/google), slate (others), violet (Anthropic/extras), amber.
export const getChartPalette = (darkMode: boolean) => darkMode ? [
  '#fb923c', // orange-400
  '#60a5fa', // blue-400
  '#34d399', // emerald-400
  '#94a3b8', // slate-400
  '#a78bfa', // violet-400
  '#fbbf24', // amber-400
] : [
  '#f97316', // orange-500
  '#3b82f6', // blue-500
  '#10b981', // emerald-500
  '#94a3b8', // slate-400
  '#8b5cf6', // violet-500
  '#f59e0b', // amber-500
];

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
