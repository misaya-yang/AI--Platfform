import { useRef, useCallback, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { ChevronDown, Send, Loader2, X, FileText, ImageIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { QuickActionsMenu } from "../components/QuickActionsMenu"; // Assume this exists or is moved
import { StyleSelector } from "../components/StyleSelector";
import type { UploadedFile } from "../hooks/useFileHandler";
import { isImageFile, formatFileSize } from "@/api/files";
import type { DatasetInfo, AssistantConfig } from "@/api/assistant";
import type { ModelReasoningOption } from "@/api/models";

const ASSISTANT_UI_V2 = import.meta.env.VITE_ASSISTANT_UI_V2 !== "false";

interface ChatInputAreaProps {
  composerId?: string;
  input: string;
  setInput: (val: string) => void;
  files: UploadedFile[];
  isUploading: boolean;
  isStreaming: boolean;
  isComposerBlocked: boolean;
  isGeneratingImage: boolean;
  isImageMode: boolean;
  hasAvailableModel: boolean;
  handleFileSelect: (files: FileList | null) => void;
  removeFile: (index: number) => void;
  onSend: () => void;
  onStop: () => void;
  handlePaste: (e: React.ClipboardEvent) => void;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  config: AssistantConfig | null;
  datasets: DatasetInfo[];
  selectedDatasets: string[];
  onToggleDataset: (id: string) => void;
  webSearchEnabled: boolean;
  setWebSearchEnabled: (val: boolean) => void;
  thinkingLevel: string;
  setThinkingLevel: (val: string) => void;
  reasoningOptions: ModelReasoningOption[];
  handleImageGenerate: () => void;
  selectedStyle: string;
  setSelectedStyle: (style: string) => void;
  onOpenConnectors?: () => void;
  connectorCount?: number;
}

export function ChatInputArea({
  composerId,
  input,
  setInput,
  files,
  isUploading,
  isStreaming,
  isComposerBlocked,
  isGeneratingImage,
  isImageMode,
  hasAvailableModel,
  handleFileSelect,
  removeFile,
  onSend,
  onStop,
  handlePaste,
  fileInputRef,
  config,
  datasets,
  selectedDatasets,
  onToggleDataset,
  webSearchEnabled,
  setWebSearchEnabled,
  thinkingLevel,
  setThinkingLevel,
  reasoningOptions,
  handleImageGenerate,
  selectedStyle,
  setSelectedStyle,
  onOpenConnectors,
  connectorCount = 0,
}: ChatInputAreaProps) {
  const { t } = useTranslation();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const shouldReduceMotion = useReducedMotion();

  const hasUploadedFiles = files.some((f) => f.status === "success" && f.response);
  const canSend =
    !isComposerBlocked &&
    !isUploading &&
    !isGeneratingImage &&
    hasAvailableModel &&
    Boolean(input.trim() || hasUploadedFiles);
  const selectedReasoningLabel =
    thinkingLevel === "auto"
      ? t("assistant.thinkingAuto", "Think auto")
      : reasoningOptions.find((option) => option.id === thinkingLevel)?.label || thinkingLevel;

  // Auto-resize textarea
  const handleTextareaChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      setInput(e.target.value);
      e.target.style.height = "auto";
      e.target.style.height = Math.min(e.target.scrollHeight, 200) + "px";
    },
    [setInput]
  );

  // Handle key press (check isComposing to avoid sending during IME composition)
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      // Don't send if user is composing with IME (e.g., typing Chinese/Japanese)
      if (e.nativeEvent.isComposing || e.keyCode === 229) {
        return;
      }
      if (e.key === "Escape" && isStreaming) {
        e.preventDefault();
        onStop();
        return;
      }
      const isSubmitShortcut =
        (e.key === "Enter" && !e.shiftKey) ||
        (e.key === "Enter" && (e.metaKey || e.ctrlKey));
      if (isSubmitShortcut && canSend) {
        e.preventDefault();
        onSend();
      }
    },
    [canSend, isStreaming, onSend, onStop]
  );

  // Reset height when input clears
  useEffect(() => {
    if (!input && textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  }, [input]);

  return (
    <div className="border-t border-[hsl(var(--assistant-border))] bg-[hsl(var(--assistant-canvas-bg))]/90 backdrop-blur-xl">
      {/* File previews */}
      <AnimatePresence>
        {files.length > 0 && (
          <motion.div
            initial={shouldReduceMotion ? false : { height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={shouldReduceMotion ? { opacity: 0 } : { height: 0, opacity: 0 }}
            className="overflow-hidden border-b border-[hsl(var(--assistant-border-soft))]"
          >
            <div className="px-4 py-3 flex flex-wrap gap-2">
              {files.map((f, index) => (
                <motion.div
                  key={`${f.file.name}-${index}`}
                  initial={shouldReduceMotion ? false : { opacity: 0, scale: 0.96 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={shouldReduceMotion ? { opacity: 0 } : { opacity: 0, scale: 0.96 }}
                  className={cn(
                    "relative group rounded-xl px-3 py-2 text-xs flex items-center gap-2 transition-colors overflow-hidden",
                    f.status === "error"
                      ? "bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400"
                      : f.status === "uploading"
                        ? "bg-primary/10 dark:bg-primary/20 text-primary dark:text-primary"
                        : f.status === "success"
                          ? "bg-emerald-100 dark:bg-emerald-900/30 text-emerald-600 dark:text-emerald-400"
                          : "bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-400"
                  )}
                >
                  {f.status === "uploading" && f.progress !== undefined && (
                    <motion.div
                      className="absolute inset-0 bg-current opacity-10"
                      initial={{ width: 0 }}
                      animate={{ width: `${f.progress}%` }}
                      transition={{ duration: 0.3 }}
                    />
                  )}
                  {isImageFile(f.file) ? (
                    <ImageIcon className="h-4 w-4 shrink-0 relative z-10" />
                  ) : (
                    <FileText className="h-4 w-4 shrink-0 relative z-10" />
                  )}
                  <span className="truncate max-w-[100px] relative z-10 font-medium">
                    {f.file.name}
                  </span>
                  <span className="text-[10px] opacity-70 relative z-10">
                    {f.status === "uploading" && f.progress !== undefined
                      ? `${f.progress}%`
                      : formatFileSize(f.file.size)}
                  </span>
                  <button
                    type="button"
                    onClick={() => removeFile(index)}
                    className="absolute -right-1 -top-1 z-20 rounded-full bg-slate-800 p-1 text-white opacity-70 transition-opacity hover:bg-slate-900 hover:opacity-100 focus-visible:opacity-100 dark:bg-slate-200 dark:text-slate-800 dark:hover:bg-white sm:opacity-0 sm:group-focus-within:opacity-100 sm:group-hover:opacity-100"
                    disabled={f.status === "uploading"}
                    aria-label={`${t("common.delete")}: ${f.file.name}`}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </motion.div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="px-3 pb-[max(12px,env(safe-area-inset-bottom))] pt-3 sm:p-4">
        <div className={cn("w-full mx-auto", ASSISTANT_UI_V2 ? "max-w-[760px]" : "max-w-3xl")}>
          {/* Input container — restrained: 10px radius, hairline border,
              no drop-shadow-sm at rest; focus-within swaps to accent-tinted
              ring without elevating the card. */}
          <div className="relative flex items-end gap-2 p-2 rounded-[10px] bg-[hsl(var(--assistant-surface-bg))] border border-[hsl(var(--assistant-border))] focus-within:border-[hsl(var(--assistant-accent))]/40 transition-colors duration-150">
            {/* Quick actions menu */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  aria-label={t("assistant.thinkingLevel", "Thinking")}
                  disabled={isComposerBlocked || isGeneratingImage}
                  className="h-9 min-w-[108px] max-w-[148px] shrink-0 justify-between gap-1.5 rounded-md border border-transparent bg-[hsl(var(--assistant-surface-soft))] px-2.5 text-[12px] font-medium text-[hsl(var(--assistant-text-secondary))] shadow-none hover:border-[hsl(var(--assistant-border))] hover:bg-[hsl(var(--assistant-chip-bg))] hover:text-[hsl(var(--assistant-text-primary))] focus-visible:border-[hsl(var(--assistant-accent))]/45 focus-visible:ring-[3px] focus-visible:ring-[hsl(var(--assistant-accent))]/15"
                >
                  <span className="truncate">{selectedReasoningLabel}</span>
                  <ChevronDown className="h-3.5 w-3.5 shrink-0 text-[hsl(var(--assistant-text-tertiary))]" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent
                side="top"
                align="start"
                sideOffset={8}
                className="min-w-[168px] border-[hsl(var(--assistant-border))] bg-[hsl(var(--assistant-surface-bg))] text-[hsl(var(--assistant-text-primary))]"
              >
                <DropdownMenuRadioGroup value={thinkingLevel} onValueChange={setThinkingLevel}>
                  <DropdownMenuRadioItem value="auto">
                    {t("assistant.thinkingAuto", "Think auto")}
                  </DropdownMenuRadioItem>
                  {reasoningOptions.map((option) => (
                    <DropdownMenuRadioItem key={option.id} value={option.id}>
                      {option.label}
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>
            <QuickActionsMenu
              onFileUpload={() => fileInputRef.current?.click()}
              onImageGenerate={handleImageGenerate}
              onToggleWebSearch={() => setWebSearchEnabled(!webSearchEnabled)}
              onOpenConnectors={onOpenConnectors}
              webSearchEnabled={webSearchEnabled}
              kbAvailable={config?.kb_enabled ?? false}
              webSearchAvailable={config?.web_search_enabled ?? false}
              disabled={isComposerBlocked || isGeneratingImage}
              connectorCount={connectorCount}
              datasets={datasets}
              selectedDatasets={selectedDatasets}
              onToggleDataset={onToggleDataset}
            />

            {/* Hidden file input */}
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept=".pdf,.docx,.doc,.md,.txt,.csv,.xlsx,.png,.jpg,.jpeg,.gif,.webp"
              className="hidden"
              disabled={isComposerBlocked || isUploading}
              onChange={(e) => {
                handleFileSelect(e.target.files);
                e.target.value = "";
              }}
            />

            {/* Text input */}
            <Textarea
              id={composerId}
              ref={textareaRef}
              value={input}
              onChange={handleTextareaChange}
              onKeyDown={handleKeyDown}
              onPaste={handlePaste}
              aria-label={t("assistant.composerAriaLabel", "Assistant message composer")}
              placeholder={
                isImageMode
                  ? t(
                      "assistant.imagePlaceholder",
                      "Describe the image you want to create... (ESC to cancel)"
                    )
                  : t(
                      hasAvailableModel
                        ? "assistant.placeholder"
                        : "assistant.noModelsPlaceholder",
                      hasAvailableModel
                        ? "Type your message... (Ctrl+V to paste images)"
                        : "No models available"
                    )
              }
              className={cn(
                "flex-1 min-h-[44px] max-h-[200px] resize-none border-0 bg-transparent focus-visible:ring-0 text-sm text-[hsl(var(--assistant-text-primary))]",
                isImageMode
                  ? "placeholder:text-[hsl(var(--assistant-accent))]"
                  : "placeholder:text-[hsl(var(--assistant-text-tertiary))]"
              )}
              disabled={isComposerBlocked || isGeneratingImage || !hasAvailableModel}
              rows={1}
            />

            {/* Send/Stop button — the ONE primary action on the page.
                Accent-tinted fill, 6px radius, no glow, no scale wiggle. */}
            {isStreaming ? (
              <Button
                type="button"
                size="icon"
                className="h-9 w-9 shrink-0 rounded-md bg-[hsl(var(--destructive))]/90 hover:bg-[hsl(var(--destructive))] text-white transition-colors duration-150"
                onClick={onStop}
                aria-label={t("assistant.stopGenerating", "Stop generating")}
                aria-keyshortcuts="Escape"
              >
                <X className="h-4 w-4" />
              </Button>
            ) : (
              <Button
                type="button"
                size="icon"
                className={cn(
                  "h-9 w-9 shrink-0 rounded-md transition-colors duration-150",
                  canSend
                    ? "bg-[hsl(var(--assistant-accent))]/15 hover:bg-[hsl(var(--assistant-accent))]/25 text-[hsl(var(--assistant-accent))] border border-[hsl(var(--assistant-accent))]/20"
                    : "bg-transparent text-[hsl(var(--assistant-text-tertiary))] cursor-not-allowed"
                )}
                onClick={onSend}
                disabled={!canSend}
                aria-label={t("common.send", "Send")}
                aria-keyshortcuts="Enter,Control+Enter,Meta+Enter"
              >
                {isUploading || isGeneratingImage ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : isImageMode ? (
                  <ImageIcon className="h-4 w-4" />
                ) : (
                  <Send className="h-4 w-4" />
                )}
              </Button>
            )}
          </div>

          {/* Control Bar - Style selector */}
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2 px-1">
            <div className="flex items-center gap-2">
              <StyleSelector
                selectedStyle={selectedStyle}
                onSelect={setSelectedStyle}
                disabled={isComposerBlocked}
              />
            </div>
            <span className="text-[11px] text-[hsl(var(--assistant-text-tertiary))]">
              {t("assistant.disclaimer", "AI responses may be inaccurate")}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
