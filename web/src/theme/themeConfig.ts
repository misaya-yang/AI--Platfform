import type { ThemeConfig } from 'antd';

// AI Gateway operations console — graphite slate + steel + restrained amber palette

export const colors = {
  primary: {
    50: '#f3f6fa', 100: '#e4ebf3', 200: '#c9d6e5', 300: '#a8bdd4',
    400: '#7f9bbd', 500: '#4f6f96', 600: '#37577c', 700: '#29435f',
    800: '#1f3349', 900: '#172538',
  },
  neutral: {
    50: '#f6f7f9', 100: '#ebeef2', 200: '#d9dee6', 300: '#c1c9d4',
    400: '#99a5b4', 500: '#6f7c8e', 600: '#505c6e', 700: '#394352',
    800: '#222a35', 900: '#171d26', 950: '#10141a',
  },
  gold: {
    50: '#fbf6ec', 100: '#f3e8cf', 200: '#e7d0a3', 300: '#d8b571',
    400: '#c79a4e', 500: '#ae7c32', 600: '#8e6329', 700: '#6f4d23',
    800: '#543b1e', 900: '#3a2a17',
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
    colorPrimary: '#37577c',
    colorInfo: '#4f6f96',
    colorSuccess: '#2f8f68',
    colorWarning: '#ae7c32',
    colorError: '#dc3545',
    fontFamily: '"Geist Sans", "Geist", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
    fontFamilyCode: '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Menlo, monospace',
    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    colorBgLayout: '#f4f6f8',
    colorText: '#1a202b',
    colorTextSecondary: '#505c6e',
    colorTextTertiary: '#6f7c8e',
    colorBorder: '#d9dee6',
    colorBorderSecondary: '#ebeef2',
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
      itemSelectedBg: 'rgba(55,87,124,0.09)',
      itemSelectedColor: '#29435f',
      itemHoverBg: '#ebeef2',
      itemBorderRadius: 6,
    },
  },
};

export const darkTheme: ThemeConfig = {
  token: {
    colorPrimary: '#8fa9cf',
    colorInfo: '#8fa9cf',
    colorSuccess: '#69b58d',
    colorWarning: '#d0a96b',
    colorError: '#f87171',
    fontFamily: '"Geist Sans", "Geist", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
    fontFamilyCode: '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Menlo, monospace',
    colorBgContainer: '#181d25',
    colorBgElevated: '#202734',
    colorBgLayout: '#11141a',
    colorText: '#eef2f6',
    colorTextSecondary: '#a6b0bf',
    colorTextTertiary: '#7f8b9b',
    colorBorder: '#303948',
    colorBorderSecondary: '#252d39',
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
      itemSelectedBg: 'rgba(143,169,207,0.12)',
      itemSelectedColor: '#b7c9e4',
      itemHoverBg: 'rgba(255,255,255,0.04)',
      itemBorderRadius: 6,
    },
  },
};

export default { lightTheme, darkTheme, colors };
