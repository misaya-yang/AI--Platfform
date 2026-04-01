/**
 * QuizCard — Main quiz container rendered within the chat stream.
 *
 * Shows quiz questions one at a time, handles answer selection,
 * submits to backend for grading, and displays results.
 */

import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { motion, AnimatePresence } from "framer-motion";
import {
  BookOpen,
  ChevronLeft,
  ChevronRight,
  Link2,
  Loader2,
  Send,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { submitQuiz } from "@/api/quiz";
import type { QuizData, QuizAttemptResult } from "../../types";
import { QuizQuestion } from "./QuizQuestion";
import { QuizResult } from "./QuizResult";
import { QuizShareDialog } from "./QuizShareDialog";

interface QuizCardProps {
  quizData: QuizData;
  /** Callback to store result on the ChatMessage */
  onResult?: (result: QuizAttemptResult) => void;
  /** Pre-existing result (if already submitted) */
  existingResult?: QuizAttemptResult;
}

type ViewMode = "quiz" | "result" | "review";

export function QuizCard({ quizData, onResult, existingResult }: QuizCardProps) {
  const { t } = useTranslation();
  const [currentIndex, setCurrentIndex] = useState(0);
  const [selectedAnswers, setSelectedAnswers] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<QuizAttemptResult | undefined>(existingResult);
  const [viewMode, setViewMode] = useState<ViewMode>(existingResult ? "result" : "quiz");
  const [reviewIndex, setReviewIndex] = useState(0);
  const [showShareDialog, setShowShareDialog] = useState(false);

  const questions = quizData.questions;
  const totalQuestions = questions.length;
  const currentQuestion = questions[viewMode === "review" ? reviewIndex : currentIndex];
  const allAnswered = questions.every((q) => selectedAnswers[q.id]);

  const handleSelect = useCallback(
    (label: string) => {
      if (result) return;
      setSelectedAnswers((prev) => ({
        ...prev,
        [currentQuestion.id]: label,
      }));
    },
    [currentQuestion, result],
  );

  const handleSubmit = useCallback(async () => {
    if (submitting || !allAnswered) return;
    setSubmitting(true);
    try {
      const res = await submitQuiz(quizData.quiz_id, selectedAnswers);
      setResult(res);
      setViewMode("result");
      onResult?.(res);
    } catch (err) {
      console.error("Quiz submit failed:", err);
    } finally {
      setSubmitting(false);
    }
  }, [submitting, allAnswered, quizData.quiz_id, selectedAnswers, onResult]);

  const handleReview = useCallback(() => {
    setReviewIndex(0);
    setViewMode("review");
  }, []);

  const handleBackToResult = useCallback(() => {
    setViewMode("result");
  }, []);

  // Find per-question result for review mode
  const getQuestionResult = (questionId: string) => {
    if (!result) return undefined;
    return result.per_question.find((pq) => pq.question_id === questionId);
  };

  return (
    <div className="rounded-2xl border border-border bg-card shadow-sm overflow-hidden max-w-lg">
      {/* Header */}
      <div className="flex items-center gap-3 px-5 py-3 bg-muted/30 border-b border-border">
        <div className="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center">
          <BookOpen className="w-4 h-4 text-primary" />
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-semibold text-foreground truncate">
            {quizData.title}
          </h3>
          <p className="text-xs text-muted-foreground">
            {totalQuestions} {t("assistant.quiz.questions", "questions")}
            {quizData.difficulty && ` · ${quizData.difficulty}`}
          </p>
        </div>
      </div>

      <div className="p-5">
        <AnimatePresence mode="wait">
          {/* Quiz mode: one question at a time */}
          {viewMode === "quiz" && currentQuestion && (
            <motion.div key={`q-${currentIndex}`}>
              {/* Progress */}
              <div className="flex items-center gap-2 mb-4">
                <span className="text-xs font-medium text-muted-foreground">
                  {currentIndex + 1} / {totalQuestions}
                </span>
                <div className="flex-1 h-1.5 rounded-full bg-muted overflow-hidden">
                  <div
                    className="h-full rounded-full bg-primary transition-all duration-300"
                    style={{ width: `${((currentIndex + 1) / totalQuestions) * 100}%` }}
                  />
                </div>
              </div>

              <QuizQuestion
                question={currentQuestion}
                selectedAnswer={selectedAnswers[currentQuestion.id]}
                onSelect={handleSelect}
              />

              {/* Navigation */}
              <div className="flex items-center justify-between mt-5 pt-4 border-t border-border">
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={currentIndex === 0}
                  onClick={() => setCurrentIndex((i) => i - 1)}
                  className="gap-1"
                >
                  <ChevronLeft className="w-4 h-4" />
                  {t("assistant.quiz.prev", "Previous")}
                </Button>

                {currentIndex < totalQuestions - 1 ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={!selectedAnswers[currentQuestion.id]}
                    onClick={() => setCurrentIndex((i) => i + 1)}
                    className="gap-1"
                  >
                    {t("assistant.quiz.next", "Next")}
                    <ChevronRight className="w-4 h-4" />
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    disabled={!allAnswered || submitting}
                    onClick={handleSubmit}
                    className="gap-1.5"
                  >
                    {submitting ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <Send className="w-4 h-4" />
                    )}
                    {t("assistant.quiz.submit", "Submit")}
                  </Button>
                )}
              </div>
            </motion.div>
          )}

          {/* Result mode */}
          {viewMode === "result" && result && (
            <motion.div key="result">
              <QuizResult result={result} onReview={handleReview} />
              <div className="mt-3 flex justify-center">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setShowShareDialog(true)}
                  className="gap-1.5"
                >
                  <Link2 className="w-3.5 h-3.5" />
                  {t("assistant.quiz.shareQuiz", "Share Quiz")}
                </Button>
              </div>
            </motion.div>
          )}

          {/* Review mode: browse questions with answers */}
          {viewMode === "review" && currentQuestion && result && (
            <motion.div key={`review-${reviewIndex}`}>
              <div className="flex items-center gap-2 mb-4">
                <span className="text-xs font-medium text-muted-foreground">
                  Review {reviewIndex + 1} / {totalQuestions}
                </span>
                <button
                  type="button"
                  onClick={handleBackToResult}
                  className="ml-auto text-xs text-primary hover:underline"
                >
                  {t("assistant.quiz.backToResults", "Back to results")}
                </button>
              </div>

              <QuizQuestion
                question={currentQuestion}
                selectedAnswer={selectedAnswers[currentQuestion.id]}
                onSelect={() => {}}
                disabled
                result={getQuestionResult(currentQuestion.id)}
              />

              <div className="flex items-center justify-between mt-5 pt-4 border-t border-border">
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={reviewIndex === 0}
                  onClick={() => setReviewIndex((i) => i - 1)}
                  className="gap-1"
                >
                  <ChevronLeft className="w-4 h-4" />
                  {t("assistant.quiz.prev", "Previous")}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={reviewIndex >= totalQuestions - 1}
                  onClick={() => setReviewIndex((i) => i + 1)}
                  className="gap-1"
                >
                  {t("assistant.quiz.next", "Next")}
                  <ChevronRight className="w-4 h-4" />
                </Button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* Share dialog */}
      <QuizShareDialog
        quizId={quizData.quiz_id}
        open={showShareDialog}
        onClose={() => setShowShareDialog(false)}
      />
    </div>
  );
}
