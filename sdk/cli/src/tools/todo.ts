/**
 * todo_write — Local task tracking tool (P2.5).
 * Allows the AI to create and manage a task list for complex multi-step work.
 */

interface TodoItem {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed";
  priority?: "high" | "medium" | "low";
}

interface TodoWriteArgs {
  todos: TodoItem[];
}

// In-memory todo store (persists for session lifetime)
let currentTodos: TodoItem[] = [];

export function todoWrite(args: TodoWriteArgs): string {
  const { todos } = args;
  if (!todos || !Array.isArray(todos)) {
    return "Error: todos must be an array of {id, content, status} objects.";
  }

  // Merge: update existing items by id, add new ones
  for (const item of todos) {
    if (!item.id || !item.content) continue;
    const existing = currentTodos.find((t) => t.id === item.id);
    if (existing) {
      existing.content = item.content;
      existing.status = item.status || existing.status;
      existing.priority = item.priority || existing.priority;
    } else {
      currentTodos.push({
        id: item.id,
        content: item.content,
        status: item.status || "pending",
        priority: item.priority || "medium",
      });
    }
  }

  // Render status
  const statusIcons: Record<string, string> = {
    pending: "○",
    in_progress: "◐",
    completed: "●",
  };

  const lines = currentTodos.map((t) => {
    const icon = statusIcons[t.status] || "○";
    return `${icon} [${t.status}] ${t.content}`;
  });

  const completed = currentTodos.filter((t) => t.status === "completed").length;
  const total = currentTodos.length;

  return `Tasks updated (${completed}/${total} done):\n${lines.join("\n")}`;
}

export function getTodos(): TodoItem[] {
  return [...currentTodos];
}

export function clearTodos(): void {
  currentTodos = [];
}

export const todoDefinition = {
  name: "todo_write",
  description:
    "Create and manage a task list to track progress on complex multi-step tasks. " +
    "Each todo has an id, content, status (pending/in_progress/completed), and optional priority.",
  parameters: {
    todos: {
      type: "array",
      description:
        "Array of todo items. Each item: {id: string, content: string, status: 'pending'|'in_progress'|'completed', priority?: 'high'|'medium'|'low'}",
      required: true,
    },
  },
  category: "os" as const,
};
