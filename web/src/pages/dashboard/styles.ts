// web/src/pages/dashboard/styles.ts
// Unified Dashboard Design Token System

// -- Spacing (4px grid) --
export const SPACING = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 20,
  '2xl': 24,
  '3xl': 32,
  '4xl': 40,
} as const;

// -- Radius --
export const RADIUS = {
  sm: 6,
  md: 8,
  lg: 12,
  xl: 16,
} as const;

// -- Typography --
export const TYPOGRAPHY = {
  pageTitle:    { fontSize: 24, fontWeight: 700 },
  sectionTitle: { fontSize: 18, fontWeight: 600 },
  cardLabel:    { fontSize: 13, fontWeight: 600, letterSpacing: '0.5px', textTransform: 'uppercase' as const },
  kpiValue:     { fontSize: 32, fontWeight: 700, fontFeatureSettings: '"tnum"' },
  kpiUnit:      { fontSize: 14, fontWeight: 500 },
  body:         { fontSize: 14, fontWeight: 400 },
  caption:      { fontSize: 12, fontWeight: 500 },
} as const;

// -- Transitions --
export const TRANSITION = {
  fast:   'all 150ms cubic-bezier(0.4, 0, 0.2, 1)',
  normal: 'all 200ms cubic-bezier(0.4, 0, 0.2, 1)',
  slow:   'all 300ms cubic-bezier(0.4, 0, 0.2, 1)',
} as const;

// -- Reduced motion CSS --
export const REDUCED_MOTION_CSS = `
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
`;

// -- Elevation / Shadows --
export const ELEVATION = (dark: boolean) => ({
  none: 'none',
  sm: dark ? '0 1px 2px rgba(0,0,0,0.3)' : '0 1px 2px rgba(0,0,0,0.05)',
  md: dark ? '0 2px 8px rgba(0,0,0,0.4)' : '0 2px 8px rgba(0,0,0,0.08)',
  lg: dark ? '0 4px 16px rgba(0,0,0,0.5)' : '0 4px 16px rgba(0,0,0,0.1)',
});

// -- Layout constants --
export const LAYOUT = {
  CARD_GAP: 12,
  CARD_PADDING: 14,
  CARD_RADIUS: 12,
  PAGE_PADDING: 12,
  KPI_HEIGHT: 120,
  PANEL_MIN_HEIGHT: 400,
  DASHBOARD_MIN_CONTENT_WIDTH: 1180,

  GRID_GAP: 16,
  SECTION_GAP: 24,
  KPI_COLUMNS: 5,
  PROVIDER_COLUMNS: 5,
  FIVE_COL_MIN_ITEM_WIDTH: 210,

  BREAKPOINTS: {
    sm: 640,
    md: 768,
    lg: 1024,
    xl: 1280,
  },
} as const;

// -- Color system (Indigo-based, WCAG AA compliant) --
export const getColors = (darkMode: boolean) => ({
  // Backgrounds — deep gray dark (not pure black)
  pageBg:    darkMode ? '#0F1117' : '#F8FAFC',
  cardBg:    darkMode ? '#1A1D27' : '#FFFFFF',
  cardHover: darkMode ? '#1E2130' : '#F8FAFC',
  innerBg:   darkMode ? '#151820' : '#F8FAFC',

  // Borders
  border:      darkMode ? 'rgba(255,255,255,0.08)' : '#E2E8F0',
  borderHover: darkMode ? 'rgba(255,255,255,0.14)' : '#CBD5E1',

  // Text — WCAG AA
  textPrimary:   darkMode ? '#E2E8F0' : '#0F172A',
  textSecondary: darkMode ? '#94A3B8' : '#475569',
  textMuted:     darkMode ? '#64748B' : '#94A3B8',

  // Semantic colors — Indigo primary
  accent:  darkMode ? '#818CF8' : '#4F46E5',
  success: darkMode ? '#34D399' : '#059669',
  warning: darkMode ? '#FBBF24' : '#D97706',
  error:   darkMode ? '#F87171' : '#DC2626',
  purple:  darkMode ? '#A78BFA' : '#7C3AED',

  // Semantic backgrounds (subtle)
  successBg: darkMode ? 'rgba(52,211,153,0.10)' : '#ECFDF5',
  warningBg: darkMode ? 'rgba(251,191,36,0.10)' : '#FFFBEB',
  errorBg:   darkMode ? 'rgba(248,113,113,0.10)' : '#FEF2F2',
  accentBg:  darkMode ? 'rgba(129,140,248,0.10)' : '#EEF2FF',

  // Shadows
  shadowSm: darkMode
    ? '0 1px 2px rgba(0,0,0,0.4)'
    : '0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.02)',
  shadowMd: darkMode
    ? '0 2px 8px rgba(0,0,0,0.5)'
    : '0 4px 12px rgba(0,0,0,0.06)',
  shadowLg: darkMode
    ? '0 4px 16px rgba(0,0,0,0.6)'
    : '0 8px 25px rgba(0,0,0,0.08)',

  // Gradients
  accentGradient:  darkMode ? 'linear-gradient(135deg, #818CF8 0%, #6366F1 100%)' : 'linear-gradient(135deg, #4F46E5 0%, #4338CA 100%)',
  successGradient: darkMode ? 'linear-gradient(135deg, #34D399 0%, #059669 100%)' : 'linear-gradient(135deg, #059669 0%, #047857 100%)',
  warningGradient: darkMode ? 'linear-gradient(135deg, #FBBF24 0%, #D97706 100%)' : 'linear-gradient(135deg, #D97706 0%, #B45309 100%)',
  errorGradient:   darkMode ? 'linear-gradient(135deg, #F87171 0%, #DC2626 100%)' : 'linear-gradient(135deg, #DC2626 0%, #B91C1C 100%)',
  purpleGradient:  darkMode ? 'linear-gradient(135deg, #A78BFA 0%, #7C3AED 100%)' : 'linear-gradient(135deg, #7C3AED 0%, #6D28D9 100%)',
});

// -- Common styles --
export const commonStyles = {
  transition: TRANSITION.normal,
  cardHeaderHeight: 48,
};

// -- Grid styles --
export const gridStyles = {
  fiveColumn: {
    display: 'grid' as const,
    gridTemplateColumns: 'repeat(5, 1fr)',
    gap: LAYOUT.GRID_GAP,
  },
  fiveColumnResponsive: {
    display: 'grid' as const,
    gridTemplateColumns: `repeat(auto-fit, minmax(${LAYOUT.FIVE_COL_MIN_ITEM_WIDTH}px, 1fr))`,
    gap: LAYOUT.GRID_GAP,
  },
};

// -- Card base styles --
export const getCardStyles = (darkMode: boolean) => {
  const colors = getColors(darkMode);
  return {
    background: colors.cardBg,
    border: `1px solid ${colors.border}`,
    borderRadius: LAYOUT.CARD_RADIUS,
    padding: LAYOUT.CARD_PADDING,
  };
};

// -- Section wrapper styles --
export const getSectionStyles = (darkMode: boolean) => {
  const colors = getColors(darkMode);
  return {
    marginBottom: LAYOUT.SECTION_GAP,
    background: colors.cardBg,
    border: `1px solid ${colors.border}`,
    borderRadius: LAYOUT.CARD_RADIUS,
    padding: LAYOUT.CARD_PADDING,
  };
};
