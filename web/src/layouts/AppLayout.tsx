import { useState, useEffect } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { Layout, Typography, Dropdown } from "antd";
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
    permission: null,
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

// Get page title key from path
function getPageTitleKey(pathname: string): string {
  const segment = pathname.split("/")[1] || "dashboard";
  const map: Record<string, string> = {
    dashboard: "nav.dashboard",
    services: "nav.services",
    knowledge: "nav.knowledge",
    playground: "nav.playground",
    assistant: "nav.assistant",
    tasks: "nav.tasks",
    users: "nav.users",
    settings: "nav.settings",
    exams: "nav.exams",
  };
  return map[segment] || "nav.dashboard";
}

// Generate initials from display name
function getInitials(name?: string): string {
  if (!name) return "U";
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) {
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }
  return name.slice(0, 2).toUpperCase();
}

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

  const [showProfileModal, setShowProfileModal] = useState(false);

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

  useEffect(() => {
    if (forcePasswordChange) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- Intentional: modal state from auth
      setShowPasswordChange(true);
    }
  }, [forcePasswordChange]);

  const filteredNavItems = navItems.filter(item =>
    item.permission === null || hasPermission(item.permission)
  );

  const siderOffset = isMobile ? (collapsed ? -210 : 0) : 0;
  const contentMarginLeft = isMobile ? 0 : collapsed ? 64 : 210;

  const pageTitleKey = getPageTitleKey(location.pathname);
  const userInitials = getInitials(user?.display_name || user?.user_id);

  return (
    <Layout className="app-layout" style={{ minHeight: '100vh' }}>
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        trigger={null}
        width={210}
        collapsedWidth={isMobile ? 0 : 64}
        className="app-sider"
        style={{
          position: 'fixed',
          left: isMobile ? siderOffset : 0,
          top: 0,
          bottom: 0,
          zIndex: 100,
          borderRight: 'none',
          background: darkMode ? '#0F1117' : '#FFFFFF',
          transition: 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
        }}
        theme={resolvedTheme}
      >
        <div className="flex flex-col h-full">
          {/* Logo area */}
          <div className="h-[52px] flex items-center px-5 mb-2">
            <Logo collapsed={collapsed} />
          </div>

          {/* Navigation */}
          <div className="flex-1 overflow-y-auto py-2 px-3 space-y-0.5 scrollbar-hide">
            {filteredNavItems.map((item) => (
              <NavLink
                key={item.key}
                to={item.key}
                className={({ isActive }) => `
                  relative group flex items-center px-3 py-2.5 rounded-lg transition-all duration-200 ease-out
                  ${isActive
                    ? 'text-primary font-semibold'
                    : 'text-muted-foreground hover:bg-muted/60 hover:text-foreground'
                  }
                  ${collapsed ? 'justify-center px-2' : ''}
                `}
              >
                {({ isActive }) => (
                  <>
                    {/* Left active indicator bar */}
                    {isActive && !collapsed && (
                      <div className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-5 bg-primary rounded-r-full" />
                    )}

                    <span className={`
                      text-[18px] transition-all duration-200 flex-shrink-0
                      ${isActive ? 'text-primary' : 'text-muted-foreground group-hover:text-foreground'}
                    `}>
                      {item.icon}
                    </span>

                    {!collapsed && (
                      <span className="ml-3 text-sm truncate">
                        {t(item.labelKey)}
                      </span>
                    )}

                    {/* Active subtle background */}
                    {isActive && (
                      <div className="absolute inset-0 bg-primary/[0.06] dark:bg-primary/[0.10] rounded-lg -z-10" />
                    )}

                    {/* Collapsed tooltip */}
                    {collapsed && (
                      <div className="absolute left-full ml-3 px-2.5 py-1.5 bg-popover text-popover-foreground text-xs font-medium rounded-md shadow-lg opacity-0 scale-95 group-hover:opacity-100 group-hover:scale-100 transition-all duration-150 pointer-events-none whitespace-nowrap z-50 border border-border/50">
                        {t(item.labelKey)}
                      </div>
                    )}
                  </>
                )}
              </NavLink>
            ))}
          </div>

          {/* Footer — divider + actions */}
          <div className={`border-t ${darkMode ? 'border-white/[0.06]' : 'border-slate-100'} p-3 space-y-1`}>
            {/* Theme Toggle */}
            <button
              onClick={toggleDarkMode}
              aria-label={darkMode ? t("theme.mode.light", "Light mode") : t("theme.mode.dark", "Dark mode")}
              className={`
                flex items-center w-full rounded-lg transition-all duration-200 hover:bg-muted/60
                ${collapsed ? 'justify-center p-2.5' : 'px-3 py-2 gap-3'}
              `}
            >
              <span className={`text-[16px] ${darkMode ? 'text-indigo-400' : 'text-amber-500'}`}>
                {darkMode ? <MoonOutlined /> : <SunOutlined />}
              </span>
              {!collapsed && (
                <span className="text-xs text-muted-foreground">
                  {darkMode ? t("theme.mode.dark", "Dark") : t("theme.mode.light", "Light")}
                </span>
              )}
            </button>

            {/* Collapse Button */}
            <button
              onClick={() => setCollapsed(!collapsed)}
              aria-label={collapsed ? t("nav.expandSidebar", "Expand sidebar") : t("nav.collapseSidebar", "Collapse sidebar")}
              className={`
                flex items-center w-full rounded-lg transition-all duration-200
                hover:bg-muted/60 text-muted-foreground hover:text-foreground
                ${collapsed ? 'justify-center p-2.5' : 'px-3 py-2 gap-3'}
              `}
            >
              {collapsed ? (
                <MenuUnfoldOutlined className="text-[16px]" />
              ) : (
                <>
                  <MenuFoldOutlined className="text-[16px]" />
                  <span className="text-xs">{t('nav.collapseSidebar')}</span>
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
        {/* Header bar */}
        <Header style={{
          padding: '0 20px',
          background: 'transparent',
          borderBottom: 'none',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          position: 'relative',
          zIndex: 50,
          height: 48,
        }}>
          <div className="flex items-center gap-2">
            {isMobile && (
              <button
                type="button"
                onClick={() => setCollapsed((prev) => !prev)}
                className="h-8 w-8 inline-flex items-center justify-center rounded-md"
                aria-label={t("nav.toggleSidebar", "Toggle sidebar")}
              >
                <MenuOutlined />
              </button>
            )}
            {/* Breadcrumb / Page title */}
            <nav className="flex items-center text-sm text-muted-foreground" aria-label="Breadcrumb">
              <span className="font-medium text-foreground">{t(pageTitleKey)}</span>
            </nav>
          </div>

          <Dropdown menu={{ items: userMenuItems, onClick: handleUserMenuClick }} trigger={['click']}>
            <div className="flex items-center gap-2.5 px-2 py-1 rounded-lg cursor-pointer hover:bg-accent/50 transition-colors">
              <div
                className="w-7 h-7 rounded-md flex items-center justify-center text-[11px] font-semibold text-primary-foreground"
                style={{ background: darkMode ? '#818CF8' : '#4F46E5' }}
              >
                {userInitials}
              </div>
              <Text style={{ fontSize: 13, fontWeight: 500 }} className="hidden sm:inline">
                {user?.display_name || user?.user_id || t('common.user')}
              </Text>
            </div>
          </Dropdown>
        </Header>

        {/* Main content */}
        <Content style={{
          padding: isMobile ? '8px' : '0 20px 20px 20px',
          minHeight: 'calc(100vh - 48px)',
          overflow: 'auto',
        }}>
          <div className="page-transition">
            <Outlet />
          </div>
        </Content>
      </Layout>

      <HelpModal open={helpModalOpen} onClose={() => setHelpModalOpen(false)} />

      <PasswordChangeModal
        open={showPasswordChange}
        allowClose={!forcePasswordChange}
        onClose={() => setShowPasswordChange(false)}
        onComplete={() => {
          setShowPasswordChange(false);
          setForcePasswordChange(false);
        }}
      />

      <ProfileModal
        open={showProfileModal}
        onClose={() => setShowProfileModal(false)}
      />

      <style>{`
        .ant-layout-sider,
        .ant-layout-sider *,
        .ant-layout-sider-children {
          border-right: none !important;
          border-inline-end: none !important;
          box-shadow: none !important;
        }

        .app-sider::-webkit-scrollbar,
        .ant-layout-content::-webkit-scrollbar {
          width: 4px;
          height: 4px;
        }

        .app-sider::-webkit-scrollbar-track,
        .ant-layout-content::-webkit-scrollbar-track {
          background: transparent;
        }

        .app-sider::-webkit-scrollbar-thumb {
          background: ${darkMode ? 'rgba(255, 255, 255, 0.08)' : 'rgba(0, 0, 0, 0.08)'};
          border-radius: 2px;
        }

        .app-sider::-webkit-scrollbar-thumb:hover {
          background: ${darkMode ? 'rgba(255, 255, 255, 0.15)' : 'rgba(0, 0, 0, 0.15)'};
        }
      `}</style>
    </Layout>
  );
}
