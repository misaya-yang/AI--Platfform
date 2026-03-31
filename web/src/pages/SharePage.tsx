/**
 * Public Share Page — ChatGPT-style read-only conversation snapshot.
 *
 * Accessible at /share/:shareId (no auth required).
 * Fetches conversation from Islamic Content Service via Gateway proxy.
 */
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "@/lib/api";

interface ShareMessage {
  role: "user" | "assistant";
  content: string;
}

interface ShareData {
  title: string;
  messages: ShareMessage[];
  message_count: number;
  agent_name: string;
  created_at: string;
  expires_at: string | null;
}

export function SharePage() {
  const { shareId } = useParams<{ shareId: string }>();
  const [data, setData] = useState<ShareData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!shareId) return;
    fetch(`/api/v1/islamic/wahda/share/${shareId}`)
      .then((resp) => {
        if (!resp.ok) throw new Error(resp.status === 404 ? "Conversation not found or expired" : "Failed to load");
        return resp.json();
      })
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [shareId]);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-950">
        <div className="animate-spin h-8 w-8 border-2 border-indigo-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-950">
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

  return (
    <div className="min-h-screen bg-white dark:bg-gray-950">
      {/* Header */}
      <header className="sticky top-0 z-10 bg-white/80 dark:bg-gray-950/80 backdrop-blur-sm border-b border-gray-200 dark:border-gray-800">
        <div className="max-w-3xl mx-auto px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            {/* Wahda Avatar */}
            <div className="w-8 h-8 rounded-full bg-gradient-to-b from-indigo-500 to-indigo-700 flex items-center justify-center">
              <svg className="w-4 h-4 text-white" fill="currentColor" viewBox="0 0 20 20">
                <path d="M10 2a8 8 0 100 16 8 8 0 000-16zm0 14a6 6 0 110-12 6 6 0 010 12z" />
              </svg>
            </div>
            <div>
              <h1 className="text-sm font-semibold text-gray-800 dark:text-gray-200">
                {data.agent_name}
              </h1>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                Shared conversation · {data.message_count} messages
              </p>
            </div>
          </div>
          <time className="text-xs text-gray-400 dark:text-gray-500">
            {new Date(data.created_at).toLocaleDateString(undefined, {
              year: "numeric", month: "short", day: "numeric",
            })}
          </time>
        </div>
      </header>

      {/* Title */}
      <div className="max-w-3xl mx-auto px-4 pt-6 pb-2">
        <h2 className="text-lg font-semibold text-gray-800 dark:text-gray-200">
          {data.title}
        </h2>
      </div>

      {/* Messages */}
      <div className="max-w-3xl mx-auto px-4 py-4 space-y-6">
        {data.messages.map((msg, i) => (
          <div
            key={i}
            className={`flex gap-3 ${msg.role === "user" ? "flex-row-reverse" : "flex-row"}`}
          >
            {/* Avatar */}
            <div
              className={`w-8 h-8 rounded-full flex-shrink-0 flex items-center justify-center text-xs font-medium ${
                msg.role === "user"
                  ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-300"
                  : "bg-gradient-to-b from-indigo-500 to-indigo-700 text-white"
              }`}
            >
              {msg.role === "user" ? "U" : "W"}
            </div>

            {/* Bubble */}
            <div
              className={`max-w-[80%] min-w-0 ${
                msg.role === "user"
                  ? "bg-emerald-500 text-white rounded-2xl rounded-tr-sm px-4 py-2.5"
                  : "bg-gray-50 dark:bg-gray-900 text-gray-800 dark:text-gray-200 rounded-2xl rounded-tl-sm px-4 py-3"
              }`}
            >
              <div className="text-sm leading-relaxed whitespace-pre-wrap break-words">
                {msg.content}
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Footer */}
      <footer className="max-w-3xl mx-auto px-4 py-8 text-center border-t border-gray-100 dark:border-gray-800 mt-8">
        <p className="text-xs text-gray-400 dark:text-gray-500">
          Shared from {data.agent_name} · AI-generated content for educational purposes only
        </p>
        {data.expires_at && (
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
            Expires {new Date(data.expires_at).toLocaleDateString()}
          </p>
        )}
      </footer>
    </div>
  );
}
