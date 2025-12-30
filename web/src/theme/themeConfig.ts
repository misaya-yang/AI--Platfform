import type { ThemeConfig } from 'antd';

// 科技感配色系统 - Cyber Tech Theme
// 设计理念：精致的科技感，深邃而不沉闷，多元而不杂乱

export const colors = {
  // 主色 - 科技蓝渐变系
  primary: {
    50: '#e6f4ff',
    100: '#bae0ff',
    200: '#91caff',
    300: '#69b1ff',
    400: '#4096ff',
    500: '#1677ff', // 主色
    600: '#0958d9',
    700: '#003eb3',
    800: '#002c8c',
    900: '#001d66',
  },
  // 青色 - 科技辅助色
  cyan: {
    50: '#e6fffb',
    100: '#b5f5ec',
    200: '#87e8de',
    300: '#5cdbd3',
    400: '#36cfc9',
    500: '#13c2c2',
    600: '#08979c',
    700: '#006d75',
    800: '#00474f',
    900: '#002329',
  },
  // 紫色 - 创意/AI 点缀色
  purple: {
    50: '#f9f0ff',
    100: '#efdbff',
    200: '#d3adf7',
    300: '#b37feb',
    400: '#9254de',
    500: '#722ed1',
    600: '#531dab',
    700: '#391085',
    800: '#22075e',
    900: '#120338',
  },
  // 橙色 - 警示/活力色
  orange: {
    50: '#fff7e6',
    100: '#ffe7ba',
    200: '#ffd591',
    300: '#ffc069',
    400: '#ffa940',
    500: '#fa8c16',
    600: '#d46b08',
    700: '#ad4e00',
    800: '#873800',
    900: '#612500',
  },
  // 中性色 - 深邃灰蓝
  neutral: {
    50: '#f8fafc',
    100: '#f1f5f9',
    200: '#e2e8f0',
    300: '#cbd5e1',
    400: '#94a3b8',
    500: '#64748b',
    600: '#475569',
    700: '#334155',
    800: '#1e293b',
    900: '#0f172a',
    950: '#020617',
  },
};

// 浅色主题配置
export const lightTheme: ThemeConfig = {
  token: {
    // 主色
    colorPrimary: colors.primary[500],
    colorInfo: colors.cyan[500],
    colorSuccess: '#52c41a',
    colorWarning: colors.orange[500],
    colorError: '#ff4d4f',

    // 背景色
    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    colorBgLayout: colors.neutral[50],
    colorBgSpotlight: colors.neutral[100],

    // 文字色
    colorText: colors.neutral[800],
    colorTextSecondary: colors.neutral[500],
    colorTextTertiary: colors.neutral[400],
    colorTextQuaternary: colors.neutral[300],

    // 边框色
    colorBorder: colors.neutral[200],
    colorBorderSecondary: colors.neutral[100],

    // 圆角
    borderRadius: 8,
    borderRadiusLG: 12,
    borderRadiusSM: 6,
    borderRadiusXS: 4,

    // 字体
    fontFamily: '"PingFang SC", "Microsoft YaHei", "Noto Sans SC", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    fontSize: 14,

    // 间距
    marginXS: 8,
    marginSM: 12,
    margin: 16,
    marginMD: 20,
    marginLG: 24,
    marginXL: 32,

    // 阴影
    boxShadow: '0 1px 2px 0 rgba(0, 0, 0, 0.03), 0 1px 6px -1px rgba(0, 0, 0, 0.02), 0 2px 4px 0 rgba(0, 0, 0, 0.02)',
    boxShadowSecondary: '0 6px 16px 0 rgba(0, 0, 0, 0.08), 0 3px 6px -4px rgba(0, 0, 0, 0.12), 0 9px 28px 8px rgba(0, 0, 0, 0.05)',

    // 动画
    motionDurationFast: '0.1s',
    motionDurationMid: '0.2s',
    motionDurationSlow: '0.3s',
  },
  components: {
    Layout: {
      headerBg: '#ffffff',
      siderBg: 'linear-gradient(180deg, #ffffff 0%, #f8fafc 100%)',
      bodyBg: colors.neutral[50],
      headerPadding: '0 24px',
      headerHeight: 64,
    },
    Menu: {
      itemBg: 'transparent',
      itemSelectedBg: `${colors.primary[500]}10`,
      itemSelectedColor: colors.primary[500],
      itemHoverBg: colors.neutral[100],
      itemHoverColor: colors.neutral[800],
      itemActiveBg: `${colors.primary[500]}15`,
      itemBorderRadius: 8,
      itemMarginInline: 8,
      itemMarginBlock: 4,
      iconSize: 18,
      collapsedIconSize: 20,
    },
    Card: {
      headerBg: 'transparent',
      colorBgContainer: '#ffffff',
      borderRadiusLG: 12,
      boxShadowTertiary: '0 1px 2px 0 rgba(0, 0, 0, 0.03), 0 1px 6px -1px rgba(0, 0, 0, 0.02)',
    },
    Button: {
      borderRadius: 8,
      controlHeight: 36,
      controlHeightLG: 44,
      controlHeightSM: 28,
      primaryShadow: '0 2px 0 rgba(22, 119, 255, 0.1)',
    },
    Input: {
      borderRadius: 8,
      controlHeight: 36,
      activeBorderColor: colors.primary[500],
      hoverBorderColor: colors.primary[300],
    },
    Select: {
      borderRadius: 8,
      controlHeight: 36,
    },
    Table: {
      headerBg: colors.neutral[50],
      headerColor: colors.neutral[600],
      rowHoverBg: colors.neutral[50],
      borderColor: colors.neutral[200],
    },
    Tabs: {
      inkBarColor: colors.primary[500],
      itemActiveColor: colors.primary[500],
      itemHoverColor: colors.primary[400],
      itemSelectedColor: colors.primary[500],
    },
    Tag: {
      borderRadiusSM: 6,
    },
    Badge: {
      dotSize: 8,
    },
    Modal: {
      borderRadiusLG: 16,
      headerBg: '#ffffff',
      contentBg: '#ffffff',
    },
    Drawer: {
      colorBgElevated: '#ffffff',
    },
    Tooltip: {
      colorBgSpotlight: colors.neutral[800],
      borderRadius: 6,
    },
    Message: {
      borderRadiusLG: 8,
    },
    Notification: {
      borderRadiusLG: 12,
    },
  },
};

// 深色主题配置
export const darkTheme: ThemeConfig = {
  token: {
    // 主色
    colorPrimary: colors.primary[400],
    colorInfo: colors.cyan[400],
    colorSuccess: '#52c41a',
    colorWarning: colors.orange[400],
    colorError: '#ff4d4f',

    // 背景色 - 深邃蓝灰
    colorBgContainer: colors.neutral[800],
    colorBgElevated: colors.neutral[700],
    colorBgLayout: colors.neutral[900],
    colorBgSpotlight: colors.neutral[700],

    // 文字色
    colorText: colors.neutral[100],
    colorTextSecondary: colors.neutral[400],
    colorTextTertiary: colors.neutral[500],
    colorTextQuaternary: colors.neutral[600],

    // 边框色
    colorBorder: colors.neutral[700],
    colorBorderSecondary: colors.neutral[800],

    // 圆角
    borderRadius: 8,
    borderRadiusLG: 12,
    borderRadiusSM: 6,
    borderRadiusXS: 4,

    // 字体
    fontFamily: '"PingFang SC", "Microsoft YaHei", "Noto Sans SC", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    fontSize: 14,

    // 阴影 - 深色模式阴影更柔和
    boxShadow: '0 1px 2px 0 rgba(0, 0, 0, 0.2), 0 1px 6px -1px rgba(0, 0, 0, 0.15)',
    boxShadowSecondary: '0 6px 16px 0 rgba(0, 0, 0, 0.32), 0 3px 6px -4px rgba(0, 0, 0, 0.28)',
  },
  components: {
    Layout: {
      headerBg: colors.neutral[800],
      siderBg: colors.neutral[900],
      bodyBg: colors.neutral[900],
    },
    Menu: {
      itemBg: 'transparent',
      itemSelectedBg: `${colors.primary[400]}20`,
      itemSelectedColor: colors.primary[400],
      itemHoverBg: `${colors.neutral[700]}`,
      itemHoverColor: colors.neutral[100],
      darkItemBg: 'transparent',
      darkItemSelectedBg: `${colors.primary[400]}20`,
      darkItemSelectedColor: colors.primary[400],
      darkItemHoverBg: colors.neutral[700],
    },
    Card: {
      colorBgContainer: colors.neutral[800],
      borderRadiusLG: 12,
    },
    Button: {
      primaryShadow: '0 2px 0 rgba(64, 150, 255, 0.15)',
    },
    Input: {
      colorBgContainer: colors.neutral[800],
      activeBorderColor: colors.primary[400],
      hoverBorderColor: colors.primary[500],
    },
    Select: {
      colorBgContainer: colors.neutral[800],
      colorBgElevated: colors.neutral[700],
    },
    Table: {
      headerBg: colors.neutral[800],
      headerColor: colors.neutral[300],
      rowHoverBg: `${colors.neutral[700]}80`,
      colorBgContainer: colors.neutral[800],
    },
    Modal: {
      headerBg: colors.neutral[800],
      contentBg: colors.neutral[800],
    },
    Drawer: {
      colorBgElevated: colors.neutral[800],
    },
    Tooltip: {
      colorBgSpotlight: colors.neutral[700],
    },
  },
};

export default { lightTheme, darkTheme, colors };
