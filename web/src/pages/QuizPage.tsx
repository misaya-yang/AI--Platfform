/**
 * Public Quiz Page — Standalone quiz experience for shared links.
 *
 * Accessible at /quiz/:shareCode (no auth required).
 * Fetches quiz from public API, allows name input, full quiz + score.
 */

import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { BookOpen, ChevronLeft, ChevronRight, Loader2, Send, User } from "lucide-react";
import { cn } from "@/lib/utils";
import type { QuizQuestionData, QuizAttemptResult } from "@/pages/assistant/types";
import { QuizQuestion } from "@/pages/assistant/components/Quiz/QuizQuestion";
import { QuizResult } from "@/pages/assistant/components/Quiz/QuizResult";

interface PublicQuizData {
  quiz_id: string;
  share_code: string;
  title: string;
  description?: string;
  question_count: number;
  difficulty?: string;
  require_name: boolean;
  questions: QuizQuestionData[];
}

type PageState = "loading" | "intro" | "quiz" | "result" | "error";

export function QuizPage() {
  const { shareCode } = useParams<{ shareCode: string }>();
  const [quiz, setQuiz] = useState<PublicQuizData | null>(null);
  const [pageState, setPageState] = useState<PageState>("loading");
  const [error, setError] = useState<string>("");
  const [displayName, setDisplayName] = useState("");
  const [currentIndex, setCurrentIndex] = useState(0);
  const [selectedAnswers, setSelectedAnswers] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [result, setResult] = useState<QuizAttemptResult | null>(null);
  const shouldReduceMotion = useReducedMotion();

  // Fetch quiz on mount
  useEffect(() => {
    if (!shareCode) {
      setError("Quiz link is invalid.");
      setPageState("error");
      return;
    }

    const controller = new AbortController();
    let savedResult: QuizAttemptResult | null = null;
    try {
      const submitted = localStorage.getItem(`quiz_submitted_${shareCode}`);
      if (submitted) {
        const parsed = JSON.parse(submitted) as Partial<QuizAttemptResult>;
        if (
          typeof parsed.total_score === "number" &&
          typeof parsed.correct_count === "number" &&
          typeof parsed.total_count === "number" &&
          Array.isArray(parsed.per_question)
        ) {
          savedResult = parsed as QuizAttemptResult;
          setResult(savedResult);
          setPageState("result");
        }
      }
    } catch {
      // Ignore unavailable or malformed browser storage and load the quiz normally.
    }

    async function loadQuiz() {
      try {
        const resp = await fetch(`/api/v1/quiz/shared/${shareCode}`, {
          signal: controller.signal,
        });
        if (!resp.ok) {
          throw new Error(
            resp.status === 404
              ? "Quiz not found, expired, or max attempts reached."
              : "Failed to load quiz.",
          );
        }
        const data = (await resp.json()) as PublicQuizData;
        setQuiz(data);
        setError("");
        setPageState(savedResult ? "result" : "intro");
      } catch (loadError) {
        if (loadError instanceof DOMException && loadError.name === "AbortError") return;
        if (savedResult) {
          setError("Quiz details could not be loaded. Showing your saved result.");
          setPageState("result");
          return;
        }
        setError(loadError instanceof Error ? loadError.message : "Failed to load quiz.");
        setPageState("error");
      }
    }

    void loadQuiz();
    return () => controller.abort();
  }, [shareCode]);

  const handleStart = useCallback(() => {
    if (quiz?.require_name && !displayName.trim()) return;
    setPageState("quiz");
  }, [quiz, displayName]);

  const handleSelect = useCallback(
    (label: string) => {
      if (!quiz) return;
      const q = quiz.questions[currentIndex];
      setSelectedAnswers((prev) => ({ ...prev, [q.id]: label }));
    },
    [quiz, currentIndex],
  );

  const handleSubmit = useCallback(async () => {
    if (!quiz || submitting) return;
    setSubmitting(true);
    setSubmitError("");
    try {
      const resp = await fetch(`/api/v1/quiz/shared/${shareCode}/submit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          answers: selectedAnswers,
          display_name: displayName.trim() || null,
        }),
      });
      if (!resp.ok) {
        throw new Error(
          resp.status === 429
            ? "This quiz has reached its attempt limit."
            : "Failed to submit quiz. Please try again.",
        );
      }
      const data = await resp.json();
      setResult(data);
      setPageState("result");
      // Remember submission in localStorage to prevent re-take on page reload
      try {
        localStorage.setItem(`quiz_submitted_${shareCode}`, JSON.stringify(data));
      } catch {
        // Storage may be unavailable in private or embedded contexts.
      }
    } catch (submitFailure) {
      setSubmitError(
        submitFailure instanceof Error
          ? submitFailure.message
          : "Failed to submit quiz. Please try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }, [quiz, shareCode, selectedAnswers, displayName, submitting]);

  const allAnswered = quiz
    ? quiz.questions.every((q) => {
        const a = selectedAnswers[q.id];
        return a != null && a.trim() !== "";
      })
    : false;

  // --- Loading ---
  if (pageState === "loading") {
    return (
      <div className="flex min-h-dvh items-center justify-center bg-background">
        <div className="flex items-center gap-3 text-sm text-muted-foreground" role="status">
          <Loader2 className="h-5 w-5 animate-spin text-primary" aria-hidden="true" />
          <span>Loading quiz…</span>
        </div>
      </div>
    );
  }

  // --- Error ---
  if (pageState === "error") {
    return (
      <div className="flex min-h-dvh items-center justify-center bg-background">
        <div className="max-w-md space-y-4 px-6 text-center" role="alert">
          <BookOpen className="w-12 h-12 mx-auto text-muted-foreground/40" />
          <h1 className="text-xl font-semibold text-foreground">{error}</h1>
          <p className="text-sm text-muted-foreground">
            This quiz link may have expired or been removed.
          </p>
        </div>
      </div>
    );
  }

  if (!quiz && !(pageState === "result" && result)) return null;
  const currentQuestion = quiz?.questions[currentIndex];
  const quizTitle = quiz?.title ?? "Quiz result";
  const questionCount = quiz?.question_count ?? result?.total_count ?? 0;

  return (
    <div className="min-h-dvh bg-background">
      {/* Header */}
      <header className="sticky top-0 z-10 bg-background/80 backdrop-blur-xs border-b border-border">
        <div className="max-w-2xl mx-auto px-4 py-3 flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center">
            <BookOpen className="w-4 h-4 text-primary" />
          </div>
          <div className="min-w-0">
            <h1 className="truncate text-sm font-semibold text-foreground">{quizTitle}</h1>
            <p className="text-xs text-muted-foreground">
              {questionCount} questions
              {quiz?.difficulty && ` · ${quiz.difficulty}`}
            </p>
          </div>
        </div>
      </header>

      <div className="max-w-2xl mx-auto px-4 py-8">
        <AnimatePresence mode="wait">
          {/* --- Intro screen --- */}
          {pageState === "intro" && quiz && (
            <motion.div
              key="intro"
              initial={shouldReduceMotion ? false : { opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={shouldReduceMotion ? { opacity: 0 } : { opacity: 0, y: -12 }}
              transition={{ duration: shouldReduceMotion ? 0 : 0.2, ease: "easeOut" }}
              className="max-w-md mx-auto text-center space-y-6"
            >
              <div className="w-16 h-16 mx-auto rounded-2xl bg-primary/10 flex items-center justify-center">
                <BookOpen className="w-8 h-8 text-primary" />
              </div>
              <div>
                <h2 className="text-xl font-bold text-foreground">{quiz.title}</h2>
                {quiz.description && (
                  <p className="mt-2 text-sm text-muted-foreground">{quiz.description}</p>
                )}
                <p className="mt-3 text-xs text-muted-foreground">
                  {quiz.question_count} questions · ~{Math.ceil(quiz.question_count * 0.5)} min
                </p>
              </div>

              {quiz.require_name && (
                <div className="relative max-w-xs mx-auto">
                  <label htmlFor="quiz-display-name" className="sr-only">
                    Your name
                  </label>
                  <User className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                  <input
                    id="quiz-display-name"
                    type="text"
                    placeholder="Your name"
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && handleStart()}
                    className="w-full rounded-xl border border-border bg-card pl-10 pr-4 py-2.5 text-sm focus:outline-hidden focus:ring-2 focus:ring-primary/30"
                  />
                </div>
              )}

              <button
                type="button"
                onClick={handleStart}
                disabled={quiz.require_name && !displayName.trim()}
                className={cn(
                  "inline-flex items-center gap-2 rounded-xl px-8 py-3 text-sm font-medium transition-colors",
                  "bg-primary text-primary-foreground hover:bg-primary/90",
                  "disabled:cursor-not-allowed disabled:opacity-50",
                )}
              >
                Start Quiz
                <ChevronRight className="w-4 h-4" />
              </button>
            </motion.div>
          )}

          {/* --- Quiz mode --- */}
          {pageState === "quiz" && quiz && currentQuestion && (
            <motion.div
              key={`q-${currentIndex}`}
              initial={shouldReduceMotion ? false : { opacity: 0, x: 16 }}
              animate={{ opacity: 1, x: 0 }}
              exit={shouldReduceMotion ? { opacity: 0 } : { opacity: 0, x: -16 }}
              transition={{ duration: shouldReduceMotion ? 0 : 0.18, ease: "easeOut" }}
              className="max-w-lg mx-auto"
            >
              {/* Progress */}
              <div className="flex items-center gap-2 mb-6">
                <span className="text-sm font-medium text-muted-foreground">
                  {currentIndex + 1} / {quiz.question_count}
                </span>
                <div
                  className="h-2 flex-1 overflow-hidden rounded-full bg-muted"
                  role="progressbar"
                  aria-label="Quiz progress"
                  aria-valuemin={1}
                  aria-valuemax={quiz.question_count}
                  aria-valuenow={currentIndex + 1}
                >
                  <div
                    className={cn(
                      "h-full rounded-full bg-primary",
                      shouldReduceMotion ? "transition-none" : "transition-[width] duration-300",
                    )}
                    style={{
                      width: `${((currentIndex + 1) / quiz.question_count) * 100}%`,
                    }}
                  />
                </div>
              </div>

              <QuizQuestion
                question={currentQuestion}
                selectedAnswer={selectedAnswers[currentQuestion.id]}
                onSelect={handleSelect}
              />

              {submitError && (
                <p className="mt-5 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive" role="alert">
                  {submitError}
                </p>
              )}

              {/* Navigation */}
              <div className="flex items-center justify-between mt-8">
                <button
                  type="button"
                  disabled={currentIndex === 0}
                  onClick={() => setCurrentIndex((i) => i - 1)}
                  className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                >
                  <ChevronLeft className="w-4 h-4" />
                  Previous
                </button>

                {currentIndex < quiz.question_count - 1 ? (
                  <button
                    type="button"
                    disabled={!selectedAnswers[currentQuestion.id]}
                    onClick={() => setCurrentIndex((i) => i + 1)}
                    className="inline-flex items-center gap-1 text-sm text-primary hover:text-primary/80 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    Next
                    <ChevronRight className="w-4 h-4" />
                  </button>
                ) : (
                  <button
                    type="button"
                    disabled={!allAnswered || submitting}
                    onClick={handleSubmit}
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-xl px-5 py-2 text-sm font-medium transition-colors",
                      "bg-primary text-primary-foreground",
                      "disabled:opacity-50 disabled:cursor-not-allowed",
                    )}
                  >
                    {submitting ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <Send className="w-4 h-4" />
                    )}
                    Submit
                  </button>
                )}
              </div>
            </motion.div>
          )}

          {/* --- Result --- */}
          {pageState === "result" && result && (
            <motion.div
              key="result"
              initial={shouldReduceMotion ? false : { opacity: 0, scale: 0.98 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ duration: shouldReduceMotion ? 0 : 0.18, ease: "easeOut" }}
              className="max-w-lg mx-auto"
            >
              {error && (
                <p className="mb-5 rounded-lg border border-amber-300/60 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-400/30 dark:bg-amber-500/10 dark:text-amber-100" role="status">
                  {error}
                </p>
              )}
              <QuizResult result={result} />

              <div className="mt-6 text-center">
                <p className="text-xs text-muted-foreground">
                  Powered by AI Platform
                </p>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
