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
  DialogDescription,
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
import type { QuizData } from "@/pages/assistant/types";

const STATUS_CONFIG: Record<string, { label: string; variant: "default" | "secondary" | "destructive" | "outline-solid" }> = {
  draft: { label: "Draft", variant: "secondary" },
  published: { label: "Published", variant: "default" },
  closed: { label: "Closed", variant: "outline" },
  archived: { label: "Archived", variant: "destructive" },
};

type ExamListParams = Parameters<typeof listExams>[0];
type QuizOption = Pick<QuizData, "quiz_id" | "title" | "question_count">;

function apiErrorMessage(error: unknown, fallback: string): string {
  if (error && typeof error === "object") {
    const maybe = error as {
      response?: { data?: { detail?: unknown } };
      message?: unknown;
    };
    const detail = maybe.response?.data?.detail;
    if (typeof detail === "string" && detail) return detail;
    if (typeof maybe.message === "string" && maybe.message) return maybe.message;
  }
  return fallback;
}

export function ExamsPage() {
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
      const params: ExamListParams = { limit: 50 };
      if (statusFilter !== "all") params.status = statusFilter;
      const data = await listExams(params);
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
    } catch (e: unknown) {
      alert(apiErrorMessage(e, "Failed to publish"));
    }
  };

  const handleClose = async (exam: Exam) => {
    if (!confirm("Close this exam? No more submissions will be accepted.")) return;
    try {
      await closeExam(exam.exam_id);
      loadExams();
    } catch (e: unknown) {
      alert(apiErrorMessage(e, "Failed to close"));
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
      const params: ExamListParams = { limit: 50 };
      if (statusFilter !== "all") params.status = statusFilter;
      const data = await listExams(params);
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
    } catch (e: unknown) {
      alert(apiErrorMessage(e, "Failed to publish"));
    }
  };

  const handleClose = async (exam: Exam) => {
    if (!confirm("Close this exam? No more submissions will be accepted.")) return;
    try {
      await closeExam(exam.exam_id);
      loadExams();
    } catch (e: unknown) {
      alert(apiErrorMessage(e, "Failed to close"));
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
  const { t } = useTranslation();
  const statusLabels: Record<string, string> = {
    all: t("exams.filterAll"),
    draft: t("exams.filterDraft"),
    published: t("exams.filterPublished"),
    closed: t("exams.filterClosed"),
  };

  return (
    <div className={showHeader ? "space-y-6 p-6 max-w-6xl mx-auto" : "space-y-4"}>
      {showHeader && (
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">{t("exams.title")}</h1>
            <p className="text-muted-foreground text-sm mt-1">{t("exams.subtitle")}</p>
          </div>
          <Button onClick={() => setShowCreate(true)} className="gap-2">
            <Plus className="h-4 w-4" />
            {t("exams.createExam")}
          </Button>
        </div>
      )}

      {!showHeader && (
        <div className="flex justify-end">
          <Button onClick={() => setShowCreate(true)} className="gap-2" size="sm">
            <Plus className="h-4 w-4" />
            {t("exams.createExam")}
          </Button>
        </div>
      )}

      {/* Filters */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1 max-w-sm">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder={t("exams.searchPlaceholder")}
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
            >
              {statusLabels[s] || s}
            </Button>
          ))}
        </div>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-4 gap-4">
        {[
          { label: t("exams.statsTotal"), value: total, icon: ClipboardList },
          { label: t("exams.statsPublished"), value: exams.filter((e) => e.status === "published").length, icon: ExternalLink },
          { label: t("exams.statsParticipants"), value: exams.reduce((s, e) => s + (e.attempt_count || 0), 0), icon: Users },
          { label: t("exams.statsAvgScore"), value: (() => {
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
          <Loader2 className="h-5 w-5 animate-spin mr-2" /> {t("common.loading", "加载中...")}
        </div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-16 text-muted-foreground">
          <ClipboardList className="h-12 w-12 mx-auto mb-3 opacity-30" />
          <p>{t("exams.noExams")}</p>
          <Button variant="outline" className="mt-4" onClick={() => setShowCreate(true)}>
            {t("exams.createFirst")}
          </Button>
        </div>
      ) : (
        <div className="space-y-3">
          {filtered.map((exam) => (
            <div
              key={exam.exam_id}
              className="rounded-xl border bg-card hover:bg-accent/30 transition-colors cursor-pointer p-4"
              onClick={() => navigate(`/exams/${exam.exam_id}`)}
            >
              <div className="flex items-start justify-between">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <h3 className="font-medium truncate">{exam.title}</h3>
                    <Badge variant={(STATUS_CONFIG[exam.status] || STATUS_CONFIG.draft).variant}>
                      {statusLabels[exam.status] || exam.status}
                    </Badge>
                  </div>
                  {exam.description && (
                    <p className="text-sm text-muted-foreground mt-1 line-clamp-1">{exam.description}</p>
                  )}
                  <div className="flex items-center gap-4 mt-2 text-xs text-muted-foreground">
                    <span className="flex items-center gap-1">
                      <ClipboardList className="h-3 w-3" /> {t("exams.questions", { count: exam.question_count })}
                    </span>
                    <span className="flex items-center gap-1">
                      <Users className="h-3 w-3" /> {t("exams.participants", { count: exam.attempt_count })}
                    </span>
                    {exam.avg_score != null && (
                      <span className="flex items-center gap-1">
                        <BarChart3 className="h-3 w-3" /> {t("exams.avg", { score: `${Math.round(exam.avg_score * 100)}%` })}
                      </span>
                    )}
                    {exam.deadline && (
                      <span className="flex items-center gap-1">
                        <Clock className="h-3 w-3" /> {t("exams.due", { date: new Date(exam.deadline).toLocaleDateString() })}
                      </span>
                    )}
                  </div>
                </div>

                <div className="flex items-center gap-2 ml-4" onClick={(e) => e.stopPropagation()}>
                  {exam.status === "draft" && (
                    <Button size="sm" onClick={() => handlePublish(exam)}>{t("exams.publish")}</Button>
                  )}
                  {exam.status === "published" && exam.share_code && (
                    <Button size="sm" variant="outline" className="gap-1" onClick={() => copyShareLink(exam)}>
                      {copiedId === exam.exam_id ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                      {copiedId === exam.exam_id ? t("exams.copied") : t("exams.link")}
                    </Button>
                  )}
                  {exam.status === "published" && (
                    <Button size="sm" variant="outline" onClick={() => handleClose(exam)}>{t("exams.close")}</Button>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

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
  const [quizzes, setQuizzes] = useState<QuizOption[]>([]);
  const [selectedQuiz, setSelectedQuiz] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [passingScore, setPassingScore] = useState("60");
  const [maxRetakes, setMaxRetakes] = useState("1");
  const [timeLimit, setTimeLimit] = useState("");
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    if (open) {
      listQuizzes({ limit: 100 }).then((data) => setQuizzes(data.quizzes));
    }
  }, [open]);

  useEffect(() => {
    if (!selectedQuiz || title) return;
    const q = quizzes.find((quiz) => quiz.quiz_id === selectedQuiz);
    if (q) setTitle(q.title);
  }, [selectedQuiz, title, quizzes]);

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
    } catch (e: unknown) {
      alert(apiErrorMessage(e, "Failed to create exam"));
    } finally {
      setCreating(false);
    }
  };

  const { t } = useTranslation();

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("exams.create.title")}</DialogTitle>
          <DialogDescription className="sr-only">
            {t(
              "exams.create.descriptionText",
              "Create an exam draft from an existing quiz and configure score, retake, and time-limit settings."
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div>
            <Label>{t("exams.create.sourceQuiz")}</Label>
            <Select value={selectedQuiz} onValueChange={setSelectedQuiz}>
              <SelectTrigger className="mt-1">
                <SelectValue placeholder={t("exams.create.selectQuiz")} />
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
            <Label>{t("exams.create.examTitle")}</Label>
            <Input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder={t("exams.create.titlePlaceholder")}
              className="mt-1"
            />
          </div>

          <div>
            <Label>{t("exams.create.description")}</Label>
            <Input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t("exams.create.descPlaceholder")}
              className="mt-1"
            />
          </div>

          <div className="grid grid-cols-3 gap-3">
            <div>
              <Label>{t("exams.create.passingScore")}</Label>
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
              <Label>{t("exams.create.maxRetakes")}</Label>
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
              <Label>{t("exams.create.timeLimit")}</Label>
              <Input
                type="number"
                min={0}
                value={timeLimit}
                onChange={(e) => setTimeLimit(e.target.value)}
                placeholder={t("exams.create.noLimit")}
                className="mt-1"
              />
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t("exams.create.cancel")}</Button>
          <Button onClick={handleCreate} disabled={!selectedQuiz || !title || creating}>
            {creating && <Loader2 className="h-4 w-4 animate-spin mr-2" />}
            {t("exams.create.createDraft")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
