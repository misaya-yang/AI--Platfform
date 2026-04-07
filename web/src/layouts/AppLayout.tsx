import { useState, useEffect } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { Layout, Typography, Dropdown } from "antd";
import type { MenuProps } from "antd";
import {
  LayoutDashboard,
  Server,
  BookOpen,
  Zap,
  Bot,
  ListTodo,
  Users,
  Settings,
  PanelLeftClose,
  PanelLeft,
  Sun,
  Moon,
  User,
  HelpCircle,
  LogOut,
  Globe,
  Monitor,
  Menu,
  Lock,
} from "lucide-react";
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
  { key: "/dashboard", labelKey: "nav.dashboard", icon: <LayoutDashboard size={22} />, permission: "console:dashboard:view" },
  { key: "/services", labelKey: "nav.services", icon: <Server size={22} />, permission: "console:services:view" },
  { key: "/knowledge", labelKey: "nav.knowledge", icon: <BookOpen size={22} />, permission: "knowledge:dataset:view" },
  { key: "/playground", labelKey: "nav.playground", icon: <Zap size={22} />, permission: "conversation:playground:access" },
  { key: "/assistant", labelKey: "nav.assistant", icon: <Bot size={22} />, permission: "conversation:playground:access" },
  { key: "/tasks", labelKey: "nav.tasks", icon: <ListTodo size={22} />, permission: null },
  { key: "/users", labelKey: "nav.users", icon: <Users size={22} />, permission: "user:list" },
  { key: "/settings", labelKey: "nav.settings", icon: <Settings size={22} />, permission: "console:settings:view" },
];

function getPageTitleKey(pathname: string): string {
  const segment = pathname.split("/")[1] || "dashboard";
  const map: Record<string, string> = {
    dashboard: "nav.dashboard", services: "nav.services", knowledge: "nav.knowledge",
    playground: "nav.playground", assistant: "nav.assistant", tasks: "nav.tasks",
    users: "nav.users", settings: "nav.settings", exams: "nav.exams",
  };
  return map[segment] || "nav.dashboard";
}

function getInitials(name?: string): string {
  if (!name) return "U";
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  return name.slice(0, 2).toUpperCase();
}

export function AppLayout() {
  const { t, i18n } = useTranslation();
  const [collapsed, setCollapsed] = useState(false);
  const [isMobile, setIsMobile] = useState(false);
  const [helpModalOpen, setHelpModalOpen] = useState(false);
  const [showPasswordChange, setShowPasswordChange] = useState(false);
  const { themeMode, resolvedTheme, darkMode, setThemeMode, toggleDarkMode } = useAppStore();
  const { user, clearAuth, hasPermission, forcePasswordChange, setForcePasswordChange } = useAuthStore();
  const location = useLocation();
  const navigate = useNavigate();
  const [showProfileModal, setShowProfileModal] = useState(false);

  const handleLanguageChange = (langCode: string) => { i18n.changeLanguage(langCode); };

  useEffect(() => {
    const mq = window.matchMedia("(max-width: 1024px)");
    const sync = () => { const m = mq.matches; setIsMobile(m); if (m) setCollapsed(true); };
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, []);

  const languageMenuItems: MenuProps['items'] = languages.map(lang => ({
    key: lang.code,
    label: `${lang.flag} ${t(lang.nameKey, lang.nativeName)}`,
    onClick: () => handleLanguageChange(lang.code),
  }));

  const themeMenuItems: MenuProps["items"] = [
    { key: "light", icon: <Sun size={14} />, label: t("theme.mode.light", "Light"), onClick: () => setThemeMode("light") },
    { key: "dark", icon: <Moon size={14} />, label: t("theme.mode.dark", "Dark"), onClick: () => setThemeMode("dark") },
    { key: "system", icon: <Monitor size={14} />, label: t("theme.mode.system", "System"), onClick: () => setThemeMode("system") },
  ];

  const userMenuItems: MenuProps['items'] = [
    { key: 'profile', label: t('user.profile'), icon: <User size={14} /> },
    { key: 'change-password', label: t('user.changePassword'), icon: <Lock size={14} /> },
    { key: "theme", label: t("theme.title", "Theme"), icon: <Monitor size={14} />, children: themeMenuItems },
    { key: 'language', label: t('user.language'), icon: <Globe size={14} />, children: languageMenuItems },
    { key: 'help', label: t('help.title'), icon: <HelpCircle size={14} /> },
    { type: 'divider' },
    { key: 'logout', label: t('user.logout'), icon: <LogOut size={14} />, danger: true },
  ];

  const handleUserMenuClick = async ({ key }: { key: string }) => {
    if (key === 'logout') { try { await logout(); } catch {} clearAuth(); navigate('/login'); }
    else if (key === 'profile') setShowProfileModal(true);
    else if (key === 'change-password') setShowPasswordChange(true);
    else if (key === 'help') setHelpModalOpen(true);
  };

  useEffect(() => {
    if (forcePasswordChange) setShowPasswordChange(true);
  }, [forcePasswordChange]);

  const filteredNavItems = navItems.filter(item => item.permission === null || hasPermission(item.permission));
  const siderOffset = isMobile ? (collapsed ? -230 : 0) : 0;
  const contentMarginLeft = isMobile ? 0 : collapsed ? 64 : 230;
  const pageTitleKey = getPageTitleKey(location.pathname);
  const userInitials = getInitials(user?.display_name || user?.user_id);

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider
        collapsible collapsed={collapsed} onCollapse={setCollapsed} trigger={null}
        width={230} collapsedWidth={isMobile ? 0 : 64}
        style={{
          position: 'fixed', left: isMobile ? siderOffset : 0, top: 0, bottom: 0, zIndex: 100,
          borderRight: darkMode ? '1px solid #27272a' : '1px solid #e5e5e5',
          background: darkMode ? '#0f0f12' : '#ffffff',
          transition: 'all 0.2s ease',
        }}
        theme={resolvedTheme}
      >
        <div className="flex flex-col h-full">
          {/* Logo */}
          <div className="h-[48px] flex items-center px-4">
            <Logo collapsed={collapsed} />
          </div>

          {/* Navigation */}
          <nav className="flex-1 overflow-y-auto px-3 pt-2 pb-2 scrollbar-hide">
            <div className="space-y-0.5">
              {filteredNavItems.map((item) => (
                <NavLink
                  key={item.key}
                  to={item.key}
                  className={({ isActive }) =>
                    `relative group flex items-center gap-3 rounded-lg transition-colors duration-150 ${
                      collapsed ? 'justify-center px-0 py-2.5' : 'px-4 py-2.5'
                    } ${
                      isActive
                        ? 'text-primary bg-primary/[0.06] dark:bg-primary/[0.08]'
                        : 'text-muted-foreground hover:text-foreground hover:bg-accent'
                    }`
                  }
                >
                  {({ isActive }) => (
                    <>
                      <span className={`flex-shrink-0 ${isActive ? 'text-primary' : ''}`}>
                        {item.icon}
                      </span>
                      {!collapsed && (
                        <span className="text-[14px] truncate">{t(item.labelKey)}</span>
                      )}
                      {collapsed && (
                        <div className="absolute left-full ml-2 px-2 py-1 bg-popover text-popover-foreground text-xs rounded-md shadow-lg opacity-0 scale-95 group-hover:opacity-100 group-hover:scale-100 transition-all duration-100 pointer-events-none whitespace-nowrap z-50 border border-border/50">
                          {t(item.labelKey)}
                        </div>
                      )}
                    </>
                  )}
                </NavLink>
              ))}
            </div>
          </nav>

          {/* Footer */}
          <div className={`border-t px-3 py-2 space-y-1 ${darkMode ? 'border-[#27272a]' : 'border-[#e5e5e5]'}`}>
            <button
              onClick={toggleDarkMode}
              aria-label={darkMode ? t("theme.mode.light") : t("theme.mode.dark")}
              className={`flex items-center w-full rounded-md transition-colors duration-150 text-muted-foreground hover:text-foreground hover:bg-accent ${
                collapsed ? 'justify-center py-3' : 'gap-3 px-4 py-2.5'
              }`}
            >
              {darkMode ? <Moon size={20} /> : <Sun size={20} />}
              {!collapsed && <span className="text-[14px]">{darkMode ? t("theme.mode.dark") : t("theme.mode.light")}</span>}
            </button>
            <button
              onClick={() => setCollapsed(!collapsed)}
              aria-label={collapsed ? t("nav.expandSidebar", "Expand") : t("nav.collapseSidebar")}
              className={`flex items-center w-full rounded-md transition-colors duration-150 text-muted-foreground hover:text-foreground hover:bg-accent ${
                collapsed ? 'justify-center py-3' : 'gap-3 px-4 py-2.5'
              }`}
            >
              {collapsed ? <PanelLeft size={20} /> : <PanelLeftClose size={20} />}
              {!collapsed && <span className="text-[14px]">{t('nav.collapseSidebar')}</span>}
            </button>
          </div>
        </div>
      </Sider>

      <Layout style={{ marginLeft: contentMarginLeft, transition: 'all 0.2s ease', background: 'transparent', minHeight: '100vh' }}>
        {/* Header */}
        <Header style={{
          padding: '0 20px', background: 'transparent',
          borderBottom: darkMode ? '1px solid #27272a' : '1px solid #e5e5e5',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          position: 'relative', zIndex: 50, height: 48,
        }}>
          <div className="flex items-center gap-2">
            {isMobile && (
              <button onClick={() => setCollapsed(p => !p)} className="h-8 w-8 inline-flex items-center justify-center rounded-md text-muted-foreground hover:text-foreground" aria-label={t("nav.toggleSidebar", "Toggle sidebar")}>
                <Menu size={18} />
              </button>
            )}
            <span className="text-sm font-medium text-foreground">{t(pageTitleKey)}</span>
          </div>
          <Dropdown menu={{ items: userMenuItems, onClick: handleUserMenuClick }} trigger={['click']}>
            <div className="flex items-center gap-2 px-1.5 py-1 rounded-md cursor-pointer hover:bg-muted/50 transition-colors">
              <div className="w-6 h-6 rounded-md flex items-center justify-center text-[10px] font-semibold text-primary bg-primary/10 border border-primary/15">
                {userInitials}
              </div>
              <Text className="hidden sm:inline" style={{ fontSize: 13, fontWeight: 500 }}>
                {user?.display_name || user?.user_id || t('common.user')}
              </Text>
            </div>
          </Dropdown>
        </Header>

        <Content style={{ padding: isMobile ? '8px' : '0 20px 20px 20px', minHeight: 'calc(100vh - 48px)', overflow: 'auto' }}>
          <div className="page-transition"><Outlet /></div>
        </Content>
      </Layout>

      <HelpModal open={helpModalOpen} onClose={() => setHelpModalOpen(false)} />
      <PasswordChangeModal
        open={showPasswordChange} allowClose={!forcePasswordChange}
        onClose={() => setShowPasswordChange(false)}
        onComplete={() => { setShowPasswordChange(false); setForcePasswordChange(false); }}
      />
      <ProfileModal open={showProfileModal} onClose={() => setShowProfileModal(false)} />

      <style>{`
        .ant-layout-sider-children { box-shadow: none !important; }
        .scrollbar-hide::-webkit-scrollbar { display: none; }
        .scrollbar-hide { -ms-overflow-style: none; scrollbar-width: none; }
      `}</style>
    </Layout>
  );
}
