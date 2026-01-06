export type SSEMessage<T = unknown> = { data: T };
export type SSEEvent<T = unknown> = { event: string; data: T };

export interface SSEFetchOptions extends RequestInit {
  signal?: AbortSignal;
}

export async function* sseFetch<T>(
  url: string,
  init: SSEFetchOptions
): AsyncGenerator<T, void, void> {
  const debug =
    import.meta.env.DEV && import.meta.env.VITE_SSE_DEBUG === "true";
  const startTime = performance.now();
  if (debug) {
    console.log(`[SSE] Starting fetch to ${url}`);
  }
  
  const resp = await fetch(url, {
    ...init,
    signal: init.signal,
  });
  
  if (debug) {
    console.log(
      `[SSE] Response received in ${(performance.now() - startTime).toFixed(0)}ms, status=${resp.status}`
    );
  }
  
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
        if (debug) {
          console.log(
            `[SSE] First data chunk received at ${(firstChunkTime - startTime).toFixed(0)}ms, size=${value.length}`
          );
        }
      }
      
      if (done) {
        if (debug) {
          console.log(
            `[SSE] Stream done. Total chunks: ${chunkCount}, duration: ${(performance.now() - startTime).toFixed(0)}ms`
          );
        }
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
      // SSE events can be separated by \n\n or \r\n\r\n - normalize to \n\n first
      const normalizedBuffer = buffer.replace(/\r\n/g, "\n");
      const parts = normalizedBuffer.split("\n\n");
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
          if (debug && (chunkCount <= 3 || chunkCount % 50 === 0)) {
            console.log(
              `[SSE] Yielding chunk #${chunkCount} at ${(performance.now() - startTime).toFixed(0)}ms`
            );
          }
          yield parsed as T;
        } catch (e) {
          if (debug) {
            console.warn("SSE parse error:", e, jsonStr);
          }
          continue;
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export async function* sseFetchEvents<T>(
  url: string,
  init: SSEFetchOptions
): AsyncGenerator<SSEEvent<T>, void, void> {
  const debug =
    import.meta.env.DEV && import.meta.env.VITE_SSE_DEBUG === "true";
  const startTime = performance.now();

  if (debug) console.log(`[SSE-Events] Starting fetch to ${url}`);

  const resp = await fetch(url, {
    ...init,
    signal: init.signal,
  });

  if (debug) {
    console.log(`[SSE-Events] Response: status=${resp.status}, content-type=${resp.headers.get("content-type")}`);
  }

  if (!resp.ok || !resp.body) {
    throw new Error(`SSE request failed: ${resp.status}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let chunkCount = 0;
  let firstChunkTime: number | null = null;
  let yieldedCount = 0;

  const parsePart = (part: string): SSEEvent<T> | null => {
    const lines = part.split("\n");
    let event = "";
    const dataLines: string[] = [];

    for (const line of lines) {
      if (line.startsWith("event:")) {
        event = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    }

    const dataStr = dataLines.join("\n");
    if (!dataStr || dataStr === "[DONE]") return null;

    try {
      const parsed = JSON.parse(dataStr) as T;
      return { event, data: parsed };
    } catch {
      return { event, data: dataStr as unknown as T };
    }
  };

  try {
    while (true) {
      const { done, value } = await reader.read();

      if (value && firstChunkTime === null) {
        firstChunkTime = performance.now();
        if (debug) {
          console.log(`[SSE-Events] First chunk at ${(firstChunkTime - startTime).toFixed(0)}ms, size=${value.length}`);
        }
      }

      if (done) {
        if (debug) {
          console.log(`[SSE-Events] Stream done. Total yielded: ${yieldedCount}, duration: ${(performance.now() - startTime).toFixed(0)}ms`);
        }

        if (buffer.trim()) {
          const evt = parsePart(buffer);
          if (evt) {
            yieldedCount++;
            yield evt;
          }
        }
        break;
      }

      buffer += decoder.decode(value, { stream: true });
      // SSE events can be separated by \n\n or \r\n\r\n - normalize to \n\n first
      const normalizedBuffer = buffer.replace(/\r\n/g, "\n");
      const parts = normalizedBuffer.split("\n\n");
      // Keep the last incomplete part in buffer
      const lastPart = parts.pop() || "";
      buffer = lastPart;

      for (const part of parts) {
        const evt = parsePart(part);
        if (!evt) continue;
        yieldedCount++;
        chunkCount++;
        if (debug && (yieldedCount <= 3 || chunkCount % 50 === 0)) {
          console.log(`[SSE-Events] Yielding #${yieldedCount} event='${evt.event}' at ${(performance.now() - startTime).toFixed(0)}ms`);
        }
        yield evt;
      }
    }
  } finally {
    reader.releaseLock();
  }
}
