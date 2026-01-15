import { useState, useEffect } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { Layout, Menu, Tooltip, Typography, Space, Dropdown } from "antd";
import type { MenuProps } from "antd";
import {
  DashboardOutlined,
  SettingOutlined,
  ThunderboltOutlined,
  CloudServerOutlined,
  UnorderedListOutlined,
  DatabaseOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  SunOutlined,
  MoonOutlined,
  UserOutlined,
  QuestionCircleOutlined,
  TeamOutlined,
  LogoutOutlined,
  GlobalOutlined,
} from "@ant-design/icons";
import { motion, AnimatePresence } from "framer-motion";
import { useTranslation } from "react-i18next";
import { useAppStore } from "@/store/useAppStore";
import { useAuthStore } from "@/store/useAuthStore";
import { logout } from "@/api/auth";
import { colors } from "@/theme/themeConfig";
import { HelpModal } from "@/components/HelpModal";
import { PasswordChangeModal } from "@/components/PasswordChangeModal";
import { ProfileModal } from "@/components/ProfileModal";
import { languages } from "@/i18n";

const { Sider, Content, Header } = Layout;
const { Text } = Typography;

// 导航菜单配置 (使用翻译键)
const navItems = [
  {
    key: "/dashboard",
    labelKey: "nav.dashboard",
    icon: <DashboardOutlined />,
    permission: "console:dashboard:view",
  },
  {
    key: "/services",
    labelKey: "nav.services",
    icon: <CloudServerOutlined />,
    permission: "console:services:view",
  },
  {
    key: "/knowledge",
    labelKey: "nav.knowledge",
    icon: <DatabaseOutlined />,
    permission: "knowledge:dataset:view",
  },
  {
    key: "/playground",
    labelKey: "nav.playground",
    icon: <ThunderboltOutlined />,
    permission: "conversation:playground:access",
  },
  {
    key: "/assistant",
    labelKey: "nav.assistant",
    icon: <ThunderboltOutlined />,
    permission: "conversation:playground:access",
  },
  {
    key: "/tasks",
    labelKey: "nav.tasks",
    icon: <UnorderedListOutlined />,
    permission: null, // 所有用户可见
  },
  {
    key: "/users",
    labelKey: "nav.users",
    icon: <TeamOutlined />,
    permission: "user:list",
  },
  {
    key: "/settings",
    labelKey: "nav.settings",
    icon: <SettingOutlined />,
    permission: "console:settings:view",
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
              AI Platform
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
function ThemeToggle({ darkMode, onToggle, tooltip }: { darkMode: boolean; onToggle: () => void; tooltip: string }) {
  return (
    <motion.div
      className="theme-toggle"
      whileTap={{ scale: 0.95 }}
    >
      <Tooltip title={tooltip}>
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

export function AppLayout() {
  const { t, i18n } = useTranslation();
  const [collapsed, setCollapsed] = useState(false);
  const [hoveredItem, setHoveredItem] = useState<string | null>(null);
  const [helpModalOpen, setHelpModalOpen] = useState(false);
  const [showPasswordChange, setShowPasswordChange] = useState(false);
  const { darkMode, toggleDarkMode } = useAppStore();
  const { user, clearAuth, hasPermission, forcePasswordChange, setForcePasswordChange } = useAuthStore();
  const location = useLocation();
  const navigate = useNavigate();

  // Profile modal state
  const [showProfileModal, setShowProfileModal] = useState(false);

  // 语言切换
  const handleLanguageChange = (langCode: string) => {
    i18n.changeLanguage(langCode);
  };

  // 语言子菜单
  const languageMenuItems: MenuProps['items'] = languages.map(lang => ({
    key: lang.code,
    label: `${lang.flag} ${lang.name}`,
    onClick: () => handleLanguageChange(lang.code),
  }));

  // 用户下拉菜单
  const userMenuItems: MenuProps['items'] = [
    { key: 'profile', label: t('user.profile'), icon: <UserOutlined /> },
    { key: 'change-password', label: t('user.changePassword'), icon: <SettingOutlined /> },
    {
      key: 'language',
      label: t('user.language'),
      icon: <GlobalOutlined />,
      children: languageMenuItems,
    },
    { key: 'help', label: t('help.title'), icon: <QuestionCircleOutlined /> },
    { type: 'divider' },
    { key: 'logout', label: t('user.logout'), icon: <LogoutOutlined />, danger: true },
  ];

  // Handle user menu click
  const handleUserMenuClick = async ({ key }: { key: string }) => {
    if (key === 'logout') {
      try {
        await logout();
      } catch {
        // Ignore logout errors
      }
      clearAuth();
      navigate('/login');
    } else if (key === 'profile') {
      setShowProfileModal(true);
    } else if (key === 'change-password') {
      setShowPasswordChange(true);
    } else if (key === 'help') {
      setHelpModalOpen(true);
    }
  };

  // Show password change modal if forced
  useEffect(() => {
    if (forcePasswordChange) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- Intentional: modal state from auth
      setShowPasswordChange(true);
    }
  }, [forcePasswordChange]);

  // 同步 dark mode 到 HTML
  useEffect(() => {
    document.documentElement.classList.toggle("dark", darkMode);
    document.body.style.colorScheme = darkMode ? 'dark' : 'light';
  }, [darkMode]);

  // 生成菜单项 - 根据权限过滤
  const filteredNavItems = navItems.filter(item =>
    item.permission === null || hasPermission(item.permission)
  );

  const menuItems: MenuProps['items'] = filteredNavItems.map(item => ({
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
        {t(item.labelKey)}
      </NavLink>
    ),
    onMouseEnter: () => setHoveredItem(item.key),
    onMouseLeave: () => setHoveredItem(null),
  }));

  return (
    <Layout className="app-layout" style={{ minHeight: '100vh', minWidth: 1200 }}>
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
        {/* Logo 区域 - 与顶部 Header 对齐 */}
        <div style={{
          padding: collapsed ? '0 12px' : '0 20px',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          borderBottom: 'none',
          height: 64, // 与顶部 Header 高度一致
          transition: 'padding 0.3s',
          position: 'relative',
        }}>
          <Logo collapsed={collapsed} />
          {/* 渐变分割线 - 中间深两边淡 */}
          <div style={{
            position: 'absolute',
            bottom: 0,
            left: '10%',
            right: '10%',
            height: 1,
            background: darkMode
              ? `linear-gradient(90deg, transparent, ${colors.neutral[600]}, transparent)`
              : `linear-gradient(90deg, transparent, ${colors.neutral[300]}, transparent)`,
          }} />
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
                {darkMode ? t('theme.dark') : t('theme.light')}
              </Text>
            )}
            <ThemeToggle
              darkMode={darkMode}
              onToggle={toggleDarkMode}
              tooltip={darkMode ? t('theme.switchToLight') : t('theme.switchToDark')}
            />
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
                {t('nav.collapseSidebar')}
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
          boxShadow: darkMode
            ? '0 1px 3px rgba(0, 0, 0, 0.3), 0 1px 2px rgba(0, 0, 0, 0.25)'
            : '0 1px 3px rgba(0, 0, 0, 0.08), 0 1px 2px rgba(0, 0, 0, 0.06)',
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
            <Text strong style={{ fontSize: 16, fontWeight: 600 }}>
              {(() => {
                const item = filteredNavItems.find(item => location.pathname.startsWith(item.key));
                return item ? t(item.labelKey) : '';
              })()}
            </Text>
          </div>

          {/* 右侧 - 工具栏 */}
          <Space size={16}>
            {/* 用户菜单 */}
            <Dropdown menu={{ items: userMenuItems, onClick: handleUserMenuClick }} trigger={['click']}>
              <motion.div
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
                className="user-menu-trigger"
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '6px 12px',
                  borderRadius: 10,
                  cursor: 'pointer',
                  background: darkMode ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.02)',
                  transition: 'all 0.25s cubic-bezier(0.4, 0, 0.2, 1)',
                }}
              >
                <div style={{
                  width: 32,
                  height: 32,
                  borderRadius: 8,
                  background: `linear-gradient(135deg, ${colors.primary[400]}, ${colors.cyan[400]})`,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  boxShadow: '0 2px 8px rgba(59, 130, 246, 0.3)',
                }}>
                  <UserOutlined style={{ color: '#fff', fontSize: 14 }} />
                </div>
                <Text style={{ fontSize: 13, fontWeight: 500 }}>{user?.display_name || user?.user_id || t('common.user')}</Text>
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

      {/* 密码修改模态框 */}
      <PasswordChangeModal
        open={showPasswordChange}
        allowClose={!forcePasswordChange}
        onClose={() => setShowPasswordChange(false)}
        onComplete={() => {
          setShowPasswordChange(false);
          setForcePasswordChange(false);
        }}
      />

      {/* 个人设置模态框 */}
      <ProfileModal
        open={showProfileModal}
        onClose={() => setShowProfileModal(false)}
      />

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

        /* 侧边栏菜单样式优化 - 更圆润现代 */
        .app-sider .ant-menu-item {
          margin: 6px 4px !important;
          border-radius: 12px !important;
          transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1) !important;
          height: 44px !important;
          line-height: 44px !important;
        }

        .app-sider .ant-menu-item:hover {
          transform: translateX(3px);
          background: ${darkMode ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.04)'} !important;
        }

        /* 菜单项hover时图标轻微放大 */
        .app-sider .ant-menu-item:hover .anticon {
          transform: scale(1.1);
          transition: transform 0.2s ease;
        }

        .app-sider .ant-menu-item-selected {
          background: linear-gradient(135deg,
            ${colors.primary[500]}25,
            ${colors.cyan[500]}18
          ) !important;
          border-left: none !important;
          box-shadow: 0 4px 12px ${colors.primary[500]}25;
        }

        .app-sider .ant-menu-item-selected::before {
          content: '';
          position: absolute;
          left: 0;
          top: 50%;
          transform: translateY(-50%);
          width: 4px;
          height: 24px;
          background: linear-gradient(180deg, ${colors.primary[400]}, ${colors.cyan[400]});
          border-radius: 0 4px 4px 0;
        }

        .app-sider .ant-menu-item-selected::after {
          display: none !important;
        }

        /* 用户菜单hover效果 */
        .user-menu-trigger:hover {
          background: ${darkMode ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.04)'} !important;
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
