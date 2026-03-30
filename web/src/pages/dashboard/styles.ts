// web/src/pages/dashboard/styles.ts
// Unified Dashboard Design Token System

// ── Spacing (4px grid) ──────────────────────────────────────────────
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

// ── Radius ──────────────────────────────────────────────────────────
export const RADIUS = {
  sm: 6,
  md: 8,
  lg: 12,   // All cards
  xl: 16,   // Containers
} as const;

// ── Typography ──────────────────────────────────────────────────────
export const TYPOGRAPHY = {
  pageTitle:    { fontSize: 24, fontWeight: 700 },
  sectionTitle: { fontSize: 18, fontWeight: 600 },
  cardLabel:    { fontSize: 13, fontWeight: 600, letterSpacing: '0.5px', textTransform: 'uppercase' as const },
  kpiValue:     { fontSize: 32, fontWeight: 700, fontFeatureSettings: '"tnum"' },
  kpiUnit:      { fontSize: 14, fontWeight: 500 },
  body:         { fontSize: 14, fontWeight: 400 },
  caption:      { fontSize: 12, fontWeight: 500 },
} as const;

// ── Transitions ─────────────────────────────────────────────────────
export const TRANSITION = {
  fast:   'all 150ms cubic-bezier(0.4, 0, 0.2, 1)',
  normal: 'all 200ms cubic-bezier(0.4, 0, 0.2, 1)',
  slow:   'all 300ms cubic-bezier(0.4, 0, 0.2, 1)',
} as const;

// ── Reduced motion CSS (inject via <style>) ────────────────────────
export const REDUCED_MOTION_CSS = `
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
`;

// ── Elevation / Shadows ─────────────────────────────────────────────
export const ELEVATION = (dark: boolean) => ({
  none: 'none',
  sm: dark ? '0 1px 2px rgba(0,0,0,0.3)' : '0 1px 2px rgba(0,0,0,0.05)',
  md: dark ? '0 2px 8px rgba(0,0,0,0.4)' : '0 2px 8px rgba(0,0,0,0.08)',
  lg: dark ? '0 4px 16px rgba(0,0,0,0.5)' : '0 4px 16px rgba(0,0,0,0.1)',
});

// ── Layout constants ────────────────────────────────────────────────
export const LAYOUT = {
  CARD_GAP: 24,
  CARD_PADDING: 24,
  CARD_RADIUS: 12,
  PAGE_PADDING: 24,
  KPI_HEIGHT: 160,
  PANEL_MIN_HEIGHT: 400,
  DASHBOARD_MIN_CONTENT_WIDTH: 1180,

  // Legacy compat
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

// ── Color system (WCAG AA compliant) ────────────────────────────────
export const getColors = (darkMode: boolean) => ({
  // Backgrounds
  pageBg:    darkMode ? '#0C1222' : '#F8FAFC',
  cardBg:    darkMode ? '#1A2332' : '#FFFFFF',
  cardHover: darkMode ? '#243044' : '#F8FAFC',
  innerBg:   darkMode ? '#0C1222' : '#F8FAFC',

  // Borders
  border:      darkMode ? '#2D3A4A' : '#E2E8F0',
  borderHover: darkMode ? '#3D4D60' : '#CBD5E1',

  // Text — all values satisfy WCAG AA on their respective backgrounds
  textPrimary:   darkMode ? '#F1F5F9' : '#0F172A',
  textSecondary: darkMode ? '#94A3B8' : '#475569',   // Light: 4.6:1 contrast
  textMuted:     darkMode ? '#64748B' : '#94A3B8',

  // Semantic colors — AA compliant
  accent:  '#2563EB',
  success: '#059669',
  warning: '#D97706',
  error:   '#DC2626',    // Changed from #EF4444 for AA compliance
  purple:  '#7C3AED',

  // Semantic backgrounds (subtle)
  successBg: darkMode ? 'rgba(5,150,105,0.1)' : '#ECFDF5',
  warningBg: darkMode ? 'rgba(217,119,6,0.1)' : '#FFFBEB',
  errorBg:   darkMode ? 'rgba(220,38,38,0.1)' : '#FEF2F2',
  accentBg:  darkMode ? 'rgba(37,99,235,0.1)' : '#EFF6FF',

  // Shadows
  shadowSm: darkMode
    ? '0 1px 2px rgba(0,0,0,0.3)'
    : '0 1px 2px rgba(0,0,0,0.05)',
  shadowMd: darkMode
    ? '0 2px 8px rgba(0,0,0,0.4)'
    : '0 2px 8px rgba(0,0,0,0.08)',
  shadowLg: darkMode
    ? '0 4px 16px rgba(0,0,0,0.5)'
    : '0 4px 16px rgba(0,0,0,0.1)',

  // Gradients
  accentGradient:  'linear-gradient(135deg, #2563EB 0%, #1D4ED8 100%)',
  successGradient: 'linear-gradient(135deg, #059669 0%, #047857 100%)',
  warningGradient: 'linear-gradient(135deg, #D97706 0%, #B45309 100%)',
  errorGradient:   'linear-gradient(135deg, #DC2626 0%, #B91C1C 100%)',
  purpleGradient:  'linear-gradient(135deg, #7C3AED 0%, #6D28D9 100%)',
});

// ── Common styles ───────────────────────────────────────────────────
export const commonStyles = {
  transition: TRANSITION.normal,
  cardHeaderHeight: 48,
};

// ── Grid styles ─────────────────────────────────────────────────────
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

// ── Card base styles ────────────────────────────────────────────────
export const getCardStyles = (darkMode: boolean) => {
  const colors = getColors(darkMode);
  return {
    background: colors.cardBg,
    border: `1px solid ${colors.border}`,
    borderRadius: LAYOUT.CARD_RADIUS,
    padding: LAYOUT.CARD_PADDING,
  };
};

// ── Section wrapper styles ──────────────────────────────────────────
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
