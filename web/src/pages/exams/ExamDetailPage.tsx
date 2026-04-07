/**
 * Exam Detail Page — view attempts, stats, question analysis, AI reports.
 */

import { useState, useEffect, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  ArrowLeft,
  Users,
  BarChart3,
  Brain,
  Copy,
  Check,
  Download,
  Loader2,
  Trophy,
  AlertCircle,
  ClipboardList,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  getExam,
  listExamAttempts,
  getExamStats,
  analyzeExam,
  listExamReports,
  getExamReport,
  getExamExportUrl,
  publishExam,
  closeExam,
  type Exam,
  type ExamAttempt,
  type ExamStats,
  type ExamReport,
} from "@/api/exams";
import { StreamOutput } from "@/components/StreamOutput";

type Tab = "participants" | "questions" | "analysis";

export function ExamDetailPage() {
  const { t } = useTranslation();
  const { examId } = useParams<{ examId: string }>();
  const navigate = useNavigate();

  const [exam, setExam] = useState<Exam | null>(null);
  const [attempts, setAttempts] = useState<ExamAttempt[]>([]);
  const [attTotal, setAttTotal] = useState(0);
  const [stats, setStats] = useState<ExamStats | null>(null);
  const [tab, setTab] = useState<Tab>("participants");
  const [loading, setLoading] = useState(true);
  const [copied, setCopied] = useState(false);

  // AI Analysis
  const [analyzing, setAnalyzing] = useState(false);
  const [reports, setReports] = useState<Array<{ report_id: string; generated_at: string }>>([]);
  const [activeReport, setActiveReport] = useState<ExamReport | null>(null);

  const load = useCallback(async () => {
    if (!examId) return;
    setLoading(true);
    try {
      const [e, att, st] = await Promise.all([
        getExam(examId),
        listExamAttempts(examId, { limit: 200 }),
        getExamStats(examId).catch(() => null),
      ]);
      setExam(e);
      setAttempts(att.attempts);
      setAttTotal(att.total);
      setStats(st);

      const reps = await listExamReports(examId).catch(() => ({ reports: [] }));
      setReports(reps.reports);
      if (reps.reports.length > 0 && !activeReport) {
        const latest = await getExamReport(examId, reps.reports[0].report_id).catch(() => null);
        if (latest) setActiveReport(latest);
      }
    } catch {
      // silent
    } finally {
      setLoading(false);
    }
  }, [examId]);

  useEffect(() => { load(); }, [load]);

  const handlePublish = async () => {
    if (!examId) return;
    try {
      await publishExam(examId);
      load();
    } catch (e: any) {
      alert(e?.response?.data?.detail || "Failed");
    }
  };

  const handleClose = async () => {
    if (!examId || !confirm("Close this exam?")) return;
    try {
      await closeExam(examId);
      load();
    } catch (e: any) {
      alert(e?.response?.data?.detail || "Failed");
    }
  };

  const handleAnalyze = async () => {
    if (!examId) return;
    setAnalyzing(true);
    try {
      const result = await analyzeExam(examId);
      setActiveReport({ report_id: result.report_id, content: result.content, model_id: "qwen3.6-plus", generated_at: new Date().toISOString() });
      load(); // refresh reports list
    } catch (e: any) {
      alert(e?.response?.data?.detail || "Analysis failed");
    } finally {
      setAnalyzing(false);
    }
  };

  const copyLink = () => {
    if (!exam?.share_code) return;
    navigator.clipboard.writeText(`${window.location.origin}/quiz/${exam.share_code}`);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleExport = () => {
    if (!examId) return;
    const token = (() => {
      try {
        const raw = localStorage.getItem("agent-gateway-auth") || sessionStorage.getItem("agent-gateway-auth");
        const obj = raw ? JSON.parse(raw) : null;
        return obj?.state?.token || obj?.token || "";
      } catch { return ""; }
    })();
    window.open(`${getExamExportUrl(examId)}?token=${token}`, "_blank");
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin mr-2" /> Loading...
      </div>
    );
  }

  if (!exam) {
    return (
      <div className="text-center py-16 text-muted-foreground">
        Exam not found
        <br />
        <Button variant="outline" className="mt-4" onClick={() => navigate("/exams")}>Back</Button>
      </div>
    );
  }

  const statusColor = exam.status === "published" ? "default" : exam.status === "closed" ? "outline" : "secondary";

  return (
    <div className="space-y-6 p-6 max-w-6xl mx-auto">
      {/* Back + Header */}
      <div>
        <Button variant="ghost" size="sm" onClick={() => navigate("/exams")} className="gap-1 mb-2 -ml-2">
          <ArrowLeft className="h-4 w-4" /> {t("exams.back")}
        </Button>
        <div className="flex items-start justify-between">
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-semibold">{exam.title}</h1>
              <Badge variant={statusColor as any}>{exam.status}</Badge>
            </div>
            {exam.description && <p className="text-muted-foreground mt-1">{exam.description}</p>}
            <div className="flex items-center gap-4 mt-2 text-sm text-muted-foreground">
              <span>{t("exams.questions", { count: exam.question_count })}</span>
              <span>{t("exams.pass", { score: Math.round((exam.passing_score || 0.6) * 100) })}</span>
              {exam.time_limit_minutes && <span>{t("exams.timeLimit", { min: exam.time_limit_minutes })}</span>}
              {exam.deadline && <span>Due: {new Date(exam.deadline).toLocaleString()}</span>}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {exam.status === "draft" && (
              <Button onClick={handlePublish}>{t("exams.publish")}</Button>
            )}
            {exam.status === "published" && (
              <>
                <Button variant="outline" className="gap-1" onClick={copyLink}>
                  {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                  {copied ? t("exams.copied") : t("exams.link")}
                </Button>
                <Button variant="outline" onClick={handleClose}>{t("exams.close")}</Button>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Stats cards */}
      {stats && (
        <div className="grid grid-cols-4 gap-4">
          <StatCard icon={Users} label={t("exams.statsParticipants")} value={stats.total_participants} />
          <StatCard
            icon={BarChart3}
            label={t("exams.statsAvgScore")}
            value={stats.avg_score != null ? `${Math.round(stats.avg_score * 100)}%` : "—"}
          />
          <StatCard icon={Trophy} label={t("exams.detail.passRate")} value={`${Math.round(stats.pass_rate * 100)}%`} />
          <StatCard
            icon={AlertCircle}
            label={t("exams.detail.scoreRange")}
            value={stats.min_score != null ? `${Math.round(stats.min_score * 100)}–${Math.round((stats.max_score || 0) * 100)}%` : "—"}
          />
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-1 border-b">
        {([
          { key: "participants", label: t("exams.detail.participants"), icon: Users },
          { key: "questions", label: t("exams.detail.questionAnalysis"), icon: ClipboardList },
          { key: "analysis", label: t("exams.detail.aiAnalysis"), icon: Brain },
        ] as const).map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
              tab === key
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <Icon className="h-4 w-4" /> {label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      {tab === "participants" && (
        <ParticipantsTab
          attempts={attempts}
          total={attTotal}
          passingScore={exam.passing_score || 0.6}
          onExport={handleExport}
        />
      )}
      {tab === "questions" && stats && <QuestionsTab stats={stats} />}
      {tab === "analysis" && (
        <AnalysisTab
          report={activeReport}
          reports={reports}
          analyzing={analyzing}
          onAnalyze={handleAnalyze}
          onSelectReport={async (rid) => {
            if (!examId) return;
            const r = await getExamReport(examId, rid);
            if (r) setActiveReport(r);
          }}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function StatCard({ icon: Icon, label, value }: { icon: any; label: string; value: string | number }) {
  return (
    <div className="rounded-xl border bg-card p-4 flex items-center gap-3">
      <div className="h-10 w-10 rounded-lg bg-primary/10 flex items-center justify-center">
        <Icon className="h-5 w-5 text-primary" />
      </div>
      <div>
        <p className="text-xl font-semibold">{value}</p>
        <p className="text-xs text-muted-foreground">{label}</p>
      </div>
    </div>
  );
}

function ParticipantsTab({
  attempts,
  total,
  passingScore,
  onExport,
}: {
  attempts: ExamAttempt[];
  total: number;
  passingScore: number;
  onExport: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">{t("exams.detail.participantCount", { count: total })}</p>
        <Button variant="outline" size="sm" className="gap-1" onClick={onExport}>
          <Download className="h-3.5 w-3.5" /> {t("exams.detail.exportCsv")}
        </Button>
      </div>
      <div className="rounded-xl border overflow-hidden">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t("exams.detail.name")}</TableHead>
              <TableHead>{t("exams.detail.score")}</TableHead>
              <TableHead>{t("exams.detail.correct")}</TableHead>
              <TableHead>{t("exams.detail.status")}</TableHead>
              <TableHead>{t("exams.detail.completed")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {attempts.length === 0 ? (
              <TableRow>
                <TableCell colSpan={5} className="text-center py-8 text-muted-foreground">
                  {t("exams.detail.noParticipants")}
                </TableCell>
              </TableRow>
            ) : (
              attempts.map((a) => {
                const score = a.total_score != null ? a.total_score : 0;
                const passed = score >= passingScore;
                return (
                  <TableRow key={a.attempt_id}>
                    <TableCell className="font-medium">{a.display_name || a.user_id || "Anonymous"}</TableCell>
                    <TableCell>
                      <span className={passed ? "text-emerald-600 dark:text-emerald-400 font-medium" : "text-red-500 font-medium"}>
                        {Math.round(score * 100)}%
                      </span>
                    </TableCell>
                    <TableCell>{a.correct_count}/{a.total_count}</TableCell>
                    <TableCell>
                      <Badge variant={passed ? "default" : "destructive"}>
                        {passed ? t("exams.detail.passed") : t("exams.detail.failed")}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-muted-foreground text-sm">
                      {a.completed_at ? new Date(a.completed_at).toLocaleString() : "—"}
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

function QuestionsTab({ stats }: { stats: ExamStats }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-3">
      {stats.per_question.map((q) => {
        const pct = Math.round(q.correct_rate * 100);
        const isWeak = pct < 50;
        return (
          <div key={q.question_num} className="rounded-xl border p-4">
            <div className="flex items-start justify-between">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-mono bg-muted px-1.5 py-0.5 rounded">Q{q.question_num}</span>
                  <span className="text-sm truncate">{q.question_text}</span>
                </div>
                <div className="flex items-center gap-3 mt-2 text-xs text-muted-foreground">
                  <span>{t("exams.detail.answer", { answer: q.correct_answer })}</span>
                  <span>{t("exams.detail.answered", { count: q.total_answered })}</span>
                  {q.most_common_wrong && <span>{t("exams.detail.mostCommonWrong", { answer: q.most_common_wrong })}</span>}
                </div>
              </div>
              <div className="ml-4 text-right">
                <span className={`text-lg font-semibold ${isWeak ? "text-red-500" : pct >= 80 ? "text-emerald-600 dark:text-emerald-400" : ""}`}>
                  {pct}%
                </span>
                <p className="text-xs text-muted-foreground">{t("exams.detail.correctRate")}</p>
              </div>
            </div>
            {/* Bar */}
            <div className="mt-2 h-1.5 bg-muted rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full transition-all ${isWeak ? "bg-red-500" : pct >= 80 ? "bg-emerald-500" : "bg-primary"}`}
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function AnalysisTab({
  report,
  reports,
  analyzing,
  onAnalyze,
  onSelectReport,
}: {
  report: ExamReport | null;
  reports: Array<{ report_id: string; generated_at: string }>;
  analyzing: boolean;
  onAnalyze: () => void;
  onSelectReport: (id: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Brain className="h-5 w-5 text-primary" />
          <span className="font-medium">{t("exams.analysis.title")}</span>
          {reports.length > 0 && (
            <span className="text-xs text-muted-foreground">({t("exams.analysis.reportCount", { count: reports.length })})</span>
          )}
        </div>
        <Button onClick={onAnalyze} disabled={analyzing} className="gap-2">
          {analyzing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Brain className="h-4 w-4" />}
          {analyzing ? t("exams.analysis.analyzing") : report ? t("exams.analysis.regenerate") : t("exams.analysis.generate")}
        </Button>
      </div>

      {report ? (
        <div className="rounded-xl border bg-card p-6">
          <div className="text-xs text-muted-foreground mb-4">
            {t("exams.analysis.generated", { time: new Date(report.generated_at).toLocaleString() })} · {t("exams.analysis.model", { model: report.model_id })}
          </div>
          <div className="prose prose-sm dark:prose-invert max-w-none">
            <StreamOutput text={report.content} isStreaming={false} />
          </div>
        </div>
      ) : (
        <div className="text-center py-12 text-muted-foreground rounded-xl border border-dashed">
          <Brain className="h-10 w-10 mx-auto mb-3 opacity-30" />
          <p>{t("exams.analysis.noReport")}</p>
          <p className="text-xs mt-1">{t("exams.analysis.noReportHint")}</p>
        </div>
      )}
    </div>
  );
}
