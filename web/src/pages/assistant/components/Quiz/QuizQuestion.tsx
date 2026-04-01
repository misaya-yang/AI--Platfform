/**
 * QuizQuestion — Renders a single question based on its type.
 *
 * Supports: mc_single, mc_multi, true_false, short_answer
 */

import { useCallback } from "react";
import { motion } from "framer-motion";
import { cn } from "@/lib/utils";
import type { QuizQuestionData } from "../../types";

interface QuizQuestionProps {
  question: QuizQuestionData;
  /** For mc_single/true_false: selected label. For mc_multi: comma-separated labels. For short_answer: text. */
  selectedAnswer: string | undefined;
  onSelect: (answer: string) => void;
  disabled?: boolean;
  result?: {
    correct: boolean;
    correct_answer: string;
    explanation?: string;
  };
}

export function QuizQuestion({
  question,
  selectedAnswer,
  onSelect,
  disabled = false,
  result,
}: QuizQuestionProps) {
  const qType = question.question_type || "mc_single";

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25 }}
      className="space-y-4"
    >
      <p className="text-sm font-medium text-foreground leading-relaxed">
        {question.question_text}
        {qType === "mc_multi" && (
          <span className="ml-1 text-xs text-muted-foreground">(Select all that apply)</span>
        )}
      </p>

      {qType === "short_answer" ? (
        <ShortAnswerInput
          value={selectedAnswer || ""}
          onChange={onSelect}
          disabled={disabled || result != null}
          result={result}
        />
      ) : (
        <OptionList
          options={question.options}
          questionType={qType}
          selectedAnswer={selectedAnswer || ""}
          onSelect={onSelect}
          disabled={disabled || result != null}
          result={result}
        />
      )}

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

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function OptionList({
  options,
  questionType,
  selectedAnswer,
  onSelect,
  disabled,
  result,
}: {
  options: QuizQuestionData["options"];
  questionType: string;
  selectedAnswer: string;
  onSelect: (answer: string) => void;
  disabled: boolean;
  result?: QuizQuestionProps["result"];
}) {
  const isMulti = questionType === "mc_multi";
  const selectedSet = new Set(
    isMulti
      ? selectedAnswer.split(",").filter(Boolean).map((l) => l.trim().toUpperCase())
      : selectedAnswer ? [selectedAnswer.toUpperCase()] : [],
  );

  const handleClick = useCallback(
    (label: string) => {
      if (disabled) return;
      if (isMulti) {
        const current = new Set(selectedSet);
        if (current.has(label.toUpperCase())) {
          current.delete(label.toUpperCase());
        } else {
          current.add(label.toUpperCase());
        }
        onSelect([...current].sort().join(","));
      } else {
        onSelect(label);
      }
    },
    [disabled, isMulti, selectedSet, onSelect],
  );

  const showResult = result != null;
  const correctSet = showResult
    ? new Set(result.correct_answer.split(",").map((l) => l.trim().toUpperCase()))
    : new Set<string>();

  return (
    <div className="space-y-2">
      {options.map((option) => {
        const label = option.label;
        const labelUp = label.toUpperCase();
        const isSelected = selectedSet.has(labelUp);
        const isCorrectOption = showResult && correctSet.has(labelUp);
        const isWrong = showResult && isSelected && !isCorrectOption;

        return (
          <button
            key={label}
            type="button"
            disabled={disabled}
            onClick={() => handleClick(label)}
            className={cn(
              "w-full flex items-start gap-3 rounded-xl px-4 py-3 text-left text-sm transition-all duration-150 border",
              !isSelected && !showResult && "border-border bg-card hover:bg-muted/50",
              isSelected && !showResult && "border-primary bg-primary/10 ring-1 ring-primary/30",
              showResult && isCorrectOption && "border-emerald-500 bg-emerald-50 dark:bg-emerald-950/30",
              isWrong && "border-red-400 bg-red-50 dark:bg-red-950/30",
              showResult && !isCorrectOption && !isWrong && "border-border bg-card opacity-60",
              disabled && "cursor-default",
            )}
          >
            {/* Badge */}
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
              {isMulti ? (isSelected ? "✓" : "") : label}
            </span>

            <span className="flex-1 pt-0.5">{option.text}</span>

            {showResult && isCorrectOption && (
              <span className="text-emerald-600 dark:text-emerald-400 text-xs font-medium pt-1">Correct</span>
            )}
            {isWrong && (
              <span className="text-red-500 text-xs font-medium pt-1">Wrong</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

function ShortAnswerInput({
  value,
  onChange,
  disabled,
  result,
}: {
  value: string;
  onChange: (val: string) => void;
  disabled: boolean;
  result?: QuizQuestionProps["result"];
}) {
  const showResult = result != null;

  return (
    <div className="space-y-2">
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        rows={3}
        placeholder="Type your answer..."
        className={cn(
          "w-full rounded-xl border px-4 py-3 text-sm bg-card focus:outline-none focus:ring-2 focus:ring-primary/30 resize-none transition-colors",
          !showResult && "border-border",
          showResult && result.correct && "border-emerald-500 bg-emerald-50 dark:bg-emerald-950/20",
          showResult && !result.correct && "border-red-400 bg-red-50 dark:bg-red-950/20",
          disabled && "cursor-default opacity-80",
        )}
      />
      {showResult && (
        <div className="flex items-center gap-2 text-xs">
          <span
            className={cn(
              "font-medium",
              result.correct ? "text-emerald-600 dark:text-emerald-400" : "text-red-500",
            )}
          >
            {result.correct ? "Correct" : "Incorrect"}
          </span>
          {!result.correct && result.correct_answer && (
            <span className="text-muted-foreground">
              Expected: {result.correct_answer}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
