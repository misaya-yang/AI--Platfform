import { useState, useEffect } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { Layout, Typography, Space, Dropdown } from "antd";
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
  DesktopOutlined,
  MenuOutlined,
} from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import { useAppStore } from "@/store/useAppStore";
import { useAuthStore } from "@/store/useAuthStore";
import { logout } from "@/api/auth";
import { HelpModal } from "@/components/HelpModal";
import { PasswordChangeModal } from "@/components/PasswordChangeModal";
import { ProfileModal } from "@/components/ProfileModal";
import { Logo } from "@/components/Logo";
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



// Theme toggle button - simple version


export function AppLayout() {
  const { t, i18n } = useTranslation();
  const [collapsed, setCollapsed] = useState(false);
  const [isMobile, setIsMobile] = useState(false);
  const [helpModalOpen, setHelpModalOpen] = useState(false);
  const [showPasswordChange, setShowPasswordChange] = useState(false);
  const {
    themeMode,
    resolvedTheme,
    darkMode,
    setThemeMode,
    toggleDarkMode,
  } = useAppStore();
  const { user, clearAuth, hasPermission, forcePasswordChange, setForcePasswordChange } = useAuthStore();
  const location = useLocation();
  const navigate = useNavigate();

  // Profile modal state
  const [showProfileModal, setShowProfileModal] = useState(false);

  // 语言切换
  const handleLanguageChange = (langCode: string) => {
    i18n.changeLanguage(langCode);
  };

  useEffect(() => {
    const mediaQuery = window.matchMedia("(max-width: 1024px)");
    const sync = () => {
      const mobile = mediaQuery.matches;
      setIsMobile(mobile);
      if (mobile) setCollapsed(true);
    };
    sync();
    mediaQuery.addEventListener("change", sync);
    return () => mediaQuery.removeEventListener("change", sync);
  }, []);

  // 语言子菜单
  const languageMenuItems: MenuProps['items'] = languages.map(lang => ({
    key: lang.code,
    label: `${lang.flag} ${t(lang.nameKey, lang.nativeName)}`,
    onClick: () => handleLanguageChange(lang.code),
  }));

  const themeMenuItems: MenuProps["items"] = [
    {
      key: "light",
      icon: <SunOutlined />,
      label: t("theme.mode.light", "Light"),
      onClick: () => setThemeMode("light"),
    },
    {
      key: "dark",
      icon: <MoonOutlined />,
      label: t("theme.mode.dark", "Dark"),
      onClick: () => setThemeMode("dark"),
    },
    {
      key: "system",
      icon: <DesktopOutlined />,
      label: t("theme.mode.system", "System"),
      onClick: () => setThemeMode("system"),
    },
  ];

  // 用户下拉菜单
  const userMenuItems: MenuProps['items'] = [
    { key: 'profile', label: t('user.profile'), icon: <UserOutlined /> },
    { key: 'change-password', label: t('user.changePassword'), icon: <SettingOutlined /> },
    {
      key: "theme",
      label: t("theme.title", "Theme"),
      icon: <DesktopOutlined />,
      children: themeMenuItems,
    },
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

  // 生成菜单项 - 根据权限过滤
  const filteredNavItems = navItems.filter(item =>
    item.permission === null || hasPermission(item.permission)
  );

  const currentThemeLabel =
    themeMode === "system"
      ? `${t("theme.mode.system", "System")} · ${resolvedTheme === "dark" ? t("theme.mode.dark", "Dark") : t("theme.mode.light", "Light")}`
      : themeMode === "dark"
        ? t("theme.mode.dark", "Dark")
        : t("theme.mode.light", "Light");

  const siderOffset = isMobile ? (collapsed ? -210 : 0) : 0;
  const contentMarginLeft = isMobile ? 0 : collapsed ? 64 : 210;


  return (
    <Layout className="app-layout" style={{ minHeight: '100vh' }}>
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        trigger={null}
        width={210}
        collapsedWidth={isMobile ? 0 : 64}
        className="app-sider bg-card/50 backdrop-blur-xl"
        style={{
          position: 'fixed',
          left: isMobile ? siderOffset : 0,
          top: 0,
          bottom: 0,
          zIndex: 100,
          borderRight: 'none',
          transition: 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
        }}
        theme={resolvedTheme}
      >
        <div className="flex flex-col h-full bg-sidebar/50">
          {/* Logo area */}
          <div className="h-[52px] flex items-center px-6 border-b border-border/40">
            <Logo collapsed={collapsed} />
          </div>

          {/* Custom Navigation Menu */}
          <div className="flex-1 overflow-y-auto py-6 px-3 space-y-1 scrollbar-hide">
            {filteredNavItems.map((item) => (
              <NavLink
                key={item.key}
                to={item.key}
                className={({ isActive }) => `
                  relative group flex items-center px-4 py-3.5 my-1.5 rounded-xl transition-all duration-300 ease-out
                  ${isActive
                    ? 'bg-primary/10 text-primary font-semibold shadow-sm shadow-primary/5'
                    : 'text-muted-foreground hover:bg-muted/60 hover:text-foreground'
                  }
                  ${collapsed ? 'justify-center px-2' : ''}
                `}
              >
                {({ isActive }) => (
                  <>
                    <span className={`
                      text-xl transition-all duration-300 transform group-hover:scale-110 flex-shrink-0
                      ${isActive ? 'text-primary' : 'text-muted-foreground group-hover:text-foreground'}
                    `}>
                      {item.icon}
                    </span>

                    {!collapsed && (
                      <span className={`ml-3.5 truncate transition-all duration-300 ${isActive ? 'translate-x-1' : 'group-hover:translate-x-1'}`}>
                        {t(item.labelKey)}
                      </span>
                    )}

                    {/* Active Indicator (Left Bar) */}
                    {isActive && !collapsed && (
                      <div className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-8 bg-primary rounded-r-full shadow-[0_0_12px_rgba(var(--primary),0.5)] animate-in slide-in-from-left-2 fade-in duration-300" />
                    )}

                    {/* Hover Tooltip for Collapsed State */}
                    {collapsed && (
                      <div className="absolute left-full ml-4 px-3 py-1.5 bg-popover text-popover-foreground text-sm font-medium rounded-lg shadow-xl opacity-0 scale-95 group-hover:opacity-100 group-hover:scale-100 transition-all duration-200 pointer-events-none whitespace-nowrap z-50 border border-border/50">
                        {t(item.labelKey)}
                        <div className="absolute left-0 top-1/2 -translate-y-1/2 -ml-1 w-2 h-2 bg-popover rotate-45 border-l border-b border-border/50" />
                      </div>
                    )}
                  </>
                )}
              </NavLink>
            ))}
          </div>

          {/* Footer Actions */}
          <div className="p-4 border-t border-border/40 bg-muted/5 space-y-3">
            {/* Theme Toggle */}
            <div className={`
              flex items-center transition-all duration-300 bg-background/50 rounded-xl p-1 border border-border/20
              ${collapsed ? 'justify-center flex-col gap-2' : 'justify-between px-3'}
            `}>
              {!collapsed && (
                <span className="text-xs font-medium text-muted-foreground ml-1">
                  {currentThemeLabel}
                </span>
              )}
              <button
                onClick={toggleDarkMode}
                className={`
                  relative flex items-center justify-center rounded-lg transition-all duration-300 hover:scale-105 active:scale-95
                  ${collapsed ? 'w-10 h-10' : 'w-8 h-8'}
                  ${darkMode ? 'bg-indigo-950/30 text-indigo-400' : 'bg-orange-50 text-orange-500'}
                `}
              >
                {darkMode ? <MoonOutlined /> : <SunOutlined />}
              </button>
            </div>

            {/* Collapse Button */}
            <button
              onClick={() => setCollapsed(!collapsed)}
              className={`
                flex items-center justify-center w-full rounded-xl transition-all duration-300
                hover:bg-primary/5 active:scale-95 text-muted-foreground hover:text-primary
                ${collapsed ? 'h-10' : 'h-10 gap-3'}
              `}
            >
              {collapsed ? (
                <MenuUnfoldOutlined className="text-lg" />
              ) : (
                <>
                  <MenuFoldOutlined className="text-lg" />
                  <span className="text-sm font-medium">{t('nav.collapseSidebar')}</span>
                </>
              )}
            </button>
          </div>
        </div>
      </Sider>

      {/* Main content area */}
      <Layout style={{
        marginLeft: contentMarginLeft,
        transition: 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
        background: 'transparent',
        minHeight: '100vh',
      }}>
        {/* 顶部导航栏 */}
        <Header style={{
          padding: isMobile ? '0 12px' : '0 20px',
          background: 'transparent',
          borderBottom: 'none',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          position: 'relative',
          zIndex: 50,
          height: 44,
        }}>
          {/* 左侧 - 面包屑/标题 */}
          <div className="flex items-center gap-3">
            {isMobile && (
              <button
                type="button"
                onClick={() => setCollapsed((prev) => !prev)}
                className="h-8 w-8 inline-flex items-center justify-center rounded-md border border-border/70 bg-background/80"
                aria-label={t("nav.toggleSidebar", "Toggle sidebar")}
              >
                <MenuOutlined />
              </button>
            )}
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
          padding: isMobile ? '8px' : '0 16px 16px 16px',
          minHeight: 'calc(100vh - 44px)',
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
          ? 'rgba(59, 130, 246, 0.12)'
          : 'rgba(59, 130, 246, 0.06)'} !important;
          color: #3B82F6 !important;
          font-weight: 600 !important;
        }

        .app-sider .ant-menu-item-selected .ant-menu-item-icon {
          color: #3B82F6 !important;
        }

        .app-sider .ant-menu-item-selected::after {
          content: '';
          position: absolute;
          left: 0;
          top: 12px;
          bottom: 12px;
          width: 3px;
          background: #3B82F6;
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
          background: ${darkMode ? 'rgba(255, 255, 255, 0.1)' : 'rgba(0, 0, 0, 0.1)'};
          border-radius: 3px;
        }

        .app-sider::-webkit-scrollbar-thumb:hover {
          background: ${darkMode ? 'rgba(255, 255, 255, 0.2)' : 'rgba(0, 0, 0, 0.2)'};
        }
      `}</style>
    </Layout>
  );
}
