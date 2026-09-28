import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { getActiveDocumentSource } from "@/api/knowledge";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";

interface SourceDocumentDialogProps {
  source: { datasetId: string; documentId: string; excerpt: string } | null;
  onClose: () => void;
}

/** The document API checks the caller's current dataset ACL on every read. */
export function SourceDocumentDialog({ source, onClose }: SourceDocumentDialogProps) {
  const { t } = useTranslation();
  const [document, setDocument] = useState<{ datasetId: string; documentId: string; title: string; content: string } | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "unavailable">("loading");
  const datasetId = source?.datasetId;
  const documentId = source?.documentId;
  const currentDocument = document?.datasetId === datasetId && document?.documentId === documentId ? document : null;

  useEffect(() => {
    if (!datasetId || !documentId) {
      setDocument(null);
      return;
    }
    let active = true;
    let requestNumber = 0;
    const load = async () => {
      const currentRequest = ++requestNumber;
      try {
        const result = await getActiveDocumentSource(datasetId, documentId);
        if (!active || currentRequest !== requestNumber) return;
        setDocument({ datasetId, documentId, title: result.title, content: result.content || "" });
        setState("ready");
      } catch {
        if (!active || currentRequest !== requestNumber) return;
        setDocument(null);
        setState("unavailable");
      }
    };
    setDocument(null);
    setState("loading");
    void load();
    const interval = window.setInterval(() => void load(), 3000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [datasetId, documentId]);

  return (
    <Dialog open={Boolean(source)} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-w-2xl">
        <DialogTitle>{currentDocument?.title || t("assistant.openSource", "Open source")}</DialogTitle>
        <DialogDescription>
          {t("assistant.sourceCurrentAccess", "This document is checked against your current access.")}
        </DialogDescription>
        {state === "ready" && currentDocument && source?.excerpt && (
          <section className="space-y-1 rounded-lg border border-[hsl(var(--assistant-border-soft))] p-3">
            <p className="text-xs font-medium">{t("assistant.citedExcerpt", "Excerpt saved with this answer")}</p>
            <p className="max-h-40 overflow-y-auto whitespace-pre-wrap break-words text-sm">{source.excerpt}</p>
          </section>
        )}
        {state === "loading" && <p role="status">{t("common.loading", "Loading...")}</p>}
        {state === "unavailable" && <p role="alert">{t("assistant.sourceUnavailable", "This source is no longer available to you. Ask the owner for access or choose another source.")}</p>}
        {state === "ready" && currentDocument && (
          <section className="space-y-2">
            <p role="note" className="text-xs text-amber-700 dark:text-amber-300">
              {t("assistant.currentSourceMayDiffer", "Current document content may differ from the excerpt saved with this answer; its original full version cannot be verified here.")}
            </p>
            <div className="max-h-[45dvh] overflow-y-auto whitespace-pre-wrap break-words text-sm" data-testid="assistant-source-content">
              {currentDocument.content || t("assistant.emptySource", "This document has no previewable text.")}
            </div>
          </section>
        )}
      </DialogContent>
    </Dialog>
  );
}
