export type SSEMessage<T = unknown> = { data: T };

export interface SSEFetchOptions extends RequestInit {
  signal?: AbortSignal;
}

export async function* sseFetch<T>(
  url: string,
  init: SSEFetchOptions
): AsyncGenerator<T, void, void> {
  const startTime = performance.now();
  console.log(`[SSE] Starting fetch to ${url}`);
  
  const resp = await fetch(url, {
    ...init,
    signal: init.signal,
  });
  
  console.log(`[SSE] Response received in ${(performance.now() - startTime).toFixed(0)}ms, status=${resp.status}`);
  
  if (!resp.ok || !resp.body) {
    throw new Error(`SSE request failed: ${resp.status}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let chunkCount = 0;
  let firstChunkTime: number | null = null;

  try {
    while (true) {
      const { done, value } = await reader.read();
      
      if (value && firstChunkTime === null) {
        firstChunkTime = performance.now();
        console.log(`[SSE] First data chunk received at ${(firstChunkTime - startTime).toFixed(0)}ms, size=${value.length}`);
      }
      
      if (done) {
        console.log(`[SSE] Stream done. Total chunks: ${chunkCount}, duration: ${(performance.now() - startTime).toFixed(0)}ms`);
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
          chunkCount++;
          if (chunkCount <= 3 || chunkCount % 50 === 0) {
            console.log(`[SSE] Yielding chunk #${chunkCount} at ${(performance.now() - startTime).toFixed(0)}ms`);
          }
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

