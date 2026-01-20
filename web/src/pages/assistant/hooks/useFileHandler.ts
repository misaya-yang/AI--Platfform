import { useState, useCallback, useRef } from "react";
import {
  uploadFileWithProgress,
  isFileTypeSupported,
  type FileUploadResponse
} from "@/api/files";
import { usePasteImage } from "@/hooks/usePasteImage";

export interface UploadedFile {
  file: File;
  status: "pending" | "uploading" | "success" | "error";
  progress?: number;
  error?: string;
  response?: FileUploadResponse;
}

export function useFileHandler() {
  const [files, setFiles] = useState<UploadedFile[]>([]);
  const [isUploading, setIsUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Upload files logic
  const uploadFiles = useCallback(async (filesToUpload: UploadedFile[]) => {
    if (filesToUpload.length === 0) return;

    // Note: isUploading is already set to true by handleFileSelect

    for (const fileEntry of filesToUpload) {
      setFiles((prev) =>
        prev.map((f) =>
          f.file === fileEntry.file ? { ...f, status: "uploading", progress: 0 } : f
        )
      );

      try {
        const response = await uploadFileWithProgress(
          fileEntry.file,
          (event) => {
            setFiles((prev) =>
              prev.map((f) =>
                f.file === fileEntry.file ? { ...f, progress: event.percent } : f
              )
            );
          },
          true
        );

        // Debug: Log upload response
        console.log("[useFileHandler] Upload success:", {
          filename: fileEntry.file.name,
          response,
          file_path: response?.file_path,
        });

        setFiles((prev) =>
          prev.map((f) =>
            f.file === fileEntry.file
              ? { ...f, status: "success", response, progress: 100 }
              : f
          )
        );
      } catch (error) {
        setFiles((prev) =>
          prev.map((f) =>
            f.file === fileEntry.file
              ? {
                  ...f,
                  status: "error",
                  error: error instanceof Error ? error.message : "Upload failed",
                }
              : f
          )
        );
      }
    }

    setIsUploading(false);
  }, []);

  // Handle file selection
  const handleFileSelect = useCallback(async (selectedFiles: FileList | null) => {
    if (!selectedFiles || selectedFiles.length === 0) return;

    const MAX_FILES = 5;

    // First, filter and validate files synchronously
    const validFiles = Array.from(selectedFiles)
      .filter((file) => isFileTypeSupported(file));

    if (validFiles.length === 0) return;

    // Set uploading state BEFORE updating files to prevent race condition
    setIsUploading(true);

    // Create new file entries
    const newFiles: UploadedFile[] = validFiles.map((file) => ({
      file,
      status: "pending" as const,
    }));

    // Update files state
    setFiles((currentFiles) => {
      const remainingSlots = MAX_FILES - currentFiles.length;
      if (remainingSlots <= 0) {
        // No slots available, reset uploading state
        setTimeout(() => setIsUploading(false), 0);
        return currentFiles;
      }
      return [...currentFiles, ...newFiles.slice(0, remainingSlots)];
    });

    // Start uploads - take only the files that will actually be added
    const filesToUpload = newFiles.slice(0, MAX_FILES);
    if (filesToUpload.length > 0) {
      // Small delay to ensure state is updated
      await new Promise(resolve => setTimeout(resolve, 10));
      await uploadFiles(filesToUpload);
    } else {
      setIsUploading(false);
    }
  }, [uploadFiles]);

  // Handle paste event
  const handlePaste = usePasteImage(handleFileSelect);

  // Remove file
  const removeFile = useCallback((index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const clearFiles = useCallback(() => {
    setFiles([]);
  }, []);

  return {
    files,
    isUploading,
    fileInputRef,
    handleFileSelect,
    handlePaste,
    removeFile,
    clearFiles
  };
}
