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
      <aside className="hidden md:flex w-[252px] flex-col gap-4 p-4" style={{ zIndex: "var(--z-sticky)" }}>
        <div className="flex h-full flex-col rounded-2xl bg-card border border-border/70 shadow-sm p-4 transition-all duration-300">
          <div className="mb-8 px-2 mt-2 flex items-center gap-3">
            <div className="h-9 w-9 rounded-xl bg-primary/10 flex items-center justify-center">
              <ServerCog className="h-5 w-5 text-primary" />
            </div>
            <span className="text-lg font-semibold tracking-tight text-foreground">
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
                        ? "bg-primary/10 text-primary border border-primary/20"
                        : "text-muted-foreground hover:bg-muted/40 hover:text-foreground"
                    )
                  }
                >
                  <Icon className="h-4 w-4 transition-transform group-hover:scale-110" />
                  {item.label}
                </NavLink>
              );
            })}
          </nav>

          <div className="mt-auto pt-4 border-t border-border/60">
             <div className="flex items-center justify-between rounded-xl p-2 hover:bg-muted/40 transition-colors">
                <span className="text-xs font-medium text-muted-foreground ml-1">Dark Mode</span>
                <Switch checked={darkMode} onCheckedChange={toggleDarkMode} className="scale-75" />
             </div>
          </div>
        </div>
      </aside>

      {/* Main Content Area */}
      <div className="flex flex-1 flex-col overflow-hidden relative">
        <header className="md:hidden flex items-center justify-between border-b bg-card px-4 py-3" style={{ zIndex: "var(--z-sticky)" }}>
           <div className="text-base font-semibold">AI Gateway</div>
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
