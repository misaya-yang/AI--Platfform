/**
 * ThinkingIndicator - Elegant AI thinking animation
 *
 * Design: Refined, subtle with clear visual feedback
 * - Smooth, non-distracting animation
 * - Larger, readable text
 * - Clear status communication
 */

import { motion } from "framer-motion";
import { useEffect, useState, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Brain, Sparkles, Search, Zap, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

interface ThinkingIndicatorProps {
  phase?: string;
  expanded?: boolean;
  className?: string;
}

interface ThinkingMessage {
  key: string;
  icon: LucideIcon;
  text: string;
}

// Elegant dot animation
function ThinkingDots() {
  return (
    <div className="flex items-center gap-1">
      {[0, 1, 2].map((i) => (
        <motion.div
          key={i}
          className="w-1.5 h-1.5 rounded-full bg-gradient-to-br from-violet-400 to-purple-500"
          animate={{
            scale: [1, 1.3, 1],
            opacity: [0.5, 1, 0.5],
          }}
          transition={{
            duration: 1.2,
            delay: i * 0.2,
            repeat: Infinity,
            ease: "easeInOut",
          }}
        />
      ))}
    </div>
  );
}

// Main thinking indicator
export function ThinkingIndicator({
  phase,
  expanded = false,
  className,
}: ThinkingIndicatorProps) {
  const { t } = useTranslation();
  const [messageIndex, setMessageIndex] = useState(0);

  const thinkingMessages: ThinkingMessage[] = useMemo(() => [
    { key: "analyzing", icon: Search, text: t("playground.thinking.analyzing", "Analyzing your request...") },
    { key: "thinking", icon: Brain, text: t("playground.thinking.thinking", "Thinking...") },
    { key: "searching", icon: Search, text: t("playground.thinking.searching", "Searching knowledge base...") },
    { key: "planning", icon: Sparkles, text: t("playground.thinking.planning", "Planning response...") },
    { key: "preparing", icon: Zap, text: t("playground.thinking.preparing", "Preparing answer...") },
  ], [t]);

  useEffect(() => {
    const interval = setInterval(() => {
      setMessageIndex((prev) => (prev + 1) % thinkingMessages.length);
    }, 3000);
    return () => clearInterval(interval);
  }, [thinkingMessages.length]);

  const currentMessage = phase
    ? thinkingMessages.find((m) => m.key === phase) || thinkingMessages[messageIndex]
    : thinkingMessages[messageIndex];

  const Icon = currentMessage.icon;

  if (expanded) {
    return (
      <div className={cn("flex flex-col items-center gap-5 py-6", className)}>
        {/* Animated icon */}
        <motion.div
          className="relative"
          animate={{ scale: [1, 1.05, 1] }}
          transition={{ duration: 2, repeat: Infinity, ease: "easeInOut" }}
        >
          <div className="absolute inset-0 bg-violet-500/20 rounded-2xl blur-xl" />
          <div className="relative w-16 h-16 rounded-2xl bg-gradient-to-br from-violet-500 to-purple-600 flex items-center justify-center shadow-lg shadow-violet-500/25">
            <Icon className="w-8 h-8 text-white" />
          </div>
        </motion.div>

        {/* Status text */}
        <motion.div
          key={currentMessage.key}
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          className="text-center"
        >
          <p className="text-[15px] font-medium text-slate-700 dark:text-slate-200">
            {currentMessage.text}
          </p>
        </motion.div>

        {/* Progress dots */}
        <ThinkingDots />
      </div>
    );
  }

  return (
    <motion.div 
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        "inline-flex items-center gap-3 py-2 px-4 mb-3 rounded-lg min-w-[200px]",
        "bg-gradient-to-r from-violet-50/80 to-purple-50/80",
        "dark:from-violet-900/20 dark:to-purple-900/20",
        "border border-violet-200/50 dark:border-violet-700/30",
        className
      )}
    >
      {/* Animated icon - compact */}
      <motion.div
        className="relative flex-shrink-0"
        animate={{ scale: [1, 1.05, 1] }}
        transition={{ duration: 2, repeat: Infinity, ease: "easeInOut" }}
      >
        <div className="absolute inset-0 bg-violet-500/20 rounded-lg blur-md" />
        <div className={cn(
          "relative w-9 h-9 rounded-lg flex items-center justify-center",
          "bg-gradient-to-br from-violet-500 to-purple-600",
          "shadow-md shadow-violet-500/25"
        )}>
          <motion.div
            animate={{ rotate: [0, 5, -5, 0] }}
            transition={{ duration: 3, repeat: Infinity, ease: "easeInOut" }}
          >
            <Icon className="w-4.5 h-4.5 text-white" />
          </motion.div>
        </div>
      </motion.div>

      {/* Status text and dots */}
      <div className="flex items-center gap-3">
        <motion.span
          key={currentMessage.key}
          initial={{ opacity: 0, x: -8 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ duration: 0.3 }}
          className="text-[14px] font-medium text-violet-700 dark:text-violet-300"
        >
          {currentMessage.text}
        </motion.span>
        <ThinkingDots />
      </div>
    </motion.div>
  );
}

export default ThinkingIndicator;
