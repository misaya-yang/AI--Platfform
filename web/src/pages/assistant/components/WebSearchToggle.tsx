/**
 * Web Search Toggle Component
 *
 * Compact toggle button for enabling/disabling web search in the input area control bar.
 */

import { useTranslation } from "react-i18next";
import { Globe } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

interface WebSearchToggleProps {
  enabled: boolean;
  onToggle: () => void;
  disabled?: boolean;
}

export function WebSearchToggle({
  enabled,
  onToggle,
  disabled,
}: WebSearchToggleProps) {
  const { t } = useTranslation();

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          onClick={onToggle}
          className={cn(
            "h-8 px-3 gap-2 rounded-full border transition-colors",
            enabled
              ? "border-blue-300 dark:border-blue-700 bg-blue-50 dark:bg-blue-900/30 hover:bg-blue-100 dark:hover:bg-blue-900/40"
              : "border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 hover:bg-slate-50 dark:hover:bg-slate-700"
          )}
          disabled={disabled}
        >
          <Globe
            className={cn(
              "h-3.5 w-3.5",
              enabled
                ? "text-blue-600 dark:text-blue-400"
                : "text-slate-500"
            )}
          />
          <span
            className={cn(
              "text-xs font-medium",
              enabled
                ? "text-blue-600 dark:text-blue-400"
                : "text-slate-600 dark:text-slate-300"
            )}
          >
            {t("assistant.webSearchLabel")}
          </span>
        </Button>
      </TooltipTrigger>
      <TooltipContent>
        {enabled ? t("assistant.webSearchOn") : t("assistant.webSearchOff")}
      </TooltipContent>
    </Tooltip>
  );
}
