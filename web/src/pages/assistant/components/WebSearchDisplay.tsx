/**
 * Web Search Display Component
 *
 * Shows web search results in a collapsible panel.
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { motion, AnimatePresence } from "framer-motion";
import { Globe, ChevronDown, ExternalLink } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { WebSearchResult } from "@/api/assistant";

interface WebSearchDisplayProps {
  results: WebSearchResult[];
}

export function WebSearchDisplay({ results }: WebSearchDisplayProps) {
  const [isOpen, setIsOpen] = useState(false);
  const { t } = useTranslation();

  if (!results || results.length === 0) return null;

  return (
    <div className="mb-4">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-2 text-xs font-medium text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200 transition-colors group"
      >
        <div className="flex items-center justify-center w-6 h-6 rounded-lg bg-gradient-to-br from-blue-500/20 to-cyan-500/20 group-hover:from-blue-500/30 group-hover:to-cyan-500/30 transition-colors">
          <Globe className="h-3.5 w-3.5 text-blue-600 dark:text-blue-400" />
        </div>
        <span>
          {results.length} {t("assistant.webResults", "web results")}
        </span>
        <motion.div
          animate={{ rotate: isOpen ? 180 : 0 }}
          transition={{ duration: 0.2 }}
        >
          <ChevronDown className="h-3.5 w-3.5" />
        </motion.div>
      </button>

      <AnimatePresence>
        {isOpen && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2, ease: "easeInOut" }}
            className="overflow-hidden"
          >
            <div className="mt-3 space-y-2 p-3 rounded-2xl bg-gradient-to-br from-blue-50/80 to-cyan-50/80 dark:from-blue-950/30 dark:to-cyan-950/30 border border-blue-200/50 dark:border-blue-800/30">
              {results.slice(0, 5).map((result, idx) => (
                <motion.div
                  key={idx}
                  initial={{ opacity: 0, x: -10 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: idx * 0.05 }}
                  className="text-xs p-3 rounded-xl bg-white/80 dark:bg-slate-900/50 border border-slate-200/60 dark:border-slate-700/40 backdrop-blur-sm"
                >
                  <div className="flex items-start justify-between gap-2">
                    <a
                      href={result.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-medium text-blue-600 dark:text-blue-400 hover:underline line-clamp-1 flex items-center gap-1"
                    >
                      {result.title}
                      <ExternalLink className="h-3 w-3 shrink-0 opacity-60" />
                    </a>
                    <Badge
                      variant="secondary"
                      className="text-[9px] px-1.5 py-0 shrink-0 bg-blue-100 dark:bg-blue-900/50 text-blue-700 dark:text-blue-300"
                    >
                      {(result.score * 100).toFixed(0)}%
                    </Badge>
                  </div>
                  <p className="line-clamp-2 text-slate-600 dark:text-slate-400 mt-1.5 leading-relaxed">
                    {result.content}
                  </p>
                </motion.div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
