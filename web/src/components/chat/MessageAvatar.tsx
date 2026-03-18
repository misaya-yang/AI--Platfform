import { motion } from "framer-motion";
import { cn } from "@/lib/utils";
import { Bot, User } from "lucide-react";

export function MessageAvatar({ isUser }: { isUser: boolean }) {
  return (
    <motion.div
      initial={{ scale: 0.8, opacity: 0 }}
      animate={{ scale: 1, opacity: 1 }}
      transition={{ duration: 0.3, ease: "easeOut" }}
      className="relative"
    >
      {/* Glow effect */}
      <div className={cn(
        "absolute inset-0 rounded-xl blur-lg opacity-40",
        isUser
            ? "bg-gradient-to-br from-emerald-400 to-teal-500"
          : "bg-gradient-to-br from-blue-500 via-cyan-500 to-sky-500"
      )} />

      {/* Avatar */}
      <div className={cn(
        "relative flex h-10 w-10 shrink-0 items-center justify-center rounded-xl",
        "shadow-lg transition-transform duration-200 hover:scale-105",
        isUser
          ? "bg-gradient-to-br from-emerald-500 via-emerald-500 to-teal-600 text-white shadow-emerald-500/25"
          : "bg-gradient-to-br from-blue-500 via-cyan-500 to-sky-600 text-white shadow-blue-500/25"
      )}>
        {isUser ? (
          <User className="h-5 w-5" strokeWidth={2.5} />
        ) : (
          <Bot className="h-5 w-5" strokeWidth={2.5} />
        )}
      </div>
    </motion.div>
  );
}
