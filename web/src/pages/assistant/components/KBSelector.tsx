/**
 * Knowledge Base Selector Component
 *
 * Multi-select list for knowledge base datasets.
 */

import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { Database, FileText, Image as ImageIcon } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { DatasetInfo } from "@/api/assistant";

interface KBSelectorProps {
  datasets: DatasetInfo[];
  selectedDatasets: string[];
  onToggle: (datasetId: string) => void;
  disabled?: boolean;
}

export function KBSelector({
  datasets,
  selectedDatasets,
  onToggle,
  disabled,
}: KBSelectorProps) {
  const { t } = useTranslation();

  if (datasets.length === 0) {
    return (
      <div className="p-4 text-center text-sm text-slate-500 rounded-xl bg-slate-50 dark:bg-slate-800/30">
        <Database className="h-8 w-8 mx-auto mb-2 opacity-40" />
        <p>{t("assistant.noKB", "No knowledge bases available")}</p>
      </div>
    );
  }

  return (
    <div className="space-y-2 max-h-[250px] overflow-y-auto pr-1">
      {datasets.map((dataset) => {
        const isSelected = selectedDatasets.includes(dataset.dataset_id);
        return (
          <motion.label
            key={dataset.dataset_id}
            whileHover={{ scale: 1.01 }}
            whileTap={{ scale: 0.99 }}
            className={cn(
              "flex items-start gap-3 p-3 rounded-xl cursor-pointer transition-all border",
              isSelected
                ? "bg-emerald-50 dark:bg-emerald-900/20 border-emerald-200 dark:border-emerald-800/50"
                : "bg-white dark:bg-slate-800/30 border-slate-200 dark:border-slate-700/50 hover:border-slate-300 dark:hover:border-slate-600",
              disabled && "opacity-50 cursor-not-allowed"
            )}
          >
            <Checkbox
              checked={isSelected}
              onCheckedChange={() => !disabled && onToggle(dataset.dataset_id)}
              disabled={disabled}
              className="mt-0.5"
            />
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-slate-700 dark:text-slate-200 truncate">
                  {dataset.name}
                </span>
                {dataset.is_multimodal && (
                  <Badge className="bg-violet-100 dark:bg-violet-900/40 text-violet-600 dark:text-violet-300 border-violet-200 dark:border-violet-800/50 text-[9px] px-1.5 py-0 h-4 gap-0.5 shrink-0">
                    <ImageIcon className="h-2.5 w-2.5" />
                    多模态
                  </Badge>
                )}
              </div>
              {dataset.description && (
                <div className="text-xs text-slate-500 dark:text-slate-400 line-clamp-1 mt-0.5">
                  {dataset.description}
                </div>
              )}
              <div className="flex items-center gap-3 mt-1.5 text-[10px] text-slate-400 dark:text-slate-500">
                <span className="flex items-center gap-1">
                  <FileText className="h-3 w-3" />
                  {dataset.document_count} docs
                </span>
                <span className="flex items-center gap-1">
                  <Database className="h-3 w-3" />
                  {dataset.chunk_count} chunks
                </span>
              </div>
            </div>
          </motion.label>
        );
      })}
    </div>
  );
}
