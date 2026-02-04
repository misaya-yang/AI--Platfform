/**
 * Compact KB Selector Component
 *
 * Multi-select dropdown for knowledge base selection in the input area control bar.
 */

import { useTranslation } from "react-i18next";
import { Database, ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuCheckboxItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Badge } from "@/components/ui/badge";
import type { DatasetInfo } from "@/api/assistant";

interface CompactKBSelectorProps {
  datasets: DatasetInfo[];
  selectedDatasets: string[];
  onToggle: (datasetId: string) => void;
  disabled?: boolean;
}

export function CompactKBSelector({
  datasets,
  selectedDatasets,
  onToggle,
  disabled,
}: CompactKBSelectorProps) {
  const { t } = useTranslation();

  if (datasets.length === 0) return null;

  const hasSelection = selectedDatasets.length > 0;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className={cn(
            "h-8 px-3 gap-2 rounded-full border bg-white dark:bg-slate-800 hover:bg-slate-50 dark:hover:bg-slate-700",
            hasSelection
              ? "border-emerald-300 dark:border-emerald-700"
              : "border-slate-200 dark:border-slate-700"
          )}
          disabled={disabled}
        >
          <Database
            className={cn(
              "h-3.5 w-3.5",
              hasSelection
                ? "text-emerald-600 dark:text-emerald-400"
                : "text-slate-500"
            )}
          />
          <span
            className={cn(
              "text-xs font-medium",
              hasSelection
                ? "text-emerald-600 dark:text-emerald-400"
                : "text-slate-600 dark:text-slate-300"
            )}
          >
            {t("assistant.kb")}
          </span>
          {hasSelection && (
            <Badge className="h-4 min-w-4 px-1 text-[10px] bg-emerald-100 dark:bg-emerald-900/50 text-emerald-700 dark:text-emerald-300 border-0">
              {selectedDatasets.length}
            </Badge>
          )}
          <ChevronDown className="h-3 w-3 text-slate-400" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className="w-[260px] max-h-[300px] overflow-y-auto rounded-xl"
      >
        <DropdownMenuLabel className="text-xs text-slate-500">
          {t("assistant.selectKB")}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {datasets.map((dataset) => (
          <DropdownMenuCheckboxItem
            key={dataset.dataset_id}
            checked={selectedDatasets.includes(dataset.dataset_id)}
            onCheckedChange={() => onToggle(dataset.dataset_id)}
            className="cursor-pointer"
          >
            <div className="flex flex-col min-w-0">
              <span className="text-sm truncate">{dataset.name}</span>
              {dataset.description && (
                <span className="text-xs text-slate-500 dark:text-slate-400 truncate">
                  {dataset.description}
                </span>
              )}
            </div>
          </DropdownMenuCheckboxItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
