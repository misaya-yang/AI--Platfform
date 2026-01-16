# AI Assistant Style & Layout Redesign

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Redesign the AI Assistant UI to add GPT/Grok-style personality presets and move settings into the input area for a cleaner experience.

**Architecture:** Remove right settings panel, consolidate controls into a control bar below the chat input. Add a style selector modal with 5 preset personality options.

**Tech Stack:** React, Tailwind CSS, Framer Motion, Radix UI (Dialog/Dropdown)

---

## Design Overview

### Layout Changes

**Before:**
```
┌────────────────────────────────────────────────────────────────────┐
│ Header                                                              │
├────────────┬───────────────────────────────────┬───────────────────┤
│ Left Panel │        Chat Area                  │ Right Panel       │
│ (History)  │                                   │ (Model/KB/Temp)   │
│            │        [Input]                    │                   │
└────────────┴───────────────────────────────────┴───────────────────┘
```

**After:**
```
┌────────────────────────────────────────────────────────────────────┐
│ Header                                                              │
├────────────┬───────────────────────────────────────────────────────┤
│ Left Panel │        Chat Area                                       │
│ (History)  │                                                        │
│            │        [Input]                                         │
│            │        [Model ▼] [Style ⚙] [KB ▼] [Web]               │
└────────────┴───────────────────────────────────────────────────────┘
```

### Style Presets (Grok-inspired)

| Style | Chinese | Description | System Prompt |
|-------|---------|-------------|---------------|
| Custom | 自定义 | No override | (none) |
| 简洁模式 | Concise | Short responses | "提供简短直接的回应，避免冗余。" |
| 正式模式 | Formal | Professional | "使用正式语气回答，保持专业严谨。" |
| 苏格拉底模式 | Socratic | Guided learning | "以引导学习的方式回答，通过提问引导用户思考。" |
| Comprehensive | 详尽模式 | Thorough | "提供详尽的解释和全面的分析。" |

---

## Implementation Tasks

### Task 1: Create Style Constants and Types

**Files:**
- Create: `web/src/pages/assistant/styles.ts`

**Step 1: Define style types and constants**

```typescript
export interface AssistantStyle {
  id: string;
  name: string;
  nameZh: string;
  description: string;
  descriptionZh: string;
  systemPrompt: string | null;
}

export const ASSISTANT_STYLES: AssistantStyle[] = [
  {
    id: "custom",
    name: "Custom",
    nameZh: "自定义",
    description: "Responds to you as you please.",
    descriptionZh: "根据您的喜好回应。",
    systemPrompt: null,
  },
  {
    id: "concise",
    name: "Concise",
    nameZh: "简洁模式",
    description: "Provides short and direct responses.",
    descriptionZh: "提供简短直接的回应。",
    systemPrompt: "提供简短直接的回应，避免冗余。保持简洁明了。",
  },
  {
    id: "formal",
    name: "Formal",
    nameZh: "正式模式",
    description: "Uses formal language to respond.",
    descriptionZh: "使用正式语气回答。",
    systemPrompt: "使用正式语气回答，保持专业严谨。避免口语化表达。",
  },
  {
    id: "socratic",
    name: "Socratic",
    nameZh: "苏格拉底模式",
    description: "Responds in a guided learning style.",
    descriptionZh: "以引导学习的方式回答。",
    systemPrompt: "以引导学习的方式回答，通过提问引导用户思考，而不是直接给出答案。",
  },
  {
    id: "comprehensive",
    name: "Comprehensive",
    nameZh: "详尽模式",
    description: "Responds with thorough explanations.",
    descriptionZh: "提供详尽的解释。",
    systemPrompt: "提供详尽的解释和全面的分析。包含相关背景知识和多角度分析。",
  },
];

export const DEFAULT_STYLE_ID = "custom";
```

**Step 2: Commit**

```bash
git add web/src/pages/assistant/styles.ts
git commit -m "feat(assistant): add style presets constants"
```

---

### Task 2: Create StyleSelector Component

**Files:**
- Create: `web/src/pages/assistant/components/StyleSelector.tsx`
- Modify: `web/src/pages/assistant/components/index.ts`

**Step 1: Create StyleSelector component**

```tsx
// StyleSelector.tsx - Modal for selecting assistant personality style

import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { Settings2, Check, X } from "lucide-react";
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
  const { t, i18n } = useTranslation();
  const isZh = i18n.language?.startsWith("zh");
  const currentStyle = ASSISTANT_STYLES.find((s) => s.id === selectedStyle);

  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 px-3 gap-2 rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 hover:bg-slate-50 dark:hover:bg-slate-700"
          disabled={disabled}
        >
          <Settings2 className="h-3.5 w-3.5 text-slate-500" />
          <span className="text-xs font-medium text-slate-600 dark:text-slate-300">
            {isZh ? currentStyle?.nameZh : currentStyle?.name}
          </span>
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-center">
            {t("assistant.styleTitle", "自定义回复风格")}
          </DialogTitle>
        </DialogHeader>
        <div className="grid grid-cols-1 gap-3 py-4">
          {ASSISTANT_STYLES.map((style) => (
            <StyleCard
              key={style.id}
              style={style}
              isSelected={selectedStyle === style.id}
              isZh={isZh}
              onClick={() => onSelect(style.id)}
            />
          ))}
        </div>
        <p className="text-xs text-center text-slate-500">
          {t("assistant.styleHint", "从上面选择一个风格，定制回复风格。")}
        </p>
      </DialogContent>
    </Dialog>
  );
}

function StyleCard({
  style,
  isSelected,
  isZh,
  onClick,
}: {
  style: AssistantStyle;
  isSelected: boolean;
  isZh: boolean;
  onClick: () => void;
}) {
  return (
    <motion.button
      whileHover={{ scale: 1.01 }}
      whileTap={{ scale: 0.99 }}
      onClick={onClick}
      className={cn(
        "relative flex items-start gap-3 p-4 rounded-xl border text-left transition-colors",
        isSelected
          ? "border-violet-500 bg-violet-50 dark:bg-violet-900/20"
          : "border-slate-200 dark:border-slate-700 hover:border-slate-300 dark:hover:border-slate-600"
      )}
    >
      {isSelected && (
        <div className="absolute top-3 right-3">
          <Check className="h-4 w-4 text-violet-600 dark:text-violet-400" />
        </div>
      )}
      <div className="flex-1">
        <div className="font-medium text-slate-800 dark:text-slate-200">
          {isZh ? style.nameZh : style.name}
        </div>
        <div className="text-sm text-slate-500 dark:text-slate-400 mt-1">
          {isZh ? style.descriptionZh : style.description}
        </div>
      </div>
    </motion.button>
  );
}
```

**Step 2: Export from index.ts**

Add to `web/src/pages/assistant/components/index.ts`:
```typescript
export { StyleSelector } from "./StyleSelector";
```

**Step 3: Commit**

```bash
git add web/src/pages/assistant/components/StyleSelector.tsx
git add web/src/pages/assistant/components/index.ts
git commit -m "feat(assistant): add StyleSelector modal component"
```

---

### Task 3: Create Compact Model Selector

**Files:**
- Create: `web/src/pages/assistant/components/CompactModelSelector.tsx`
- Modify: `web/src/pages/assistant/components/index.ts`

**Step 1: Create CompactModelSelector for input area**

```tsx
// CompactModelSelector.tsx - Compact dropdown for model selection in input area

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

interface CompactModelSelectorProps {
  models: ModelInfo[];
  selectedModel: string;
  onSelect: (modelId: string) => void;
  disabled?: boolean;
}

export function CompactModelSelector({
  models,
  selectedModel,
  onSelect,
  disabled,
}: CompactModelSelectorProps) {
  const { t } = useTranslation();
  const groupedModels = groupModelsByProvider(models);
  const selectedModelInfo = models.find((m) => m.id === selectedModel);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 px-3 gap-2 rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 hover:bg-slate-50 dark:hover:bg-slate-700"
          disabled={disabled || models.length === 0}
        >
          <Sparkles className="h-3.5 w-3.5 text-violet-500" />
          <span className="text-xs font-medium text-slate-600 dark:text-slate-300 max-w-[120px] truncate">
            {selectedModelInfo?.name || t("assistant.selectModel", "Select model")}
          </span>
          <ChevronDown className="h-3 w-3 text-slate-400" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className="w-[260px] max-h-[350px] overflow-y-auto rounded-xl"
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
                  {selectedModel === model.id ? (
                    <Check className="h-4 w-4 shrink-0 text-violet-500" />
                  ) : (
                    <div className="w-4" />
                  )}
                  <span className="truncate text-sm">{model.name}</span>
                </div>
                {model.supports_vision && (
                  <Badge variant="secondary" className="text-[9px] px-1 py-0">
                    Vision
                  </Badge>
                )}
              </DropdownMenuItem>
            ))}
          </div>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
```

**Step 2: Export from index.ts**

**Step 3: Commit**

---

### Task 4: Create Compact KB Selector

**Files:**
- Create: `web/src/pages/assistant/components/CompactKBSelector.tsx`
- Modify: `web/src/pages/assistant/components/index.ts`

**Step 1: Create CompactKBSelector dropdown with multi-select**

```tsx
// CompactKBSelector.tsx - Compact dropdown for KB selection with checkboxes

import { useTranslation } from "react-i18next";
import { Database, ChevronDown, Check } from "lucide-react";
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

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className={cn(
            "h-8 px-3 gap-2 rounded-full border bg-white dark:bg-slate-800 hover:bg-slate-50 dark:hover:bg-slate-700",
            selectedDatasets.length > 0
              ? "border-emerald-300 dark:border-emerald-700"
              : "border-slate-200 dark:border-slate-700"
          )}
          disabled={disabled}
        >
          <Database className={cn(
            "h-3.5 w-3.5",
            selectedDatasets.length > 0 ? "text-emerald-600 dark:text-emerald-400" : "text-slate-500"
          )} />
          <span className="text-xs font-medium text-slate-600 dark:text-slate-300">
            {t("assistant.kb", "知识库")}
          </span>
          {selectedDatasets.length > 0 && (
            <Badge className="h-4 min-w-4 px-1 text-[10px] bg-emerald-100 dark:bg-emerald-900/50 text-emerald-700 dark:text-emerald-300">
              {selectedDatasets.length}
            </Badge>
          )}
          <ChevronDown className="h-3 w-3 text-slate-400" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className="w-[240px] max-h-[300px] overflow-y-auto rounded-xl"
      >
        <DropdownMenuLabel className="text-xs text-slate-500">
          {t("assistant.selectKB", "选择知识库")}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {datasets.map((dataset) => (
          <DropdownMenuCheckboxItem
            key={dataset.dataset_id}
            checked={selectedDatasets.includes(dataset.dataset_id)}
            onCheckedChange={() => onToggle(dataset.dataset_id)}
            className="cursor-pointer"
          >
            <div className="flex flex-col">
              <span className="text-sm">{dataset.name}</span>
              {dataset.description && (
                <span className="text-xs text-slate-500 truncate max-w-[180px]">
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
```

**Step 2: Export and commit**

---

### Task 5: Create Web Search Toggle Button

**Files:**
- Create: `web/src/pages/assistant/components/WebSearchToggle.tsx`
- Modify: `web/src/pages/assistant/components/index.ts`

**Step 1: Create compact toggle button**

```tsx
// WebSearchToggle.tsx - Compact toggle button for web search

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
            "h-8 px-3 gap-2 rounded-full border",
            enabled
              ? "border-blue-300 dark:border-blue-700 bg-blue-50 dark:bg-blue-900/30"
              : "border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 hover:bg-slate-50 dark:hover:bg-slate-700"
          )}
          disabled={disabled}
        >
          <Globe className={cn(
            "h-3.5 w-3.5",
            enabled ? "text-blue-600 dark:text-blue-400" : "text-slate-500"
          )} />
          <span className={cn(
            "text-xs font-medium",
            enabled ? "text-blue-600 dark:text-blue-400" : "text-slate-600 dark:text-slate-300"
          )}>
            Web
          </span>
        </Button>
      </TooltipTrigger>
      <TooltipContent>
        {enabled
          ? t("assistant.webSearchOn", "点击关闭联网搜索")
          : t("assistant.webSearchOff", "点击开启联网搜索")}
      </TooltipContent>
    </Tooltip>
  );
}
```

**Step 2: Export and commit**

---

### Task 6: Refactor AssistantPage - Remove Right Panel

**Files:**
- Modify: `web/src/pages/assistant/index.tsx`

**Changes:**
1. Add style state: `const [selectedStyle, setSelectedStyle] = useState("custom")`
2. Import new components
3. Remove `showRightPanel` state and related code
4. Remove entire right panel `<motion.aside>` section
5. Remove right panel toggle button from header

**Step 1: Add imports and state**

```typescript
// Add imports
import { ASSISTANT_STYLES, DEFAULT_STYLE_ID } from "./styles";
import {
  StyleSelector,
  CompactModelSelector,
  CompactKBSelector,
  WebSearchToggle,
} from "./components";

// Add state
const [selectedStyle, setSelectedStyle] = useState(DEFAULT_STYLE_ID);
```

**Step 2: Remove right panel toggle from header**

Remove the right panel toggle button and `showRightPanel` state.

**Step 3: Remove entire right panel section**

Delete the `{showRightPanel && ...}` AnimatePresence block (~160 lines).

**Step 4: Commit**

---

### Task 7: Add Control Bar to Input Area

**Files:**
- Modify: `web/src/pages/assistant/index.tsx`

**Add below the input container (after `</div>` for input-container):**

```tsx
{/* Control Bar */}
<div className="flex items-center gap-2 mt-3 px-1 flex-wrap">
  <CompactModelSelector
    models={models}
    selectedModel={selectedModel}
    onSelect={setSelectedModel}
    disabled={isStreaming}
  />
  <StyleSelector
    selectedStyle={selectedStyle}
    onSelect={setSelectedStyle}
    disabled={isStreaming}
  />
  {config?.kb_enabled && datasets.length > 0 && (
    <CompactKBSelector
      datasets={datasets}
      selectedDatasets={selectedDatasets}
      onToggle={toggleDataset}
      disabled={isStreaming}
    />
  )}
  {config?.web_search_enabled && (
    <WebSearchToggle
      enabled={webSearchEnabled}
      onToggle={() => setWebSearchEnabled(!webSearchEnabled)}
      disabled={isStreaming}
    />
  )}
</div>
```

**Step 2: Remove the old status bar** (the line showing model name)

**Step 3: Commit**

---

### Task 8: Integrate Style into Chat Request

**Files:**
- Modify: `web/src/pages/assistant/index.tsx`

**In `sendMessage` function, modify the `chatStream` call:**

```typescript
// Get style system prompt
const stylePrompt = ASSISTANT_STYLES.find(s => s.id === selectedStyle)?.systemPrompt;

const stream = chatStream(
  {
    message: messageContent,
    session_id: sessionId || undefined,
    history,
    model_id: selectedModel,
    temperature,
    system_prompt: stylePrompt || undefined,  // Add this line
    kb_dataset_ids: selectedDatasets,
    // ... rest of params
  },
  abortControllerRef.current.signal
);
```

**Step 2: Verify API supports system_prompt parameter**

Check `web/src/api/assistant.ts` - if not present, add it to the request type.

**Step 3: Commit**

---

### Task 9: Add i18n Translations

**Files:**
- Modify: `web/src/i18n/locales/zh-CN.json`
- Modify: `web/src/i18n/locales/en-US.json`

**Add translations:**

```json
// zh-CN.json
{
  "assistant": {
    "styleTitle": "自定义回复风格",
    "styleHint": "从上面选择一个风格，定制 AI 的回复风格。",
    "kb": "知识库",
    "selectKB": "选择知识库",
    "webSearchOn": "点击关闭联网搜索",
    "webSearchOff": "点击开启联网搜索"
  }
}

// en-US.json
{
  "assistant": {
    "styleTitle": "Customize Response Style",
    "styleHint": "Select a style above to customize how AI responds.",
    "kb": "Knowledge Base",
    "selectKB": "Select Knowledge Base",
    "webSearchOn": "Click to disable web search",
    "webSearchOff": "Click to enable web search"
  }
}
```

---

### Task 10: Test and Verify

**Manual Testing Checklist:**
1. [ ] Model dropdown works and shows all providers
2. [ ] Style selector modal opens and closes properly
3. [ ] Style selection persists across messages
4. [ ] KB selector shows checkboxes and badges
5. [ ] Web search toggle highlights when active
6. [ ] All controls are disabled during streaming
7. [ ] Layout looks good on different screen sizes
8. [ ] Dark mode styling is consistent

**Commands:**
```bash
cd web && pnpm dev
# Open http://localhost:5173/assistant
# Test each control
```

---

## Files Changed Summary

**New Files:**
- `web/src/pages/assistant/styles.ts`
- `web/src/pages/assistant/components/StyleSelector.tsx`
- `web/src/pages/assistant/components/CompactModelSelector.tsx`
- `web/src/pages/assistant/components/CompactKBSelector.tsx`
- `web/src/pages/assistant/components/WebSearchToggle.tsx`

**Modified Files:**
- `web/src/pages/assistant/index.tsx`
- `web/src/pages/assistant/components/index.ts`
- `web/src/i18n/locales/zh-CN.json`
- `web/src/i18n/locales/en-US.json`
- `web/src/api/assistant.ts` (if system_prompt not supported)
