import { useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { Share2, Copy, Check, X, ExternalLink } from "lucide-react";
import { createConversationShare, type ShareInfo } from "@/api/assistant";
import { toast } from "@/hooks/use-toast";

interface ShareDialogProps {
  sessionId: string;
  messageCount: number;
  artifactCount: number;
  isOpen: boolean;
  onClose: () => void;
}

export function ShareDialog({ sessionId, messageCount, artifactCount, isOpen, onClose }: ShareDialogProps) {
  const { t } = useTranslation();
  const [isCreating, setIsCreating] = useState(false);
  const [shareInfo, setShareInfo] = useState<ShareInfo | null>(null);
  const [copied, setCopied] = useState(false);
  const [includeArtifacts, setIncludeArtifacts] = useState(true);

  const handleCreate = useCallback(async () => {
    setIsCreating(true);
    try {
      const info = await createConversationShare(sessionId, {
        include_artifacts: includeArtifacts,
      });
      setShareInfo(info);
      toast.success(t("assistant.shareCreated", "Share link created!"));
    } catch (err) {
      toast.error(t("assistant.shareFailed", "Failed to create share link"));
      console.error("Share creation failed:", err);
    } finally {
      setIsCreating(false);
    }
  }, [sessionId, includeArtifacts, t]);

  const handleCopy = useCallback(async () => {
    if (!shareInfo) return;
    const fullUrl = `${window.location.origin}${shareInfo.share_url}`;
    try {
      await navigator.clipboard.writeText(fullUrl);
      setCopied(true);
      toast.success(t("assistant.linkCopied", "Link copied to clipboard"));
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback for HTTP
      const ta = document.createElement("textarea");
      ta.value = fullUrl;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }, [shareInfo, t]);

  const handleClose = useCallback(() => {
    setShareInfo(null);
    setCopied(false);
    onClose();
  }, [onClose]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={handleClose}>
      <div
        className="bg-white dark:bg-slate-800 rounded-2xl shadow-2xl w-full max-w-md mx-4 overflow-hidden"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="assistant-share-dialog-title"
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-200 dark:border-slate-700">
          <div className="flex items-center gap-2">
            <Share2 className="w-5 h-5 text-primary" />
            <h3 id="assistant-share-dialog-title" className="text-lg font-semibold">{t("assistant.shareConversation", "Share Conversation")}</h3>
          </div>
          <button type="button" onClick={handleClose} className="p-1 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-700" aria-label={t("common.close", "Close")}>
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body */}
        <div className="px-6 py-5 space-y-4">
          {!shareInfo ? (
            <>
              {/* Preview */}
              <div className="p-4 rounded-xl bg-slate-50 dark:bg-slate-700/50 space-y-2">
                <div className="flex justify-between text-sm">
                  <span className="text-muted-foreground">{t("assistant.messages", "Messages")}</span>
                  <span className="font-medium">{messageCount}</span>
                </div>
                {artifactCount > 0 && (
                  <div className="flex justify-between text-sm">
                    <span className="text-muted-foreground">{t("assistant.artifacts", "Artifacts")}</span>
                    <span className="font-medium">{artifactCount}</span>
                  </div>
                )}
              </div>

              {/* Include artifacts toggle */}
              {artifactCount > 0 && (
                <label className="flex items-center gap-3 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={includeArtifacts}
                    onChange={(e) => setIncludeArtifacts(e.target.checked)}
                    className="w-4 h-4 rounded border-slate-300"
                  />
                  <span className="text-sm">
                    {t("assistant.includeArtifacts", "Include generated files & images")}
                    <span className="text-muted-foreground ml-1">({artifactCount})</span>
                  </span>
                </label>
              )}

              <p className="text-xs text-muted-foreground">
                {t("assistant.shareNote", "Anyone with the link can view this conversation. You can revoke the link at any time.")}
              </p>

              <button
                type="button"
                onClick={handleCreate}
                disabled={isCreating}
                className="w-full py-2.5 px-4 rounded-xl bg-primary text-white font-medium hover:bg-primary/90 disabled:opacity-50 transition-colors flex items-center justify-center gap-2"
              >
                {isCreating ? (
                  <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                ) : (
                  <Share2 className="w-4 h-4" />
                )}
                {isCreating ? t("assistant.creating", "Creating...") : t("assistant.createShareLink", "Create Share Link")}
              </button>
            </>
          ) : (
            <>
              {/* Success state */}
              <div className="text-center space-y-3">
                <div className="w-12 h-12 rounded-full bg-green-100 dark:bg-green-900/30 flex items-center justify-center mx-auto">
                  <Check className="w-6 h-6 text-green-600" />
                </div>
                <p className="font-medium">{t("assistant.shareLinkReady", "Share link is ready!")}</p>
                <p className="text-sm text-muted-foreground">
                  {shareInfo.message_count} {t("assistant.messages", "messages")}
                  {shareInfo.artifact_count > 0 && ` · ${shareInfo.artifact_count} ${t("assistant.artifacts", "artifacts")}`}
                </p>
              </div>

              {/* URL display */}
              <div className="flex items-center gap-2 p-3 rounded-xl bg-slate-50 dark:bg-slate-700/50 border border-slate-200 dark:border-slate-600">
                <input
                  readOnly
                  value={`${window.location.origin}${shareInfo.share_url}`}
                  className="flex-1 bg-transparent text-sm font-mono truncate outline-hidden"
                />
                <button
                  type="button"
                  onClick={handleCopy}
                  className="shrink-0 p-2 rounded-lg hover:bg-slate-200 dark:hover:bg-slate-600 transition-colors"
                  aria-label={copied ? t("assistant.linkCopied", "Link copied") : t("common.copy", "Copy")}
                >
                  {copied ? <Check className="w-4 h-4 text-green-500" /> : <Copy className="w-4 h-4" />}
                </button>
                <a
                  href={shareInfo.share_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="shrink-0 p-2 rounded-lg hover:bg-slate-200 dark:hover:bg-slate-600 transition-colors"
                  aria-label={t("common.openInNewTab", "Open share link")}
                >
                  <ExternalLink className="w-4 h-4" />
                </a>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
