/**
 * ThinkingIndicator - Manus-style AI thinking animation
 *
 * A beautiful, animated component that shows the AI is processing/thinking.
 * Features:
 * - Animated brain/neural network visualization
 * - Cycling status messages with i18n support
 * - Subtle particle effects
 * - Smooth transitions
 */

import { motion } from "framer-motion";
import { useEffect, useState, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Brain, Sparkles, Lightbulb, Zap, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

interface ThinkingIndicatorProps {
  /** Current phase/stage of thinking */
  phase?: string;
  /** Whether to show expanded view */
  expanded?: boolean;
  /** Custom class name */
  className?: string;
}

// Thinking status message configuration
interface ThinkingMessage {
  key: string;
  icon: LucideIcon;
  text: string;
}

// Neural network node component
function NeuralNode({
  delay,
  size = "sm",
}: {
  delay: number;
  size?: "sm" | "md" | "lg";
}) {
  const sizeClasses = {
    sm: "w-1.5 h-1.5",
    md: "w-2 h-2",
    lg: "w-2.5 h-2.5",
  };

  return (
    <motion.div
      className={cn(
        "rounded-full bg-gradient-to-r from-violet-500 to-purple-500",
        sizeClasses[size]
      )}
      animate={{
        scale: [1, 1.3, 1],
        opacity: [0.5, 1, 0.5],
      }}
      transition={{
        duration: 1.5,
        delay,
        repeat: Infinity,
        ease: "easeInOut",
      }}
    />
  );
}

// Main neural network animation
function NeuralNetworkAnimation() {
  return (
    <div className="relative w-12 h-12 flex items-center justify-center">
      {/* Center node */}
      <div className="absolute">
        <NeuralNode delay={0} size="lg" />
      </div>

      {/* Surrounding nodes */}
      <div className="absolute" style={{ top: "4px", left: "50%", transform: "translateX(-50%)" }}>
        <NeuralNode delay={0.2} size="sm" />
      </div>
      <div className="absolute" style={{ bottom: "4px", left: "50%", transform: "translateX(-50%)" }}>
        <NeuralNode delay={0.4} size="sm" />
      </div>
      <div className="absolute" style={{ left: "4px", top: "50%", transform: "translateY(-50%)" }}>
        <NeuralNode delay={0.6} size="sm" />
      </div>
      <div className="absolute" style={{ right: "4px", top: "50%", transform: "translateY(-50%)" }}>
        <NeuralNode delay={0.8} size="sm" />
      </div>

      {/* Orbiting particle */}
      <motion.div
        className="absolute w-1 h-1 rounded-full bg-fuchsia-400"
        animate={{
          rotate: 360,
        }}
        transition={{
          duration: 3,
          repeat: Infinity,
          ease: "linear",
        }}
        style={{
          transformOrigin: "center",
          offsetPath: "path('M 24 6 A 18 18 0 1 1 24 42 A 18 18 0 1 1 24 6')",
          offsetDistance: "0%",
        }}
      >
        <motion.div
          animate={{
            offsetDistance: ["0%", "100%"],
          }}
          transition={{
            duration: 3,
            repeat: Infinity,
            ease: "linear",
          }}
        />
      </motion.div>

      {/* Pulse ring */}
      <motion.div
        className="absolute w-10 h-10 rounded-full border border-violet-500/30"
        animate={{
          scale: [1, 1.3, 1],
          opacity: [0.3, 0, 0.3],
        }}
        transition={{
          duration: 2,
          repeat: Infinity,
          ease: "easeOut",
        }}
      />
    </div>
  );
}

// Compact thinking indicator (default)
function CompactThinking({ currentMessage }: { currentMessage: ThinkingMessage }) {
  const Icon = currentMessage.icon;

  return (
    <div className="flex items-center gap-3">
      {/* Animated icon container */}
      <div className="relative">
        <motion.div
          className="w-9 h-9 rounded-xl bg-gradient-to-br from-violet-500/15 to-purple-500/15 flex items-center justify-center border border-violet-500/20"
          animate={{
            boxShadow: [
              "0 0 0 0 rgba(139, 92, 246, 0)",
              "0 0 0 6px rgba(139, 92, 246, 0.08)",
              "0 0 0 0 rgba(139, 92, 246, 0)",
            ],
          }}
          transition={{
            duration: 2,
            repeat: Infinity,
            ease: "easeInOut",
          }}
        >
          <motion.div
            animate={{ rotate: [0, 5, -5, 0] }}
            transition={{ duration: 2, repeat: Infinity }}
          >
            <Icon className="w-4.5 h-4.5 text-violet-500" />
          </motion.div>
        </motion.div>

        {/* Sparkle effects */}
        <motion.div
          className="absolute -top-1 -right-1 w-2.5 h-2.5"
          animate={{
            scale: [0, 1, 0],
            opacity: [0, 1, 0],
          }}
          transition={{
            duration: 1.5,
            repeat: Infinity,
            delay: 0.5,
          }}
        >
          <Sparkles className="w-2.5 h-2.5 text-fuchsia-400" />
        </motion.div>
      </div>

      {/* Status text */}
      <div className="flex flex-col">
        <motion.span
          key={currentMessage.key}
          initial={{ opacity: 0, y: 5 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -5 }}
          className="text-sm font-medium text-slate-700 dark:text-zinc-200"
        >
          {currentMessage.text}
        </motion.span>
        {/* Progress dots */}
        <div className="flex gap-1.5 mt-1.5">
          {[0, 1, 2].map((i) => (
            <motion.div
              key={i}
              className="w-1.5 h-1.5 rounded-full bg-violet-400"
              animate={{
                scale: [1, 1.5, 1],
                opacity: [0.3, 1, 0.3],
              }}
              transition={{
                duration: 0.8,
                delay: i * 0.15,
                repeat: Infinity,
              }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

// Expanded thinking indicator (for longer waits)
function ExpandedThinking({ currentMessage }: { currentMessage: ThinkingMessage }) {
  return (
    <div className="flex flex-col items-center gap-4 py-4">
      {/* Neural network animation */}
      <NeuralNetworkAnimation />

      {/* Status text */}
      <div className="flex flex-col items-center gap-2">
        <motion.span
          key={currentMessage.key}
          initial={{ opacity: 0, y: 5 }}
          animate={{ opacity: 1, y: 0 }}
          className="text-sm font-medium text-slate-700 dark:text-zinc-200"
        >
          {currentMessage.text}
        </motion.span>

        {/* Progress bar */}
        <div className="w-32 h-1 bg-slate-200 dark:bg-zinc-700 rounded-full overflow-hidden">
          <motion.div
            className="h-full bg-gradient-to-r from-violet-500 via-purple-500 to-fuchsia-500"
            animate={{
              x: ["-100%", "100%"],
            }}
            transition={{
              duration: 1.5,
              repeat: Infinity,
              ease: "easeInOut",
            }}
            style={{ width: "50%" }}
          />
        </div>
      </div>
    </div>
  );
}

export function ThinkingIndicator({
  phase,
  expanded = false,
  className,
}: ThinkingIndicatorProps) {
  const { t } = useTranslation();
  const [messageIndex, setMessageIndex] = useState(0);

  // Create messages with translated text
  const thinkingMessages: ThinkingMessage[] = useMemo(() => [
    { key: "analyzing", icon: Brain, text: t("playground.thinking.analyzing", "Analyzing...") },
    { key: "thinking", icon: Lightbulb, text: t("playground.thinking.thinking", "Thinking...") },
    { key: "planning", icon: Sparkles, text: t("playground.thinking.planning", "Planning...") },
    { key: "preparing", icon: Zap, text: t("playground.thinking.preparing", "Preparing response...") },
  ], [t]);

  // Cycle through messages
  useEffect(() => {
    const interval = setInterval(() => {
      setMessageIndex((prev) => (prev + 1) % thinkingMessages.length);
    }, 2500);

    return () => clearInterval(interval);
  }, [thinkingMessages.length]);

  // Use phase-specific message if provided
  const currentMessage = phase
    ? thinkingMessages.find((m) => m.key === phase) || thinkingMessages[messageIndex]
    : thinkingMessages[messageIndex];

  return (
    <div className={cn("", className)}>
      {expanded ? (
        <ExpandedThinking currentMessage={currentMessage} />
      ) : (
        <CompactThinking currentMessage={currentMessage} />
      )}
    </div>
  );
}

export default ThinkingIndicator;
