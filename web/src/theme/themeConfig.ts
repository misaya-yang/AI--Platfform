import type { ThemeConfig } from 'antd';

// shadcn/ui zinc theme — single purple primary, all else grayscale

export const colors = {
  primary: {
    50: '#f5f3ff', 100: '#ede9fe', 200: '#ddd6fe', 300: '#c4b5fd',
    400: '#a78bfa', 500: '#8b5cf6', 600: '#7c3aed', 700: '#6d28d9',
    800: '#5b21b6', 900: '#4c1d95',
  },
  neutral: {
    50: '#fafafa', 100: '#f5f5f5', 200: '#e5e5e5', 300: '#d4d4d4',
    400: '#a3a3a3', 500: '#737373', 600: '#525252', 700: '#404040',
    800: '#262626', 900: '#171717', 950: '#09090b',
  },
};

const sharedComponents: ThemeConfig['components'] = {
  Button: { borderRadius: 6, primaryShadow: 'none' },
  Card: { borderRadiusLG: 8 },
  Input: { borderRadius: 6 },
  Select: { borderRadius: 6 },
  Table: { borderRadius: 8 },
  Modal: { borderRadiusLG: 12 },
  Tag: { borderRadiusSM: 6 },
};

export const lightTheme: ThemeConfig = {
  token: {
    colorPrimary: '#7c3aed',
    colorInfo: '#8b5cf6',
    colorSuccess: '#22c55e',
    colorWarning: '#f59e0b',
    colorError: '#ef4444',
    fontFamily: '"IBM Plex Sans", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
    fontFamilyCode: '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Menlo, monospace',
    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    colorBgLayout: '#f5f5f5',
    colorText: '#09090b',
    colorTextSecondary: '#525252',
    colorTextTertiary: '#737373',
    colorBorder: '#e5e5e5',
    colorBorderSecondary: '#f5f5f5',
    borderRadius: 6,
    borderRadiusLG: 8,
    borderRadiusSM: 6,
    borderRadiusXS: 4,
    boxShadow: 'none',
    boxShadowSecondary: '0 1px 2px rgba(0,0,0,0.05)',
  },
  components: {
    ...sharedComponents,
    Menu: {
      itemBg: 'transparent',
      itemSelectedBg: 'rgba(124,58,237,0.06)',
      itemSelectedColor: '#7c3aed',
      itemHoverBg: '#f5f5f5',
      itemBorderRadius: 6,
    },
  },
};

export const darkTheme: ThemeConfig = {
  token: {
    colorPrimary: '#a78bfa',
    colorInfo: '#a78bfa',
    colorSuccess: '#4ade80',
    colorWarning: '#fbbf24',
    colorError: '#f87171',
    fontFamily: '"IBM Plex Sans", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
    fontFamilyCode: '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Menlo, monospace',
    colorBgContainer: '#18181b',  // --card
    colorBgElevated: '#1e1e22',
    colorBgLayout: '#09090b',     // --background
    colorText: '#fafafa',
    colorTextSecondary: '#a3a3a3',
    colorTextTertiary: '#737373',
    colorBorder: '#27272a',       // --border: solid hex, not rgba
    colorBorderSecondary: '#1e1e22',
    borderRadius: 6,
    borderRadiusLG: 8,
    borderRadiusSM: 6,
    borderRadiusXS: 4,
    boxShadow: 'none',
    boxShadowSecondary: 'none',
  },
  components: {
    ...sharedComponents,
    Menu: {
      itemBg: 'transparent',
      itemSelectedBg: 'rgba(167,139,250,0.08)',
      itemSelectedColor: '#c4b5fd',
      itemHoverBg: 'rgba(255,255,255,0.04)',
      itemBorderRadius: 6,
    },
  },
};

export default { lightTheme, darkTheme, colors };
