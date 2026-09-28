import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { getDocument } from "@/api/knowledge";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";

interface SourceDocumentDialogProps {
  source: { datasetId: string; documentId: string } | null;
  onClose: () => void;
}

/** The document API checks the caller's current dataset ACL on every read. */
export function SourceDocumentDialog({ source, onClose }: SourceDocumentDialogProps) {
  const { t } = useTranslation();
  const [document, setDocument] = useState<{ title: string; content: string } | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "unavailable">("loading");
  const datasetId = source?.datasetId;
  const documentId = source?.documentId;

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
        const result = await getDocument(datasetId, documentId);
        if (!active || currentRequest !== requestNumber) return;
        setDocument({ title: result.title, content: result.content || "" });
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
        <DialogTitle>{document?.title || t("assistant.openSource", "Open source")}</DialogTitle>
        <DialogDescription>
          {t("assistant.sourceCurrentAccess", "This document is checked against your current access.")}
        </DialogDescription>
        {state === "loading" && <p role="status">{t("common.loading", "Loading...")}</p>}
        {state === "unavailable" && <p role="alert">{t("assistant.sourceUnavailable", "This source is no longer available to you. Ask the owner for access or choose another source.")}</p>}
        {state === "ready" && document && (
          <div className="max-h-[65dvh] overflow-y-auto whitespace-pre-wrap break-words text-sm" data-testid="assistant-source-content">
            {document.content || t("assistant.emptySource", "This document has no previewable text.")}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
