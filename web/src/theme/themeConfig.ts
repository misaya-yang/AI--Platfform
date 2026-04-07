import type { ThemeConfig } from 'antd';

// Unified Design System — Indigo-blue primary with consistent tokens

export const colors = {
  // Primary - Indigo Blue (unified brand color)
  primary: {
    50: '#EEF2FF',
    100: '#E0E7FF',
    200: '#C7D2FE',
    300: '#A5B4FC',
    400: '#818CF8',
    500: '#6366F1',
    600: '#4F46E5',
    700: '#4338CA',
    800: '#3730A3',
    900: '#312E81',
  },
  // Neutral - Slate Gray
  neutral: {
    50: '#F8FAFC',
    100: '#F1F5F9',
    200: '#E2E8F0',
    300: '#CBD5E1',
    400: '#94A3B8',
    500: '#64748B',
    600: '#475569',
    700: '#334155',
    800: '#1E293B',
    900: '#0F172A',
    950: '#020617',
  },
};

// Shared component tokens
const sharedComponents: ThemeConfig['components'] = {
  Button: {
    borderRadius: 8,
    primaryShadow: 'none',
  },
  Card: {
    borderRadiusLG: 12,
  },
  Input: {
    borderRadius: 8,
  },
  Select: {
    borderRadius: 8,
  },
  Table: {
    borderRadius: 8,
  },
  Modal: {
    borderRadiusLG: 16,
  },
  Tag: {
    borderRadiusSM: 6,
  },
};

// Light theme configuration
export const lightTheme: ThemeConfig = {
  token: {
    colorPrimary: '#4F46E5',
    colorInfo: '#6366F1',
    colorSuccess: '#059669',
    colorWarning: '#D97706',
    colorError: '#DC2626',
    fontFamily: '"IBM Plex Sans", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif',
    fontFamilyCode: '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Menlo, monospace',

    colorBgContainer: '#FFFFFF',
    colorBgElevated: '#FFFFFF',
    colorBgLayout: '#F8FAFC',

    colorText: '#0F172A',
    colorTextSecondary: '#475569',
    colorTextTertiary: '#64748B',

    colorBorder: '#E2E8F0',
    colorBorderSecondary: '#F1F5F9',

    borderRadius: 8,
    borderRadiusLG: 12,
    borderRadiusSM: 6,
    borderRadiusXS: 4,

    boxShadow: 'none',
    boxShadowSecondary: '0 4px 6px -1px rgba(0, 0, 0, 0.07), 0 2px 4px -2px rgba(0, 0, 0, 0.05)',
  },
  components: {
    ...sharedComponents,
    Menu: {
      itemBg: 'transparent',
      itemSelectedBg: '#EEF2FF',
      itemSelectedColor: '#4F46E5',
      itemHoverBg: '#F8FAFC',
      itemBorderRadius: 8,
    },
  },
};

// Dark theme configuration
export const darkTheme: ThemeConfig = {
  token: {
    colorPrimary: '#818CF8',
    colorInfo: '#818CF8',
    colorSuccess: '#34D399',
    colorWarning: '#FBBF24',
    colorError: '#F87171',
    fontFamily: '"IBM Plex Sans", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif',
    fontFamilyCode: '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Menlo, monospace',

    colorBgContainer: '#1A1D27',
    colorBgElevated: '#1E2130',
    colorBgLayout: '#0F1117',

    colorText: '#E2E8F0',
    colorTextSecondary: '#94A3B8',
    colorTextTertiary: '#64748B',

    colorBorder: 'rgba(255, 255, 255, 0.08)',
    colorBorderSecondary: 'rgba(255, 255, 255, 0.05)',

    borderRadius: 8,
    borderRadiusLG: 12,
    borderRadiusSM: 6,
    borderRadiusXS: 4,

    boxShadow: 'none',
    boxShadowSecondary: '0 4px 6px -1px rgba(0, 0, 0, 0.3)',
  },
  components: {
    ...sharedComponents,
    Menu: {
      itemBg: 'transparent',
      itemSelectedBg: 'rgba(129, 140, 248, 0.12)',
      itemSelectedColor: '#A5B4FC',
      itemHoverBg: 'rgba(255, 255, 255, 0.04)',
      itemBorderRadius: 8,
    },
  },
};

export default { lightTheme, darkTheme, colors };
