/**
 * Task Panel Component
 *
 * Displays task progress during agentic workflows:
 * - Progress bar showing completed/total tasks
 * - Current goal display
 * - Task list with status icons
 * - Collapsible collected information section
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { motion, AnimatePresence } from "framer-motion";
import {
  Circle,
  Loader2,
  CheckCircle2,
  AlertCircle,
  ChevronDown,
  Target,
  ListTodo,
  Database,
} from "lucide-react";
import { cn } from "@/lib/utils";

// =============================================================================
// Types
// =============================================================================

export type TaskStatus = "pending" | "in_progress" | "completed" | "failed";

export interface Task {
  id: string;
  description: string;
  status: TaskStatus;
  result?: string;
  error?: string;
}

export interface CollectedInfo {
  key: string;
  value: string;
  source: string;
}

export interface TaskPanelProps {
  goal?: string;
  tasks: Task[];
  collectedInfo: CollectedInfo[];
  isVisible: boolean;
}

// =============================================================================
// Status Icon Component
// =============================================================================

function StatusIcon({ status }: { status: TaskStatus }) {
  switch (status) {
    case "pending":
      return <Circle className="h-4 w-4 text-slate-400 dark:text-slate-500" />;
    case "in_progress":
      return <Loader2 className="h-4 w-4 text-blue-500 animate-spin" />;
    case "completed":
      return <CheckCircle2 className="h-4 w-4 text-emerald-500" />;
    case "failed":
      return <AlertCircle className="h-4 w-4 text-red-500" />;
    default:
      return <Circle className="h-4 w-4 text-slate-400 dark:text-slate-500" />;
  }
}

// =============================================================================
// Progress Bar Component
// =============================================================================

function ProgressBar({ completed, total }: { completed: number; total: number }) {
  const percentage = total > 0 ? Math.round((completed / total) * 100) : 0;

  return (
    <div className="w-full">
      <div className="flex items-center justify-between mb-1.5 text-xs">
        <span className="font-medium text-slate-600 dark:text-slate-400">
          {completed} / {total} tasks
        </span>
        <span className="text-slate-500 dark:text-slate-500">{percentage}%</span>
      </div>
      <div className="h-2 w-full bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden">
        <motion.div
          className="h-full bg-gradient-to-r from-blue-500 to-emerald-500 rounded-full"
          initial={{ width: 0 }}
          animate={{ width: `${percentage}%` }}
          transition={{ duration: 0.3, ease: "easeOut" }}
        />
      </div>
    </div>
  );
}

// =============================================================================
// Task Item Component
// =============================================================================

function TaskItem({ task }: { task: Task }) {
  const getStatusClasses = () => {
    switch (task.status) {
      case "in_progress":
        return "bg-blue-50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800/40";
      case "completed":
        return "bg-emerald-50 dark:bg-emerald-900/20 border-emerald-200 dark:border-emerald-800/40";
      case "failed":
        return "bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800/40";
      default:
        return "bg-slate-50 dark:bg-slate-800/40 border-slate-200 dark:border-slate-700/40";
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0, x: -10 }}
      animate={{ opacity: 1, x: 0 }}
      className={cn(
        "flex items-start gap-3 p-3 rounded-xl border transition-colors",
        getStatusClasses()
      )}
    >
      <div className="mt-0.5 flex-shrink-0">
        <StatusIcon status={task.status} />
      </div>
      <div className="flex-1 min-w-0">
        <p
          className={cn(
            "text-sm leading-relaxed",
            task.status === "completed" && "text-slate-500 dark:text-slate-400",
            task.status === "failed" && "text-red-700 dark:text-red-300"
          )}
        >
          {task.description}
        </p>
        {task.result && task.status === "completed" && (
          <p className="mt-1 text-xs text-emerald-600 dark:text-emerald-400 line-clamp-2">
            {task.result}
          </p>
        )}
        {task.error && task.status === "failed" && (
          <p className="mt-1 text-xs text-red-600 dark:text-red-400 line-clamp-2">
            {task.error}
          </p>
        )}
      </div>
    </motion.div>
  );
}

// =============================================================================
// Collected Info Section Component
// =============================================================================

function CollectedInfoSection({ collectedInfo }: { collectedInfo: CollectedInfo[] }) {
  const [isOpen, setIsOpen] = useState(false);
  const { t } = useTranslation();

  if (collectedInfo.length === 0) return null;

  return (
    <div className="mt-4">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-2 text-xs font-medium text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200 transition-colors group"
      >
        <div className="flex items-center justify-center w-6 h-6 rounded-lg bg-gradient-to-br from-violet-500/20 to-purple-500/20 group-hover:from-violet-500/30 group-hover:to-purple-500/30 transition-colors">
          <Database className="h-3.5 w-3.5 text-violet-600 dark:text-violet-400" />
        </div>
        <span>
          {collectedInfo.length}{" "}
          {t("assistant.collectedInfo", "collected information")}
        </span>
        <motion.div
          animate={{ rotate: isOpen ? 180 : 0 }}
          transition={{ duration: 0.2 }}
        >
          <ChevronDown className="h-3.5 w-3.5" />
        </motion.div>
      </button>

      <AnimatePresence>
        {isOpen && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2, ease: "easeInOut" }}
            className="overflow-hidden"
          >
            <div className="mt-3 space-y-2 p-3 rounded-2xl bg-gradient-to-br from-violet-50/80 to-purple-50/80 dark:from-violet-950/30 dark:to-purple-950/30 border border-violet-200/50 dark:border-violet-800/30">
              {collectedInfo.map((info, idx) => (
                <motion.div
                  key={`${info.key}-${idx}`}
                  initial={{ opacity: 0, x: -10 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: idx * 0.05 }}
                  className="text-xs p-3 rounded-xl bg-white/80 dark:bg-slate-900/50 border border-slate-200/60 dark:border-slate-700/40"
                >
                  <div className="flex items-center justify-between mb-1">
                    <span className="font-medium text-slate-700 dark:text-slate-300">
                      {info.key}
                    </span>
                    <span className="text-slate-400 dark:text-slate-500 text-[10px]">
                      {info.source}
                    </span>
                  </div>
                  <p className="text-slate-600 dark:text-slate-400 line-clamp-2">
                    {info.value}
                  </p>
                </motion.div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// =============================================================================
// Main TaskPanel Component
// =============================================================================

export function TaskPanel({ goal, tasks, collectedInfo, isVisible }: TaskPanelProps) {
  const { t } = useTranslation();

  // Don't render if not visible or no tasks
  // Defensive check for tasks array
  const safeTasks = Array.isArray(tasks) ? tasks : [];
  if (!isVisible || safeTasks.length === 0) {
    return null;
  }

  const completedTasks = safeTasks.filter((t) => t.status === "completed").length;
  const failedTasks = safeTasks.filter((t) => t.status === "failed").length;
  const totalTasks = safeTasks.length;

  return (
    <motion.div
      initial={{ opacity: 0, y: -20 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -20 }}
      className="mb-4 p-4 rounded-2xl bg-gradient-to-br from-slate-50 to-slate-100/50 dark:from-slate-900/80 dark:to-slate-800/50 border border-slate-200/60 dark:border-slate-700/40 backdrop-blur-sm"
    >
      {/* Header with Goal */}
      {goal && (
        <div className="flex items-start gap-3 mb-4">
          <div className="flex items-center justify-center w-8 h-8 rounded-xl bg-gradient-to-br from-blue-500/20 to-indigo-500/20">
            <Target className="h-4 w-4 text-blue-600 dark:text-blue-400" />
          </div>
          <div className="flex-1 min-w-0">
            <h4 className="text-xs font-medium text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-1">
              {t("assistant.taskGoal", "Goal")}
            </h4>
            <p className="text-sm font-medium text-slate-700 dark:text-slate-200 leading-relaxed">
              {goal}
            </p>
          </div>
        </div>
      )}

      {/* Progress Bar */}
      <div className="mb-4">
        <ProgressBar completed={completedTasks + failedTasks} total={totalTasks} />
      </div>

      {/* Task List */}
      <div className="space-y-2">
        <div className="flex items-center gap-2 text-xs font-medium text-slate-600 dark:text-slate-400 mb-2">
          <ListTodo className="h-3.5 w-3.5" />
          <span>{t("assistant.taskList", "Tasks")}</span>
        </div>
        <AnimatePresence mode="popLayout">
          {Array.from(new Map(safeTasks.map(task => [task.id, task])).values()).map((task, index) => (
            <TaskItem key={task.id || `task-${index}`} task={task} />
          ))}
        </AnimatePresence>
      </div>

      {/* Collected Information */}
      <CollectedInfoSection collectedInfo={collectedInfo} />
    </motion.div>
  );
}

export default TaskPanel;
