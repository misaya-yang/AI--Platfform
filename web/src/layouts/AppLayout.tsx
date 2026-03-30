import { useState, useEffect } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { Layout, Typography, Dropdown, Drawer } from "antd";
import type { MenuProps } from "antd";
import {
  DashboardOutlined,
  SettingOutlined,
  ThunderboltOutlined,
  CloudServerOutlined,
  UnorderedListOutlined,
  DatabaseOutlined,
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
import { languages } from "@/i18n";

const { Content } = Layout;
const { Text } = Typography;

const navItems = [
  { key: "/dashboard", labelKey: "nav.dashboard", icon: <DashboardOutlined />, permission: "console:dashboard:view" },
  { key: "/services", labelKey: "nav.services", icon: <CloudServerOutlined />, permission: "console:services:view" },
  { key: "/knowledge", labelKey: "nav.knowledge", icon: <DatabaseOutlined />, permission: "knowledge:dataset:view" },
  { key: "/playground", labelKey: "nav.playground", icon: <ThunderboltOutlined />, permission: "conversation:playground:access" },
  { key: "/assistant", labelKey: "nav.assistant", icon: <ThunderboltOutlined />, permission: "conversation:playground:access" },
  { key: "/tasks", labelKey: "nav.tasks", icon: <UnorderedListOutlined />, permission: null },
  { key: "/users", labelKey: "nav.users", icon: <TeamOutlined />, permission: "user:list" },
  { key: "/settings", labelKey: "nav.settings", icon: <SettingOutlined />, permission: "console:settings:view" },
];

export function AppLayout() {
  const { t, i18n } = useTranslation();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [isMobile, setIsMobile] = useState(false);
  const [helpModalOpen, setHelpModalOpen] = useState(false);
  const [showPasswordChange, setShowPasswordChange] = useState(false);
  const [showProfileModal, setShowProfileModal] = useState(false);
  const { themeMode, resolvedTheme, darkMode, setThemeMode, toggleDarkMode } = useAppStore();
  const { user, clearAuth, hasPermission, forcePasswordChange, setForcePasswordChange } = useAuthStore();
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    const mq = window.matchMedia("(max-width: 768px)");
    const sync = () => setIsMobile(mq.matches);
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    if (forcePasswordChange) setShowPasswordChange(true);
  }, [forcePasswordChange]);

  const filteredNavItems = navItems.filter(
    (item) => item.permission === null || hasPermission(item.permission)
  );

  const languageMenuItems: MenuProps["items"] = languages.map((lang) => ({
    key: lang.code,
    label: `${lang.flag} ${t(lang.nameKey, lang.nativeName)}`,
    onClick: () => i18n.changeLanguage(lang.code),
  }));

  const themeMenuItems: MenuProps["items"] = [
    { key: "light", icon: <SunOutlined />, label: t("theme.mode.light", "Light"), onClick: () => setThemeMode("light") },
    { key: "dark", icon: <MoonOutlined />, label: t("theme.mode.dark", "Dark"), onClick: () => setThemeMode("dark") },
    { key: "system", icon: <DesktopOutlined />, label: t("theme.mode.system", "System"), onClick: () => setThemeMode("system") },
  ];

  const userMenuItems: MenuProps["items"] = [
    { key: "profile", label: t("user.profile"), icon: <UserOutlined /> },
    { key: "change-password", label: t("user.changePassword"), icon: <SettingOutlined /> },
    { key: "theme", label: t("theme.title", "Theme"), icon: <DesktopOutlined />, children: themeMenuItems },
    { key: "language", label: t("user.language"), icon: <GlobalOutlined />, children: languageMenuItems },
    { key: "help", label: t("help.title"), icon: <QuestionCircleOutlined /> },
    { type: "divider" },
    { key: "logout", label: t("user.logout"), icon: <LogoutOutlined />, danger: true },
  ];

  const handleUserMenuClick = async ({ key }: { key: string }) => {
    if (key === "logout") {
      try { await logout(); } catch { /* ignore */ }
      clearAuth();
      navigate("/login");
    } else if (key === "profile") setShowProfileModal(true);
    else if (key === "change-password") setShowPasswordChange(true);
    else if (key === "help") setHelpModalOpen(true);
  };

  // --- Shared nav link renderer ---
  const renderNavLinks = (vertical = false) =>
    filteredNavItems.map((item) => (
      <NavLink
        key={item.key}
        to={item.key}
        onClick={() => vertical && setMobileMenuOpen(false)}
        className={({ isActive }) =>
          vertical
            ? `flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm transition-colors ${
                isActive
                  ? "bg-primary/10 text-primary font-semibold"
                  : "text-muted-foreground hover:text-foreground hover:bg-muted/50"
              }`
            : `topnav-link ${isActive ? "active" : ""}`
        }
      >
        <span className={vertical ? "text-base" : "topnav-icon"}>{item.icon}</span>
        <span>{t(item.labelKey)}</span>
      </NavLink>
    ));

  return (
    <div className="app-root">
      {/* ─── Top Navbar ─── */}
      <header className="topnav">
        <div className="topnav-inner">
          {/* Left: Logo + Nav */}
          <div className="topnav-left">
            {isMobile ? (
              <button
                className="topnav-mobile-trigger"
                onClick={() => setMobileMenuOpen(true)}
                aria-label={t("nav.toggleSidebar", "Toggle menu")}
              >
                <MenuOutlined />
              </button>
            ) : null}
            <span className="topnav-logo">AI Platform</span>
            {!isMobile && <nav className="topnav-links">{renderNavLinks()}</nav>}
          </div>

          {/* Right: Theme + User */}
          <div className="topnav-right">
            <button onClick={toggleDarkMode} className="topnav-theme-btn" title="Toggle theme">
              {darkMode ? <MoonOutlined /> : <SunOutlined />}
            </button>
            <Dropdown menu={{ items: userMenuItems, onClick: handleUserMenuClick }} trigger={["click"]}>
              <div className="topnav-user">
                <div className="topnav-user-avatar">
                  <UserOutlined />
                </div>
                {!isMobile && (
                  <Text className="topnav-user-name">
                    {user?.display_name || user?.user_id || t("common.user")}
                  </Text>
                )}
              </div>
            </Dropdown>
          </div>
        </div>
      </header>

      {/* ─── Mobile Nav Drawer ─── */}
      <Drawer
        open={mobileMenuOpen}
        onClose={() => setMobileMenuOpen(false)}
        placement="left"
        width={280}
        styles={{ body: { padding: "16px 12px" } }}
        title={<span className="font-semibold">AI Platform</span>}
      >
        <nav className="flex flex-col gap-1">{renderNavLinks(true)}</nav>
      </Drawer>

      {/* ─── Main Content ─── */}
      <main className="app-main">
        <Outlet />
      </main>

      {/* Modals */}
      <HelpModal open={helpModalOpen} onClose={() => setHelpModalOpen(false)} />
      <PasswordChangeModal
        open={showPasswordChange}
        allowClose={!forcePasswordChange}
        onClose={() => setShowPasswordChange(false)}
        onComplete={() => { setShowPasswordChange(false); setForcePasswordChange(false); }}
      />
      <ProfileModal open={showProfileModal} onClose={() => setShowProfileModal(false)} />

      {/* ─── Scoped Styles ─── */}
      <style>{`
        .app-root {
          min-height: 100vh;
          display: flex;
          flex-direction: column;
          background: var(--page-bg, hsl(var(--background)));
        }

        /* ── Top Navbar ── */
        .topnav {
          position: sticky;
          top: 0;
          z-index: 100;
          height: 48px;
          border-bottom: 1px solid ${darkMode ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)'};
          background: ${darkMode ? 'rgba(10,10,11,0.85)' : 'rgba(255,255,255,0.85)'};
          backdrop-filter: blur(12px);
          -webkit-backdrop-filter: blur(12px);
        }
        .topnav-inner {
          max-width: 100%;
          height: 100%;
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 0 20px;
        }
        .topnav-left {
          display: flex;
          align-items: center;
          gap: 24px;
          min-width: 0;
        }
        .topnav-logo {
          font-weight: 700;
          font-size: 15px;
          letter-spacing: -0.02em;
          color: ${darkMode ? '#EDEDEF' : '#111'};
          white-space: nowrap;
          flex-shrink: 0;
        }
        .topnav-links {
          display: flex;
          align-items: center;
          gap: 2px;
        }
        .topnav-link {
          display: flex;
          align-items: center;
          gap: 5px;
          padding: 6px 12px;
          border-radius: 6px;
          font-size: 13px;
          font-weight: 500;
          color: ${darkMode ? '#8B8B8E' : '#6B7280'};
          text-decoration: none;
          transition: color 0.15s, background 0.15s;
          white-space: nowrap;
        }
        .topnav-link:hover {
          color: ${darkMode ? '#EDEDEF' : '#111'};
          background: ${darkMode ? 'rgba(255,255,255,0.05)' : 'rgba(0,0,0,0.04)'};
        }
        .topnav-link.active {
          color: ${darkMode ? '#EDEDEF' : '#111'};
          background: ${darkMode ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)'};
        }
        .topnav-icon {
          font-size: 14px;
          opacity: 0.7;
        }
        .topnav-link.active .topnav-icon {
          opacity: 1;
        }

        /* Right section */
        .topnav-right {
          display: flex;
          align-items: center;
          gap: 8px;
          flex-shrink: 0;
        }
        .topnav-theme-btn {
          display: flex;
          align-items: center;
          justify-content: center;
          width: 32px;
          height: 32px;
          border-radius: 6px;
          border: none;
          background: transparent;
          color: ${darkMode ? '#8B8B8E' : '#6B7280'};
          cursor: pointer;
          transition: all 0.15s;
          font-size: 14px;
        }
        .topnav-theme-btn:hover {
          background: ${darkMode ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.04)'};
          color: ${darkMode ? '#EDEDEF' : '#111'};
        }
        .topnav-user {
          display: flex;
          align-items: center;
          gap: 8px;
          padding: 4px 10px;
          border-radius: 6px;
          cursor: pointer;
          transition: background 0.15s;
        }
        .topnav-user:hover {
          background: ${darkMode ? 'rgba(255,255,255,0.05)' : 'rgba(0,0,0,0.04)'};
        }
        .topnav-user-avatar {
          width: 28px;
          height: 28px;
          border-radius: 6px;
          background: ${darkMode ? 'rgba(59,130,246,0.15)' : 'rgba(37,99,235,0.08)'};
          display: flex;
          align-items: center;
          justify-content: center;
          color: #3B82F6;
          font-size: 13px;
        }
        .topnav-user-name {
          font-size: 13px !important;
          font-weight: 500 !important;
          color: ${darkMode ? '#EDEDEF' : '#111'} !important;
        }

        /* Mobile trigger */
        .topnav-mobile-trigger {
          display: flex;
          align-items: center;
          justify-content: center;
          width: 32px;
          height: 32px;
          border-radius: 6px;
          border: 1px solid ${darkMode ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'};
          background: transparent;
          color: ${darkMode ? '#8B8B8E' : '#6B7280'};
          cursor: pointer;
        }

        /* ── Main Content ── */
        .app-main {
          flex: 1;
          padding: 20px;
          min-height: calc(100vh - 48px);
        }
        @media (max-width: 768px) {
          .app-main { padding: 12px; }
          .topnav-inner { padding: 0 12px; }
        }

        /* ── Scrollbar ── */
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb {
          background: ${darkMode ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.1)'};
          border-radius: 3px;
        }
        ::-webkit-scrollbar-thumb:hover {
          background: ${darkMode ? 'rgba(255,255,255,0.2)' : 'rgba(0,0,0,0.2)'};
        }
      `}</style>
    </div>
  );
}
