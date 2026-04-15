/**
 * Type-ahead hook for Playground input — fetches Wahda suggestions
 * based on the first word of the input (how, what, when, who, why, where).
 */
import { useState, useCallback, useRef, useEffect } from "react";
import { getTypeahead } from "@/api/wahda";

const TRIGGER_WORDS = ["how", "what", "when", "who", "why", "where", "is", "can", "does"];

export function useTypeahead() {
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [visible, setVisible] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>();
  // Suppress re-trigger after dismiss (e.g. input clear fires onTextChange(""))
  const suppressedRef = useRef(false);

  const check = useCallback((text: string) => {
    const trimmed = text.trim().toLowerCase();

    // Only trigger on short input starting with a question word
    if (!trimmed || trimmed.length > 30 || trimmed.includes("\n")) {
      setSuggestions([]);
      setVisible(false);
      return;
    }

    // If dismissed, don't re-trigger until user types a new word
    if (suppressedRef.current) {
      suppressedRef.current = false;
      return;
    }

    const firstWord = trimmed.split(/\s+/)[0];
    if (!TRIGGER_WORDS.includes(firstWord)) {
      setSuggestions([]);
      setVisible(false);
      return;
    }

    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      try {
        const results = await getTypeahead(trimmed);
        if (results.length > 0) {
          setSuggestions(results);
          setVisible(true);
        } else {
          setSuggestions([]);
          setVisible(false);
        }
      } catch {
        setSuggestions([]);
        setVisible(false);
      }
    }, 400);
  }, []);

  const dismiss = useCallback(() => {
    setSuggestions([]);
    setVisible(false);
    suppressedRef.current = true;
  }, []);

  // Cleanup timer on unmount
  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, []);

  return { suggestions, visible, check, dismiss };
}
