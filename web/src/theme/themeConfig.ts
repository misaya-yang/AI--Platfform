import type { ThemeConfig } from 'antd';

// ─── Linear / Vercel–grade Design System ───────────────────────────────
// Single primary hue (indigo-600), strict 4-variant tag system,
// layered surface hierarchy, restrained palette.

export const colors = {
  primary: {
    50: '#eef2ff', 100: '#e0e7ff', 200: '#c7d2fe', 300: '#a5b4fc',
    400: '#818cf8', 500: '#6366f1', 600: '#4f46e5', 700: '#4338ca',
    800: '#3730a3', 900: '#312e81',
  },
  neutral: {
    50: '#f8f9fb', 100: '#f1f3f5', 200: '#e5e7eb', 300: '#d1d5db',
    400: '#9ca3af', 500: '#6b7280', 600: '#4b5563', 700: '#374151',
    800: '#1f2937', 900: '#111827', 950: '#030712',
  },
};

const sharedComponents: ThemeConfig['components'] = {
  Button: { borderRadius: 8, primaryShadow: 'none' },
  Card: { borderRadiusLG: 12 },
  Input: { borderRadius: 8 },
  Select: { borderRadius: 8 },
  Table: { borderRadius: 8 },
  Modal: { borderRadiusLG: 12 },
  Tag: { borderRadiusSM: 6 },
};

export const lightTheme: ThemeConfig = {
  token: {
    colorPrimary: '#4f46e5',
    colorInfo: '#6366f1',
    colorSuccess: '#059669',
    colorWarning: '#d97706',
    colorError: '#dc2626',
    fontFamily: '"IBM Plex Sans", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
    fontFamilyCode: '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Menlo, monospace',
    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    colorBgLayout: '#f8f9fb',
    colorText: '#111827',
    colorTextSecondary: '#4b5563',
    colorTextTertiary: '#6b7280',
    colorBorder: '#e5e7eb',
    colorBorderSecondary: '#f1f3f5',
    borderRadius: 8,
    borderRadiusLG: 12,
    borderRadiusSM: 6,
    borderRadiusXS: 4,
    boxShadow: 'none',
    boxShadowSecondary: '0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.06)',
  },
  components: {
    ...sharedComponents,
    Menu: {
      itemBg: 'transparent',
      itemSelectedBg: 'rgba(79,70,229,0.06)',
      itemSelectedColor: '#4f46e5',
      itemHoverBg: '#f8f9fb',
      itemBorderRadius: 8,
    },
  },
};

export const darkTheme: ThemeConfig = {
  token: {
    colorPrimary: '#818cf8',
    colorInfo: '#818cf8',
    colorSuccess: '#34d399',
    colorWarning: '#fbbf24',
    colorError: '#f87171',
    fontFamily: '"IBM Plex Sans", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
    fontFamilyCode: '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Menlo, monospace',
    // ── Linear-style layered surfaces ──
    colorBgContainer: '#16161f',
    colorBgElevated: '#1c1c28',
    colorBgLayout: '#0a0a0f',
    colorText: '#e8e8ed',
    colorTextSecondary: '#8b8b9e',
    colorTextTertiary: '#5c5c6f',
    colorBorder: 'rgba(255,255,255,0.06)',
    colorBorderSecondary: 'rgba(255,255,255,0.04)',
    borderRadius: 8,
    borderRadiusLG: 12,
    borderRadiusSM: 6,
    borderRadiusXS: 4,
    boxShadow: 'none',
    boxShadowSecondary: '0 1px 3px rgba(0,0,0,0.4)',
  },
  components: {
    ...sharedComponents,
    Menu: {
      itemBg: 'transparent',
      itemSelectedBg: 'rgba(129,140,248,0.08)',
      itemSelectedColor: '#a5b4fc',
      itemHoverBg: 'rgba(255,255,255,0.03)',
      itemBorderRadius: 8,
    },
  },
};

export default { lightTheme, darkTheme, colors };
