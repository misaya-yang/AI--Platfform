/**
 * Prompt Suggestions Component
 *
 * Displays suggested prompt cards on the welcome screen.
 */

import { motion } from "framer-motion";
import { Badge } from "@/components/ui/badge";
import type { SuggestedPrompt } from "../types";

interface PromptCardProps {
  prompt: SuggestedPrompt;
  onClick: () => void;
}

export function PromptCard({ prompt, onClick }: PromptCardProps) {
  return (
    <motion.button
      whileHover={{ scale: 1.02, y: -2 }}
      whileTap={{ scale: 0.98 }}
      onClick={onClick}
      className="flex flex-col items-start gap-2 p-4 rounded-2xl bg-white dark:bg-slate-800/60 border border-slate-200/80 dark:border-slate-700/50 hover:border-violet-300 dark:hover:border-violet-700 hover:shadow-lg hover:shadow-violet-500/5 transition-all duration-200 text-left group"
    >
      <div className="flex items-center gap-2">
        <div className="flex items-center justify-center w-8 h-8 rounded-xl bg-gradient-to-br from-violet-500/10 to-purple-500/10 text-violet-600 dark:text-violet-400 group-hover:from-violet-500/20 group-hover:to-purple-500/20 transition-colors">
          {prompt.icon}
        </div>
        <Badge variant="secondary" className="text-[10px] px-1.5 py-0 bg-slate-100 dark:bg-slate-700 text-slate-500 dark:text-slate-400">
          {prompt.category}
        </Badge>
      </div>
      <span className="text-sm font-medium text-slate-700 dark:text-slate-200">
        {prompt.title}
      </span>
      <span className="text-xs text-slate-500 dark:text-slate-400 line-clamp-1">
        "{prompt.prompt}..."
      </span>
    </motion.button>
  );
}

interface PromptSuggestionsProps {
  prompts: SuggestedPrompt[];
  onPromptClick: (prompt: string) => void;
}

export function PromptSuggestions({ prompts, onPromptClick }: PromptSuggestionsProps) {
  return (
    <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
      {prompts.map((prompt, idx) => (
        <motion.div
          key={idx}
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: idx * 0.05 }}
        >
          <PromptCard
            prompt={prompt}
            onClick={() => onPromptClick(prompt.prompt)}
          />
        </motion.div>
      ))}
    </div>
  );
}
