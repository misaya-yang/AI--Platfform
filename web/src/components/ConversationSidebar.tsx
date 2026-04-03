/**
 * Conversation Sidebar — GPT-style session list with rename + folder grouping.
 *
 * Features:
 * - Date-based grouping (Today / Yesterday / Last 7 days / Older)
 * - Folder/Project grouping (sessions with metadata.folder)
 * - Inline rename (double-click title)
 * - Context menu (rename / move to folder / delete)
 * - Search across all sessions
 */

import { useCallback, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { cn } from "@/lib/utils";
import { CustomizeDialog } from "@/pages/assistant/components/CustomizeDialog";
import {
  FolderOpen,
  FolderPlus,
  MessageSquare,
  Pencil,
  Plus,
  Search,
  Settings,
  Trash2,
  X,
  Loader2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { SessionSummary } from "@/api/sessions";
import { updateSession } from "@/api/sessions";

// ── Types ────────────────────────────────────────────────────────────

interface ConversationSidebarProps {
  sessions: SessionSummary[];
  activeSessionId: string | null;
  isLoading?: boolean;
  onNewChat: () => void;
  onSelectSession: (sessionId: string) => void;
  onDeleteSession: (sessionId: string) => void;
  onSessionsChange?: (sessions: SessionSummary[]) => void;
}

// ── Helpers ──────────────────────────────────────────────────────────

function getSessionTitle(session: SessionSummary, fallback: string): string {
  const meta = session.metadata as Record<string, unknown> | undefined;
  if (meta?.title && typeof meta.title === "string") return meta.title;
  return `${fallback} ${session.session_id.slice(0, 8)}...`;
}

function getSessionFolder(session: SessionSummary): string | null {
  const meta = session.metadata as Record<string, unknown> | undefined;
  return (meta?.folder as string) || null;
}

interface GroupedSessions {
  folders: Record<string, SessionSummary[]>;
  today: SessionSummary[];
  yesterday: SessionSummary[];
  lastWeek: SessionSummary[];
  older: SessionSummary[];
}

function groupSessions(sessions: SessionSummary[]): GroupedSessions {
  const groups: GroupedSessions = { folders: {}, today: [], yesterday: [], lastWeek: [], older: [] };
  if (!Array.isArray(sessions)) return groups;

  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today); yesterday.setDate(yesterday.getDate() - 1);
  const lastWeek = new Date(today); lastWeek.setDate(lastWeek.getDate() - 7);

  for (const session of sessions) {
    const folder = getSessionFolder(session);
    if (folder) {
      (groups.folders[folder] ??= []).push(session);
      continue;
    }
    const d = new Date(session.updated_at || session.created_at);
    if (d >= today) groups.today.push(session);
    else if (d >= yesterday) groups.yesterday.push(session);
    else if (d >= lastWeek) groups.lastWeek.push(session);
    else groups.older.push(session);
  }

  const byDate = (a: SessionSummary, b: SessionSummary) =>
    new Date(b.updated_at || b.created_at).getTime() - new Date(a.updated_at || a.created_at).getTime();
  groups.today.sort(byDate);
  groups.yesterday.sort(byDate);
  groups.lastWeek.sort(byDate);
  groups.older.sort(byDate);
  Object.values(groups.folders).forEach((arr) => arr.sort(byDate));

  return groups;
}

// ── Session Item (with inline rename) ────────────────────────────────

function SessionItem({
  session, isActive, onSelect, onDelete, onRename, onMoveToFolder, fallbackLabel,
}: {
  session: SessionSummary;
  isActive: boolean;
  onSelect: () => void;
  onDelete: () => void;
  onRename: (newTitle: string) => void;
  onMoveToFolder: (folder: string | null) => void;
  fallbackLabel: string;
}) {
  const { t } = useTranslation();
  const [isEditing, setIsEditing] = useState(false);
  const [editValue, setEditValue] = useState("");
  const [showMenu, setShowMenu] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const title = getSessionTitle(session, fallbackLabel);

  const startEdit = useCallback(() => {
    setEditValue(title);
    setIsEditing(true);
    setShowMenu(false);
    setTimeout(() => inputRef.current?.select(), 50);
  }, [title]);

  const commitEdit = useCallback(() => {
    setIsEditing(false);
    const trimmed = editValue.trim();
    if (trimmed && trimmed !== title) onRename(trimmed);
  }, [editValue, title, onRename]);

  return (
    <motion.div layout className="group relative">
      {isEditing ? (
        <div className="px-2 py-1">
          <input
            ref={inputRef}
            value={editValue}
            onChange={(e) => setEditValue(e.target.value)}
            onBlur={commitEdit}
            onKeyDown={(e) => { if (e.key === "Enter") commitEdit(); if (e.key === "Escape") setIsEditing(false); }}
            className="w-full rounded-md border border-primary/40 bg-background px-2 py-1.5 text-sm outline-none focus:border-primary"
            autoFocus
          />
        </div>
      ) : (
        <button
          type="button"
          className={cn(
            "flex w-full items-center gap-2 rounded-lg px-3 py-2 pr-16 text-left transition-all",
            isActive ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-muted/50 hover:text-foreground"
          )}
          onClick={onSelect}
          onDoubleClick={startEdit}
          onContextMenu={(e) => { e.preventDefault(); setShowMenu(!showMenu); }}
        >
          <MessageSquare className="h-4 w-4 shrink-0" />
          <span className="flex-1 truncate text-sm">{title}</span>
        </button>
      )}

      {/* Action buttons (visible on hover) */}
      {!isEditing && (
        <div className="absolute right-1 top-1/2 -translate-y-1/2 flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity bg-background/80 rounded-md px-0.5">
          <button type="button" onClick={(e) => { e.stopPropagation(); startEdit(); }} className="rounded p-1.5 hover:bg-muted" title={t("assistant.rename", "Rename")}>
            <Pencil className="h-3.5 w-3.5 text-muted-foreground" />
          </button>
          <button type="button" onClick={(e) => { e.stopPropagation(); onDelete(); }} className="rounded p-1.5 hover:bg-destructive/10 hover:text-destructive" title={t("assistant.delete", "Delete")}>
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {/* Context menu */}
      {showMenu && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setShowMenu(false)} />
          <div className="absolute left-8 top-full z-50 w-48 rounded-lg border bg-popover p-1 shadow-lg">
            <button className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted" onClick={startEdit}>
              <Pencil className="h-3.5 w-3.5" /> {t("assistant.rename", "Rename")}
            </button>
            <button className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted" onClick={() => { setShowMenu(false); const f = prompt(t("assistant.folderName", "Folder name:")); if (f) onMoveToFolder(f.trim()); }}>
              <FolderOpen className="h-3.5 w-3.5" /> {t("assistant.moveToFolder", "Move to folder")}
            </button>
            {getSessionFolder(session) && (
              <button className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted" onClick={() => { setShowMenu(false); onMoveToFolder(null); }}>
                <X className="h-3.5 w-3.5" /> {t("assistant.removeFromFolder", "Remove from folder")}
              </button>
            )}
            <hr className="my-1 border-border/50" />
            <button className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm text-destructive hover:bg-destructive/10" onClick={() => { setShowMenu(false); onDelete(); }}>
              <Trash2 className="h-3.5 w-3.5" /> {t("assistant.delete", "Delete")}
            </button>
          </div>
        </>
      )}
    </motion.div>
  );
}

// ── Section Group ────────────────────────────────────────────────────

function SectionGroup({
  label, icon, sessions, activeSessionId, onSelectSession, onDeleteSession, onRename, onMoveToFolder, fallbackLabel, collapsible = false,
}: {
  label: string;
  icon?: React.ReactNode;
  sessions: SessionSummary[];
  activeSessionId: string | null;
  onSelectSession: (id: string) => void;
  onDeleteSession: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onMoveToFolder: (id: string, folder: string | null) => void;
  fallbackLabel: string;
  collapsible?: boolean;
}) {
  const [collapsed, setCollapsed] = useState(false);
  if (sessions.length === 0) return null;

  return (
    <div className="mb-3">
      <button
        type="button"
        className="flex w-full items-center gap-1.5 px-3 py-1 text-xs font-medium text-muted-foreground/70 uppercase tracking-wider hover:text-muted-foreground"
        onClick={() => collapsible && setCollapsed(!collapsed)}
      >
        {icon}
        <span className="flex-1 text-left">{label}</span>
        {collapsible && <span className="text-[10px]">{collapsed ? "▸" : "▾"}</span>}
      </button>
      {!collapsed && (
        <div className="space-y-0.5 mt-0.5">
          {sessions.map((s) => (
            <SessionItem
              key={s.session_id}
              session={s}
              isActive={s.session_id === activeSessionId}
              onSelect={() => onSelectSession(s.session_id)}
              onDelete={() => onDeleteSession(s.session_id)}
              onRename={(title) => onRename(s.session_id, title)}
              onMoveToFolder={(folder) => onMoveToFolder(s.session_id, folder)}
              fallbackLabel={fallbackLabel}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Main Component ───────────────────────────────────────────────────

export function ConversationSidebar({
  sessions, activeSessionId, isLoading = false,
  onNewChat, onSelectSession, onDeleteSession, onSessionsChange,
}: ConversationSidebarProps) {
  const { t } = useTranslation();
  const [searchQuery, setSearchQuery] = useState("");
  const [showCustomize, setShowCustomize] = useState(false);
  const fallback = t("assistant.chatFallback", "Chat");

  const filtered = useMemo(() => {
    if (!Array.isArray(sessions)) return [];
    if (!searchQuery.trim()) return sessions;
    const q = searchQuery.toLowerCase();
    return sessions.filter((s) => {
      const title = getSessionTitle(s, fallback).toLowerCase();
      const folder = getSessionFolder(s)?.toLowerCase() || "";
      return title.includes(q) || folder.includes(q);
    });
  }, [sessions, searchQuery, fallback]);

  const grouped = useMemo(() => groupSessions(filtered), [filtered]);
  const folderNames = Object.keys(grouped.folders).sort();

  const hasNoSessions = !isLoading && filtered.length === 0;

  const handleRename = useCallback(async (sessionId: string, newTitle: string) => {
    try {
      await updateSession(sessionId, { metadata: { title: newTitle } });
      onSessionsChange?.(sessions.map((s) =>
        s.session_id === sessionId
          ? { ...s, metadata: { ...(s.metadata as Record<string, unknown> || {}), title: newTitle } }
          : s
      ));
    } catch (e) { console.error("Rename failed:", e); }
  }, [sessions, onSessionsChange]);

  const handleMoveToFolder = useCallback(async (sessionId: string, folder: string | null) => {
    try {
      const meta = folder ? { folder } : { folder: null };
      await updateSession(sessionId, { metadata: meta });
      onSessionsChange?.(sessions.map((s) =>
        s.session_id === sessionId
          ? { ...s, metadata: { ...(s.metadata as Record<string, unknown> || {}), folder: folder ?? undefined } }
          : s
      ));
    } catch (e) { console.error("Move failed:", e); }
  }, [sessions, onSessionsChange]);

  const commonProps = {
    activeSessionId, onSelectSession, onDeleteSession,
    onRename: handleRename, onMoveToFolder: handleMoveToFolder, fallbackLabel: fallback,
  };

  return (
    <div className="flex flex-col h-full">
      {/* New Chat */}
      <div className="p-3 border-b border-border/40">
        <Button onClick={onNewChat} className="w-full gap-2" variant="default">
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
            <button type="button" className="absolute right-2 top-1/2 -translate-y-1/2 p-0.5 rounded hover:bg-muted" onClick={() => setSearchQuery("")}>
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
            <p className="text-sm text-muted-foreground">{t("assistant.noHistory")}</p>
          </div>
        ) : (
          <>
            {/* Folders (Projects) */}
            {folderNames.length > 0 && (
              <div className="mb-2">
                <div className="px-3 py-1 text-[10px] font-semibold text-muted-foreground/50 uppercase tracking-widest">
                  {t("assistant.projects", "Projects")}
                </div>
                {folderNames.map((folder) => (
                  <SectionGroup
                    key={`folder-${folder}`}
                    label={folder}
                    icon={<FolderOpen className="h-3 w-3" />}
                    sessions={grouped.folders[folder]}
                    collapsible
                    {...commonProps}
                  />
                ))}
              </div>
            )}

            {/* Date groups */}
            <SectionGroup label={t("assistant.today")} sessions={grouped.today} {...commonProps} />
            <SectionGroup label={t("assistant.yesterday")} sessions={grouped.yesterday} {...commonProps} />
            <SectionGroup label={t("assistant.lastWeek")} sessions={grouped.lastWeek} {...commonProps} />
            <SectionGroup label={t("assistant.older")} sessions={grouped.older} {...commonProps} />
          </>
        )}
      </div>

      <CustomizeDialog open={showCustomize} onClose={() => setShowCustomize(false)} />
    </div>
  );
}
