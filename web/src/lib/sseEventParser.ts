export interface ParsedSSEEvent<T> {
  event: string;
  data: T;
  id?: string;
}

/** Parse one normalized SSE event block, retaining the resume cursor. */
export function parseSseEventPart<T>(part: string): ParsedSSEEvent<T> | null {
  const lines = part.split("\n");
  let event = "";
  let id: string | undefined;
  const dataLines: string[] = [];

  for (const line of lines) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("id:")) {
      const value = line.slice(3).trim();
      if (value) id = value;
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim());
    }
  }

  const dataStr = dataLines.join("\n");
  if (!dataStr || dataStr === "[DONE]") return null;

  try {
    const parsed = JSON.parse(dataStr) as T;
    return { event, data: parsed, ...(id ? { id } : {}) };
  } catch {
    return { event, data: dataStr as unknown as T, ...(id ? { id } : {}) };
  }
}
