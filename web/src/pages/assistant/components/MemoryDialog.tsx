import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  deleteAssistantMemoryItem,
  getAssistantMemory,
  setAssistantMemoryEnabled,
  updateAssistantMemoryItem,
  type AssistantMemoryState,
} from "@/api/assistantMemory";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";

interface MemoryDialogProps {
  open: boolean;
  onClose: () => void;
}

export function MemoryDialog({ open, onClose }: MemoryDialogProps) {
  const { t } = useTranslation();
  const [state, setState] = useState<AssistantMemoryState | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [deleting, setDeleting] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let disposed = false;
    setError("");
    void getAssistantMemory().then((value) => {
      if (!disposed) setState(value);
    }).catch(() => {
      if (!disposed) setError(t("assistant.memoryLoadFailed", "Could not load memory settings."));
    });
    return () => { disposed = true; };
  }, [open, t]);

  const reload = async () => setState(await getAssistantMemory());
  const toggle = async (enabled: boolean) => {
    setBusy(true);
    setError("");
    try {
      await setAssistantMemoryEnabled(enabled);
      await reload();
    } catch {
      setError(t("assistant.memorySaveFailed", "Could not save this memory change."));
    } finally {
      setBusy(false);
    }
  };
  const saveItem = async (key: string) => {
    setBusy(true);
    setError("");
    try {
      await updateAssistantMemoryItem(key, editValue);
      await reload();
      setEditing(null);
    } catch {
      setError(t("assistant.memorySaveFailed", "Could not save this memory change."));
    } finally {
      setBusy(false);
    }
  };
  const removeItem = async (key: string) => {
    setBusy(true);
    setError("");
    try {
      await deleteAssistantMemoryItem(key);
      await reload();
      setDeleting(null);
    } catch {
      setError(t("assistant.memorySaveFailed", "Could not save this memory change."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(value) => { if (!value) onClose(); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("assistant.memoryTitle", "Assistant memory")}</DialogTitle>
          <DialogDescription>
            {t("assistant.memoryEffect", "Changes affect new runs. Turning memory off also disables session memory for those runs; it does not erase existing records or revoke an active run.")}
          </DialogDescription>
        </DialogHeader>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        {!state ? (
          <p role="status" className="text-sm">{t("common.loading", "Loading…")}</p>
        ) : (
          <div className="space-y-4">
            <label className="flex items-center gap-3 text-sm">
              <input type="checkbox" checked={state.enabled} disabled={busy} onChange={(event) => void toggle(event.target.checked)} />
              {t("assistant.memoryEnabled", "Use memory on new runs")}
            </label>
            <p className="text-xs text-muted-foreground">{t("assistant.memoryScope", "These are your long-term records. Conversation history and session memory are separate. Deleting a record removes it from future retrieval, but cannot erase answers already produced.")}</p>
            <div className="max-h-[45dvh] space-y-3 overflow-y-auto">
              {state.items.length === 0 && <p className="text-sm text-muted-foreground">{t("assistant.memoryEmpty", "No long-term records saved.")}</p>}
              {state.items.map((item) => (
                <div key={item.key} className="rounded-lg border p-3 text-sm">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="break-all font-medium">{item.key}</p>
                      <p className="text-xs text-muted-foreground">{item.source} · {item.updated_at ? new Date(item.updated_at).toLocaleString() : ""}</p>
                    </div>
                    <div className="flex shrink-0 gap-2">
                      <button type="button" disabled={busy} onClick={() => {
                        setEditing(item.key);
                        setEditValue(typeof item.value === "string" ? item.value : JSON.stringify(item.value));
                        setDeleting(null);
                      }} className="underline">{t("common.edit", "Edit")}</button>
                      <button type="button" disabled={busy} onClick={() => setDeleting(item.key)} className="text-red-600 underline">{t("common.delete", "Delete")}</button>
                    </div>
                  </div>
                  {editing === item.key ? (
                    <div className="mt-2 space-y-2">
                      <textarea aria-label={`${t("common.edit", "Edit")}: ${item.key}`} value={editValue} onChange={(event) => setEditValue(event.target.value)} className="w-full rounded border bg-transparent p-2" rows={3} />
                      <button type="button" disabled={busy || !editValue.trim()} onClick={() => void saveItem(item.key)} className="rounded bg-primary px-3 py-1 text-white disabled:opacity-50">{t("common.save", "Save")}</button>
                    </div>
                  ) : <p className="mt-2 whitespace-pre-wrap break-words">{typeof item.value === "string" ? item.value : JSON.stringify(item.value)}</p>}
                  {deleting === item.key && (
                    <div role="alert" className="mt-2 flex items-center gap-3 text-xs">
                      <span>{t("assistant.memoryDeleteConfirm", "Remove this record from future use?")}</span>
                      <button type="button" disabled={busy} onClick={() => void removeItem(item.key)} className="text-red-600 underline">{t("assistant.memoryDelete", "Remove")}</button>
                      <button type="button" onClick={() => setDeleting(null)} className="underline">{t("common.cancel", "Cancel")}</button>
                    </div>
                  )}
                </div>
              ))}
              {state.has_more && <p className="text-xs text-muted-foreground">{t("assistant.memoryMore", "More than 100 records exist; only the latest 100 are shown.")}</p>}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
