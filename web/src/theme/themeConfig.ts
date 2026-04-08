import type { ThemeConfig } from 'antd';

// Hejaz Islamic education platform — deep green + warm gold palette

export const colors = {
  primary: {
    50: '#f0faf4', 100: '#d5f0e0', 200: '#a8e0c1', 300: '#6cc899',
    400: '#3daa73', 500: '#1a4731', 600: '#163d2a', 700: '#123322',
    800: '#0e291b', 900: '#0a1f14',
  },
  neutral: {
    50: '#f7f8f7', 100: '#eef0ef', 200: '#dde1de', 300: '#c4cac6',
    400: '#98a29b', 500: '#6d786f', 600: '#4d574f', 700: '#3a433c',
    800: '#272e29', 900: '#161b17', 950: '#0c100d',
  },
  gold: {
    50: '#fdf9ef', 100: '#f9f0d5', 200: '#f0dca6', 300: '#e5c472',
    400: '#d9ab44', 500: '#c9a84c', 600: '#b08f3a', 700: '#8e7230',
    800: '#6b5625', 900: '#4a3c1a',
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
    colorPrimary: '#1a4731',
    colorInfo: '#3daa73',
    colorSuccess: '#22c55e',
    colorWarning: '#d9ab44',
    colorError: '#dc3545',
    fontFamily: '"Geist Sans", "Geist", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
    fontFamilyCode: '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Menlo, monospace',
    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    colorBgLayout: '#f0f2f0',
    colorText: '#161b17',
    colorTextSecondary: '#4d574f',
    colorTextTertiary: '#6d786f',
    colorBorder: '#dde1de',
    colorBorderSecondary: '#eef0ef',
    borderRadius: 6,
    borderRadiusLG: 8,
    borderRadiusSM: 6,
    borderRadiusXS: 4,
    boxShadow: 'none',
    boxShadowSecondary: '0 1px 2px rgba(0,0,0,0.04)',
  },
  components: {
    ...sharedComponents,
    Menu: {
      itemBg: 'transparent',
      itemSelectedBg: 'rgba(26,71,49,0.06)',
      itemSelectedColor: '#1a4731',
      itemHoverBg: '#eef0ef',
      itemBorderRadius: 6,
    },
  },
};

export const darkTheme: ThemeConfig = {
  token: {
    colorPrimary: '#3daa73',
    colorInfo: '#3daa73',
    colorSuccess: '#4ade80',
    colorWarning: '#e5c472',
    colorError: '#f87171',
    fontFamily: '"Geist Sans", "Geist", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
    fontFamilyCode: '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Menlo, monospace',
    colorBgContainer: '#1a211c',
    colorBgElevated: '#212923',
    colorBgLayout: '#0c100d',
    colorText: '#eef0ef',
    colorTextSecondary: '#98a29b',
    colorTextTertiary: '#6d786f',
    colorBorder: '#2e3830',
    colorBorderSecondary: '#212923',
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
      itemSelectedBg: 'rgba(61,170,115,0.08)',
      itemSelectedColor: '#6cc899',
      itemHoverBg: 'rgba(255,255,255,0.04)',
      itemBorderRadius: 6,
    },
  },
};

export default { lightTheme, darkTheme, colors };
