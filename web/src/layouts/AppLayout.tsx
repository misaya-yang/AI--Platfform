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

const NAV_ICON_SIZE = 18;

const navItems = [
  { key: "/dashboard", labelKey: "nav.dashboard", icon: <LayoutDashboard size={NAV_ICON_SIZE} strokeWidth={2} />, permission: "console:dashboard:view" },
  { key: "/services", labelKey: "nav.services", icon: <Server size={NAV_ICON_SIZE} strokeWidth={2} />, permission: "console:services:view" },
  { key: "/knowledge", labelKey: "nav.knowledge", icon: <BookOpen size={NAV_ICON_SIZE} strokeWidth={2} />, permission: "knowledge:dataset:view" },
  { key: "/playground", labelKey: "nav.playground", icon: <Zap size={NAV_ICON_SIZE} strokeWidth={2} />, permission: "conversation:playground:access" },
  { key: "/assistant", labelKey: "nav.assistant", icon: <Bot size={NAV_ICON_SIZE} strokeWidth={2} />, permission: "conversation:playground:access" },
  { key: "/tasks", labelKey: "nav.tasks", icon: <ListTodo size={NAV_ICON_SIZE} strokeWidth={2} />, permission: null },
  { key: "/users", labelKey: "nav.users", icon: <Users size={NAV_ICON_SIZE} strokeWidth={2} />, permission: "user:list" },
  { key: "/settings", labelKey: "nav.settings", icon: <Settings size={NAV_ICON_SIZE} strokeWidth={2} />, permission: "console:settings:view" },
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
  const SIDER_WIDTH = 220;
  const siderOffset = isMobile ? (collapsed ? -SIDER_WIDTH : 0) : 0;
  const contentMarginLeft = isMobile ? 0 : collapsed ? 64 : SIDER_WIDTH;
  const pageTitleKey = getPageTitleKey(location.pathname);
  const userInitials = getInitials(user?.display_name || user?.user_id);

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider
        collapsible collapsed={collapsed} onCollapse={setCollapsed} trigger={null}
        width={SIDER_WIDTH} collapsedWidth={isMobile ? 0 : 64}
        style={{
          position: 'fixed', left: isMobile ? siderOffset : 0, top: 0, bottom: 0, zIndex: 100,
          borderRight: '1px solid hsl(var(--border))',
          background: 'hsl(var(--sidebar-bg))',
          transition: 'all 0.3s cubic-bezier(0.16, 1, 0.3, 1)',
        }}
        theme={resolvedTheme}
      >
        <div className="flex flex-col h-full">
          {/* Brand — 60px tall with bottom hairline per design */}
          <div
            className={`flex items-center gap-2.5 border-b border-border/60 ${collapsed ? 'justify-center px-0' : 'px-[18px]'}`}
            style={{ height: 60, flexShrink: 0 }}
          >
            <Logo collapsed={collapsed} />
          </div>

          {/* Navigation */}
          <nav className="flex-1 overflow-y-auto px-2.5 scrollbar-hide py-2.5">
            <div className="flex flex-col gap-[2px]">
              {filteredNavItems.map((item) => (
                <NavLink
                  key={item.key}
                  to={item.key}
                  className={({ isActive }) =>
                    `app-nav-link relative group flex items-center rounded-lg transition-colors duration-140 ease-out ${
                      collapsed ? 'justify-center py-[9px]' : 'gap-3 px-3 py-[9px]'
                    } ${isActive ? 'app-nav-item-active' : ''}`
                  }
                >
                  {({ isActive }) => (
                    <>
                      {isActive && !collapsed && (
                        <span
                          aria-hidden
                          className="absolute left-0 top-2 bottom-2 w-[3px]"
                          style={{
                            background: 'hsl(var(--primary))',
                            borderRadius: '0 2px 2px 0',
                          }}
                        />
                      )}
                      <span className="app-nav-icon flex-shrink-0">{item.icon}</span>
                      {!collapsed && (
                        <span className="app-nav-label truncate">
                          {t(item.labelKey)}
                        </span>
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

          {/* Footer — theme + collapse */}
          <div className="px-2.5 py-2.5 border-t border-border/60 flex flex-col gap-[2px]">
            <button
              onClick={toggleDarkMode}
              aria-label={darkMode ? t("theme.mode.light") : t("theme.mode.dark")}
              className={`app-nav-link flex items-center rounded-lg transition-colors duration-140 ${
                collapsed ? 'justify-center py-2' : 'gap-3 px-3 py-2'
              }`}
            >
              <span className="app-nav-icon flex-shrink-0">
                {darkMode ? <Moon size={NAV_ICON_SIZE} strokeWidth={2} /> : <Sun size={NAV_ICON_SIZE} strokeWidth={2} />}
              </span>
              {!collapsed && (
                <>
                  <span className="app-nav-label flex-1 text-left">
                    {darkMode ? t("theme.mode.dark", "深色模式") : t("theme.mode.light", "浅色模式")}
                  </span>
                  <svg width="10" height="10" viewBox="0 0 10 10" fill="none" style={{ color: 'hsl(var(--muted-foreground) / 0.5)', transform: 'rotate(-90deg)' }}>
                    <path d="M2.5 3.8l2.5 2.5 2.5-2.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </>
              )}
            </button>
            <button
              onClick={() => setCollapsed(!collapsed)}
              aria-label={collapsed ? t("nav.expandSidebar", "Expand") : t("nav.collapseSidebar")}
              className={`app-nav-link flex items-center rounded-lg transition-colors duration-140 ${
                collapsed ? 'justify-center py-2' : 'gap-3 px-3 py-2'
              }`}
            >
              <span className="app-nav-icon flex-shrink-0" style={{ transform: collapsed ? 'rotate(180deg)' : 'none', transition: '.18s' }}>
                {collapsed ? <PanelLeft size={NAV_ICON_SIZE} strokeWidth={2} /> : <PanelLeftClose size={NAV_ICON_SIZE} strokeWidth={2} />}
              </span>
              {!collapsed && (
                <span className="app-nav-label">{t('nav.collapseSidebar', '收起侧栏')}</span>
              )}
            </button>
          </div>
        </div>
      </Sider>

      <Layout style={{ marginLeft: contentMarginLeft, transition: 'all 0.3s cubic-bezier(0.16, 1, 0.3, 1)', background: 'transparent', minHeight: '100vh' }}>
        {/* Header */}
        <Header style={{
          padding: '0 20px', background: 'transparent',
          borderBottom: darkMode ? '1px solid #2e3830' : '1px solid #dde1de',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          position: 'relative', zIndex: 50, height: 48,
        }}>
          <div className="flex items-center gap-2">
            {isMobile && (
              <button onClick={() => setCollapsed(p => !p)} className="h-8 w-8 inline-flex items-center justify-center rounded-md text-muted-foreground hover:text-foreground" aria-label={t("nav.toggleSidebar", "Toggle sidebar")}>
                <Menu size={18} />
              </button>
            )}
            {!location.pathname.startsWith("/dashboard") && (
              <span className="text-sm font-medium text-foreground">{t(pageTitleKey)}</span>
            )}
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

        /* Base nav link — matches design-handoff Sidebar */
        .app-nav-link {
          color: hsl(215 16% 38%); /* textMid */
          cursor: pointer;
          font-size: 13px;
        }
        .dark .app-nav-link { color: hsl(220 8% 65%); }
        .app-nav-link:hover { background: hsl(var(--muted) / 0.5); color: hsl(var(--foreground)); }
        .app-nav-link:hover .app-nav-icon { color: hsl(var(--foreground)); }
        .app-nav-icon {
          color: inherit;
          display: inline-flex;
          align-items: center;
        }
        .app-nav-label {
          font-family: "Inter", -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
          font-size: 13px;
          font-weight: 500;
          letter-spacing: 0;
          line-height: 1.2;
        }

        /* Active — indigo soft bg + indigo icon/text + 600 weight */
        .app-nav-item-active {
          background: hsl(var(--primary) / 0.10);
          color: hsl(var(--primary)) !important;
        }
        .app-nav-item-active:hover {
          background: hsl(var(--primary) / 0.13);
        }
        .app-nav-item-active .app-nav-icon,
        .app-nav-item-active:hover .app-nav-icon {
          color: hsl(var(--primary));
        }
        .app-nav-item-active .app-nav-label {
          color: hsl(var(--primary));
          font-weight: 600;
        }
        .dark .app-nav-item-active { background: hsl(var(--primary) / 0.18); }
        .dark .app-nav-item-active:hover { background: hsl(var(--primary) / 0.24); }
      `}</style>
    </Layout>
  );
}
