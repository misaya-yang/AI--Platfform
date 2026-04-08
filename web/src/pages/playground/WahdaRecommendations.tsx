/**
 * Wahda Recommendation Cards — seasonal questions, type-ahead, trending.
 *
 * Displayed in the Playground empty state when service is selected
 * but no messages exist yet.
 */
import { useEffect, useState, useCallback, useRef } from "react";
import { useTranslation } from "react-i18next";
import {
  getRecommendations,
  getTypeahead,
  getTrending,
  type RecommendationResponse,
  type TrendingResponse,
} from "@/api/wahda";
import { cn } from "@/lib/utils";
import {
  Sparkles,
  TrendingUp,
  Search,
  Calendar,
  ChevronLeft,
  ChevronRight,
  RefreshCw,
} from "lucide-react";

interface Props {
  onSelectQuestion: (question: string) => void;
}

export function WahdaRecommendations({ onSelectQuestion }: Props) {
  const { t } = useTranslation();
  const [recommendations, setRecommendations] = useState<RecommendationResponse | null>(null);
  const [trending, setTrending] = useState<TrendingResponse | null>(null);
  const [typeaheadInput, setTypeaheadInput] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>();

  // Load recommendations + trending on mount
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [rec, trend] = await Promise.allSettled([
          getRecommendations(),
          getTrending(),
        ]);
        if (cancelled) return;
        if (rec.status === "fulfilled") setRecommendations(rec.value);
        if (trend.status === "fulfilled") setTrending(trend.value);
      } catch {
        if (!cancelled) setError(true);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Type-ahead debounced
  const handleTypeahead = useCallback((value: string) => {
    setTypeaheadInput(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (value.length < 2) {
      setSuggestions([]);
      return;
    }
    debounceRef.current = setTimeout(async () => {
      try {
        const results = await getTypeahead(value);
        setSuggestions(results);
      } catch {
        setSuggestions([]);
      }
    }, 300);
  }, []);

  // Horizontal scroll
  const scroll = (dir: "left" | "right") => {
    scrollRef.current?.scrollBy({
      left: dir === "left" ? -280 : 280,
      behavior: "smooth",
    });
  };

  // Refresh
  const refresh = async () => {
    setLoading(true);
    try {
      const [rec, trend] = await Promise.allSettled([
        getRecommendations(),
        getTrending(),
      ]);
      if (rec.status === "fulfilled") setRecommendations(rec.value);
      if (trend.status === "fulfilled") setTrending(trend.value);
    } finally {
      setLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="animate-pulse text-sm text-muted-foreground">Loading recommendations...</div>
      </div>
    );
  }

  if (error && !recommendations && !trending) {
    return null; // Fall back to default empty state
  }

  const seasonalQuestions = recommendations?.questions || [];
  const trendingQuestions = trending?.questions || [];
  const eventName = recommendations?.event_name;

  return (
    <div className="flex h-full flex-col items-center justify-center p-6 gap-6 max-w-2xl mx-auto">
      {/* Header */}
      <div className="text-center">
        <div className="mb-3 h-14 w-14 mx-auto rounded-2xl bg-gradient-to-br from-emerald-500 via-teal-500 to-cyan-500 flex items-center justify-center shadow-lg shadow-emerald-500/20">
          <Sparkles className="h-7 w-7 text-white" />
        </div>
        <h2 className="text-xl font-semibold tracking-tight">
          {eventName
            ? `${eventName}`
            : t("playground.wahda.title", "What would you like to learn?")}
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          {t("playground.wahda.subtitle", "Choose a question below or type your own")}
        </p>
      </div>

      {/* Seasonal / Daily Recommendations */}
      {seasonalQuestions.length > 0 && (
        <div className="w-full">
          <div className="flex items-center gap-2 mb-2.5 px-1">
            <Calendar className="h-3.5 w-3.5 text-emerald-500" />
            <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
              {eventName
                ? t("playground.wahda.seasonal", "Seasonal")
                : t("playground.wahda.dailyPicks", "Daily Picks")}
            </span>
            <button
              onClick={refresh}
              className="ml-auto p-1 rounded-md hover:bg-muted transition-colors"
              title="Refresh"
            >
              <RefreshCw className="h-3 w-3 text-muted-foreground" />
            </button>
          </div>
          <div className="relative">
            <button
              onClick={() => scroll("left")}
              className="absolute -left-3 top-1/2 -translate-y-1/2 z-10 h-7 w-7 rounded-full bg-background border shadow-sm flex items-center justify-center hover:bg-muted transition-colors"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </button>
            <div
              ref={scrollRef}
              className="flex gap-2.5 overflow-x-auto scrollbar-hide scroll-smooth px-1 pb-1"
              style={{ scrollbarWidth: "none", msOverflowStyle: "none" }}
            >
              {seasonalQuestions.map((q, i) => (
                <button
                  key={i}
                  onClick={() => onSelectQuestion(q)}
                  className={cn(
                    "flex-shrink-0 w-[250px] rounded-xl border bg-card/80 p-3.5",
                    "text-left text-sm leading-relaxed",
                    "hover:bg-accent/50 hover:border-primary/20 hover:shadow-sm",
                    "transition-all duration-200 cursor-pointer",
                    "active:scale-[0.98]"
                  )}
                >
                  <span className="line-clamp-3">{q}</span>
                </button>
              ))}
            </div>
            <button
              onClick={() => scroll("right")}
              className="absolute -right-3 top-1/2 -translate-y-1/2 z-10 h-7 w-7 rounded-full bg-background border shadow-sm flex items-center justify-center hover:bg-muted transition-colors"
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      )}

      {/* Trending Questions */}
      {trendingQuestions.length > 0 && (
        <div className="w-full">
          <div className="flex items-center gap-2 mb-2.5 px-1">
            <TrendingUp className="h-3.5 w-3.5 text-orange-500" />
            <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
              {t("playground.wahda.trending", "Trending This Week")}
            </span>
            {trending?.source === "fallback" && (
              <span className="text-[10px] text-muted-foreground/60">(sample)</span>
            )}
          </div>
          <div className="grid grid-cols-1 gap-1.5">
            {trendingQuestions.slice(0, 5).map((q, i) => (
              <button
                key={i}
                onClick={() => onSelectQuestion(q)}
                className={cn(
                  "w-full rounded-lg border border-transparent bg-muted/40 px-3.5 py-2.5",
                  "text-left text-sm",
                  "hover:bg-accent/50 hover:border-primary/15",
                  "transition-all duration-150 cursor-pointer",
                  "active:scale-[0.99]"
                )}
              >
                <span className="line-clamp-2">{q}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Type-ahead Search */}
      <div className="w-full">
        <div className="flex items-center gap-2 mb-2.5 px-1">
          <Search className="h-3.5 w-3.5 text-blue-500" />
          <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
            {t("playground.wahda.explore", "Explore Topics")}
          </span>
        </div>
        <div className="relative">
          <input
            type="text"
            value={typeaheadInput}
            onChange={(e) => handleTypeahead(e.target.value)}
            placeholder={t("playground.wahda.typeaheadPlaceholder", "Try: how, what, when, why...")}
            className={cn(
              "w-full rounded-xl border bg-muted/30 px-4 py-2.5",
              "text-sm placeholder:text-muted-foreground/50",
              "focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary/30",
              "transition-all duration-200"
            )}
          />
          {suggestions.length > 0 && (
            <div className="mt-1.5 space-y-1">
              {suggestions.map((s, i) => (
                <button
                  key={i}
                  onClick={() => {
                    onSelectQuestion(s);
                    setTypeaheadInput("");
                    setSuggestions([]);
                  }}
                  className={cn(
                    "w-full rounded-lg bg-blue-500/5 border border-blue-500/10 px-3.5 py-2",
                    "text-left text-sm",
                    "hover:bg-blue-500/10 hover:border-blue-500/20",
                    "transition-all duration-150 cursor-pointer"
                  )}
                >
                  {s}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
