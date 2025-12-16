export type SSEMessage<T = unknown> = { data: T };

export async function* sseFetch<T>(
  url: string,
  init: RequestInit
): AsyncGenerator<T, void, void> {
  const resp = await fetch(url, init);
  if (!resp.ok || !resp.body) {
    throw new Error(`SSE request failed: ${resp.status}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        // 处理缓冲区中剩余的数据
        if (buffer.trim()) {
          const line = buffer.split("\n").find((l) => l.startsWith("data:"));
          if (line) {
            const jsonStr = line.slice(5).trim();
            if (jsonStr && jsonStr !== "[DONE]") {
              try {
                const parsed = JSON.parse(jsonStr);
                yield parsed as T;
              } catch {
                // 解析失败，忽略
              }
            }
          }
        }
        break;
      }
      
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";
      
      for (const part of parts) {
        const lines = part.split("\n");
        // 找到 data: 行
        const dataLine = lines.find((l) => l.startsWith("data:"));
        if (!dataLine) continue;
        
        const jsonStr = dataLine.slice(5).trim();
        if (!jsonStr || jsonStr === "[DONE]") continue;
        
        try {
          const parsed = JSON.parse(jsonStr);
          yield parsed as T;
        } catch (e) {
          console.warn("SSE parse error:", e, jsonStr);
          continue;
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

