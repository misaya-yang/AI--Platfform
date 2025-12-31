import { useState, useEffect } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { Layout, Menu, Tooltip, Typography, Space, Badge, Dropdown } from "antd";
import type { MenuProps } from "antd";
import {
  DashboardOutlined,
  SettingOutlined,
  ThunderboltOutlined,
  CloudServerOutlined,
  CloudSyncOutlined,
  UnorderedListOutlined,
  DatabaseOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  SunOutlined,
  MoonOutlined,
  BellOutlined,
  UserOutlined,
  QuestionCircleOutlined,
} from "@ant-design/icons";
import { motion, AnimatePresence } from "framer-motion";
import { useAppStore } from "@/store/useAppStore";
import { colors } from "@/theme/themeConfig";
import { HelpModal } from "@/components/HelpModal";

const { Sider, Content, Header } = Layout;
const { Text } = Typography;

// 导航菜单配置
const navItems = [
  {
    key: "/dashboard",
    label: "仪表盘",
    icon: <DashboardOutlined />,
    description: "系统概览与数据统计"
  },
  {
    key: "/services",
    label: "服务管理",
    icon: <CloudServerOutlined />,
    description: "管理已注册的服务"
  },
  {
    key: "/knowledge",
    label: "知识库",
    icon: <DatabaseOutlined />,
    description: "管理知识库和文档"
  },
  {
    key: "/confluence",
    label: "Confluence",
    icon: <CloudSyncOutlined />,
    description: "Confluence 文档同步"
  },
  {
    key: "/playground",
    label: "智能对话",
    icon: <ThunderboltOutlined />,
    description: "AI 对话测试"
  },
  {
    key: "/tasks",
    label: "任务管理",
    icon: <UnorderedListOutlined />,
    description: "异步任务追踪"
  },
  {
    key: "/settings",
    label: "系统设置",
    icon: <SettingOutlined />,
    description: "系统配置选项"
  },
];

// Logo 组件 - 科技感渐变
function Logo({ collapsed }: { collapsed: boolean }) {
  return (
    <div className="logo-container">
      <motion.div
        className="logo-icon"
        whileHover={{ scale: 1.05, rotate: 5 }}
        transition={{ type: "spring", stiffness: 400, damping: 10 }}
      >
        <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
          <defs>
            <linearGradient id="logoGradient" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stopColor={colors.primary[400]} />
              <stop offset="50%" stopColor={colors.cyan[400]} />
              <stop offset="100%" stopColor={colors.purple[400]} />
            </linearGradient>
            <filter id="glow">
              <feGaussianBlur stdDeviation="2" result="coloredBlur"/>
              <feMerge>
                <feMergeNode in="coloredBlur"/>
                <feMergeNode in="SourceGraphic"/>
              </feMerge>
            </filter>
          </defs>
          <rect x="2" y="2" width="28" height="28" rx="8" fill="url(#logoGradient)" filter="url(#glow)" opacity="0.15"/>
          <path
            d="M16 6L26 12V20L16 26L6 20V12L16 6Z"
            stroke="url(#logoGradient)"
            strokeWidth="2"
            fill="none"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <circle cx="16" cy="16" r="4" fill="url(#logoGradient)" />
          <path d="M16 12V8" stroke="url(#logoGradient)" strokeWidth="2" strokeLinecap="round"/>
          <path d="M19.5 14L23 11.5" stroke="url(#logoGradient)" strokeWidth="2" strokeLinecap="round"/>
          <path d="M19.5 18L23 20.5" stroke="url(#logoGradient)" strokeWidth="2" strokeLinecap="round"/>
          <path d="M16 20V24" stroke="url(#logoGradient)" strokeWidth="2" strokeLinecap="round"/>
          <path d="M12.5 18L9 20.5" stroke="url(#logoGradient)" strokeWidth="2" strokeLinecap="round"/>
          <path d="M12.5 14L9 11.5" stroke="url(#logoGradient)" strokeWidth="2" strokeLinecap="round"/>
        </svg>
      </motion.div>
      <AnimatePresence>
        {!collapsed && (
          <motion.div
            className="logo-text"
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -10 }}
            transition={{ duration: 0.2 }}
          >
            <Text strong style={{
              fontSize: 18,
              background: `linear-gradient(135deg, ${colors.primary[400]}, ${colors.cyan[400]})`,
              WebkitBackgroundClip: 'text',
              WebkitTextFillColor: 'transparent',
              letterSpacing: '-0.5px'
            }}>
              AI Gateway
            </Text>
            <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: -4 }}>
              Unified AI Services
            </Text>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// 主题切换按钮
function ThemeToggle({ darkMode, onToggle }: { darkMode: boolean; onToggle: () => void }) {
  return (
    <motion.div
      className="theme-toggle"
      whileTap={{ scale: 0.95 }}
    >
      <Tooltip title={darkMode ? "切换到浅色模式" : "切换到深色模式"}>
        <div
          onClick={onToggle}
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            width: 36,
            height: 36,
            borderRadius: 8,
            cursor: 'pointer',
            transition: 'all 0.2s',
            background: darkMode ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.04)',
          }}
        >
          <AnimatePresence mode="wait">
            <motion.div
              key={darkMode ? 'moon' : 'sun'}
              initial={{ scale: 0, rotate: -180 }}
              animate={{ scale: 1, rotate: 0 }}
              exit={{ scale: 0, rotate: 180 }}
              transition={{ duration: 0.3 }}
            >
              {darkMode ? (
                <MoonOutlined style={{ fontSize: 18, color: colors.primary[400] }} />
              ) : (
                <SunOutlined style={{ fontSize: 18, color: colors.orange[500] }} />
              )}
            </motion.div>
          </AnimatePresence>
        </div>
      </Tooltip>
    </motion.div>
  );
}

// 用户下拉菜单
const userMenuItems: MenuProps['items'] = [
  { key: 'profile', label: '个人设置', icon: <UserOutlined /> },
  { key: 'help', label: '帮助文档', icon: <QuestionCircleOutlined /> },
  { type: 'divider' },
  { key: 'logout', label: '退出登录', danger: true },
];

export function AppLayout() {
  const [collapsed, setCollapsed] = useState(false);
  const [hoveredItem, setHoveredItem] = useState<string | null>(null);
  const [helpModalOpen, setHelpModalOpen] = useState(false);
  const { darkMode, toggleDarkMode } = useAppStore();
  const location = useLocation();

  // 同步 dark mode 到 HTML
  useEffect(() => {
    document.documentElement.classList.toggle("dark", darkMode);
    document.body.style.colorScheme = darkMode ? 'dark' : 'light';
  }, [darkMode]);

  // 生成菜单项
  const menuItems: MenuProps['items'] = navItems.map(item => ({
    key: item.key,
    icon: (
      <motion.span
        animate={{
          scale: hoveredItem === item.key ? 1.1 : 1,
        }}
        transition={{ type: "spring", stiffness: 400, damping: 17 }}
      >
        {item.icon}
      </motion.span>
    ),
    label: (
      <NavLink to={item.key} style={{ color: 'inherit' }}>
        {item.label}
      </NavLink>
    ),
    onMouseEnter: () => setHoveredItem(item.key),
    onMouseLeave: () => setHoveredItem(null),
  }));

  return (
    <Layout className="app-layout" style={{ minHeight: '100vh' }}>
      {/* 侧边栏 */}
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        trigger={null}
        width={240}
        collapsedWidth={72}
        className="app-sider"
        style={{
          position: 'fixed',
          left: 0,
          top: 0,
          bottom: 0,
          zIndex: 100,
          background: darkMode
            ? `linear-gradient(180deg, ${colors.neutral[900]} 0%, ${colors.neutral[950]} 100%)`
            : `linear-gradient(180deg, #ffffff 0%, ${colors.neutral[50]} 100%)`,
          borderRight: `1px solid ${darkMode ? colors.neutral[800] : colors.neutral[200]}`,
          transition: 'all 0.3s cubic-bezier(0.2, 0, 0, 1)',
          overflow: 'hidden',
        }}
      >
        {/* Logo 区域 */}
        <div style={{
          padding: collapsed ? '20px 12px' : '20px 16px',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          borderBottom: `1px solid ${darkMode ? colors.neutral[800] : colors.neutral[200]}`,
          minHeight: 72,
          transition: 'padding 0.3s'
        }}>
          <Logo collapsed={collapsed} />
        </div>

        {/* 导航菜单 */}
        <div style={{
          flex: 1,
          overflow: 'auto',
          padding: '16px 8px',
        }}>
          <Menu
            mode="inline"
            selectedKeys={[location.pathname]}
            items={menuItems}
            style={{
              border: 'none',
              background: 'transparent',
            }}
          />
        </div>

        {/* 底部操作区 */}
        <div style={{
          padding: '16px',
          borderTop: `1px solid ${darkMode ? colors.neutral[800] : colors.neutral[200]}`,
          display: 'flex',
          flexDirection: 'column',
          gap: 12,
        }}>
          {/* 主题切换 */}
          <div style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: collapsed ? 'center' : 'space-between',
            padding: collapsed ? '0' : '0 4px',
          }}>
            {!collapsed && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {darkMode ? '深色模式' : '浅色模式'}
              </Text>
            )}
            <ThemeToggle darkMode={darkMode} onToggle={toggleDarkMode} />
          </div>

          {/* 折叠按钮 */}
          <motion.div
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
            onClick={() => setCollapsed(!collapsed)}
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              padding: '10px',
              borderRadius: 8,
              cursor: 'pointer',
              background: darkMode ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.02)',
              transition: 'background 0.2s',
            }}
          >
            {collapsed ? (
              <MenuUnfoldOutlined style={{ fontSize: 16, color: colors.neutral[500] }} />
            ) : (
              <MenuFoldOutlined style={{ fontSize: 16, color: colors.neutral[500] }} />
            )}
            {!collapsed && (
              <Text type="secondary" style={{ marginLeft: 8, fontSize: 13 }}>
                收起侧栏
              </Text>
            )}
          </motion.div>
        </div>
      </Sider>

      {/* 主内容区 */}
      <Layout style={{
        marginLeft: collapsed ? 72 : 240,
        transition: 'margin-left 0.3s cubic-bezier(0.2, 0, 0, 1)',
        background: darkMode ? colors.neutral[900] : colors.neutral[50],
        minHeight: '100vh',
      }}>
        {/* 顶部导航栏 */}
        <Header style={{
          padding: '0 24px',
          background: darkMode
            ? `rgba(${parseInt(colors.neutral[900].slice(1,3), 16)}, ${parseInt(colors.neutral[900].slice(3,5), 16)}, ${parseInt(colors.neutral[900].slice(5,7), 16)}, 0.8)`
            : 'rgba(255, 255, 255, 0.8)',
          backdropFilter: 'blur(12px)',
          borderBottom: `1px solid ${darkMode ? colors.neutral[800] : colors.neutral[200]}`,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          position: 'sticky',
          top: 0,
          zIndex: 50,
          height: 64,
        }}>
          {/* 左侧 - 面包屑/标题 */}
          <div>
            <Text strong style={{ fontSize: 16 }}>
              {navItems.find(item => location.pathname.startsWith(item.key))?.label || ''}
            </Text>
          </div>

          {/* 右侧 - 工具栏 */}
          <Space size={16}>
            {/* 通知 */}
            <Tooltip title="通知">
              <Badge count={3} size="small" offset={[-2, 2]}>
                <motion.div
                  whileHover={{ scale: 1.1 }}
                  whileTap={{ scale: 0.95 }}
                  style={{
                    width: 36,
                    height: 36,
                    borderRadius: 8,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    cursor: 'pointer',
                    background: darkMode ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.02)',
                  }}
                >
                  <BellOutlined style={{ fontSize: 18 }} />
                </motion.div>
              </Badge>
            </Tooltip>

            {/* 帮助 */}
            <Tooltip title="帮助文档">
              <motion.div
                whileHover={{ scale: 1.1 }}
                whileTap={{ scale: 0.95 }}
                onClick={() => setHelpModalOpen(true)}
                style={{
                  width: 36,
                  height: 36,
                  borderRadius: 8,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  cursor: 'pointer',
                  background: darkMode ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.02)',
                }}
              >
                <QuestionCircleOutlined style={{ fontSize: 18 }} />
              </motion.div>
            </Tooltip>

            {/* 用户菜单 */}
            <Dropdown menu={{ items: userMenuItems }} trigger={['click']}>
              <motion.div
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '6px 12px',
                  borderRadius: 8,
                  cursor: 'pointer',
                  background: darkMode ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.02)',
                }}
              >
                <div style={{
                  width: 28,
                  height: 28,
                  borderRadius: 6,
                  background: `linear-gradient(135deg, ${colors.primary[400]}, ${colors.cyan[400]})`,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                }}>
                  <UserOutlined style={{ color: '#fff', fontSize: 14 }} />
                </div>
                <Text style={{ fontSize: 13 }}>管理员</Text>
              </motion.div>
            </Dropdown>
          </Space>
        </Header>

        {/* 主内容 */}
        <Content style={{
          padding: '24px',
          minHeight: 'calc(100vh - 64px)',
          overflow: 'auto',
        }}>
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, ease: "easeOut" }}
          >
            <Outlet />
          </motion.div>
        </Content>
      </Layout>

      {/* 帮助文档模态框 */}
      <HelpModal open={helpModalOpen} onClose={() => setHelpModalOpen(false)} />

      {/* 全局样式 */}
      <style>{`
        .app-layout {
          --transition-timing: cubic-bezier(0.2, 0, 0, 1);
        }

        .logo-container {
          display: flex;
          align-items: center;
          gap: 12px;
        }

        .logo-icon {
          flex-shrink: 0;
        }

        .logo-text {
          display: flex;
          flex-direction: column;
          min-width: 0;
        }

        /* 侧边栏菜单样式优化 */
        .app-sider .ant-menu-item {
          margin: 4px 0 !important;
          border-radius: 8px !important;
          transition: all 0.2s var(--transition-timing) !important;
        }

        .app-sider .ant-menu-item:hover {
          transform: translateX(2px);
        }

        .app-sider .ant-menu-item-selected {
          background: linear-gradient(135deg,
            ${colors.primary[500]}15,
            ${colors.cyan[500]}10
          ) !important;
          border-left: 3px solid ${colors.primary[500]} !important;
        }

        .app-sider .ant-menu-item-selected::after {
          display: none !important;
        }

        /* 滚动条美化 */
        .app-sider::-webkit-scrollbar,
        .ant-layout-content::-webkit-scrollbar {
          width: 6px;
          height: 6px;
        }

        .app-sider::-webkit-scrollbar-track,
        .ant-layout-content::-webkit-scrollbar-track {
          background: transparent;
        }

        .app-sider::-webkit-scrollbar-thumb {
          background: ${colors.neutral[400]}40;
          border-radius: 3px;
        }

        .app-sider::-webkit-scrollbar-thumb:hover {
          background: ${colors.neutral[400]}60;
        }

        /* 深色模式下的滚动条 */
        .dark .app-sider::-webkit-scrollbar-thumb {
          background: ${colors.neutral[600]}40;
        }

        .dark .app-sider::-webkit-scrollbar-thumb:hover {
          background: ${colors.neutral[600]}60;
        }

        /* 动画效果 */
        @keyframes pulse-glow {
          0%, 100% {
            box-shadow: 0 0 0 0 ${colors.primary[500]}40;
          }
          50% {
            box-shadow: 0 0 20px 5px ${colors.primary[500]}20;
          }
        }
      `}</style>
    </Layout>
  );
}
