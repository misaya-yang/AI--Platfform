/**
 * QuizQuestion — Renders a single multiple-choice question with selectable options.
 */

import { motion } from "framer-motion";
import { cn } from "@/lib/utils";
import type { QuizQuestionData } from "../../types";

interface QuizQuestionProps {
  question: QuizQuestionData;
  selectedAnswer: string | undefined;
  onSelect: (label: string) => void;
  disabled?: boolean;
  /** After grading: shows correct/incorrect indicators */
  result?: {
    correct: boolean;
    correct_answer: string;
    explanation?: string;
  };
}

const OPTION_LETTERS = ["A", "B", "C", "D", "E", "F"];

export function QuizQuestion({
  question,
  selectedAnswer,
  onSelect,
  disabled = false,
  result,
}: QuizQuestionProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25 }}
      className="space-y-4"
    >
      {/* Question text */}
      <p className="text-sm font-medium text-foreground leading-relaxed">
        {question.question_text}
      </p>

      {/* Options */}
      <div className="space-y-2">
        {question.options.map((option, idx) => {
          const label = option.label || OPTION_LETTERS[idx];
          const isSelected = selectedAnswer === label;
          const showResult = result != null;
          const isCorrectOption = showResult && result.correct_answer === label;
          const isWrong = showResult && isSelected && !result.correct;

          return (
            <button
              key={label}
              type="button"
              disabled={disabled || showResult}
              onClick={() => onSelect(label)}
              className={cn(
                "w-full flex items-start gap-3 rounded-xl px-4 py-3 text-left text-sm transition-all duration-150",
                "border",
                // Default state
                !isSelected && !showResult && "border-border bg-card hover:bg-muted/50",
                // Selected (before submit)
                isSelected && !showResult && "border-primary bg-primary/10 ring-1 ring-primary/30",
                // Correct answer (after submit)
                showResult && isCorrectOption && "border-emerald-500 bg-emerald-50 dark:bg-emerald-950/30",
                // Wrong answer (after submit)
                isWrong && "border-red-400 bg-red-50 dark:bg-red-950/30",
                // Unselected after submit
                showResult && !isCorrectOption && !isWrong && "border-border bg-card opacity-60",
                // Disabled
                (disabled || showResult) && "cursor-default",
              )}
            >
              {/* Letter badge */}
              <span
                className={cn(
                  "flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center text-xs font-bold",
                  !isSelected && !showResult && "bg-muted text-muted-foreground",
                  isSelected && !showResult && "bg-primary text-primary-foreground",
                  showResult && isCorrectOption && "bg-emerald-500 text-white",
                  isWrong && "bg-red-500 text-white",
                  showResult && !isCorrectOption && !isWrong && "bg-muted text-muted-foreground",
                )}
              >
                {label}
              </span>

              {/* Option text */}
              <span className="flex-1 pt-0.5">{option.text}</span>

              {/* Result indicator */}
              {showResult && isCorrectOption && (
                <span className="text-emerald-600 dark:text-emerald-400 text-xs font-medium pt-1">
                  Correct
                </span>
              )}
              {isWrong && (
                <span className="text-red-500 text-xs font-medium pt-1">
                  Wrong
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* Explanation (after grading) */}
      {result && result.explanation && (
        <motion.div
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: "auto" }}
          className="rounded-lg bg-muted/50 border border-border px-4 py-3"
        >
          <p className="text-xs font-medium text-muted-foreground mb-1">Explanation</p>
          <p className="text-sm text-foreground/80">{result.explanation}</p>
        </motion.div>
      )}
    </motion.div>
  );
}
