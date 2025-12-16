import type { Task } from "@/types/gateway";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export function TaskTable({
  tasks,
  onSelect,
}: {
  tasks: Task[];
  onSelect?: (task: Task) => void;
}) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>ID</TableHead>
          <TableHead>Service</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Created</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {tasks.map((t) => (
          <TableRow
            key={t.task_id}
            className="cursor-pointer"
            onClick={() => onSelect?.(t)}
          >
            <TableCell className="truncate max-w-[220px]">
              {t.task_id}
            </TableCell>
            <TableCell>{t.service_id}</TableCell>
            <TableCell>{t.status}</TableCell>
            <TableCell>{new Date(t.created_at).toLocaleString()}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

