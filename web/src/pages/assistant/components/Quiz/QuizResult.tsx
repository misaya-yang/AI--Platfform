/**
 * QuizResult — Score summary, per-question review, retake, and attempts history.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { CheckCircle2, Clock, RefreshCw, Trophy, Users, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import { listAttempts, type QuizAttemptSummary } from "@/api/quiz";
import type { QuizAttemptResult } from "../../types";

interface QuizResultProps {
  result: QuizAttemptResult;
  quizId?: string;
  onReview?: () => void;
  onRetake?: () => void;
}

export function QuizResult({ result, quizId, onReview, onRetake }: QuizResultProps) {
  const { t } = useTranslation();
  const pct = Math.round(result.total_score * 100);
  const isGood = pct >= 70;
  const [showHistory, setShowHistory] = useState(false);
  const [attempts, setAttempts] = useState<QuizAttemptSummary[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);

  const fetchHistory = useCallback(async () => {
    if (!quizId || loadingHistory) return;
    setLoadingHistory(true);
    try {
      const data = await listAttempts(quizId);
      setAttempts(data.attempts);
      setShowHistory(true);
    } catch {
      // Silently fail — history is optional
    } finally {
      setLoadingHistory(false);
    }
  }, [quizId, loadingHistory]);

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

      {/* Action buttons */}
      <div className="flex items-center gap-2 flex-wrap">
        {onReview && (
          <button
            type="button"
            onClick={onReview}
            className="text-sm text-primary hover:underline"
          >
            {t("assistant.quiz.reviewAnswers", "Review Answers")}
          </button>
        )}
        {onRetake && (
          <button
            type="button"
            onClick={onRetake}
            className="inline-flex items-center gap-1 text-sm text-primary hover:underline"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            {t("assistant.quiz.retake", "Retake Quiz")}
          </button>
        )}
        {quizId && (
          <button
            type="button"
            onClick={fetchHistory}
            disabled={loadingHistory}
            className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground ml-auto"
          >
            <Users className="w-3.5 h-3.5" />
            {t("assistant.quiz.viewHistory", "History")}
          </button>
        )}
      </div>

      {/* Attempts history */}
      {showHistory && attempts.length > 0 && (
        <motion.div
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: "auto" }}
          className="rounded-xl border border-border overflow-hidden"
        >
          <div className="px-4 py-2 bg-muted/30 border-b border-border">
            <span className="text-xs font-medium text-muted-foreground">
              {t("assistant.quiz.attemptHistory", "Attempt History")} ({attempts.length})
            </span>
          </div>
          <div className="divide-y divide-border">
            {attempts.map((a) => (
              <div key={a.attempt_id} className="flex items-center gap-3 px-4 py-2 text-xs">
                <span className="font-medium text-foreground">
                  {a.display_name || a.user_id || "Anonymous"}
                </span>
                <span
                  className={cn(
                    "font-bold",
                    (a.total_score ?? 0) >= 0.7
                      ? "text-emerald-600 dark:text-emerald-400"
                      : "text-amber-600 dark:text-amber-400",
                  )}
                >
                  {a.correct_count}/{a.total_count} ({Math.round((a.total_score ?? 0) * 100)}%)
                </span>
                <span className="text-muted-foreground ml-auto flex items-center gap-1">
                  <Clock className="w-3 h-3" />
                  {a.completed_at
                    ? new Date(a.completed_at).toLocaleDateString(undefined, {
                        month: "short",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                      })
                    : "In progress"}
                </span>
              </div>
            ))}
          </div>
        </motion.div>
      )}
    </motion.div>
  );
}
