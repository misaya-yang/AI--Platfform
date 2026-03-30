import type { ThemeConfig } from 'antd';

// Industrial editorial theme tokens

export const colors = {
  // Primary - Blue
  primary: {
    50: '#EFF6FF',
    100: '#DBEAFE',
    200: '#BFDBFE',
    300: '#93C5FD',
    400: '#60A5FA',
    500: '#3B82F6',
    600: '#2563EB',
    700: '#1D4ED8',
    800: '#1E40AF',
    900: '#1E3A8A',
  },
  // Neutral - Gray
  neutral: {
    50: '#F9FAFB',
    100: '#F3F4F6',
    200: '#E5E7EB',
    300: '#D1D5DB',
    400: '#9CA3AF',
    500: '#6B7280',
    600: '#4B5563',
    700: '#374151',
    800: '#1F2937',
    900: '#111827',
    950: '#030712',
  },
};

// Light theme configuration
export const lightTheme: ThemeConfig = {
  token: {
    colorPrimary: '#2563EB',
    colorInfo: '#3B82F6',
    colorSuccess: '#22C55E',
    colorWarning: '#D97706',
    colorError: '#EF4444',
    fontFamily: '"IBM Plex Sans", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif',
    fontFamilyCode: '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Menlo, monospace',

    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    colorBgLayout: '#F1F5F9',

    colorText: '#1E293B',
    colorTextSecondary: '#475569',
    colorTextTertiary: '#64748B',

    colorBorder: '#CBD5E1',
    colorBorderSecondary: '#E2E8F0',

    borderRadius: 8,
    borderRadiusLG: 8,
    borderRadiusSM: 6,
    borderRadiusXS: 4,

    boxShadow: 'none',
    boxShadowSecondary: '0 4px 6px -1px rgba(0, 0, 0, 0.1)',
  },
  components: {
    Button: {
      borderRadius: 6,
      primaryShadow: 'none',
    },
    Card: {
      borderRadiusLG: 8,
    },
    Menu: {
      itemBg: 'transparent',
      itemSelectedBg: '#EFF6FF',
      itemSelectedColor: '#1D4ED8',
      itemHoverBg: '#F1F5F9',
      itemBorderRadius: 6,
    },
  },
};

// Dark theme configuration
export const darkTheme: ThemeConfig = {
  token: {
    colorPrimary: '#3B82F6',
    colorInfo: '#3B82F6',
    colorSuccess: '#22C55E',
    colorWarning: '#EAB308',
    colorError: '#EF4444',
    fontFamily: '"IBM Plex Sans", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif',
    fontFamilyCode: '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Menlo, monospace',

    colorBgContainer: '#161618',
    colorBgElevated: '#1E1E21',
    colorBgLayout: '#0A0A0B',

    colorText: '#EDEDEF',
    colorTextSecondary: '#8B8B8E',
    colorTextTertiary: '#5A5A5D',

    colorBorder: 'rgba(255,255,255,0.06)',
    colorBorderSecondary: 'rgba(255,255,255,0.04)',

    borderRadius: 8,
    borderRadiusLG: 8,
    borderRadiusSM: 6,
    borderRadiusXS: 4,

    boxShadow: 'none',
    boxShadowSecondary: '0 4px 6px -1px rgba(0, 0, 0, 0.3)',
  },
  components: {
    Button: {
      borderRadius: 6,
      primaryShadow: 'none',
    },
    Card: {
      borderRadiusLG: 8,
    },
    Menu: {
      itemBg: 'transparent',
      itemSelectedBg: 'rgba(59, 130, 246, 0.12)',
      itemSelectedColor: '#60A5FA',
      itemHoverBg: 'rgba(255, 255, 255, 0.04)',
      itemBorderRadius: 6,
    },
  },
};

export default { lightTheme, darkTheme, colors };
