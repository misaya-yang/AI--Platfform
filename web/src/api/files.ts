/**
 * File Upload API Client
 *
 * API for uploading and managing files for AI analysis.
 */

import { api } from "@/lib/api";

// ============================================================
// Types
// ============================================================

export interface FileUploadResponse {
  file_id: string;
  file_path: string;
  filename: string;
  size_bytes: number;
  mime_type: string;
  uploaded_at: string;
  message: string;
}

export interface FileInfo {
  file_id: string;
  file_path: string;
  filename: string;
  size_bytes: number;
  mime_type: string;
  uploaded_at: string;
}

export interface FilesListResponse {
  files: FileInfo[];
  total: number;
}

// ============================================================
// File Upload APIs
// ============================================================

/**
 * Upload a single file for analysis.
 *
 * @param file - The file to upload
 * @returns Upload response with file path for agent use
 */
export async function uploadFile(file: File): Promise<FileUploadResponse> {
  const formData = new FormData();
  formData.append("file", file);

  const { data } = await api.post<FileUploadResponse>(
    "/api/v1/files/upload",
    formData,
    {
      headers: {
        "Content-Type": "multipart/form-data",
      },
    }
  );
  return data;
}

/**
 * Upload multiple files (max 5).
 *
 * @param files - Array of files to upload
 * @returns Array of upload responses
 */
export async function uploadMultipleFiles(
  files: File[]
): Promise<FileUploadResponse[]> {
  const formData = new FormData();
  files.forEach((file) => {
    formData.append("files", file);
  });

  const { data } = await api.post<FileUploadResponse[]>(
    "/api/v1/files/upload/multiple",
    formData,
    {
      headers: {
        "Content-Type": "multipart/form-data",
      },
    }
  );
  return data;
}

/**
 * List files uploaded by the current user.
 *
 * @returns List of uploaded files
 */
export async function listFiles(): Promise<FilesListResponse> {
  const { data } = await api.get<FilesListResponse>("/api/v1/files");
  return data;
}

/**
 * Get information about a specific file.
 *
 * @param fileId - The file ID
 * @returns File information
 */
export async function getFileInfo(fileId: string): Promise<FileInfo> {
  const { data } = await api.get<FileInfo>(`/api/v1/files/${fileId}`);
  return data;
}

/**
 * Delete an uploaded file.
 *
 * @param fileId - The file ID to delete
 */
export async function deleteFile(
  fileId: string
): Promise<{ status: string; file_id: string }> {
  const { data } = await api.delete<{ status: string; file_id: string }>(
    `/api/v1/files/${fileId}`
  );
  return data;
}

// ============================================================
// Utility Functions
// ============================================================

/**
 * Check if a file type is supported for upload.
 */
export function isFileTypeSupported(file: File): boolean {
  const supportedExtensions = [
    ".pdf",
    ".docx",
    ".doc",
    ".md",
    ".txt",
    ".csv",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
  ];

  const ext = "." + file.name.split(".").pop()?.toLowerCase();
  return supportedExtensions.includes(ext);
}

/**
 * Check if a file is an image.
 */
export function isImageFile(file: File): boolean {
  return file.type.startsWith("image/");
}

/**
 * Check if a file is a document.
 */
export function isDocumentFile(file: File): boolean {
  const documentTypes = [
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "text/markdown",
    "text/plain",
    "text/csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ];
  return documentTypes.includes(file.type);
}

/**
 * Format file size for display.
 */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
