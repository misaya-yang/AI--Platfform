/**
 * buildTimeline — pure transformation from ChatMessage → TimelineStep[].
 *
 * Split from ActivityTimeline.tsx so that the Fast-Refresh rule
 * (components-only exports) is satisfied.
 */

import type {
  ChatMessage as ChatMessageType,
  SearchStatusItem,
  ToolCall,
  ToolResult,
  WebSearchResult,
} from "../types";
import type {
  TimelineIcon,
  TimelineSource,
  TimelineStepData,
} from "./TimelineStep";

// ---------------------------------------------------------------------------
// Tool → icon classification
// ---------------------------------------------------------------------------

const WEB_TOOLS = new Set([
  "search_web",
  "web_search",
  "tavily_search",
  "google_search",
  "web_fetch",
  "fetch_url",
  "browse",
]);

const FILE_READ_TOOLS = new Set([
  "fs_read",
  "read_file",
  "fs_glob",
  "fs_grep",
  "glob",
  "grep",
  "list_files",
]);

const FILE_WRITE_TOOLS = new Set([
  "fs_write",
  "write_file",
  "edit_file",
  "fs_edit",
  "fs_patch",
  "apply_patch",
]);

const CODE_TOOLS = new Set([
  "execute_python_code",
  "run_python",
  "run_code",
  "python",
  "bash",
  "shell",
  "execute_code",
]);

const IMAGE_TOOLS = new Set([
  "generate_image",
  "create_image",
  "gemini_image",
  "smart_image_generator",
  "draw_image",
]);

const DOC_TOOLS = new Set([
  "generate_document",
  "generate_pptx",
  "generate_docx",
  "generate_pdf",
  "create_document",
]);

function iconForTool(name: string): TimelineIcon {
  const n = name.toLowerCase();
  if (WEB_TOOLS.has(n) || n.includes("search_web") || n.includes("web_fetch")) return "web";
  if (FILE_WRITE_TOOLS.has(n) || n.includes("write") || n.includes("edit")) return "file_write";
  if (FILE_READ_TOOLS.has(n) || n.includes("read") || n.includes("glob") || n.includes("grep")) return "file_read";
  if (CODE_TOOLS.has(n) || n.includes("python") || n.includes("code") || n.includes("bash")) return "code";
  if (IMAGE_TOOLS.has(n) || n.includes("image")) return "image";
  if (DOC_TOOLS.has(n) || n.includes("doc") || n.includes("pptx") || n.includes("pdf")) return "document";
  return "other";
}

// ---------------------------------------------------------------------------
// Parsing helpers
// ---------------------------------------------------------------------------

function asString(value: unknown): string | undefined {
  if (typeof value === "string") return value;
  return undefined;
}

function asNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  return undefined;
}

function truncate(text: string, max = 180): string {
  const cleaned = text.replace(/\s+/g, " ").trim();
  if (cleaned.length <= max) return cleaned;
  return cleaned.slice(0, max - 1) + "…";
}

function domainFromUrl(url: string): string | null {
  try {
    const u = new URL(url);
    return u.hostname.replace(/^www\./, "");
  } catch {
    return null;
  }
}

const URL_RE = /\bhttps?:\/\/[^\s<>"'\])]+/gi;

function extractSources(result: unknown, limit = 8): TimelineSource[] {
  const sources: TimelineSource[] = [];
  const seen = new Set<string>();

  const add = (url: string, title?: string) => {
    const cleanUrl = url.replace(/[.,;:)\]]+$/, "");
    const domain = domainFromUrl(cleanUrl);
    if (!domain) return;
    if (seen.has(cleanUrl)) return;
    seen.add(cleanUrl);
    sources.push({ domain, url: cleanUrl, title });
  };

  const walk = (value: unknown, depth = 0) => {
    if (sources.length >= limit || depth > 4) return;
    if (value == null) return;
    if (typeof value === "string") {
      const matches = value.match(URL_RE);
      if (matches) {
        for (const m of matches) {
          add(m);
          if (sources.length >= limit) return;
        }
      }
      return;
    }
    if (Array.isArray(value)) {
      for (const item of value) walk(item, depth + 1);
      return;
    }
    if (typeof value === "object") {
      const obj = value as Record<string, unknown>;
      const url = asString(obj.url) ?? asString(obj.source_url) ?? asString(obj.link);
      const title = asString(obj.title) ?? asString(obj.name);
      if (url) add(url, title);
      for (const v of Object.values(obj)) walk(v, depth + 1);
    }
  };

  walk(result);
  return sources;
}

function firstResultSummary(result: unknown): string | undefined {
  if (!result) return undefined;
  if (typeof result === "string") return truncate(result);
  if (Array.isArray(result)) {
    const head = result[0];
    if (head && typeof head === "object") {
      const o = head as Record<string, unknown>;
      return truncate(
        asString(o.content) ??
          asString(o.summary) ??
          asString(o.snippet) ??
          asString(o.text) ??
          JSON.stringify(o),
      );
    }
    if (typeof head === "string") return truncate(head);
  }
  if (typeof result === "object") {
    const o = result as Record<string, unknown>;
    if (asString(o.answer)) return truncate(asString(o.answer)!);
    if (asString(o.summary)) return truncate(asString(o.summary)!);
    if (asString(o.content)) return truncate(asString(o.content)!);
    if (Array.isArray(o.results) && o.results.length > 0) {
      return firstResultSummary(o.results);
    }
    try {
      return truncate(JSON.stringify(o));
    } catch {
      return undefined;
    }
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// Title / body builders
// ---------------------------------------------------------------------------

type TFn = (key: string, opts?: Record<string, unknown>) => string;

function buildToolTitle(t: TFn, tc: ToolCall, icon: TimelineIcon): string {
  const args = tc.arguments ?? {};
  const query =
    asString(args.query) ?? asString(args.q) ?? asString(args.search_query);
  const url = asString(args.url) ?? asString(args.target);
  const path =
    asString(args.path) ?? asString(args.file_path) ?? asString(args.filename);
  const prompt = asString(args.prompt);

  if (icon === "web") {
    if (tc.name.toLowerCase().includes("fetch") || url) {
      const host = url ? domainFromUrl(url) ?? url : tc.name;
      return t("playground.activity.step.fetch", {
        host,
        defaultValue: "Fetching {{host}}",
      });
    }
    if (query) {
      return t("playground.activity.step.searchWeb", {
        query: truncate(query, 80),
        defaultValue: 'Searching the web for "{{query}}"',
      });
    }
    return t("playground.activity.step.searchWebGeneric", {
      defaultValue: "Searching the web",
    });
  }

  if (icon === "file_read") {
    if (path) return t("playground.activity.step.readFile", { path, defaultValue: "Reading {{path}}" });
    if (query)
      return t("playground.activity.step.grep", {
        query: truncate(query, 80),
        defaultValue: 'Searching files for "{{query}}"',
      });
    return t("playground.activity.step.readFileGeneric", { defaultValue: "Reading files" });
  }

  if (icon === "file_write") {
    if (path) return t("playground.activity.step.writeFile", { path, defaultValue: "Writing {{path}}" });
    return t("playground.activity.step.writeFileGeneric", { defaultValue: "Writing a file" });
  }

  if (icon === "code") {
    return t("playground.activity.step.runCode", { defaultValue: "Running code" });
  }

  if (icon === "image") {
    if (prompt)
      return t("playground.activity.step.generateImage", {
        prompt: truncate(prompt, 80),
        defaultValue: "Generating image: {{prompt}}",
      });
    return t("playground.activity.step.generateImageGeneric", {
      defaultValue: "Generating image",
    });
  }

  if (icon === "document") {
    return t("playground.activity.step.generateDocument", { defaultValue: "Generating document" });
  }

  const displayName = tc.name && tc.name.trim() && tc.name !== "tool" ? tc.name : "";
  if (!displayName) {
    // Name missing or still the placeholder from the reducer seed — prefer
    // a name-free generic label over the jarring "Using ".
    return t("playground.activity.step.toolGenericNoName", {
      defaultValue: "External tool",
    });
  }
  return t("playground.activity.step.toolGeneric", {
    name: displayName,
    defaultValue: "Using {{name}}",
  });
}

function buildToolBody(
  t: TFn,
  tc: ToolCall,
  tr: ToolResult | undefined,
  icon: TimelineIcon,
): string {
  if (tr?.error) return String(tr.error);

  const summary = tr?.result != null ? firstResultSummary(tr.result) : undefined;
  if (summary) return summary;

  const args = tc.arguments ?? {};
  const url = asString(args.url);
  const path = asString(args.path) ?? asString(args.file_path);
  const prompt = asString(args.prompt);

  if (icon === "web" && url) return url;
  if ((icon === "file_read" || icon === "file_write") && path) return path;
  if (icon === "image" && prompt) return truncate(prompt);

  if (tc.status === "running") {
    return t("playground.activity.running", { defaultValue: "Running…" });
  }
  return "";
}

// ---------------------------------------------------------------------------
// Main entrypoint
// ---------------------------------------------------------------------------

export interface TimelineBuildResult {
  steps: TimelineStepData[];
  totalDurationMs: number;
}

export function buildTimeline(
  message: ChatMessageType,
  t: TFn,
): TimelineBuildResult {
  const steps: TimelineStepData[] = [];

  // 1. Thinking (single blob)
  const thinkingBody =
    (message.streamingThinkingContent && message.streamingThinkingContent.trim()) ||
    (message.thinkingContent && message.thinkingContent.trim()) ||
    "";

  if (thinkingBody) {
    const isStreamingThinking =
      !!message.isThinkingStreaming ||
      (!!message.streamingThinkingContent && !message.thinkingContent);
    steps.push({
      kind: "thinking",
      id: `thinking-${message.id}`,
      title: isStreamingThinking
        ? t("playground.activity.thinking", { defaultValue: "Thinking" })
        : t("playground.activity.thought", { defaultValue: "Thought process" }),
      body: thinkingBody,
    });
  }

  // 2. Map tool_call_id → result
  const resultMap = new Map<string, ToolResult>();
  for (const r of message.toolResults ?? []) {
    if (r?.tool_call_id) resultMap.set(r.tool_call_id, r);
  }

  // 3. Tool steps
  const toolCalls = message.toolCalls ?? [];
  let totalToolMs = 0;

  for (const tc of toolCalls) {
    const icon = iconForTool(tc.name);
    const tr = resultMap.get(tc.id);
    const title = buildToolTitle(t, tc, icon);
    const body = buildToolBody(t, tc, tr, icon);
    const durationMs = tr?.duration_ms;
    if (typeof durationMs === "number") totalToolMs += durationMs;

    // Primary query-ish argument, rendered as a code block under the tool
    // name in the Claude-style design. search_web.query, web_fetch.url,
    // fs_read.path, generate_image.prompt — whichever is the salient input.
    const args = tc.arguments ?? {};
    const queryArg =
      asString(args.query) ??
      asString(args.q) ??
      asString(args.search_query) ??
      asString(args.url) ??
      asString(args.target) ??
      asString(args.path) ??
      asString(args.file_path) ??
      asString(args.filename) ??
      asString(args.prompt);

    let sources: TimelineSource[] | undefined;
    if (icon === "web") {
      const fromResult = tr ? extractSources(tr.result) : [];
      sources = fromResult.length ? fromResult : undefined;
      if ((!sources || sources.length === 0) && tc.name.toLowerCase().includes("fetch")) {
        const url = asString(tc.arguments?.url);
        if (url) {
          const domain = domainFromUrl(url);
          if (domain) sources = [{ domain, url }];
        }
      }
    }

    const status: "running" | "completed" | "error" =
      tc.status === "error"
        ? "error"
        : tc.status === "completed"
          ? "completed"
          : "running";

    steps.push({
      kind: "tool",
      id: tc.id,
      icon,
      title,
      body,
      sources,
      durationMs,
      status,
      toolName: tc.name,
      queryArg: queryArg ?? undefined,
    });
  }

  // 4. Historical fallback: no toolCalls but we have searchStatus
  if (toolCalls.length === 0 && message.searchStatus && message.searchStatus.length > 0) {
    for (const [idx, s] of message.searchStatus.entries()) {
      const item = s as SearchStatusItem;
      const icon: TimelineIcon =
        item.type === "web" ? "web" : item.type === "kb" ? "file_read" : "file_read";
      let title: string;
      if (item.type === "web") {
        title = item.query
          ? t("playground.activity.step.searchWeb", {
              query: truncate(item.query, 80),
              defaultValue: 'Searching the web for "{{query}}"',
            })
          : t("playground.activity.step.searchWebGeneric", {
              defaultValue: "Searching the web",
            });
      } else if (item.type === "kb") {
        const ds = item.datasets?.[0];
        title = ds
          ? t("playground.activity.step.searchKb", {
              dataset: ds,
              defaultValue: "Searching {{dataset}}",
            })
          : t("playground.activity.step.searchKbGeneric", {
              defaultValue: "Searching knowledge base",
            });
      } else {
        title = t("playground.activity.step.processFiles", {
          defaultValue: "Processing files",
        });
      }
      const body =
        item.resultCount != null
          ? t("playground.activity.foundResults", {
              count: item.resultCount,
              defaultValue: "{{count}} results",
            })
          : "";
      if (typeof item.durationMs === "number") totalToolMs += item.durationMs;

      let sources: TimelineSource[] | undefined;
      if (item.type === "web" && message.webSearchResults) {
        sources = (message.webSearchResults as WebSearchResult[])
          .slice(0, 8)
          .map((r) => {
            const domain = domainFromUrl(r.url);
            return domain ? { domain, url: r.url, title: r.title } : null;
          })
          .filter((x): x is TimelineSource => x !== null);
        if (sources.length === 0) sources = undefined;
      }

      const status: "running" | "completed" | "error" =
        item.state === "error"
          ? "error"
          : item.state === "completed"
            ? "completed"
            : "running";

      steps.push({
        kind: "tool",
        id: `hist-${idx}-${item.type}`,
        icon,
        title,
        body,
        sources,
        durationMs: item.durationMs,
        status,
      });
    }
  }

  const thinkingMs = asNumber(message.processSummary?.thinkingDurationMs) ?? 0;
  const totalDurationMs =
    asNumber(message.durationMs) ?? totalToolMs + thinkingMs;

  return { steps, totalDurationMs };
}
