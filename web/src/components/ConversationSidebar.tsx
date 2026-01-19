/**
 * Conversation Sidebar Component
 *
 * Displays a list of chat sessions grouped by date with:
 * - New chat button
 * - Search functionality
 * - Date-based grouping (Today, Yesterday, Last 7 days, Older)
 * - Session selection and deletion
 */

import { useCallback, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { motion, AnimatePresence } from "framer-motion";
import { cn } from "@/lib/utils";
import {
  MessageSquare,
  Plus,
  Search,
  Trash2,
  X,
  Loader2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { SessionSummary } from "@/api/sessions";

interface ConversationSidebarProps {
  sessions: SessionSummary[];
  activeSessionId: string | null;
  isLoading?: boolean;
  onNewChat: () => void;
  onSelectSession: (sessionId: string) => void;
  onDeleteSession: (sessionId: string) => void;
}

interface GroupedSessions {
  today: SessionSummary[];
  yesterday: SessionSummary[];
  lastWeek: SessionSummary[];
  older: SessionSummary[];
}

function getSessionTitle(session: SessionSummary): string {
  const metadata = session.metadata as Record<string, unknown> | undefined;
  if (metadata?.title && typeof metadata.title === "string") {
    return metadata.title;
  }
  // Fallback to session ID prefix
  return `Chat ${session.session_id.slice(0, 8)}...`;
}

function groupSessionsByDate(sessions: SessionSummary[]): GroupedSessions {
  const groups: GroupedSessions = {
    today: [],
    yesterday: [],
    lastWeek: [],
    older: [],
  };

  if (!Array.isArray(sessions)) {
    return groups;
  }

  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  const lastWeek = new Date(today);
  lastWeek.setDate(lastWeek.getDate() - 7);

  for (const session of sessions) {
    const sessionDate = new Date(session.updated_at || session.created_at);

    if (sessionDate >= today) {
      groups.today.push(session);
    } else if (sessionDate >= yesterday) {
      groups.yesterday.push(session);
    } else if (sessionDate >= lastWeek) {
      groups.lastWeek.push(session);
    } else {
      groups.older.push(session);
    }
  }

  // Sort each group by date (newest first)
  const sortByDate = (a: SessionSummary, b: SessionSummary) => {
    const dateA = new Date(a.updated_at || a.created_at).getTime();
    const dateB = new Date(b.updated_at || b.created_at).getTime();
    return dateB - dateA;
  };

  groups.today.sort(sortByDate);
  groups.yesterday.sort(sortByDate);
  groups.lastWeek.sort(sortByDate);
  groups.older.sort(sortByDate);

  return groups;
}

function SessionItem({
  session,
  isActive,
  onSelect,
  onDelete,
}: {
  session: SessionSummary;
  isActive: boolean;
  onSelect: () => void;
  onDelete: () => void;
}) {
  const [showDelete, setShowDelete] = useState(false);
  const title = getSessionTitle(session);

  return (
    <motion.div
      layout
      initial={{ opacity: 0, x: -10 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: -10 }}
      className={cn(
        "group relative flex items-center gap-2 px-3 py-2 rounded-lg cursor-pointer transition-all",
        isActive
          ? "bg-primary/10 text-primary"
          : "hover:bg-muted/50 text-muted-foreground hover:text-foreground"
      )}
      onMouseEnter={() => setShowDelete(true)}
      onMouseLeave={() => setShowDelete(false)}
      onClick={onSelect}
    >
      <MessageSquare className="h-4 w-4 shrink-0" />
      <span className="flex-1 truncate text-sm">{title}</span>
      <AnimatePresence>
        {showDelete && (
          <motion.button
            initial={{ opacity: 0, scale: 0.8 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.8 }}
            className="shrink-0 p-1 rounded hover:bg-destructive/10 hover:text-destructive"
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </motion.button>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

function SessionGroup({
  label,
  sessions,
  activeSessionId,
  onSelectSession,
  onDeleteSession,
}: {
  label: string;
  sessions: SessionSummary[];
  activeSessionId: string | null;
  onSelectSession: (sessionId: string) => void;
  onDeleteSession: (sessionId: string) => void;
}) {
  if (sessions.length === 0) return null;

  return (
    <div className="mb-4">
      <div className="px-3 py-1.5 text-xs font-medium text-muted-foreground uppercase tracking-wider">
        {label}
      </div>
      <div className="space-y-0.5">
        {sessions.map((session) => (
          <SessionItem
            key={session.session_id}
            session={session}
            isActive={session.session_id === activeSessionId}
            onSelect={() => onSelectSession(session.session_id)}
            onDelete={() => onDeleteSession(session.session_id)}
          />
        ))}
      </div>
    </div>
  );
}

export function ConversationSidebar({
  sessions,
  activeSessionId,
  isLoading = false,
  onNewChat,
  onSelectSession,
  onDeleteSession,
}: ConversationSidebarProps) {
  const { t } = useTranslation();
  const [searchQuery, setSearchQuery] = useState("");

  // Filter sessions by search query
  const filteredSessions = useMemo(() => {
    if (!Array.isArray(sessions)) return [];
    if (!searchQuery.trim()) return sessions;
    const query = searchQuery.toLowerCase();
    return sessions.filter((session) => {
      const title = getSessionTitle(session).toLowerCase();
      return title.includes(query);
    });
  }, [sessions, searchQuery]);

  // Group filtered sessions by date
  const groupedSessions = useMemo(
    () => groupSessionsByDate(filteredSessions),
    [filteredSessions]
  );

  const hasNoSessions =
    !isLoading &&
    groupedSessions.today.length === 0 &&
    groupedSessions.yesterday.length === 0 &&
    groupedSessions.lastWeek.length === 0 &&
    groupedSessions.older.length === 0;

  return (
    <div className="flex flex-col h-full">
      {/* New Chat Button */}
      <div className="p-3 border-b">
        <Button
          onClick={onNewChat}
          className="w-full gap-2"
          variant="default"
        >
          <Plus className="h-4 w-4" />
          {t("assistant.newChat")}
        </Button>
      </div>

      {/* Search */}
      <div className="p-3">
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder={t("assistant.searchConversations")}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="pl-8 pr-8 h-9 text-sm"
          />
          {searchQuery && (
            <button
              className="absolute right-2 top-1/2 -translate-y-1/2 p-0.5 rounded hover:bg-muted"
              onClick={() => setSearchQuery("")}
            >
              <X className="h-3.5 w-3.5 text-muted-foreground" />
            </button>
          )}
        </div>
      </div>

      {/* Session List */}
      <div className="flex-1 overflow-y-auto px-2">
        {isLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : hasNoSessions ? (
          <div className="flex flex-col items-center justify-center py-12 px-4 text-center">
            <MessageSquare className="h-10 w-10 text-muted-foreground/40 mb-3" />
            <p className="text-sm text-muted-foreground">
              {t("assistant.noHistory")}
            </p>
            <p className="text-xs text-muted-foreground/60 mt-1">
              {t("assistant.startNewChat")}
            </p>
          </div>
        ) : (
          <>
            <SessionGroup
              label={t("assistant.today")}
              sessions={groupedSessions.today}
              activeSessionId={activeSessionId}
              onSelectSession={onSelectSession}
              onDeleteSession={onDeleteSession}
            />
            <SessionGroup
              label={t("assistant.yesterday")}
              sessions={groupedSessions.yesterday}
              activeSessionId={activeSessionId}
              onSelectSession={onSelectSession}
              onDeleteSession={onDeleteSession}
            />
            <SessionGroup
              label={t("assistant.lastWeek")}
              sessions={groupedSessions.lastWeek}
              activeSessionId={activeSessionId}
              onSelectSession={onSelectSession}
              onDeleteSession={onDeleteSession}
            />
            <SessionGroup
              label={t("assistant.older")}
              sessions={groupedSessions.older}
              activeSessionId={activeSessionId}
              onSelectSession={onSelectSession}
              onDeleteSession={onDeleteSession}
            />
          </>
        )}
      </div>
    </div>
  );
}
