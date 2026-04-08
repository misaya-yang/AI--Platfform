/**
 * Type-ahead hook for Playground input — fetches Wahda suggestions
 * based on the first word of the input (how, what, when, who, why, where).
 */
import { useState, useCallback, useRef } from "react";
import { getTypeahead } from "@/api/wahda";

const TRIGGER_WORDS = ["how", "what", "when", "who", "why", "where", "is", "can", "does"];

export function useTypeahead() {
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [visible, setVisible] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>();

  const check = useCallback((text: string) => {
    const trimmed = text.trim().toLowerCase();

    // Only trigger on short input starting with a question word
    if (!trimmed || trimmed.length > 30 || trimmed.includes("\n")) {
      setSuggestions([]);
      setVisible(false);
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
  }, []);

  return { suggestions, visible, check, dismiss };
}
