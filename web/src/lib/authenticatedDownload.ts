import { api } from "@/lib/api";

function protectedApiPath(url: string): boolean {
  return url.startsWith("/api/") && !url.startsWith("//");
}

function permittedDirectUrl(url: string): boolean {
  return /^https?:\/\//i.test(url) || /^(blob:|data:)/i.test(url);
}

async function resolveArtifactUrl(url: string): Promise<{ href: string; temporary: boolean }> {
  if (protectedApiPath(url)) {
    const response = await api.get<Blob>(url, { responseType: "blob" });
    return { href: URL.createObjectURL(response.data), temporary: true };
  }
  if (permittedDirectUrl(url)) return { href: url, temporary: false };
  throw new Error("Unsupported artifact URL");
}

export async function downloadAssistantArtifact(url: string, filename: string): Promise<void> {
  const resolved = await resolveArtifactUrl(url);
  const link = document.createElement("a");
  link.href = resolved.href;
  link.download = filename;
  link.rel = "noopener noreferrer";
  document.body.appendChild(link);
  link.click();
  link.remove();
  if (resolved.temporary) window.setTimeout(() => URL.revokeObjectURL(resolved.href), 60_000);
}

export async function openAssistantArtifact(url: string): Promise<void> {
  if (!protectedApiPath(url)) {
    if (!permittedDirectUrl(url)) throw new Error("Unsupported artifact URL");
    window.open(url, "_blank", "noopener,noreferrer");
    return;
  }
  const previewWindow = window.open("about:blank", "_blank");
  try {
    const resolved = await resolveArtifactUrl(url);
    if (!previewWindow) {
      URL.revokeObjectURL(resolved.href);
      throw new Error("Preview popup was blocked");
    }
    previewWindow.location.replace(resolved.href);
    window.setTimeout(() => URL.revokeObjectURL(resolved.href), 60_000);
  } catch (error) {
    previewWindow?.close();
    throw error;
  }
}
