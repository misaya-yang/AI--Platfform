/**
 * Search Status Component
 *
 * Displays the current search status during KB/web retrieval.
 * Shows animated indicators similar to GPT's "Searching..." display.
 */

import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { motion, AnimatePresence } from "framer-motion";
import { Database, Globe, Loader2, CheckCircle2, XCircle, FileText } from "lucide-react";
import { cn } from "@/lib/utils";

export type SearchType = "kb" | "web" | "files";
export type SearchState = "searching" | "completed" | "error";

interface SearchStatusItem {
  type: SearchType;
  state: SearchState;
  query?: string;
  datasets?: string[];
  resultCount?: number;
  durationMs?: number;
  error?: string;
}

interface SearchStatusProps {
  items: SearchStatusItem[];
  className?: string;
}

function SearchIcon({ type, state }: { type: SearchType; state: SearchState }) {
  const isSearching = state === "searching";

  if (type === "kb") {
    return (
      <div className="relative">
        {isSearching ? (
          <Loader2 className="h-4 w-4 animate-spin text-emerald-500" />
        ) : state === "completed" ? (
          <CheckCircle2 className="h-4 w-4 text-emerald-500" />
        ) : (
          <XCircle className="h-4 w-4 text-red-500" />
        )}
      </div>
    );
  }

  if (type === "files") {
    return (
      <div className="relative">
        {isSearching ? (
          <Loader2 className="h-4 w-4 animate-spin text-violet-500" />
        ) : state === "completed" ? (
          <CheckCircle2 className="h-4 w-4 text-violet-500" />
        ) : (
          <XCircle className="h-4 w-4 text-red-500" />
        )}
      </div>
    );
  }

  // type === "web"
  return (
    <div className="relative">
      {isSearching ? (
        <Loader2 className="h-4 w-4 animate-spin text-blue-500" />
      ) : state === "completed" ? (
        <CheckCircle2 className="h-4 w-4 text-blue-500" />
      ) : (
        <XCircle className="h-4 w-4 text-red-500" />
      )}
    </div>
  );
}

function SearchStatusItem({ item }: { item: SearchStatusItem }) {
  const { t } = useTranslation();
  const isKB = item.type === "kb";
  const isFiles = item.type === "files";
  const isSearching = item.state === "searching";

  const getStatusText = () => {
    if (isSearching) {
      if (isKB) {
        return t("assistant.searchingKB", "Searching knowledge base...");
      }
      if (isFiles) {
        return t("assistant.processingFiles", "Processing files...");
      }
      return t("assistant.searchingWeb", "Searching the web...");
    }

    if (item.state === "error") {
      return item.error || t("assistant.searchError", "Search failed");
    }

    // Completed
    if (isKB && item.resultCount !== undefined) {
      return t("assistant.kbResultsFound", "Found {{count}} sources", {
        count: item.resultCount,
      });
    }

    if (isFiles && item.resultCount !== undefined) {
      return t("assistant.filesProcessed", "Processed {{count}} files", {
        count: item.resultCount,
      });
    }

    if (item.type === "web" && item.resultCount !== undefined) {
      return t("assistant.webResultsFound", "Found {{count}} results", {
        count: item.resultCount,
      });
    }

    return t("assistant.searchComplete", "Search complete");
  };

  const getQueryDisplay = () => {
    if (!item.query) return null;

    const maxLength = 50;
    const displayQuery =
      item.query.length > maxLength
        ? `${item.query.slice(0, maxLength)}...`
        : item.query;

    return (
      <span className="text-muted-foreground/60">
        {t("assistant.searchingFor", 'for "{{query}}"', { query: displayQuery })}
      </span>
    );
  };

  // Determine background color based on type and state
  const getBgClass = () => {
    if (isSearching) return "bg-slate-100 dark:bg-slate-800/60";
    if (item.state === "error") return "bg-red-50 dark:bg-red-900/20";
    if (isFiles) return "bg-violet-50 dark:bg-violet-900/20";
    return "bg-emerald-50 dark:bg-emerald-900/20";
  };

  // Determine text color based on type and state
  const getTextClass = () => {
    if (isSearching) return "text-slate-700 dark:text-slate-300";
    if (item.state === "error") return "text-red-700 dark:text-red-300";
    if (isFiles) return "text-violet-700 dark:text-violet-300";
    return "text-emerald-700 dark:text-emerald-300";
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -10 }}
      className={cn(
        "flex items-center gap-2 px-3 py-2 rounded-lg text-sm",
        getBgClass()
      )}
    >
      <SearchIcon type={item.type} state={item.state} />

      <div className="flex flex-col gap-0.5 min-w-0">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className={cn("font-medium", getTextClass())}>
            {getStatusText()}
          </span>
          {isSearching && getQueryDisplay()}
        </div>

        {/* Dataset names for KB search */}
        {isKB && item.datasets && item.datasets.length > 0 && (
          <div className="flex items-center gap-1 text-xs text-muted-foreground/60">
            <Database className="h-3 w-3" />
            <span className="truncate">
              {item.datasets.length === 1
                ? item.datasets[0]
                : t("assistant.datasetsCount", "{{count}} datasets", {
                    count: item.datasets.length,
                  })}
            </span>
          </div>
        )}

        {/* Duration */}
        {!isSearching && item.durationMs !== undefined && (
          <span className="text-xs text-muted-foreground/50">
            {(item.durationMs / 1000).toFixed(2)}s
          </span>
        )}
      </div>
    </motion.div>
  );
}

export function SearchStatus({ items, className }: SearchStatusProps) {
  if (items.length === 0) return null;

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <AnimatePresence mode="popLayout">
        {items.map((item, index) => (
          <SearchStatusItem key={`${item.type}-${index}`} item={item} />
        ))}
      </AnimatePresence>
    </div>
  );
}

/**
 * Hook to manage search status state
 */
export function useSearchStatus() {
  const [items, setItems] = useState<SearchStatusItem[]>([]);

  const startKBSearch = (query: string, datasets: string[]) => {
    setItems((prev) => [
      ...prev,
      {
        type: "kb",
        state: "searching",
        query,
        datasets,
      },
    ]);
  };

  const completeKBSearch = (resultCount: number, durationMs: number) => {
    setItems((prev) =>
      prev.map((item) =>
        item.type === "kb" && item.state === "searching"
          ? { ...item, state: "completed", resultCount, durationMs }
          : item
      )
    );
  };

  const failKBSearch = (error: string) => {
    setItems((prev) =>
      prev.map((item) =>
        item.type === "kb" && item.state === "searching"
          ? { ...item, state: "error", error }
          : item
      )
    );
  };

  const startWebSearch = (query: string) => {
    setItems((prev) => [
      ...prev,
      {
        type: "web",
        state: "searching",
        query,
      },
    ]);
  };

  const completeWebSearch = (resultCount: number, durationMs: number) => {
    setItems((prev) =>
      prev.map((item) =>
        item.type === "web" && item.state === "searching"
          ? { ...item, state: "completed", resultCount, durationMs }
          : item
      )
    );
  };

  const failWebSearch = (error: string) => {
    setItems((prev) =>
      prev.map((item) =>
        item.type === "web" && item.state === "searching"
          ? { ...item, state: "error", error }
          : item
      )
    );
  };

  const reset = () => {
    setItems([]);
  };

  return {
    items,
    startKBSearch,
    completeKBSearch,
    failKBSearch,
    startWebSearch,
    completeWebSearch,
    failWebSearch,
    reset,
  };
}

export default SearchStatus;
