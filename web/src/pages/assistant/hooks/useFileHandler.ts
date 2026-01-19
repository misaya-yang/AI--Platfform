import { useState, useCallback, useRef } from "react";
import { 
  uploadFileWithProgress, 
  isFileTypeSupported, 
  type UploadResponse 
} from "@/api/files";
import { usePasteImage } from "@/hooks/usePasteImage";

export interface UploadedFile {
  file: File;
  status: "pending" | "uploading" | "success" | "error";
  progress?: number;
  error?: string;
  response?: UploadResponse;
}

export function useFileHandler() {
  const [files, setFiles] = useState<UploadedFile[]>([]);
  const [isUploading, setIsUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Upload files logic
  const uploadFiles = useCallback(async (filesToUpload: UploadedFile[]) => {
    if (filesToUpload.length === 0) return;

    setIsUploading(true);

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
    setFiles((currentFiles) => {
      const remainingSlots = MAX_FILES - currentFiles.length;
      if (remainingSlots <= 0) return currentFiles;

      const validFiles = Array.from(selectedFiles)
        .filter((file) => isFileTypeSupported(file))
        .slice(0, remainingSlots);

      if (validFiles.length === 0) return currentFiles;

      const newFiles: UploadedFile[] = validFiles.map((file) => ({
        file,
        status: "pending" as const,
      }));

      // Start uploads automatically
      setTimeout(() => uploadFiles(newFiles), 0);

      return [...currentFiles, ...newFiles];
    });
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
