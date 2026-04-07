// web/src/pages/dashboard/styles.ts
// Linear-grade Dashboard Design Token System

export const SPACING = {
  xs: 4, sm: 8, md: 12, lg: 16, xl: 20, '2xl': 24, '3xl': 32, '4xl': 40,
} as const;

export const RADIUS = { sm: 6, md: 8, lg: 12, xl: 16 } as const;

export const TYPOGRAPHY = {
  pageTitle:    { fontSize: 22, fontWeight: 600 },
  sectionTitle: { fontSize: 16, fontWeight: 600 },
  cardLabel:    { fontSize: 12, fontWeight: 500, letterSpacing: '0.03em', textTransform: 'uppercase' as const },
  kpiValue:     { fontSize: 28, fontWeight: 700, fontFeatureSettings: '"tnum"' },
  kpiUnit:      { fontSize: 13, fontWeight: 500 },
  body:         { fontSize: 14, fontWeight: 400 },
  caption:      { fontSize: 12, fontWeight: 500 },
} as const;

export const TRANSITION = {
  fast:   'all 150ms cubic-bezier(0.4, 0, 0.2, 1)',
  normal: 'all 200ms cubic-bezier(0.4, 0, 0.2, 1)',
  slow:   'all 300ms cubic-bezier(0.4, 0, 0.2, 1)',
} as const;

export const REDUCED_MOTION_CSS = `
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
`;

export const ELEVATION = (dark: boolean) => ({
  none: 'none',
  sm: dark ? 'none' : '0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.06)',
  md: dark ? 'none' : '0 2px 8px rgba(0,0,0,0.06)',
  lg: dark ? 'none' : '0 8px 24px rgba(0,0,0,0.08)',
});

export const LAYOUT = {
  CARD_GAP: 12,
  CARD_PADDING: 16,
  CARD_RADIUS: 12,
  PAGE_PADDING: 0,
  KPI_HEIGHT: 'auto' as unknown as number, // auto height for KPI cards
  PANEL_MIN_HEIGHT: 400,
  DASHBOARD_MIN_CONTENT_WIDTH: 1060,
  GRID_GAP: 16,
  SECTION_GAP: 20,
  KPI_COLUMNS: 5,
  PROVIDER_COLUMNS: 5,
  FIVE_COL_MIN_ITEM_WIDTH: 200,
  BREAKPOINTS: { sm: 640, md: 768, lg: 1024, xl: 1280 },
} as const;

// ── Linear-grade color system ──────────────────────────────────────────
export const getColors = (darkMode: boolean) => ({
  // Surfaces — Linear-style layered depth
  pageBg:    darkMode ? '#0a0a0f' : '#f8f9fb',
  cardBg:    darkMode ? '#16161f' : '#ffffff',
  cardHover: darkMode ? '#1c1c28' : '#f8f9fb',
  innerBg:   darkMode ? '#111118' : '#f3f4f6',

  // Borders — barely visible, layer separation only
  border:      darkMode ? 'rgba(255,255,255,0.06)' : '#e5e7eb',
  borderHover: darkMode ? 'rgba(255,255,255,0.10)' : '#d1d5db',

  // Text — strict 3-level hierarchy
  textPrimary:   darkMode ? '#e8e8ed' : '#111827',
  textSecondary: darkMode ? '#8b8b9e' : '#4b5563',
  textMuted:     darkMode ? '#5c5c6f' : '#9ca3af',

  // Semantic — 1 primary + 4 semantic only
  accent:  darkMode ? '#818cf8' : '#4f46e5',
  success: darkMode ? '#34d399' : '#059669',
  warning: darkMode ? '#fbbf24' : '#d97706',
  error:   darkMode ? '#f87171' : '#dc2626',
  purple:  darkMode ? '#a78bfa' : '#7c3aed',

  // Subtle backgrounds for tags/badges
  successBg: darkMode ? 'rgba(52,211,153,0.08)' : 'rgba(5,150,105,0.06)',
  warningBg: darkMode ? 'rgba(251,191,36,0.08)' : 'rgba(217,119,6,0.06)',
  errorBg:   darkMode ? 'rgba(248,113,113,0.08)' : 'rgba(220,38,38,0.06)',
  accentBg:  darkMode ? 'rgba(129,140,248,0.08)' : 'rgba(79,70,229,0.06)',

  // Shadows — dark mode uses border only, no shadow
  shadowSm: darkMode ? 'none' : '0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.06)',
  shadowMd: darkMode ? 'none' : '0 2px 8px rgba(0,0,0,0.06)',
  shadowLg: darkMode ? 'none' : '0 8px 24px rgba(0,0,0,0.08)',

  // Gradients (kept for chart use only)
  accentGradient:  darkMode ? 'linear-gradient(135deg, #818cf8, #6366f1)' : 'linear-gradient(135deg, #4f46e5, #4338ca)',
  successGradient: darkMode ? 'linear-gradient(135deg, #34d399, #059669)' : 'linear-gradient(135deg, #059669, #047857)',
  warningGradient: darkMode ? 'linear-gradient(135deg, #fbbf24, #d97706)' : 'linear-gradient(135deg, #d97706, #b45309)',
  errorGradient:   darkMode ? 'linear-gradient(135deg, #f87171, #dc2626)' : 'linear-gradient(135deg, #dc2626, #b91c1c)',
  purpleGradient:  darkMode ? 'linear-gradient(135deg, #a78bfa, #7c3aed)' : 'linear-gradient(135deg, #7c3aed, #6d28d9)',
});

export const commonStyles = { transition: TRANSITION.normal, cardHeaderHeight: 48 };

export const gridStyles = {
  fiveColumn: { display: 'grid' as const, gridTemplateColumns: 'repeat(5, 1fr)', gap: LAYOUT.GRID_GAP },
  fiveColumnResponsive: {
    display: 'grid' as const,
    gridTemplateColumns: `repeat(auto-fit, minmax(${LAYOUT.FIVE_COL_MIN_ITEM_WIDTH}px, 1fr))`,
    gap: LAYOUT.GRID_GAP,
  },
};

export const getCardStyles = (darkMode: boolean) => {
  const c = getColors(darkMode);
  return {
    background: c.cardBg,
    border: `1px solid ${c.border}`,
    borderRadius: LAYOUT.CARD_RADIUS,
    padding: LAYOUT.CARD_PADDING,
  };
};

export const getSectionStyles = (darkMode: boolean) => {
  const c = getColors(darkMode);
  return {
    marginBottom: LAYOUT.SECTION_GAP,
    background: c.cardBg,
    border: `1px solid ${c.border}`,
    borderRadius: LAYOUT.CARD_RADIUS,
    padding: LAYOUT.CARD_PADDING,
  };
};
