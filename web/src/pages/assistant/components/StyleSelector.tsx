/**
 * Style Selector Component
 *
 * Modal dialog for selecting assistant personality style.
 * Inspired by Grok's style customization feature.
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { Settings2, Check } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { ASSISTANT_STYLES, type AssistantStyle } from "../styles";

interface StyleSelectorProps {
  selectedStyle: string;
  onSelect: (styleId: string) => void;
  disabled?: boolean;
}

export function StyleSelector({
  selectedStyle,
  onSelect,
  disabled,
}: StyleSelectorProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const currentStyle = ASSISTANT_STYLES.find((s) => s.id === selectedStyle);

  const handleSelect = (styleId: string) => {
    onSelect(styleId);
    setOpen(false);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 px-3 gap-2 rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 hover:bg-slate-50 dark:hover:bg-slate-700"
          disabled={disabled}
        >
          <Settings2 className="h-3.5 w-3.5 text-slate-500" />
          <span className="text-xs font-medium text-slate-600 dark:text-slate-300">
            {currentStyle ? t(currentStyle.nameKey) : ""}
          </span>
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-center">
            {t("assistant.styleTitle")}
          </DialogTitle>
        </DialogHeader>
        <div className="grid grid-cols-1 gap-3 py-4">
          {ASSISTANT_STYLES.map((style) => (
            <StyleCard
              key={style.id}
              style={style}
              isSelected={selectedStyle === style.id}
              onClick={() => handleSelect(style.id)}
            />
          ))}
        </div>
        <p className="text-xs text-center text-slate-500 dark:text-slate-400">
          {t("assistant.styleHint")}
        </p>
      </DialogContent>
    </Dialog>
  );
}

function StyleCard({
  style,
  isSelected,
  onClick,
}: {
  style: AssistantStyle;
  isSelected: boolean;
  onClick: () => void;
}) {
  const { t } = useTranslation();
  return (
    <motion.button
      whileHover={{ scale: 1.01 }}
      whileTap={{ scale: 0.99 }}
      onClick={onClick}
      className={cn(
        "relative flex items-start gap-3 p-4 rounded-xl border text-left transition-colors",
        isSelected
          ? "border-violet-500 bg-violet-50 dark:bg-violet-900/20"
          : "border-slate-200 dark:border-slate-700 hover:border-slate-300 dark:hover:border-slate-600 bg-white dark:bg-slate-800"
      )}
    >
      {isSelected && (
        <div className="absolute top-3 right-3">
          <Check className="h-4 w-4 text-violet-600 dark:text-violet-400" />
        </div>
      )}
      <div className="flex-1 pr-6">
        <div className="font-medium text-slate-800 dark:text-slate-200">
          {t(style.nameKey)}
        </div>
        <div className="text-sm text-slate-500 dark:text-slate-400 mt-1">
          {t(style.descriptionKey)}
        </div>
      </div>
    </motion.button>
  );
}
