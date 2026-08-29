import { useEffect, useState } from "react";
import { AlertTriangle, Loader2, RefreshCcw, SearchX } from "lucide-react";
import { useTranslation } from "react-i18next";

import { listDatasetQueries, listQueryFeedback } from "@/api/knowledge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { QueryFeedback, QueryHistoryItem } from "@/types/knowledge";

interface QueryObservabilityTabProps {
  datasetId?: string;
}

type ResultFilter = "all" | "zero" | "hits";

export function QueryObservabilityTab({ datasetId }: QueryObservabilityTabProps) {
  const { t } = useTranslation();
  const [resultFilter, setResultFilter] = useState<ResultFilter>("all");
  const [mode, setMode] = useState("all");
  const [queries, setQueries] = useState<QueryHistoryItem[]>([]);
  const [queryCursor, setQueryCursor] = useState<string | null>(null);
  const [queryHasMore, setQueryHasMore] = useState(false);
  const [queryLoading, setQueryLoading] = useState(false);
  const [queryError, setQueryError] = useState("");
  const [negativeFeedback, setNegativeFeedback] = useState<QueryFeedback[]>([]);
  const [feedbackCursor, setFeedbackCursor] = useState<string | null>(null);
  const [feedbackHasMore, setFeedbackHasMore] = useState(false);
  const [feedbackLoading, setFeedbackLoading] = useState(false);
  const [feedbackError, setFeedbackError] = useState("");

  async function loadQueries(append = false) {
    if (!datasetId || queryLoading) return;
    setQueryLoading(true);
    setQueryError("");
    try {
      const page = await listDatasetQueries(datasetId, {
        limit: 50,
        zeroResults:
          resultFilter === "all" ? undefined : resultFilter === "zero",
        mode: mode === "all" ? undefined : mode,
        cursor: append ? queryCursor || undefined : undefined,
      });
      setQueries((current) => (append ? [...current, ...page.queries] : page.queries));
      setQueryCursor(page.next_cursor || null);
      setQueryHasMore(page.has_more);
    } catch (error) {
      setQueryError(error instanceof Error ? error.message : String(error));
    } finally {
      setQueryLoading(false);
    }
  }

  async function loadNegativeFeedback(append = false) {
    if (!datasetId || feedbackLoading) return;
    setFeedbackLoading(true);
    setFeedbackError("");
    try {
      const page = await listQueryFeedback(datasetId, {
        limit: 50,
        rating: "negative",
        cursor: append ? feedbackCursor || undefined : undefined,
      });
      setNegativeFeedback((current) =>
        append ? [...current, ...page.feedback] : page.feedback
      );
      setFeedbackCursor(page.next_cursor || null);
      setFeedbackHasMore(page.has_more);
    } catch (error) {
      setFeedbackError(error instanceof Error ? error.message : String(error));
    } finally {
      setFeedbackLoading(false);
    }
  }

  useEffect(() => {
    setQueries([]);
    setQueryCursor(null);
    void loadQueries(false);
    // loadQueries is deliberately keyed by the explicit filters below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [datasetId, resultFilter, mode]);

  useEffect(() => {
    setNegativeFeedback([]);
    setFeedbackCursor(null);
    void loadNegativeFeedback(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [datasetId]);

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,2fr)_minmax(320px,1fr)]">
      <Card className="overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/60 p-4">
          <div>
            <h3 className="font-semibold">{t("knowledge.observability.queryTitle")}</h3>
            <p className="text-xs text-muted-foreground">
              {t("knowledge.observability.queryHint")}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Select value={resultFilter} onValueChange={(value) => setResultFilter(value as ResultFilter)}>
              <SelectTrigger className="w-36" data-testid="query-result-filter">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t("knowledge.observability.allQueries")}</SelectItem>
                <SelectItem value="zero">{t("knowledge.observability.zeroResults")}</SelectItem>
                <SelectItem value="hits">{t("knowledge.observability.withHits")}</SelectItem>
              </SelectContent>
            </Select>
            <Select value={mode} onValueChange={setMode}>
              <SelectTrigger className="w-32" data-testid="query-mode-filter">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t("knowledge.observability.allModes")}</SelectItem>
                <SelectItem value="hybrid">Hybrid</SelectItem>
                <SelectItem value="dense">Dense</SelectItem>
                <SelectItem value="bm25">BM25</SelectItem>
              </SelectContent>
            </Select>
            <Button size="icon" variant="outline" onClick={() => void loadQueries(false)}>
              <RefreshCcw className={queryLoading ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
            </Button>
          </div>
        </div>

        {queryError ? (
          <div role="alert" className="m-4 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
            {queryError}
          </div>
        ) : null}

        <div className="divide-y divide-border/60" data-testid="query-log-list">
          {queries.map((query) => {
            const totalMs = Number(query.stage_timings?.total_ms ?? 0);
            return (
              <article key={query.id} className="space-y-2 p-4">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <p className="min-w-0 flex-1 break-words text-sm font-medium">{query.content}</p>
                  <div className="flex items-center gap-2">
                    <Badge variant="outline">{query.mode || "-"}</Badge>
                    <Badge className={(query.hit_count || 0) === 0 ? "bg-rose-500/10 text-rose-600" : "bg-emerald-500/10 text-emerald-600"}>
                      {t("knowledge.observability.hitCount", { count: query.hit_count || 0 })}
                    </Badge>
                  </div>
                </div>
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                  <span>{new Date(query.created_at).toLocaleString()}</span>
                  <span>TopK {query.top_k ?? "-"}</span>
                  <span>{totalMs > 0 ? `${totalMs.toFixed(1)} ms` : "-"}</span>
                  <span>{query.source}</span>
                  {query.trace_id ? <span className="font-mono">{query.trace_id.slice(0, 8)}</span> : null}
                </div>
              </article>
            );
          })}
          {!queryLoading && queries.length === 0 ? (
            <div className="flex flex-col items-center gap-2 p-12 text-center text-muted-foreground">
              <SearchX className="h-8 w-8" />
              <p className="text-sm">{t("knowledge.observability.noQueries")}</p>
            </div>
          ) : null}
        </div>
        {queryHasMore ? (
          <div className="border-t border-border/60 p-3 text-center">
            <Button variant="outline" onClick={() => void loadQueries(true)} disabled={queryLoading}>
              {queryLoading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {t("knowledge.observability.loadMore")}
            </Button>
          </div>
        ) : null}
      </Card>

      <Card className="overflow-hidden">
        <div className="flex items-center justify-between border-b border-border/60 p-4">
          <div>
            <h3 className="flex items-center gap-2 font-semibold">
              <AlertTriangle className="h-4 w-4 text-amber-600" />
              {t("knowledge.observability.negativeTitle")}
            </h3>
            <p className="text-xs text-muted-foreground">
              {t("knowledge.observability.negativeHint")}
            </p>
          </div>
          <Button size="icon" variant="outline" onClick={() => void loadNegativeFeedback(false)}>
            <RefreshCcw className={feedbackLoading ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
          </Button>
        </div>
        {feedbackError ? (
          <div role="alert" className="m-4 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
            {feedbackError}
          </div>
        ) : null}
        <div className="divide-y divide-border/60" data-testid="negative-feedback-list">
          {negativeFeedback.map((feedback) => (
            <article key={feedback.feedback_id} className="space-y-2 p-4">
              <div className="flex items-center justify-between gap-2">
                <Badge className="bg-rose-500/10 text-rose-600">
                  {t(`knowledge.feedback.reasons.${feedback.reason_code}`)}
                </Badge>
                <span className="text-xs text-muted-foreground">
                  {new Date(feedback.created_at).toLocaleString()}
                </span>
              </div>
              <p className="line-clamp-3 text-sm">
                {feedback.query_content || t("knowledge.observability.queryUnavailable")}
              </p>
              {feedback.comment ? (
                <p className="rounded-md bg-muted/50 p-2 text-xs text-muted-foreground">
                  {feedback.comment}
                </p>
              ) : null}
              <p className="text-xs font-mono text-muted-foreground">
                {feedback.target_type} · {feedback.target_id.slice(0, 12)}
              </p>
            </article>
          ))}
          {!feedbackLoading && negativeFeedback.length === 0 ? (
            <p className="p-10 text-center text-sm text-muted-foreground">
              {t("knowledge.observability.noNegative")}
            </p>
          ) : null}
        </div>
        {feedbackHasMore ? (
          <div className="border-t border-border/60 p-3 text-center">
            <Button variant="outline" onClick={() => void loadNegativeFeedback(true)} disabled={feedbackLoading}>
              {feedbackLoading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {t("knowledge.observability.loadMore")}
            </Button>
          </div>
        ) : null}
      </Card>
    </div>
  );
}
