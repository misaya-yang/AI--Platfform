import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";
import { AxiosError } from "axios";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Generate a UUID with fallback for insecure contexts (non-HTTPS).
 * crypto.randomUUID requires a secure context; this function catches
 * the error and falls back to a Math.random-based implementation.
 */
export function generateUUID(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
  } catch {
    // crypto.randomUUID exists but throws in insecure context
  }
  // Fallback implementation (RFC 4122 v4 compliant)
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = Math.random() * 16 | 0;
    const v = c === 'x' ? r : (r & 0x3 | 0x8);
    return v.toString(16);
  });
}

/**
 * Extract user-friendly error message from various error types.
 * Handles axios errors, standard errors, and unknown error types.
 */
export function getErrorMessage(error: unknown): string {
  // Handle axios errors - extract the detail from response
  if (error instanceof AxiosError) {
    const detail = error.response?.data?.detail;
    if (detail) {
      return typeof detail === "string" ? detail : JSON.stringify(detail);
    }
    // Fallback to status text or axios message
    return error.response?.statusText || error.message;
  }

  // Handle standard Error objects
  if (error instanceof Error) {
    return error.message;
  }

  // Handle string errors
  if (typeof error === "string") {
    return error;
  }

  // Fallback for unknown error types
  return "An unexpected error occurred";
}

