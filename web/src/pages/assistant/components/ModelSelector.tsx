/**
 * Model Selector Component
 *
 * Dropdown for selecting LLM models, grouped by provider.
 */

import { useTranslation } from "react-i18next";
import { Sparkles, ChevronDown, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Badge } from "@/components/ui/badge";
import {
  groupModelsByProvider,
  getProviderDisplayName,
  type ModelInfo,
} from "@/api/assistant";

interface ModelSelectorProps {
  models: ModelInfo[];
  selectedModel: string;
  onSelect: (modelId: string) => void;
  disabled?: boolean;
}

export function ModelSelector({
  models,
  selectedModel,
  onSelect,
  disabled,
}: ModelSelectorProps) {
  const { t } = useTranslation();
  const groupedModels = groupModelsByProvider(models);
  const selectedModelInfo = models.find((m) => m.id === selectedModel);

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <div className="flex items-center justify-center w-8 h-8 rounded-xl bg-primary/10">
          <Sparkles className="h-4 w-4 text-primary" />
        </div>
        <span className="text-sm font-semibold text-slate-700 dark:text-slate-200">
          {t("assistant.model", "Model")}
        </span>
      </div>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="outline"
            className="w-full justify-between gap-2 font-normal h-11 rounded-xl border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800/50 hover:bg-slate-50 dark:hover:bg-slate-800"
            disabled={disabled || models.length === 0}
          >
            <span className="truncate text-slate-700 dark:text-slate-200">
              {selectedModelInfo?.name || t("assistant.selectModel", "Select model")}
            </span>
            <ChevronDown className="h-4 w-4 shrink-0 opacity-50" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="start"
          className="w-[280px] max-h-[400px] overflow-y-auto rounded-xl"
        >
          {Object.entries(groupedModels).map(([provider, providerModels], index) => (
            <div key={provider}>
              {index > 0 && <DropdownMenuSeparator />}
              <DropdownMenuLabel className="text-xs text-slate-500 font-medium">
                {getProviderDisplayName(provider)}
              </DropdownMenuLabel>
              {providerModels.map((model) => (
                <DropdownMenuItem
                  key={model.id}
                  className="flex items-center justify-between gap-2 cursor-pointer rounded-lg"
                  onClick={() => onSelect(model.id)}
                >
                  <div className="flex items-center gap-2 min-w-0">
                    {selectedModel === model.id && (
                      <Check className="h-4 w-4 shrink-0 text-primary" />
                    )}
                    {selectedModel !== model.id && <div className="w-4" />}
                    <span className="truncate">{model.name}</span>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    {model.supports_vision && (
                      <Badge variant="secondary" className="text-[9px] px-1 py-0 bg-primary/10 dark:bg-primary/20 text-primary dark:text-primary/80">
                        Vision
                      </Badge>
                    )}
                  </div>
                </DropdownMenuItem>
              ))}
            </div>
          ))}
          {models.length === 0 && (
            <div className="px-2 py-4 text-sm text-slate-500 text-center">
              {t("assistant.noModels", "No models available")}
            </div>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
