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
import { useTranslation } from "react-i18next";
import { useAppStore } from "@/store/useAppStore";
import { useAuthStore } from "@/store/useAuthStore";
import { logout } from "@/api/auth";
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

// Logo component - refined version
function Logo({ collapsed }: { collapsed: boolean }) {
  return (
    <div className="flex items-center gap-3">
      <div className="flex-shrink-0 relative">
        <div className="absolute inset-0 bg-indigo-500 blur-md opacity-20 animate-pulse"></div>
        <svg width="34" height="34" viewBox="0 0 32 32" fill="none" className="relative z-10">
          <rect x="2" y="2" width="28" height="28" rx="9" fill="url(#logoGradient)" />
          <path
            d="M16 7L25 12.5V20.5L16 26L7 20.5V12.5L16 7Z"
            stroke="white"
            strokeWidth="1.5"
            strokeOpacity="0.8"
            fill="none"
          />
          <circle cx="16" cy="16" r="4" fill="white" />
          <defs>
            <linearGradient id="logoGradient" x1="2" y1="2" x2="30" y2="30" gradientUnits="userSpaceOnUse">
              <stop offset="0" stopColor="#6366F1" />
              <stop offset="1" stopColor="#8B5CF6" />
            </linearGradient>
          </defs>
        </svg>
      </div>
      {!collapsed && (
        <div className="flex flex-col">
          <span className="text-base font-bold text-foreground leading-tight tracking-tight">
            AI Platform
          </span>
          <span className="text-[10px] uppercase font-bold text-muted-foreground/60 tracking-widest mt-0.5">
            Gateway Console
          </span>
        </div>
      )}
    </div>
  );
}

// Theme toggle button - simple version
function ThemeToggle({ darkMode, onToggle, tooltip }: { darkMode: boolean; onToggle: () => void; tooltip: string }) {
  return (
    <Tooltip title={tooltip}>
      <button
        onClick={onToggle}
        className="flex items-center justify-center w-9 h-9 rounded-md hover:bg-accent transition-colors"
      >
        {darkMode ? (
          <MoonOutlined className="text-lg text-primary" />
        ) : (
          <SunOutlined className="text-lg text-amber-500" />
        )}
      </button>
    </Tooltip>
  );
}

export function AppLayout() {
  const { t, i18n } = useTranslation();
  const [collapsed, setCollapsed] = useState(false);
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
    icon: item.icon,
    label: (
      <NavLink to={item.key} style={{ color: 'inherit' }}>
        {t(item.labelKey)}
      </NavLink>
    ),
  }));

  return (
    <Layout className="app-layout" style={{ minHeight: '100vh', minWidth: 1200 }}>
      {/* 侧边栏 */}
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        trigger={null}
        width={220}
        collapsedWidth={64}
        className="app-sider"
        style={{
          position: 'fixed',
          left: 0,
          top: 0,
          bottom: 0,
          zIndex: 100,
          background: darkMode ? '#111827' : '#ffffff',
          borderRight: `1px solid ${darkMode ? '#374151' : '#E5E7EB'}`,
          transition: 'width 200ms',
        }}
      >
        {/* Logo area */}
        <div style={{
          padding: collapsed ? '0 12px' : '0 20px',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          height: 64,
          borderBottom: `1px solid ${darkMode ? '#374151' : '#E5E7EB'}`,
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
          borderTop: `1px solid ${darkMode ? '#374151' : '#E5E7EB'}`,
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

          {/* Collapse button */}
          <button
            onClick={() => setCollapsed(!collapsed)}
            className="flex items-center justify-center w-full px-3 py-2 rounded-md hover:bg-accent transition-colors cursor-pointer"
          >
            {collapsed ? (
              <MenuUnfoldOutlined className="text-base text-muted-foreground" />
            ) : (
              <>
                <MenuFoldOutlined className="text-base text-muted-foreground" />
                <span className="ml-2 text-sm text-muted-foreground">
                  {t('nav.collapseSidebar')}
                </span>
              </>
            )}
          </button>
        </div>
      </Sider>

      {/* Main content area */}
      <Layout style={{
        marginLeft: collapsed ? 64 : 220,
        transition: 'margin-left 200ms',
        background: darkMode ? '#111827' : '#F9FAFB',
        minHeight: '100vh',
      }}>
        {/* 顶部导航栏 */}
        <Header style={{
          padding: '0 24px',
          background: darkMode ? 'rgba(17, 24, 39, 0.9)' : 'rgba(255, 255, 255, 0.9)',
          backdropFilter: 'blur(8px)',
          borderBottom: `1px solid ${darkMode ? '#374151' : '#E5E7EB'}`,
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
              <div
                className="user-menu-trigger flex items-center gap-2 px-3 py-1.5 rounded-lg cursor-pointer hover:bg-accent transition-colors"
              >
                <div className="w-8 h-8 rounded-md bg-primary/10 flex items-center justify-center">
                  <UserOutlined className="text-primary" />
                </div>
                <Text style={{ fontSize: 13, fontWeight: 500 }}>{user?.display_name || user?.user_id || t('common.user')}</Text>
              </div>
            </Dropdown>
          </Space>
        </Header>

        {/* Main content */}
        <Content style={{
          padding: '24px',
          minHeight: 'calc(100vh - 64px)',
          overflow: 'auto',
        }}>
          <Outlet />
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

      {/* Global styles */}
      <style>{`
        .app-sider .ant-menu-item {
          margin: 4px 12px !important;
          border-radius: 10px !important;
          height: 44px !important;
          line-height: 44px !important;
          width: calc(100% - 24px) !important;
          transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1) !important;
          /* Improved contrast for dark mode - use brighter text color */
          color: ${darkMode ? '#D1D5DB' : '#374151'} !important;
        }

        .app-sider .ant-menu-item .ant-menu-item-icon {
          font-size: 16px !important;
          transition: transform 0.2s !important;
          /* Icon color matching text for better readability */
          color: ${darkMode ? '#9CA3AF' : '#6B7280'} !important;
        }

        .app-sider .ant-menu-item:hover .ant-menu-item-icon {
          transform: scale(1.1);
          color: ${darkMode ? '#E5E7EB' : '#374151'} !important;
        }

        .app-sider .ant-menu-item:hover {
          background: ${darkMode ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.03)'} !important;
          color: ${darkMode ? '#F9FAFB' : '#111827'} !important;
        }

        .app-sider .ant-menu-item-selected {
          background: ${darkMode 
            ? 'linear-gradient(135deg, rgba(99, 102, 241, 0.2) 0%, rgba(139, 92, 246, 0.1) 100%)' 
            : 'linear-gradient(135deg, #EEF2FF 0%, #F5F3FF 100%)'} !important;
          color: #6366F1 !important;
          font-weight: 600 !important;
          box-shadow: ${darkMode ? '0 4px 12px rgba(0,0,0,0.2)' : '0 2px 8px rgba(99, 102, 241, 0.08)'};
        }

        .app-sider .ant-menu-item-selected .ant-menu-item-icon {
          color: #6366F1 !important;
        }

        .app-sider .ant-menu-item-selected::after {
          content: '';
          position: absolute;
          left: 0;
          top: 12px;
          bottom: 12px;
          width: 3px;
          background: #6366F1;
          border-radius: 0 4px 4px 0;
          display: block !important;
        }

        /* Scrollbar styling */
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
          background: ${darkMode ? 'rgba(75, 85, 99, 0.4)' : 'rgba(156, 163, 175, 0.4)'};
          border-radius: 3px;
        }

        .app-sider::-webkit-scrollbar-thumb:hover {
          background: ${darkMode ? 'rgba(75, 85, 99, 0.6)' : 'rgba(156, 163, 175, 0.6)'};
        }
      `}</style>
    </Layout>
  );
}
