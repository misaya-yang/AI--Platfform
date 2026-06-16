/**
 * QuizShareDialog — Generate and copy a shareable quiz link.
 */

import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, Copy, Link2, Loader2, X } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { Button } from "@/components/ui/button";
import { createQuizShare, type ShareQuizResponse } from "@/api/quiz";

interface QuizShareDialogProps {
  quizId: string;
  open: boolean;
  onClose: () => void;
}

export function QuizShareDialog({ quizId, open, onClose }: QuizShareDialogProps) {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(false);
  const [share, setShare] = useState<ShareQuizResponse | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  const [requireName, setRequireName] = useState(true);

  const handleGenerate = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await createQuizShare(quizId, {
        require_name: requireName,
      });
      setShare(result);
    } catch (err) {
      setError("Failed to generate share link");
      console.error(err);
    } finally {
      setLoading(false);
    }
  }, [quizId, requireName]);

  const handleCopy = useCallback(() => {
    if (!share) return;
    const url = `${window.location.origin}/quiz/${share.share_code}`;
    navigator.clipboard.writeText(url).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [share]);

  if (!open) return null;

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-xs"
        onClick={onClose}
      >
        <motion.div
          initial={{ scale: 0.95, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.95, opacity: 0 }}
          onClick={(e) => e.stopPropagation()}
          className="bg-card border border-border rounded-2xl shadow-xl w-full max-w-md mx-4 overflow-hidden"
        >
          {/* Header */}
          <div className="flex items-center justify-between px-5 py-4 border-b border-border">
            <div className="flex items-center gap-2">
              <Link2 className="w-4 h-4 text-primary" />
              <h3 className="text-sm font-semibold text-foreground">
                {t("assistant.quiz.shareQuiz", "Share Quiz")}
              </h3>
            </div>
            <button
              type="button"
              onClick={onClose}
              className="p-1 rounded-lg hover:bg-muted transition-colors"
            >
              <X className="w-4 h-4 text-muted-foreground" />
            </button>
          </div>

          <div className="p-5 space-y-4">
            {!share ? (
              <>
                {/* Options */}
                <label className="flex items-center gap-3 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={requireName}
                    onChange={(e) => setRequireName(e.target.checked)}
                    className="rounded border-border"
                  />
                  <span className="text-sm text-foreground">
                    {t("assistant.quiz.requireName", "Require name before taking")}
                  </span>
                </label>

                {error && (
                  <p className="text-xs text-red-500">{error}</p>
                )}

                <Button
                  onClick={handleGenerate}
                  disabled={loading}
                  className="w-full gap-2"
                >
                  {loading ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <Link2 className="w-4 h-4" />
                  )}
                  {t("assistant.quiz.generateLink", "Generate Share Link")}
                </Button>
              </>
            ) : (
              <>
                {/* Share link generated */}
                <div className="rounded-xl bg-muted/50 border border-border p-3">
                  <p className="text-xs text-muted-foreground mb-1">Share Link</p>
                  <div className="flex items-center gap-2">
                    <code className="flex-1 text-sm text-foreground truncate">
                      {window.location.origin}/quiz/{share.share_code}
                    </code>
                    <button
                      type="button"
                      onClick={handleCopy}
                      className="shrink-0 p-1.5 rounded-lg hover:bg-muted transition-colors"
                    >
                      {copied ? (
                        <Check className="w-4 h-4 text-emerald-500" />
                      ) : (
                        <Copy className="w-4 h-4 text-muted-foreground" />
                      )}
                    </button>
                  </div>
                </div>

                <p className="text-xs text-muted-foreground">
                  Anyone with this link can take the quiz
                  {share.require_name ? " (name required)" : ""}.
                </p>

                <Button onClick={handleCopy} className="w-full gap-2" variant="outline">
                  {copied ? (
                    <>
                      <Check className="w-4 h-4" />
                      {t("assistant.quiz.copied", "Copied!")}
                    </>
                  ) : (
                    <>
                      <Copy className="w-4 h-4" />
                      {t("assistant.quiz.copyLink", "Copy Link")}
                    </>
                  )}
                </Button>
              </>
            )}
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}
