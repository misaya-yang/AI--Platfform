/**
 * Type-ahead hook — client-side fuzzy matching against pre-loaded question pool.
 *
 * Loads the full question pool once on mount (~115 questions, <5KB),
 * then filters locally on every keystroke with zero network latency.
 * Questions matching the full input prefix are ranked first.
 */
import { useState, useCallback, useRef, useEffect } from "react";
import { getTypeaheadPool } from "@/api/wahda";

const TRIGGER_WORDS = new Set([
  "how", "what", "when", "who", "why", "where", "is", "can", "does",
]);

const MAX_SUGGESTIONS = 3;

/** Simple fuzzy match: all words in query must appear in the question. */
function fuzzyMatch(question: string, queryWords: string[]): boolean {
  const lower = question.toLowerCase();
  return queryWords.every((w) => lower.includes(w));
}

/** Score: prefix match (starts with full input) gets priority 0, else 1. */
function matchScore(question: string, fullInput: string): number {
  return question.toLowerCase().startsWith(fullInput) ? 0 : 1;
}

export function useTypeahead() {
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [visible, setVisible] = useState(false);
  const poolRef = useRef<string[]>([]);
  const loadedRef = useRef(false);
  const suppressedRef = useRef(false);

  // Load question pool once (lazy, non-blocking)
  useEffect(() => {
    if (loadedRef.current) return;
    loadedRef.current = true;

    // Use requestIdleCallback to avoid blocking initial render
    const load = () => {
      getTypeaheadPool()
        .then((questions) => { poolRef.current = questions; })
        .catch(() => { /* Pool load failed — typeahead silent fallback */ });
    };

    if (typeof requestIdleCallback === "function") {
      requestIdleCallback(load);
    } else {
      setTimeout(load, 500);
    }
  }, []);

  const check = useCallback((text: string) => {
    const trimmed = text.trim().toLowerCase();

    if (!trimmed || trimmed.length > 60 || trimmed.includes("\n")) {
      setSuggestions([]);
      setVisible(false);
      return;
    }

    if (suppressedRef.current) {
      suppressedRef.current = false;
      return;
    }

    const words = trimmed.split(/\s+/);
    const firstWord = words[0];
    if (!TRIGGER_WORDS.has(firstWord)) {
      setSuggestions([]);
      setVisible(false);
      return;
    }

    // Client-side fuzzy filter — instant, no network
    const pool = poolRef.current;
    if (pool.length === 0) {
      setSuggestions([]);
      setVisible(false);
      return;
    }

    const matches = pool
      .filter((q) => fuzzyMatch(q, words))
      .sort((a, b) => matchScore(a, trimmed) - matchScore(b, trimmed))
      .slice(0, MAX_SUGGESTIONS);

    if (matches.length > 0) {
      setSuggestions(matches);
      setVisible(true);
    } else {
      setSuggestions([]);
      setVisible(false);
    }
  }, []);

  const dismiss = useCallback(() => {
    setSuggestions([]);
    setVisible(false);
    suppressedRef.current = true;
  }, []);

  return { suggestions, visible, check, dismiss };
}
