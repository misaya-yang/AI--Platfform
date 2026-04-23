
import logoUrl from "@/assets/logo.png";

export function Logo({ collapsed }: { collapsed: boolean }) {
    return (
        <div className="flex items-center gap-3 select-none group">
            <div className="relative flex items-center justify-center w-9 h-9">
                <img
                    src={logoUrl}
                    alt="AI Platform Logo"
                    className="w-full h-full object-contain drop-shadow-md transition-transform duration-500 group-hover:scale-105"
                />
            </div>

            {!collapsed && (
                <div className="flex flex-col justify-center animate-in fade-in slide-in-from-left-2 duration-300 ml-1">
                    <span
                        className="text-[15px] font-semibold text-foreground leading-none"
                        style={{ letterSpacing: "-0.2px" }}
                    >
                        Hejaz AI
                    </span>
                </div>
            )}
        </div>
    );
}
