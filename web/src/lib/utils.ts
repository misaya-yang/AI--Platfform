import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";
import { AxiosError } from "axios";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
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

