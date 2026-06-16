/**
 * Public Quiz Page — Standalone quiz experience for shared links.
 *
 * Accessible at /quiz/:shareCode (no auth required).
 * Fetches quiz from public API, allows name input, full quiz + score.
 */

import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
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
  const [result, setResult] = useState<QuizAttemptResult | null>(null);

  // Fetch quiz on mount
  useEffect(() => {
    if (!shareCode) return;

    // Check if already submitted from this browser
    const submitted = localStorage.getItem(`quiz_submitted_${shareCode}`);
    if (submitted) {
      try {
        const savedResult = JSON.parse(submitted);
        setResult(savedResult);
        setPageState("result");
        // Still load quiz data for review
        fetch(`/api/v1/quiz/shared/${shareCode}`)
          .then((r) => r.ok ? r.json() : null)
          .then((d) => { if (d) setQuiz(d); })
          .catch(() => {});
        return;
      } catch { /* corrupted localStorage, continue normally */ }
    }

    fetch(`/api/v1/quiz/shared/${shareCode}`)
      .then((resp) => {
        if (!resp.ok) {
          throw new Error(
            resp.status === 404
              ? "Quiz not found, expired, or max attempts reached."
              : "Failed to load quiz.",
          );
        }
        return resp.json();
      })
      .then((data) => {
        setQuiz(data);
        setPageState("intro");
      })
      .catch((e) => {
        setError(e.message);
        setPageState("error");
      });
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
    try {
      const resp = await fetch(`/api/v1/quiz/shared/${shareCode}/submit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          answers: selectedAnswers,
          display_name: displayName.trim() || null,
        }),
      });
      if (!resp.ok) throw new Error("Submit failed");
      const data = await resp.json();
      setResult(data);
      setPageState("result");
      // Remember submission in localStorage to prevent re-take on page reload
      try {
        localStorage.setItem(`quiz_submitted_${shareCode}`, JSON.stringify(data));
      } catch {
        // Storage may be unavailable in private or embedded contexts.
      }
    } catch {
      setError("Failed to submit quiz. Please try again.");
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
      <div className="min-h-screen flex items-center justify-center bg-background">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    );
  }

  // --- Error ---
  if (pageState === "error") {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background">
        <div className="text-center space-y-4 max-w-md px-6">
          <BookOpen className="w-12 h-12 mx-auto text-muted-foreground/40" />
          <h1 className="text-xl font-semibold text-foreground">{error}</h1>
          <p className="text-sm text-muted-foreground">
            This quiz link may have expired or been removed.
          </p>
        </div>
      </div>
    );
  }

  if (!quiz) return null;
  const currentQuestion = quiz.questions[currentIndex];

  return (
    <div className="min-h-screen bg-background">
      {/* Header */}
      <header className="sticky top-0 z-10 bg-background/80 backdrop-blur-xs border-b border-border">
        <div className="max-w-2xl mx-auto px-4 py-3 flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center">
            <BookOpen className="w-4 h-4 text-primary" />
          </div>
          <div>
            <h1 className="text-sm font-semibold text-foreground">{quiz.title}</h1>
            <p className="text-xs text-muted-foreground">
              {quiz.question_count} questions
              {quiz.difficulty && ` · ${quiz.difficulty}`}
            </p>
          </div>
        </div>
      </header>

      <div className="max-w-2xl mx-auto px-4 py-8">
        <AnimatePresence mode="wait">
          {/* --- Intro screen --- */}
          {pageState === "intro" && (
            <motion.div
              key="intro"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -20 }}
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
                  <User className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                  <input
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
                  "inline-flex items-center gap-2 px-8 py-3 rounded-xl text-sm font-medium transition-all",
                  "bg-primary text-primary-foreground shadow-lg hover:shadow-xl hover:scale-[1.02]",
                  "disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:scale-100",
                )}
              >
                Start Quiz
                <ChevronRight className="w-4 h-4" />
              </button>
            </motion.div>
          )}

          {/* --- Quiz mode --- */}
          {pageState === "quiz" && currentQuestion && (
            <motion.div
              key={`q-${currentIndex}`}
              initial={{ opacity: 0, x: 20 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -20 }}
              className="max-w-lg mx-auto"
            >
              {/* Progress */}
              <div className="flex items-center gap-2 mb-6">
                <span className="text-sm font-medium text-muted-foreground">
                  {currentIndex + 1} / {quiz.question_count}
                </span>
                <div className="flex-1 h-2 rounded-full bg-muted overflow-hidden">
                  <div
                    className="h-full rounded-full bg-primary transition-all duration-300"
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
                      "inline-flex items-center gap-1.5 px-5 py-2 rounded-xl text-sm font-medium transition-all",
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
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              className="max-w-lg mx-auto"
            >
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
