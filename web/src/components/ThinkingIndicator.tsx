/**
 * ThinkingIndicator - Professional AI thinking animation
 *
 * Design: Minimal, refined with clear visual feedback
 * - Pulsing ring indicator (no bouncing dots)
 * - Phase-aware rotating messages
 * - Shimmer progress bar for visual momentum
 * - Reduced motion support
 */

import { motion, AnimatePresence } from "framer-motion";
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

// Main thinking indicator
export function ThinkingIndicator({
  phase,
  expanded = false,
  className,
}: ThinkingIndicatorProps) {
  const { t } = useTranslation();
  const [messageIndex, setMessageIndex] = useState(0);

  const thinkingMessages: ThinkingMessage[] = useMemo(
    () => [
      {
        key: "analyzing",
        icon: Search,
        text: t(
          "playground.thinking.analyzing",
          "Analyzing your request..."
        ),
      },
      {
        key: "thinking",
        icon: Brain,
        text: t("playground.thinking.thinking", "Thinking..."),
      },
      {
        key: "searching",
        icon: Search,
        text: t(
          "playground.thinking.searching",
          "Searching knowledge base..."
        ),
      },
      {
        key: "planning",
        icon: Sparkles,
        text: t(
          "playground.thinking.planning",
          "Planning response..."
        ),
      },
      {
        key: "preparing",
        icon: Zap,
        text: t(
          "playground.thinking.preparing",
          "Preparing answer..."
        ),
      },
    ],
    [t]
  );

  useEffect(() => {
    const interval = setInterval(() => {
      setMessageIndex((prev) => (prev + 1) % thinkingMessages.length);
    }, 3000);
    return () => clearInterval(interval);
  }, [thinkingMessages.length]);

  const currentMessage = phase
    ? thinkingMessages.find((m) => m.key === phase) ||
      thinkingMessages[messageIndex]
    : thinkingMessages[messageIndex];

  const Icon = currentMessage.icon;

  if (expanded) {
    return (
      <div
        className={cn(
          "flex flex-col items-center gap-5 py-8",
          className
        )}
      >
        {/* Pulsing ring indicator */}
        <div className="relative">
          <div className="w-14 h-14 rounded-2xl bg-primary flex items-center justify-center shadow-lg shadow-primary/20 animate-thinking-ring">
            <Icon className="w-7 h-7 text-white" />
          </div>
        </div>

        {/* Status text */}
        <AnimatePresence mode="wait">
          <motion.p
            key={currentMessage.key}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.25 }}
            className="text-[15px] font-medium text-slate-600 dark:text-slate-300"
          >
            {currentMessage.text}
          </motion.p>
        </AnimatePresence>

        {/* Shimmer bar */}
        <div className="w-32 h-1 rounded-full bg-primary/10 dark:bg-primary/20 overflow-hidden">
          <div className="h-full bg-linear-to-r from-transparent via-primary/60 to-transparent animate-shimmer" />
        </div>
      </div>
    );
  }

  // Compact mode
  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        "inline-flex items-center gap-3 py-2.5 px-4 rounded-xl",
        "bg-primary/5 dark:bg-primary/10",
        "",
        "border border-primary/15 dark:border-primary/20",
        className
      )}
    >
      {/* Animated indicator */}
      <div className="relative shrink-0">
        <div className="w-8 h-8 rounded-lg bg-primary flex items-center justify-center shadow-md shadow-primary/20 animate-thinking-ring">
          <Icon className="w-4 h-4 text-white" />
        </div>
      </div>

      {/* Status text */}
      <div className="flex flex-col gap-1 min-w-0">
        <AnimatePresence mode="wait">
          <motion.span
            key={currentMessage.key}
            initial={{ opacity: 0, x: -6 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 6 }}
            transition={{ duration: 0.2 }}
            className="text-[13px] font-medium text-violet-700 dark:text-violet-300 whitespace-nowrap"
          >
            {currentMessage.text}
          </motion.span>
        </AnimatePresence>

        {/* Inline shimmer bar */}
        <div className="w-24 h-[3px] rounded-full bg-violet-100 dark:bg-violet-900/30 overflow-hidden">
          <div className="h-full bg-linear-to-r from-transparent via-violet-500/50 to-transparent animate-shimmer" />
        </div>
      </div>
    </motion.div>
  );
}

export default ThinkingIndicator;
