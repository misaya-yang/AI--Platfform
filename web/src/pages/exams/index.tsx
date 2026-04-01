/**
 * Exam Management Page — list, create, publish, close exams.
 */

import { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  Plus,
  Search,
  ClipboardList,
  Users,
  Clock,
  BarChart3,
  ExternalLink,
  MoreHorizontal,
  Copy,
  Check,
  Loader2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Label } from "@/components/ui/label";
import {
  listExams,
  createExam,
  publishExam,
  closeExam,
  type Exam,
  type CreateExamRequest,
} from "@/api/exams";
import { listQuizzes } from "@/api/quiz";

const STATUS_CONFIG: Record<string, { label: string; variant: "default" | "secondary" | "destructive" | "outline" }> = {
  draft: { label: "Draft", variant: "secondary" },
  published: { label: "Published", variant: "default" },
  closed: { label: "Closed", variant: "outline" },
  archived: { label: "Archived", variant: "destructive" },
};

export function ExamsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [exams, setExams] = useState<Exam[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [search, setSearch] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const loadExams = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, unknown> = { limit: 50 };
      if (statusFilter !== "all") params.status = statusFilter;
      const data = await listExams(params as any);
      setExams(data.exams);
      setTotal(data.total);
    } catch {
      // silent
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => { loadExams(); }, [loadExams]);

  const filtered = search
    ? exams.filter((e) => e.title.toLowerCase().includes(search.toLowerCase()))
    : exams;

  const handlePublish = async (exam: Exam) => {
    try {
      await publishExam(exam.exam_id);
      loadExams();
    } catch (e: any) {
      alert(e?.response?.data?.detail || "Failed to publish");
    }
  };

  const handleClose = async (exam: Exam) => {
    if (!confirm("Close this exam? No more submissions will be accepted.")) return;
    try {
      await closeExam(exam.exam_id);
      loadExams();
    } catch (e: any) {
      alert(e?.response?.data?.detail || "Failed to close");
    }
  };

  const copyShareLink = (exam: Exam) => {
    if (!exam.share_code) return;
    const url = `${window.location.origin}/quiz/${exam.share_code}`;
    navigator.clipboard.writeText(url).then(() => {
      setCopiedId(exam.exam_id);
      setTimeout(() => setCopiedId(null), 2000);
    });
  };

  return (
    <ExamsContent
      exams={exams}
      total={total}
      loading={loading}
      filtered={filtered}
      statusFilter={statusFilter}
      setStatusFilter={setStatusFilter}
      search={search}
      setSearch={setSearch}
      showCreate={showCreate}
      setShowCreate={setShowCreate}
      copiedId={copiedId}
      navigate={navigate}
      handlePublish={handlePublish}
      handleClose={handleClose}
      copyShareLink={copyShareLink}
      loadExams={loadExams}
      showHeader
    />
  );
}

/**
 * Embeddable version for Services page tab (no outer wrapper/header).
 */
export function ExamsTabContent() {
  const navigate = useNavigate();
  const [exams, setExams] = useState<Exam[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [search, setSearch] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const loadExams = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, unknown> = { limit: 50 };
      if (statusFilter !== "all") params.status = statusFilter;
      const data = await listExams(params as any);
      setExams(data.exams);
      setTotal(data.total);
    } catch {
      // silent
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => { loadExams(); }, [loadExams]);

  const filtered = search
    ? exams.filter((e) => e.title.toLowerCase().includes(search.toLowerCase()))
    : exams;

  const handlePublish = async (exam: Exam) => {
    try {
      await publishExam(exam.exam_id);
      loadExams();
    } catch (e: any) {
      alert(e?.response?.data?.detail || "Failed to publish");
    }
  };

  const handleClose = async (exam: Exam) => {
    if (!confirm("Close this exam? No more submissions will be accepted.")) return;
    try {
      await closeExam(exam.exam_id);
      loadExams();
    } catch (e: any) {
      alert(e?.response?.data?.detail || "Failed to close");
    }
  };

  const copyShareLink = (exam: Exam) => {
    if (!exam.share_code) return;
    const url = `${window.location.origin}/quiz/${exam.share_code}`;
    navigator.clipboard.writeText(url).then(() => {
      setCopiedId(exam.exam_id);
      setTimeout(() => setCopiedId(null), 2000);
    });
  };

  return (
    <ExamsContent
      exams={exams}
      total={total}
      loading={loading}
      filtered={filtered}
      statusFilter={statusFilter}
      setStatusFilter={setStatusFilter}
      search={search}
      setSearch={setSearch}
      showCreate={showCreate}
      setShowCreate={setShowCreate}
      copiedId={copiedId}
      navigate={navigate}
      handlePublish={handlePublish}
      handleClose={handleClose}
      copyShareLink={copyShareLink}
      loadExams={loadExams}
      showHeader={false}
    />
  );
}

// ---------------------------------------------------------------------------
// Shared Content Component
// ---------------------------------------------------------------------------

function ExamsContent({
  exams, total, loading, filtered, statusFilter, setStatusFilter,
  search, setSearch, showCreate, setShowCreate, copiedId,
  navigate, handlePublish, handleClose, copyShareLink, loadExams,
  showHeader,
}: {
  exams: Exam[]; total: number; loading: boolean; filtered: Exam[];
  statusFilter: string; setStatusFilter: (v: string) => void;
  search: string; setSearch: (v: string) => void;
  showCreate: boolean; setShowCreate: (v: boolean) => void;
  copiedId: string | null;
  navigate: (path: string) => void;
  handlePublish: (e: Exam) => void;
  handleClose: (e: Exam) => void;
  copyShareLink: (e: Exam) => void;
  loadExams: () => void;
  showHeader: boolean;
}) {
  return (
    <div className={showHeader ? "space-y-6 p-6 max-w-6xl mx-auto" : "space-y-4"}>
      {showHeader && (
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Exam Management</h1>
            <p className="text-muted-foreground text-sm mt-1">
              Create, publish, and track exam results
            </p>
          </div>
          <Button onClick={() => setShowCreate(true)} className="gap-2">
            <Plus className="h-4 w-4" />
            Create Exam
          </Button>
        </div>
      )}

      {/* Create button for tab mode */}
      {!showHeader && (
        <div className="flex justify-end">
          <Button onClick={() => setShowCreate(true)} className="gap-2" size="sm">
            <Plus className="h-4 w-4" />
            Create Exam
          </Button>
        </div>
      )}

      {/* Filters */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1 max-w-sm">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search exams..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9"
          />
        </div>
        <div className="flex gap-1">
          {["all", "draft", "published", "closed"].map((s) => (
            <Button
              key={s}
              variant={statusFilter === s ? "default" : "ghost"}
              size="sm"
              onClick={() => setStatusFilter(s)}
              className="capitalize"
            >
              {s === "all" ? "All" : STATUS_CONFIG[s]?.label || s}
            </Button>
          ))}
        </div>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-4 gap-4">
        {[
          { label: "Total", value: total, icon: ClipboardList },
          { label: "Published", value: exams.filter((e) => e.status === "published").length, icon: ExternalLink },
          { label: "Participants", value: exams.reduce((s, e) => s + (e.attempt_count || 0), 0), icon: Users },
          { label: "Avg Score", value: (() => {
            const scored = exams.filter((e) => e.avg_score != null);
            if (!scored.length) return "—";
            return `${Math.round((scored.reduce((s, e) => s + (e.avg_score || 0), 0) / scored.length) * 100)}%`;
          })(), icon: BarChart3 },
        ].map(({ label, value, icon: Icon }) => (
          <div key={label} className="rounded-xl border bg-card p-4 flex items-center gap-3">
            <div className="h-10 w-10 rounded-lg bg-primary/10 flex items-center justify-center">
              <Icon className="h-5 w-5 text-primary" />
            </div>
            <div>
              <p className="text-2xl font-semibold">{value}</p>
              <p className="text-xs text-muted-foreground">{label}</p>
            </div>
          </div>
        ))}
      </div>

      {/* Exam list */}
      {loading ? (
        <div className="flex items-center justify-center py-12 text-muted-foreground">
          <Loader2 className="h-5 w-5 animate-spin mr-2" /> Loading...
        </div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-16 text-muted-foreground">
          <ClipboardList className="h-12 w-12 mx-auto mb-3 opacity-30" />
          <p>No exams found</p>
          <Button variant="outline" className="mt-4" onClick={() => setShowCreate(true)}>
            Create your first exam
          </Button>
        </div>
      ) : (
        <div className="space-y-3">
          {filtered.map((exam) => {
            const cfg = STATUS_CONFIG[exam.status] || STATUS_CONFIG.draft;
            return (
              <div
                key={exam.exam_id}
                className="rounded-xl border bg-card hover:bg-accent/30 transition-colors cursor-pointer p-4"
                onClick={() => navigate(`/exams/${exam.exam_id}`)}
              >
                <div className="flex items-start justify-between">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <h3 className="font-medium truncate">{exam.title}</h3>
                      <Badge variant={cfg.variant}>{cfg.label}</Badge>
                    </div>
                    {exam.description && (
                      <p className="text-sm text-muted-foreground mt-1 line-clamp-1">{exam.description}</p>
                    )}
                    <div className="flex items-center gap-4 mt-2 text-xs text-muted-foreground">
                      <span className="flex items-center gap-1">
                        <ClipboardList className="h-3 w-3" /> {exam.question_count} questions
                      </span>
                      <span className="flex items-center gap-1">
                        <Users className="h-3 w-3" /> {exam.attempt_count} participants
                      </span>
                      {exam.avg_score != null && (
                        <span className="flex items-center gap-1">
                          <BarChart3 className="h-3 w-3" /> {Math.round(exam.avg_score * 100)}% avg
                        </span>
                      )}
                      {exam.deadline && (
                        <span className="flex items-center gap-1">
                          <Clock className="h-3 w-3" /> Due {new Date(exam.deadline).toLocaleDateString()}
                        </span>
                      )}
                    </div>
                  </div>

                  {/* Actions */}
                  <div className="flex items-center gap-2 ml-4" onClick={(e) => e.stopPropagation()}>
                    {exam.status === "draft" && (
                      <Button size="sm" onClick={() => handlePublish(exam)}>Publish</Button>
                    )}
                    {exam.status === "published" && exam.share_code && (
                      <Button
                        size="sm"
                        variant="outline"
                        className="gap-1"
                        onClick={() => copyShareLink(exam)}
                      >
                        {copiedId === exam.exam_id ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                        {copiedId === exam.exam_id ? "Copied" : "Link"}
                      </Button>
                    )}
                    {exam.status === "published" && (
                      <Button size="sm" variant="outline" onClick={() => handleClose(exam)}>Close</Button>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Create Exam Dialog */}
      <CreateExamDialog
        open={showCreate}
        onClose={() => setShowCreate(false)}
        onCreated={() => { setShowCreate(false); loadExams(); }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Create Exam Dialog
// ---------------------------------------------------------------------------

function CreateExamDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [quizzes, setQuizzes] = useState<Array<{ quiz_id: string; title: string; question_count: number }>>([]);
  const [selectedQuiz, setSelectedQuiz] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [passingScore, setPassingScore] = useState("60");
  const [maxRetakes, setMaxRetakes] = useState("1");
  const [timeLimit, setTimeLimit] = useState("");
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    if (open) {
      listQuizzes({ limit: 100 }).then((data) => setQuizzes(data.quizzes as any));
    }
  }, [open]);

  useEffect(() => {
    if (selectedQuiz && !title) {
      const q = quizzes.find((q) => q.quiz_id === selectedQuiz);
      if (q) setTitle(q.title);
    }
  }, [selectedQuiz]);

  const handleCreate = async () => {
    if (!selectedQuiz || !title) return;
    setCreating(true);
    try {
      const req: CreateExamRequest = {
        quiz_id: selectedQuiz,
        title,
        description: description || undefined,
        passing_score: parseInt(passingScore) / 100,
        max_retakes: parseInt(maxRetakes) || 1,
        time_limit_minutes: timeLimit ? parseInt(timeLimit) : undefined,
      };
      await createExam(req);
      onCreated();
      // Reset
      setSelectedQuiz("");
      setTitle("");
      setDescription("");
    } catch (e: any) {
      alert(e?.response?.data?.detail || "Failed to create exam");
    } finally {
      setCreating(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Create Exam</DialogTitle>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div>
            <Label>Source Quiz</Label>
            <Select value={selectedQuiz} onValueChange={setSelectedQuiz}>
              <SelectTrigger className="mt-1">
                <SelectValue placeholder="Select a quiz..." />
              </SelectTrigger>
              <SelectContent>
                {quizzes.map((q) => (
                  <SelectItem key={q.quiz_id} value={q.quiz_id}>
                    {q.title} ({q.question_count} Q)
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div>
            <Label>Exam Title</Label>
            <Input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Enter exam title"
              className="mt-1"
            />
          </div>

          <div>
            <Label>Description (optional)</Label>
            <Input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Exam description"
              className="mt-1"
            />
          </div>

          <div className="grid grid-cols-3 gap-3">
            <div>
              <Label>Passing Score %</Label>
              <Input
                type="number"
                min={0}
                max={100}
                value={passingScore}
                onChange={(e) => setPassingScore(e.target.value)}
                className="mt-1"
              />
            </div>
            <div>
              <Label>Max Retakes</Label>
              <Input
                type="number"
                min={1}
                max={10}
                value={maxRetakes}
                onChange={(e) => setMaxRetakes(e.target.value)}
                className="mt-1"
              />
            </div>
            <div>
              <Label>Time Limit (min)</Label>
              <Input
                type="number"
                min={0}
                value={timeLimit}
                onChange={(e) => setTimeLimit(e.target.value)}
                placeholder="No limit"
                className="mt-1"
              />
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button onClick={handleCreate} disabled={!selectedQuiz || !title || creating}>
            {creating && <Loader2 className="h-4 w-4 animate-spin mr-2" />}
            Create as Draft
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
