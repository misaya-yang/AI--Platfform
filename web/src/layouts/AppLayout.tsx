import { useEffect } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  LayoutDashboard,
  Settings,
  Sparkles,
  ServerCog,
  ListChecks,
  Database,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { Switch } from "@/components/ui/switch";
import { useAppStore } from "@/store/useAppStore";

const navItems = [
  { to: "/dashboard", label: "仪表盘", icon: LayoutDashboard },
  { to: "/services", label: "服务管理", icon: ServerCog },
  { to: "/knowledge", label: "知识库", icon: Database },
  { to: "/playground", label: "智能对话", icon: Sparkles },
  { to: "/tasks", label: "任务管理", icon: ListChecks },
  { to: "/settings", label: "系统设置", icon: Settings },
];

export function AppLayout() {
  const { darkMode, toggleDarkMode } = useAppStore();
  const location = useLocation();
  const isKnowledge = location.pathname.startsWith("/knowledge");

  useEffect(() => {
    document.documentElement.classList.toggle("dark", darkMode);
  }, [darkMode]);

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background text-foreground aurora-bg font-sans selection:bg-primary/20">
      {/* Floating Sidebar - uses z-sticky to stay above content but below overlays */}
      <aside className="hidden md:flex w-[260px] flex-col gap-4 p-4" style={{ zIndex: "var(--z-sticky)" }}>
        <div className="flex h-full flex-col rounded-2xl glass border border-white/20 dark:border-white/10 shadow-xl p-4 transition-all duration-300">
          <div className="mb-8 px-2 mt-2 flex items-center gap-2">
            <div className="h-8 w-8 rounded-lg bg-gradient-to-br from-indigo-500 to-purple-600 shadow-lg flex items-center justify-center">
              <ServerCog className="h-5 w-5 text-white" />
            </div>
            <span className="text-lg font-bold tracking-tight bg-gradient-to-r from-indigo-500 to-purple-600 bg-clip-text text-transparent">
              AI Gateway
            </span>
          </div>
          
          <nav className="space-y-2 flex-1">
            {navItems.map((item) => {
              const Icon = item.icon;
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    cn(
                      "group flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-all duration-200 outline-none",
                      isActive 
                        ? "bg-primary text-primary-foreground shadow-md" 
                        : "text-muted-foreground hover:bg-white/50 dark:hover:bg-white/10 hover:text-foreground"
                    )
                  }
                >
                  <Icon className="h-4 w-4 transition-transform group-hover:scale-110" />
                  {item.label}
                </NavLink>
              );
            })}
          </nav>

          <div className="mt-auto pt-4 border-t border-border/50">
             <div className="flex items-center justify-between rounded-xl p-2 hover:bg-white/50 dark:hover:bg-white/10 transition-colors">
                <span className="text-xs font-medium text-muted-foreground ml-1">Dark Mode</span>
                <Switch checked={darkMode} onCheckedChange={toggleDarkMode} className="scale-75" />
             </div>
          </div>
        </div>
      </aside>

      {/* Main Content Area */}
      <div className="flex flex-1 flex-col overflow-hidden relative">
        <header className="md:hidden flex items-center justify-between border-b bg-card/80 backdrop-blur-sm px-4 py-3" style={{ zIndex: "var(--z-sticky)" }}>
           <div className="font-semibold">AI Gateway</div>
           <Switch checked={darkMode} onCheckedChange={toggleDarkMode} />
        </header>
        
        <main className="flex-1 overflow-auto p-4 md:p-6 scroll-smooth">
          <div
            className={cn(
              "mx-auto h-full animate-in fade-in slide-in-from-bottom-4 duration-500",
              isKnowledge ? "max-w-none" : "max-w-6xl"
            )}
          >
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
