/**
 * ThinkingPanel — displays model thinking/reasoning tokens in real-time.
 *
 * During streaming: open collapsible panel with pulsing icon and live content.
 * After completion: collapsed panel showing "Thought for Xs", expandable.
 */

import { useState, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { motion, AnimatePresence } from "framer-motion";
import { Brain, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

interface ThinkingPanelProps {
  streamingContent?: string;
  finalContent?: string;
  isStreaming: boolean;
  className?: string;
}

export function ThinkingPanel({
  streamingContent,
  finalContent,
  isStreaming,
  className,
}: ThinkingPanelProps) {
  const { t } = useTranslation();
  const [userExpanded, setUserExpanded] = useState<boolean | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const startTimeRef = useRef<number | null>(null);
  const contentRef = useRef<HTMLDivElement>(null);

  const content = streamingContent || finalContent || "";
  const defaultExpanded = isStreaming;
  const isExpanded = userExpanded ?? defaultExpanded;

  // Track elapsed time during streaming
  useEffect(() => {
    if (isStreaming) {
      if (!startTimeRef.current) startTimeRef.current = Date.now();
      const interval = setInterval(() => {
        setElapsedSeconds(
          Math.floor((Date.now() - (startTimeRef.current || Date.now())) / 1000)
        );
      }, 1000);
      return () => clearInterval(interval);
    } else if (startTimeRef.current) {
      // Freeze the final elapsed time
      setElapsedSeconds(
        Math.floor((Date.now() - startTimeRef.current) / 1000)
      );
    }
  }, [isStreaming]);

  // Auto-scroll to bottom during streaming
  useEffect(() => {
    if (isStreaming && contentRef.current) {
      contentRef.current.scrollTop = contentRef.current.scrollHeight;
    }
  }, [content, isStreaming]);

  if (!content && !isStreaming) return null;

  const timeLabel = elapsedSeconds > 0 ? `${elapsedSeconds}s` : "";

  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: "auto" }}
      exit={{ opacity: 0, height: 0 }}
      transition={{ duration: 0.2 }}
      className={cn("mb-3", className)}
    >
      {/* Header — always visible. Muted-gray/italic treatment per design
          brief: thinking is secondary context, not a second accent.
          Violet removed (single-accent rule). */}
      <button
        type="button"
        onClick={() => setUserExpanded((current) => !(current ?? defaultExpanded))}
        className="flex items-center gap-2 py-1.5 text-xs font-medium italic text-[hsl(var(--assistant-text-secondary))] hover:text-[hsl(var(--assistant-text-primary))] transition-colors select-none w-full text-left"
      >
        <Brain
          className={cn(
            "w-3.5 h-3.5 shrink-0",
            isStreaming && "animate-pulse"
          )}
        />
        <span>
          {isStreaming
            ? t("assistant.thinkingInProgress", "Thinking...")
            : timeLabel
              ? t("assistant.thoughtFor", "Thought for {{time}}", { time: timeLabel })
              : t("assistant.thoughtProcess", "Thought process")}
        </span>
        {isStreaming && timeLabel && (
          <span className="text-[hsl(var(--assistant-text-tertiary))] tabular-nums not-italic font-mono">
            {timeLabel}
          </span>
        )}
        <ChevronRight
          className={cn(
            "w-3 h-3 ml-auto transition-transform shrink-0",
            isExpanded && "rotate-90"
          )}
        />
      </button>

      {/* Content — collapsible */}
      <AnimatePresence initial={false}>
        {isExpanded && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.15 }}
          >
            <div
              ref={contentRef}
              className={cn(
                "pl-5 pr-2 py-2 text-sm leading-relaxed italic",
                "border-l-2 border-[hsl(var(--assistant-border))]",
                "text-[hsl(var(--assistant-text-secondary))]",
                "whitespace-pre-wrap wrap-break-word",
                isStreaming && "max-h-48 overflow-y-auto"
              )}
            >
              {content}
              {isStreaming && (
                <span className="inline-block w-1.5 h-4 ml-0.5 bg-[hsl(var(--assistant-accent))] animate-pulse align-text-bottom rounded-sm not-italic" />
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
