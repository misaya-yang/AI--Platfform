/**
 * Quick Actions Menu Component
 *
 * ChatGPT-style popup menu for quick access to:
 * - File upload
 * - Knowledge base toggle
 * - Web search toggle
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { Plus, Paperclip, Database, Globe } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface QuickActionsMenuProps {
  onFileUpload: () => void;
  onToggleKB: () => void;
  onToggleWebSearch: () => void;
  kbEnabled: boolean;
  webSearchEnabled: boolean;
  kbAvailable: boolean;
  webSearchAvailable: boolean;
  selectedKBCount: number;
  disabled: boolean;
}

export function QuickActionsMenu({
  onFileUpload,
  onToggleKB,
  onToggleWebSearch,
  kbEnabled,
  webSearchEnabled,
  kbAvailable,
  webSearchAvailable,
  selectedKBCount,
  disabled,
}: QuickActionsMenuProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className={cn(
            "h-10 w-10 rounded-xl transition-all duration-200",
            open
              ? "bg-violet-100 dark:bg-violet-900/30 text-violet-600 dark:text-violet-400"
              : "hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-500 dark:text-slate-400"
          )}
          disabled={disabled}
        >
          <motion.div
            animate={{ rotate: open ? 45 : 0 }}
            transition={{ duration: 0.2 }}
          >
            <Plus className="h-5 w-5" />
          </motion.div>
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        side="top"
        className="w-64 p-2 rounded-2xl border-slate-200 dark:border-slate-700 shadow-xl"
      >
        <div className="space-y-1">
          {/* File Upload */}
          <button
            onClick={() => {
              onFileUpload();
              setOpen(false);
            }}
            className="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors text-left group"
          >
            <div className="flex items-center justify-center w-8 h-8 rounded-lg bg-purple-100 dark:bg-purple-900/30 text-purple-600 dark:text-purple-400 group-hover:bg-purple-200 dark:group-hover:bg-purple-900/50 transition-colors">
              <Paperclip className="h-4 w-4" />
            </div>
            <div>
              <div className="text-sm font-medium text-slate-700 dark:text-slate-200">
                {t("assistant.uploadFiles", "Upload files")}
              </div>
              <div className="text-xs text-slate-500 dark:text-slate-400">
                {t("assistant.uploadFilesDesc", "Images, PDFs, documents")}
              </div>
            </div>
          </button>

          {/* Knowledge Base */}
          {kbAvailable && (
            <button
              onClick={() => {
                onToggleKB();
                setOpen(false);
              }}
              className="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors text-left group"
            >
              <div className={cn(
                "flex items-center justify-center w-8 h-8 rounded-lg transition-colors",
                kbEnabled
                  ? "bg-emerald-100 dark:bg-emerald-900/30 text-emerald-600 dark:text-emerald-400"
                  : "bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400 group-hover:bg-emerald-100 dark:group-hover:bg-emerald-900/30 group-hover:text-emerald-600 dark:group-hover:text-emerald-400"
              )}>
                <Database className="h-4 w-4" />
              </div>
              <div className="flex-1">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium text-slate-700 dark:text-slate-200">
                    {t("assistant.knowledgeBase", "Knowledge Base")}
                  </span>
                  {selectedKBCount > 0 && (
                    <Badge className="bg-emerald-100 dark:bg-emerald-900/50 text-emerald-700 dark:text-emerald-300 text-[10px] px-1.5">
                      {selectedKBCount} {t("assistant.selected", "selected")}
                    </Badge>
                  )}
                </div>
                <div className="text-xs text-slate-500 dark:text-slate-400">
                  {t("assistant.kbDesc", "Search internal documents")}
                </div>
              </div>
            </button>
          )}

          {/* Web Search */}
          {webSearchAvailable && (
            <button
              onClick={() => {
                onToggleWebSearch();
                setOpen(false);
              }}
              className="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors text-left group"
            >
              <div className={cn(
                "flex items-center justify-center w-8 h-8 rounded-lg transition-colors",
                webSearchEnabled
                  ? "bg-blue-100 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400"
                  : "bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400 group-hover:bg-blue-100 dark:group-hover:bg-blue-900/30 group-hover:text-blue-600 dark:group-hover:text-blue-400"
              )}>
                <Globe className="h-4 w-4" />
              </div>
              <div className="flex-1">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium text-slate-700 dark:text-slate-200">
                    {t("assistant.webSearch", "Web Search")}
                  </span>
                  {webSearchEnabled && (
                    <Badge className="bg-blue-100 dark:bg-blue-900/50 text-blue-700 dark:text-blue-300 text-[10px] px-1.5">
                      {t("assistant.enabled", "Enabled")}
                    </Badge>
                  )}
                </div>
                <div className="text-xs text-slate-500 dark:text-slate-400">
                  {t("assistant.webSearchDesc", "Search the web for info")}
                </div>
              </div>
            </button>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
