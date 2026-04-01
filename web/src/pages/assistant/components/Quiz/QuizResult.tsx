/**
 * QuizResult — Score summary and per-question review after quiz submission.
 */

import { motion } from "framer-motion";
import { CheckCircle2, XCircle, Trophy } from "lucide-react";
import { cn } from "@/lib/utils";
import type { QuizAttemptResult } from "../../types";

interface QuizResultProps {
  result: QuizAttemptResult;
  onReview?: () => void;
}

export function QuizResult({ result, onReview }: QuizResultProps) {
  const pct = Math.round(result.total_score * 100);
  const isGood = pct >= 70;

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.3 }}
      className="space-y-4"
    >
      {/* Score header */}
      <div className="flex items-center gap-4 p-4 rounded-xl bg-muted/40 border border-border">
        <div
          className={cn(
            "flex items-center justify-center w-14 h-14 rounded-2xl",
            isGood
              ? "bg-emerald-100 dark:bg-emerald-950/40"
              : "bg-amber-100 dark:bg-amber-950/40",
          )}
        >
          <Trophy
            className={cn(
              "w-7 h-7",
              isGood
                ? "text-emerald-600 dark:text-emerald-400"
                : "text-amber-600 dark:text-amber-400",
            )}
          />
        </div>
        <div className="flex-1">
          <div className="text-2xl font-bold text-foreground">
            {result.correct_count}/{result.total_count}
          </div>
          <div className="text-sm text-muted-foreground">
            {pct}% correct
          </div>
        </div>

        {/* Progress bar */}
        <div className="w-24 h-2 rounded-full bg-muted overflow-hidden">
          <motion.div
            initial={{ width: 0 }}
            animate={{ width: `${pct}%` }}
            transition={{ duration: 0.6, delay: 0.2 }}
            className={cn(
              "h-full rounded-full",
              isGood ? "bg-emerald-500" : "bg-amber-500",
            )}
          />
        </div>
      </div>

      {/* Per-question summary */}
      <div className="space-y-1.5">
        {result.per_question.map((pq) => (
          <div
            key={pq.question_id}
            className={cn(
              "flex items-center gap-2 px-3 py-2 rounded-lg text-sm",
              pq.correct
                ? "bg-emerald-50 dark:bg-emerald-950/20 text-emerald-700 dark:text-emerald-300"
                : "bg-red-50 dark:bg-red-950/20 text-red-700 dark:text-red-300",
            )}
          >
            {pq.correct ? (
              <CheckCircle2 className="w-4 h-4 flex-shrink-0" />
            ) : (
              <XCircle className="w-4 h-4 flex-shrink-0" />
            )}
            <span className="font-medium">Q{pq.question_num}</span>
            <span className="text-xs opacity-70 truncate flex-1">
              {pq.correct ? "Correct" : `Wrong (you: ${pq.user_answer}, answer: ${pq.correct_answer})`}
            </span>
          </div>
        ))}
      </div>

      {/* Review button */}
      {onReview && (
        <button
          type="button"
          onClick={onReview}
          className="w-full text-center text-sm text-primary hover:underline py-1"
        >
          Review Answers
        </button>
      )}
    </motion.div>
  );
}
