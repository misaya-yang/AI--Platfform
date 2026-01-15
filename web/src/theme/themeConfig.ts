import type { ThemeConfig } from 'antd';

// Simplified Indigo-based Theme
// Design principle: Clean, solid colors, no gradients or heavy shadows

export const colors = {
  // Primary - Indigo
  primary: {
    50: '#EEF2FF',
    100: '#E0E7FF',
    200: '#C7D2FE',
    300: '#A5B4FC',
    400: '#818CF8',
    500: '#6366F1', // Main primary
    600: '#4F46E5',
    700: '#4338CA',
    800: '#3730A3',
    900: '#312E81',
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
    colorPrimary: '#6366F1',
    colorInfo: '#3B82F6',
    colorSuccess: '#22C55E',
    colorWarning: '#F59E0B',
    colorError: '#EF4444',

    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    colorBgLayout: '#F9FAFB',

    colorText: '#111827',
    colorTextSecondary: '#6B7280',
    colorTextTertiary: '#9CA3AF',

    colorBorder: '#E5E7EB',
    colorBorderSecondary: '#F3F4F6',

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
      itemSelectedBg: '#EEF2FF',
      itemSelectedColor: '#6366F1',
      itemHoverBg: '#F3F4F6',
      itemBorderRadius: 6,
    },
  },
};

// Dark theme configuration
export const darkTheme: ThemeConfig = {
  token: {
    colorPrimary: '#6366F1',
    colorInfo: '#3B82F6',
    colorSuccess: '#22C55E',
    colorWarning: '#F59E0B',
    colorError: '#EF4444',

    colorBgContainer: '#1F2937',
    colorBgElevated: '#374151',
    colorBgLayout: '#111827',

    colorText: '#F9FAFB',
    colorTextSecondary: '#9CA3AF',
    colorTextTertiary: '#6B7280',

    colorBorder: '#374151',
    colorBorderSecondary: '#1F2937',

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
      itemSelectedBg: 'rgba(99, 102, 241, 0.2)',
      itemSelectedColor: '#818CF8',
      itemHoverBg: '#374151',
      itemBorderRadius: 6,
    },
  },
};

export default { lightTheme, darkTheme, colors };
