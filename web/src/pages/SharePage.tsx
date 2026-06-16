/**
 * Public Share Page — read-only conversation snapshot with artifacts.
 *
 * Accessible at /share/:shareId (no auth required).
 * Supports conversation shares from /api/v1/assistant/shares/:code.
 *
 * Phase 3 retheme: aligned to the single-accent (gold) palette shared with
 * the main /assistant surface. All hard-coded slate/gray/indigo/emerald/
 * blue/purple tones were swapped for --assistant-* tokens. Gold is not
 * used on this page at all — it shares the main app's neutral chrome so
 * an embedded <QuizCard scope="share"> carries the only gold moments.
 */
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowRight, Download } from "lucide-react";
import { formatFileSize, getFormatLabel } from "@/lib/format";
import { QuizCard } from "@/pages/assistant/components/Quiz";
import type { QuizData } from "@/pages/assistant/types";

// ── Types ────────────────────────────────────────────────────────────

interface ShareMessage {
  role: "user" | "assistant";
  content: string;
  timestamp?: string;
  metadata?: Record<string, unknown>;
  /** Frozen quiz payload attached at share-creation time. Present only on
   *  assistant messages that produced a quiz via the generate_quiz tool. */
  quiz_data?: QuizData;
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

// ── Component ────────────────────────────────────────────────────────

export function SharePage() {
  const { shareId } = useParams<{ shareId: string }>();
  const [convShare, setConvShare] = useState<ConversationShareData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!shareId) return;

    fetch(`/api/v1/assistant/shares/${shareId}`)
      .then((resp) => {
        if (resp.ok) return resp.json().then((d: ConversationShareData) => setConvShare(d));
        throw new Error(resp.status === 404 ? "Conversation not found or expired" : "Failed to load");
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [shareId]);

  if (loading) {
    return (
      <div className="assistant-v2 min-h-screen flex items-center justify-center bg-[hsl(var(--assistant-canvas-bg))]">
        <div className="h-6 w-6 rounded-full border-2 border-[hsl(var(--assistant-border))] border-t-[hsl(var(--assistant-accent))] animate-spin" />
      </div>
    );
  }

  if (error || !convShare) {
    return (
      <div className="assistant-v2 min-h-screen flex items-center justify-center bg-[hsl(var(--assistant-canvas-bg))]">
        <div className="text-center space-y-3 max-w-md px-6">
          <h1 className="text-[18px] font-semibold text-[hsl(var(--assistant-text-primary))]">
            {error || "Conversation not found"}
          </h1>
          <p className="text-[13px] text-[hsl(var(--assistant-text-secondary))]">
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
      <div className="assistant-v2 min-h-screen bg-[hsl(var(--assistant-canvas-bg))] text-[hsl(var(--assistant-text-primary))]">
        {/* Header */}
        <header className="sticky top-0 z-10 bg-[hsl(var(--assistant-canvas-bg)/0.85)] backdrop-blur-xs border-b border-[hsl(var(--assistant-border))]">
          <div className="max-w-3xl mx-auto px-4 py-3 flex items-center justify-between">
            <div className="flex items-center gap-2.5">
              <div className="w-7 h-7 rounded-full bg-[hsl(var(--assistant-surface-bg))] border border-[hsl(var(--assistant-border))] flex items-center justify-center">
                <svg
                  className="w-3.5 h-3.5 text-[hsl(var(--assistant-text-secondary))]"
                  fill="currentColor"
                  viewBox="0 0 20 20"
                  aria-hidden
                >
                  <path d="M10 2a8 8 0 100 16 8 8 0 000-16zm0 14a6 6 0 110-12 6 6 0 010 12z" />
                </svg>
              </div>
              <div>
                <h1 className="text-[13px] font-medium text-[hsl(var(--assistant-text-primary))]">
                  AI Assistant{snapshot.model_id ? ` · ${snapshot.model_id}` : ""}
                </h1>
                <p className="text-[11px] font-mono text-[hsl(var(--assistant-text-tertiary))] mt-0.5">
                  Shared · {snapshot.messages.length} messages
                  {artifact_count > 0 && ` · ${artifact_count} files`}
                  {view_count > 0 && ` · ${view_count} views`}
                </p>
              </div>
            </div>
            <time className="text-[11px] font-mono text-[hsl(var(--assistant-text-tertiary))]">
              {new Date(created_at).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })}
            </time>
          </div>
        </header>

        {/* Title */}
        {title && (
          <div className="max-w-3xl mx-auto px-4 pt-6 pb-2">
            <h2 className="text-[18px] font-semibold text-[hsl(var(--assistant-text-primary))] tracking-tight">
              {title}
            </h2>
          </div>
        )}

        {/* Messages */}
        <div className="max-w-3xl mx-auto px-4 py-4 space-y-5">
          {snapshot.messages.map((msg, i) => {
            const msgArtifacts = msg.role === "assistant" ? getMessageArtifacts(msg) : [];
            const isUser = msg.role === "user";
            return (
              <div
                key={i}
                className={`flex gap-3 ${isUser ? "flex-row-reverse" : "flex-row"}`}
              >
                {/* Avatar */}
                <div
                  className="w-7 h-7 rounded-full shrink-0 flex items-center justify-center text-[11px] font-medium bg-[hsl(var(--assistant-surface-bg))] border border-[hsl(var(--assistant-border))] text-[hsl(var(--assistant-text-secondary))]"
                  aria-hidden
                >
                  {isUser ? "U" : "AI"}
                </div>

                {/* Bubble / copy */}
                <div
                  className={`max-w-[80%] min-w-0 ${
                    isUser
                      ? "bg-[hsl(var(--assistant-user-bubble))] text-[hsl(var(--assistant-text-primary))] rounded-[14px] rounded-tr-sm px-3.5 py-2"
                      : "text-[hsl(var(--assistant-text-primary))]"
                  }`}
                >
                  {isUser ? (
                    <div className="text-[14px] leading-relaxed whitespace-pre-wrap wrap-break-word">
                      {msg.content}
                    </div>
                  ) : (
                    <div className="assistant-copy text-[14px] leading-relaxed prose prose-sm max-w-none">
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

                  {/* Inline interactive quiz — anonymous submit wired to
                       /assistant/shares/:code/quiz/:id/submit. State keyed by
                       shareCode so it never collides with the author's main-app
                       session state. */}
                  {msg.role === "assistant" && msg.quiz_data && (
                    <div className="mt-3">
                      <QuizCard
                        quizData={msg.quiz_data}
                        scope="share"
                        shareCode={convShare.share_code}
                      />
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        {/* CTA + Footer */}
        <div className="max-w-3xl mx-auto px-4 py-6 flex justify-center">
          <a
            href="/assistant"
            className="act-btn act-hover inline-flex items-center gap-1.5 h-8 px-3 rounded-md text-[13px] font-medium text-[hsl(var(--assistant-text-secondary))] hover:text-[hsl(var(--assistant-text-primary))]"
          >
            Start a new conversation
            <ArrowRight className="w-[14px] h-[14px]" />
          </a>
        </div>
        <footer className="max-w-3xl mx-auto px-4 py-6 text-center">
          <p className="text-[11px] font-mono text-[hsl(var(--assistant-text-tertiary))]">
            Shared from AI Platform · AI-generated content
          </p>
          {convShare.expires_at && (
            <p className="text-[11px] font-mono text-[hsl(var(--assistant-text-tertiary))] mt-1">
              Expires {new Date(convShare.expires_at).toLocaleDateString()}
            </p>
          )}
        </footer>
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
    <div className="rounded-[10px] border border-[hsl(var(--assistant-border))] overflow-hidden bg-[hsl(var(--assistant-surface-bg))]">
      {isImage && (
        <a href={downloadUrl} target="_blank" rel="noopener noreferrer" className="block">
          <img
            src={downloadUrl}
            alt={artifact.title}
            className="w-full max-h-[400px] object-contain bg-[hsl(var(--assistant-surface-soft))]"
            loading="lazy"
          />
        </a>
      )}
      <div className="flex items-center gap-2.5 p-2.5">
        {/* Monospace uppercase format glyph (matches ArtifactsPanel treatment) */}
        <span
          className="shrink-0 px-1.5 py-[2px] text-[9px] font-mono font-semibold tracking-wider uppercase rounded bg-[hsl(var(--assistant-chip-bg))] text-[hsl(var(--assistant-text-tertiary))]"
          aria-hidden
        >
          {label.slice(0, 4)}
        </span>
        <div className="flex-1 min-w-0">
          <p className="text-[13px] font-medium truncate text-[hsl(var(--assistant-text-primary))]">
            {artifact.title || artifact.filename}
          </p>
          <p className="text-[11px] font-mono text-[hsl(var(--assistant-text-tertiary))]">
            {label} · {formatFileSize(artifact.size_bytes)}
          </p>
        </div>
        <a
          href={downloadUrl}
          download={artifact.filename}
          className="shrink-0 inline-flex items-center justify-center w-7 h-7 rounded-md text-[hsl(var(--assistant-text-secondary))] hover:bg-[hsl(var(--assistant-surface-soft))] hover:text-[hsl(var(--assistant-text-primary))] transition-colors duration-150"
          title="Download"
          aria-label="Download"
        >
          <Download className="w-[14px] h-[14px]" />
        </a>
      </div>
    </div>
  );
}
