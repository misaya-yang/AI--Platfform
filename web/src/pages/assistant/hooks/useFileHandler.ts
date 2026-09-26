import { useState, useCallback, useRef } from "react";
import {
  uploadFileWithProgress,
  isFileTypeSupported,
  type FileUploadResponse,
} from "@/api/files";
import { usePasteImage } from "@/hooks/usePasteImage";
import { generateUUID } from "@/lib/utils";
import { planAssistantFileSelection } from "../fileSelection";

export interface UploadedFile {
  id: string;
  file: File;
  status: "pending" | "uploading" | "success" | "error";
  selected: boolean;
  progress?: number;
  errorCode?: "unsupported" | "too_large" | "upload_failed";
  response?: FileUploadResponse;
}

const MAX_ASSISTANT_ATTACHMENT_BYTES = 32 * 1024 * 1024;

export function useFileHandler(sessionKey: string) {
  const [filesBySession, setFilesBySession] = useState<Record<string, UploadedFile[]>>({});
  const filesBySessionRef = useRef<Record<string, UploadedFile[]>>({});
  const [uploadingBySession, setUploadingBySession] = useState<Record<string, number>>({});
  const uploadCountsRef = useRef<Record<string, number>>({});
  const [noticesBySession, setNoticesBySession] = useState<Record<string, string>>({});
  const fileInputRef = useRef<HTMLInputElement>(null);
  const files = filesBySession[sessionKey] ?? [];
  const isUploading = (uploadingBySession[sessionKey] ?? 0) > 0;
  const selectionNotice = noticesBySession[sessionKey] ?? "";

  const updateSessionFiles = useCallback((key: string, update: (entries: UploadedFile[]) => UploadedFile[]) => {
    const next = update(filesBySessionRef.current[key] ?? []);
    filesBySessionRef.current = { ...filesBySessionRef.current, [key]: next };
    setFilesBySession(filesBySessionRef.current);
  }, []);

  const changeUploadCount = useCallback((key: string, delta: number) => {
    const count = Math.max(0, (uploadCountsRef.current[key] ?? 0) + delta);
    uploadCountsRef.current = { ...uploadCountsRef.current, [key]: count };
    setUploadingBySession(uploadCountsRef.current);
  }, []);

  const uploadFiles = useCallback(async (key: string, entries: UploadedFile[]) => {
    if (entries.length === 0) return;
    changeUploadCount(key, 1);
    try {
      for (const entry of entries) {
        updateSessionFiles(key, (current) => current.map((item) =>
          item.id === entry.id
            ? { ...item, status: "uploading", progress: 0, errorCode: undefined }
            : item,
        ));
        try {
          const response = await uploadFileWithProgress(entry.file, (event) => {
            updateSessionFiles(key, (current) => current.map((item) =>
              item.id === entry.id ? { ...item, progress: event.percent } : item,
            ));
          }, true);
          updateSessionFiles(key, (current) => current.map((item) =>
            item.id === entry.id
              ? { ...item, status: "success", selected: true, response, progress: 100 }
              : item,
          ));
        } catch (error) {
          const status = (error as { response?: { status?: number } })?.response?.status;
          updateSessionFiles(key, (current) => current.map((item) =>
            item.id === entry.id
              ? { ...item, status: "error", selected: false,
                  errorCode: status === 413 ? "too_large" : "upload_failed" }
              : item,
          ));
        }
      }
    } finally {
      changeUploadCount(key, -1);
    }
  }, [changeUploadCount, updateSessionFiles]);

  const handleFileSelect = useCallback((selectedFiles: FileList | null) => {
    if (!selectedFiles?.length) return;
    const incoming = Array.from(selectedFiles);
    const plan = planAssistantFileSelection(incoming, filesBySessionRef.current[sessionKey]?.length ?? 0);
    const accepted = plan.accepted.map((file): UploadedFile => {
      const supported = isFileTypeSupported(file);
      const validSize = file.size > 0 && file.size <= MAX_ASSISTANT_ATTACHMENT_BYTES;
      return {
        id: generateUUID(),
        file,
        status: supported && validSize ? "pending" : "error",
        selected: false,
        errorCode: !supported ? "unsupported" : !validSize ? "too_large" : undefined,
      };
    });
    setNoticesBySession((current) => ({
      ...current,
      [sessionKey]: plan.exceedsLimit ? "limit" : "",
    }));
    updateSessionFiles(sessionKey, (current) => [...current, ...accepted]);
    void uploadFiles(sessionKey, accepted.filter((entry) => entry.status === "pending"));
  }, [sessionKey, updateSessionFiles, uploadFiles]);

  const handlePaste = usePasteImage(handleFileSelect);

  const removeFile = useCallback((index: number) => {
    updateSessionFiles(sessionKey, (current) => current.filter((_, itemIndex) => itemIndex !== index));
  }, [sessionKey, updateSessionFiles]);

  const retryFile = useCallback((index: number) => {
    const entry = filesBySessionRef.current[sessionKey]?.[index];
    if (!entry || entry.status !== "error" || entry.errorCode === "unsupported" || entry.errorCode === "too_large") return;
    void uploadFiles(sessionKey, [entry]);
  }, [sessionKey, uploadFiles]);

  const toggleFileSelection = useCallback((index: number) => {
    updateSessionFiles(sessionKey, (current) => current.map((entry, itemIndex) =>
      itemIndex === index && entry.status === "success"
        ? { ...entry, selected: !entry.selected }
        : entry,
    ));
  }, [sessionKey, updateSessionFiles]);

  const consumeSelectedFiles = useCallback(() => {
    updateSessionFiles(sessionKey, (current) => current.filter((entry) =>
      !(entry.status === "success" && entry.selected),
    ));
  }, [sessionKey, updateSessionFiles]);

  const clearFiles = useCallback(() => {
    updateSessionFiles(sessionKey, () => []);
  }, [sessionKey, updateSessionFiles]);

  const rekeyFiles = useCallback((fromKey: string, toKey: string) => {
    if (fromKey === toKey) return;
    const incoming = filesBySessionRef.current[fromKey] ?? [];
    if (!incoming.length) return;
    const existing = filesBySessionRef.current[toKey] ?? [];
    const ids = new Set(existing.map((entry) => entry.id));
    filesBySessionRef.current = {
      ...filesBySessionRef.current,
      [fromKey]: [],
      [toKey]: [...existing, ...incoming.filter((entry) => !ids.has(entry.id))],
    };
    setFilesBySession(filesBySessionRef.current);
    setNoticesBySession((current) => ({ ...current, [toKey]: current[fromKey] ?? "", [fromKey]: "" }));
  }, []);

  return {
    files,
    isUploading,
    selectionNotice,
    fileInputRef,
    handleFileSelect,
    handlePaste,
    removeFile,
    retryFile,
    toggleFileSelection,
    consumeSelectedFiles,
    clearFiles,
    rekeyFiles,
  };
}
