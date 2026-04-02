/**
 * Public Share Page — read-only conversation snapshot with artifacts.
 *
 * Accessible at /share/:shareId (no auth required).
 * Supports two share types:
 * 1. Conversation shares (new) — from /api/v1/assistant/shares/:code
 * 2. Legacy Playground shares — from /api/v1/islamic/wahda/share/:id
 */
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Download } from "lucide-react";

// ── Types ────────────────────────────────────────────────────────────

interface ShareMessage {
  role: "user" | "assistant";
  content: string;
  timestamp?: string;
  metadata?: Record<string, unknown>;
}

interface ShareArtifact {
  artifact_id: string;
  type: string;
  format: string;
  title: string;
  filename: string;
  size_bytes: number;
  mime_type?: string;
}

interface ConversationShareData {
  share_code: string;
  title: string;
  snapshot: {
    messages: ShareMessage[];
    artifacts: ShareArtifact[];
    model_id?: string;
    shared_at?: string;
  };
  message_count: number;
  artifact_count: number;
  view_count: number;
  created_at: string;
  expires_at: string | null;
}

interface LegacyShareData {
  title: string;
  messages: ShareMessage[];
  message_count: number;
  agent_name: string;
  created_at: string;
  expires_at: string | null;
}

// ── Helpers ──────────────────────────────────────────────────────────

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function getFormatLabel(format?: string, mimeType?: string): string {
  const f = (format || "").toLowerCase();
  if (f === "docx" || f === "doc" || mimeType?.includes("word")) return "DOCX";
  if (f === "pdf" || mimeType?.includes("pdf")) return "PDF";
  if (f === "pptx" || mimeType?.includes("presentation")) return "PPTX";
  if (f === "png" || f === "jpg" || f === "jpeg" || f === "gif" || mimeType?.startsWith("image/")) return (f || "IMG").toUpperCase();
  if (f === "md" || f === "markdown") return "MD";
  if (f) return f.toUpperCase().slice(0, 6);
  return "FILE";
}

// ── Component ────────────────────────────────────────────────────────

export function SharePage() {
  const { shareId } = useParams<{ shareId: string }>();
  const [convShare, setConvShare] = useState<ConversationShareData | null>(null);
  const [legacyShare, setLegacyShare] = useState<LegacyShareData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!shareId) return;

    // Try conversation share first, then fall back to legacy
    fetch(`/api/v1/assistant/shares/${shareId}`)
      .then((resp) => {
        if (resp.ok) return resp.json().then((d: ConversationShareData) => setConvShare(d));
        // Fall back to legacy Islamic Content share
        return fetch(`/api/v1/islamic/wahda/share/${shareId}`)
          .then((r) => {
            if (!r.ok) throw new Error(r.status === 404 ? "Conversation not found or expired" : "Failed to load");
            return r.json();
          })
          .then((d: LegacyShareData) => setLegacyShare(d));
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [shareId]);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-white dark:bg-gray-950">
        <div className="animate-spin h-8 w-8 border-2 border-blue-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  if (error || (!convShare && !legacyShare)) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-white dark:bg-gray-950">
        <div className="text-center space-y-4 max-w-md px-6">
          <div className="text-6xl">🔗</div>
          <h1 className="text-xl font-semibold text-gray-800 dark:text-gray-200">
            {error || "Conversation not found"}
          </h1>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            This shared conversation may have expired or been removed.
          </p>
        </div>
      </div>
    );
  }

  // ── Conversation Share (new — with artifacts) ──────────────────────

  if (convShare) {
    const { snapshot, title, artifact_count, view_count, created_at } = convShare;
    const artifactMap = new Map(snapshot.artifacts.map((a) => [a.artifact_id, a]));

    // Find artifact_ids per message from metadata
    const getMessageArtifacts = (msg: ShareMessage): ShareArtifact[] => {
      const ids = (msg.metadata?.artifact_ids as string[]) || [];
      return ids.map((id) => artifactMap.get(id)).filter(Boolean) as ShareArtifact[];
    };

    return (
      <div className="min-h-screen bg-white dark:bg-gray-950">
        {/* Header */}
        <header className="sticky top-0 z-10 bg-white/80 dark:bg-gray-950/80 backdrop-blur-sm border-b border-gray-200 dark:border-gray-800">
          <div className="max-w-3xl mx-auto px-4 py-3 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-full bg-gradient-to-b from-blue-500 to-blue-700 flex items-center justify-center">
                <svg className="w-4 h-4 text-white" fill="currentColor" viewBox="0 0 20 20">
                  <path d="M10 2a8 8 0 100 16 8 8 0 000-16zm0 14a6 6 0 110-12 6 6 0 010 12z" />
                </svg>
              </div>
              <div>
                <h1 className="text-sm font-semibold text-gray-800 dark:text-gray-200">
                  AI Assistant{snapshot.model_id ? ` · ${snapshot.model_id}` : ""}
                </h1>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  Shared · {snapshot.messages.length} messages
                  {artifact_count > 0 && ` · ${artifact_count} files`}
                  {view_count > 0 && ` · ${view_count} views`}
                </p>
              </div>
            </div>
            <time className="text-xs text-gray-400">
              {new Date(created_at).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })}
            </time>
          </div>
        </header>

        {/* Title */}
        {title && (
          <div className="max-w-3xl mx-auto px-4 pt-6 pb-2">
            <h2 className="text-lg font-semibold text-gray-800 dark:text-gray-200">{title}</h2>
          </div>
        )}

        {/* Messages */}
        <div className="max-w-3xl mx-auto px-4 py-4 space-y-6">
          {snapshot.messages.map((msg, i) => {
            const msgArtifacts = msg.role === "assistant" ? getMessageArtifacts(msg) : [];
            return (
              <div key={i} className={`flex gap-3 ${msg.role === "user" ? "flex-row-reverse" : "flex-row"}`}>
                {/* Avatar */}
                <div className={`w-8 h-8 rounded-full flex-shrink-0 flex items-center justify-center text-xs font-medium ${
                  msg.role === "user"
                    ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-300"
                    : "bg-gradient-to-b from-blue-500 to-blue-700 text-white"
                }`}>
                  {msg.role === "user" ? "U" : "AI"}
                </div>

                {/* Bubble */}
                <div className={`max-w-[80%] min-w-0 ${
                  msg.role === "user"
                    ? "bg-emerald-500 text-white rounded-2xl rounded-tr-sm px-4 py-2.5"
                    : "bg-gray-50 dark:bg-gray-900 text-gray-800 dark:text-gray-200 rounded-2xl rounded-tl-sm px-4 py-3"
                }`}>
                  {msg.role === "user" ? (
                    <div className="text-sm leading-relaxed whitespace-pre-wrap break-words">{msg.content}</div>
                  ) : (
                    <div className="text-sm leading-relaxed prose prose-sm dark:prose-invert max-w-none">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
                    </div>
                  )}

                  {/* Inline artifacts */}
                  {msgArtifacts.length > 0 && (
                    <div className="mt-3 space-y-2">
                      {msgArtifacts.map((art) => (
                        <ArtifactCard key={art.artifact_id} artifact={art} shareCode={convShare.share_code} />
                      ))}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        {/* CTA + Footer */}
        <div className="max-w-3xl mx-auto px-4 py-6 flex justify-center">
          <a href="/assistant"
            className="inline-flex items-center gap-2 px-6 py-3 bg-gradient-to-r from-blue-500 to-blue-700 text-white rounded-full text-sm font-medium shadow-lg hover:shadow-xl transition-all">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
            </svg>
            Start a new conversation
          </a>
        </div>
        <footer className="max-w-3xl mx-auto px-4 py-8 text-center border-t border-gray-100 dark:border-gray-800">
          <p className="text-xs text-gray-400">Shared from AI Platform · AI-generated content</p>
          {convShare.expires_at && (
            <p className="text-xs text-gray-400 mt-1">Expires {new Date(convShare.expires_at).toLocaleDateString()}</p>
          )}
        </footer>
      </div>
    );
  }

  // ── Legacy Share (old Playground) ──────────────────────────────────

  if (legacyShare) {
    return (
      <div className="min-h-screen bg-white dark:bg-gray-950">
        <header className="sticky top-0 z-10 bg-white/80 dark:bg-gray-950/80 backdrop-blur-sm border-b border-gray-200 dark:border-gray-800">
          <div className="max-w-3xl mx-auto px-4 py-3 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-full bg-gradient-to-b from-indigo-500 to-indigo-700 flex items-center justify-center">
                <svg className="w-4 h-4 text-white" fill="currentColor" viewBox="0 0 20 20">
                  <path d="M10 2a8 8 0 100 16 8 8 0 000-16zm0 14a6 6 0 110-12 6 6 0 010 12z" />
                </svg>
              </div>
              <div>
                <h1 className="text-sm font-semibold text-gray-800 dark:text-gray-200">{legacyShare.agent_name}</h1>
                <p className="text-xs text-gray-500">{legacyShare.message_count} messages</p>
              </div>
            </div>
            <time className="text-xs text-gray-400">
              {new Date(legacyShare.created_at).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })}
            </time>
          </div>
        </header>
        <div className="max-w-3xl mx-auto px-4 pt-6 pb-2">
          <h2 className="text-lg font-semibold text-gray-800 dark:text-gray-200">{legacyShare.title}</h2>
        </div>
        <div className="max-w-3xl mx-auto px-4 py-4 space-y-6">
          {legacyShare.messages.map((msg, i) => (
            <div key={i} className={`flex gap-3 ${msg.role === "user" ? "flex-row-reverse" : "flex-row"}`}>
              <div className={`w-8 h-8 rounded-full flex-shrink-0 flex items-center justify-center text-xs font-medium ${
                msg.role === "user"
                  ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-300"
                  : "bg-gradient-to-b from-indigo-500 to-indigo-700 text-white"
              }`}>
                {msg.role === "user" ? "U" : "W"}
              </div>
              <div className={`max-w-[80%] min-w-0 ${
                msg.role === "user"
                  ? "bg-emerald-500 text-white rounded-2xl rounded-tr-sm px-4 py-2.5"
                  : "bg-gray-50 dark:bg-gray-900 text-gray-800 dark:text-gray-200 rounded-2xl rounded-tl-sm px-4 py-3"
              }`}>
                {msg.role === "user" ? (
                  <div className="text-sm leading-relaxed whitespace-pre-wrap break-words">{msg.content}</div>
                ) : (
                  <div className="text-sm leading-relaxed prose prose-sm dark:prose-invert max-w-none">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
        <div className="max-w-3xl mx-auto px-4 py-6 flex justify-center">
          <a href="/playground"
            className="inline-flex items-center gap-2 px-6 py-3 bg-gradient-to-r from-indigo-500 to-indigo-700 text-white rounded-full text-sm font-medium shadow-lg hover:shadow-xl transition-all">
            Continue this conversation
          </a>
        </div>
      </div>
    );
  }

  return null;
}

// ── Inline Artifact Card ─────────────────────────────────────────────

function ArtifactCard({ artifact, shareCode }: { artifact: ShareArtifact; shareCode: string }) {
  const downloadUrl = `/api/v1/assistant/shares/${shareCode}/artifact/${artifact.artifact_id}`;
  const isImage = artifact.type === "image" || artifact.mime_type?.startsWith("image/");
  const label = getFormatLabel(artifact.format, artifact.mime_type);

  return (
    <div className="rounded-xl border border-gray-200 dark:border-gray-700 overflow-hidden bg-white dark:bg-gray-800">
      {isImage && (
        <a href={downloadUrl} target="_blank" rel="noopener noreferrer" className="block">
          <img
            src={downloadUrl}
            alt={artifact.title}
            className="w-full max-h-[400px] object-contain bg-gray-100 dark:bg-gray-900"
            loading="lazy"
          />
        </a>
      )}
      <div className="flex items-center gap-3 p-3">
        <div className={`w-9 h-9 rounded-lg flex items-center justify-center text-white text-[9px] font-bold ${
          isImage ? "bg-purple-500" : "bg-blue-500"
        }`}>
          {label}
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium truncate">{artifact.title || artifact.filename}</p>
          <p className="text-xs text-gray-500">{label} · {formatFileSize(artifact.size_bytes)}</p>
        </div>
        <a
          href={downloadUrl}
          download={artifact.filename}
          className="shrink-0 p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          title="Download"
        >
          <Download className="w-4 h-4 text-gray-500" />
        </a>
      </div>
    </div>
  );
}
