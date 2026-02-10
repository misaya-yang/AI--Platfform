// web/src/pages/dashboard/styles.ts
// Unified Dashboard Layout System - Consistent spacing and styling

/**
 * Dashboard Layout Constants
 *
 * All spacing values are based on a 4px grid system:
 * - xs: 4px   (1 unit)
 * - sm: 8px   (2 units)
 * - md: 12px  (3 units)
 * - base: 16px (4 units) - Primary spacing
 * - lg: 20px  (5 units)
 * - xl: 24px  (6 units) - Section spacing
 */
export const LAYOUT = {
  // Grid system
  GRID_GAP: 16,           // Consistent gap between all grid items
  SECTION_GAP: 24,        // Gap between major sections (KPI, Providers, Panels)
  CARD_PADDING: 20,       // Inner padding for cards
  CARD_RADIUS: 12,        // Border radius for cards
  PAGE_PADDING: 24,       // Page container padding

  // Grid columns
  KPI_COLUMNS: 5,         // Number of KPI cards
  PROVIDER_COLUMNS: 5,    // Number of provider cards per row
  FIVE_COL_MIN_ITEM_WIDTH: 210, // Prevent cards from over-shrinking on narrow screens
  DASHBOARD_MIN_CONTENT_WIDTH: 1180, // Trigger horizontal scroll before text is crushed

  // Breakpoints for responsive
  BREAKPOINTS: {
    sm: 640,
    md: 768,
    lg: 1024,
    xl: 1280,
  },
} as const;

/**
 * Generate dark/light mode colors and shadows
 */
export const getColors = (darkMode: boolean) => ({
  // Backgrounds
  pageBg: darkMode ? "#0f172a" : "#f1f5f9",
  cardBg: darkMode ? "#1e293b" : "#ffffff",
  innerBg: darkMode ? "#0f172a" : "#f8fafc",

  // Borders
  border: darkMode ? "#334155" : "#e2e8f0",
  borderHover: darkMode ? "#475569" : "#cbd5e1",

  // Text
  textPrimary: darkMode ? "#f1f5f9" : "#1e293b",
  textSecondary: darkMode ? "#94a3b8" : "#64748b",
  textMuted: darkMode ? "#64748b" : "#94a3b8",

  // Accents
  accent: "#3b82f6",
  success: "#10b981",
  warning: "#f59e0b",
  error: "#ef4444",
  purple: "#8b5cf6",

  // Shadows
  shadowSm: darkMode 
    ? "0 1px 3px 0 rgba(0, 0, 0, 0.4)" 
    : "0 1px 3px 0 rgba(0, 0, 0, 0.05), 0 1px 2px -1px rgba(0, 0, 0, 0.05)",
  shadowMd: darkMode 
    ? "0 4px 12px -2px rgba(0, 0, 0, 0.5), 0 2px 6px -1px rgba(0, 0, 0, 0.3)" 
    : "0 4px 12px -2px rgba(0, 0, 0, 0.08), 0 2px 6px -1px rgba(0, 0, 0, 0.04)",
  shadowLg: darkMode
    ? "0 12px 24px -4px rgba(0, 0, 0, 0.6), 0 8px 16px -4px rgba(0, 0, 0, 0.4)"
    : "0 12px 24px -4px rgba(0, 0, 0, 0.12), 0 8px 16px -4px rgba(0, 0, 0, 0.08)",
  
  // Gradients
  accentGradient: "linear-gradient(135deg, #3b82f6 0%, #2563eb 100%)",
  successGradient: "linear-gradient(135deg, #10b981 0%, #059669 100%)",
  warningGradient: "linear-gradient(135deg, #f59e0b 0%, #d97706 100%)",
  errorGradient: "linear-gradient(135deg, #ef4444 0%, #dc2626 100%)",
  purpleGradient: "linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%)",
});

/**
 * Common styles for consistency
 */
export const commonStyles = {
  transition: "all 0.2s cubic-bezier(0.4, 0, 0.2, 1)",
  cardHeaderHeight: 48,
};

/**
 * CSS Grid styles for consistent 5-column layouts
 */
export const gridStyles = {
  // 5-column grid for KPI and Provider cards
  fiveColumn: {
    display: "grid" as const,
    gridTemplateColumns: "repeat(5, 1fr)",
    gap: LAYOUT.GRID_GAP,
  },

  // Responsive 5-column that stacks on mobile
  fiveColumnResponsive: {
    display: "grid" as const,
    gridTemplateColumns: `repeat(${LAYOUT.KPI_COLUMNS}, minmax(${LAYOUT.FIVE_COL_MIN_ITEM_WIDTH}px, 1fr))`,
    minWidth: `${LAYOUT.KPI_COLUMNS * LAYOUT.FIVE_COL_MIN_ITEM_WIDTH + (LAYOUT.KPI_COLUMNS - 1) * LAYOUT.GRID_GAP}px`,
    gap: LAYOUT.GRID_GAP,
  },
};

/**
 * Card base styles
 */
export const getCardStyles = (darkMode: boolean) => {
  const colors = getColors(darkMode);
  return {
    background: colors.cardBg,
    border: `1px solid ${colors.border}`,
    borderRadius: LAYOUT.CARD_RADIUS,
    padding: LAYOUT.CARD_PADDING,
  };
};

/**
 * Section wrapper styles (for KPI, Providers, etc.)
 */
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
