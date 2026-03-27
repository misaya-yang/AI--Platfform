import { memo, useMemo, useState, type ReactNode } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { ImageIcon, Download, ExternalLink, FileDown } from "lucide-react";
import "katex/dist/katex.min.css";
import { useLatexCopy } from "@/hooks/useLatexCopy";
import { useTranslation } from "react-i18next";

/**
 * Custom URL transform that allows data: URLs for base64 images.
 * By default, react-markdown filters out data: URLs for security.
 * Since our images come from trusted AI generation, we allow them.
 * @see https://github.com/remarkjs/react-markdown#urltransform
 */
function allowDataUrlTransform(url: string): string {
  // Allow data: URLs for images (base64 encoded)
  if (url.startsWith("data:image/")) {
    return url;
  }
  // Use default transform for all other URLs (security filtering)
  return defaultUrlTransform(url);
}

interface StreamOutputProps {
  text: string;
  /** Whether content is still streaming */
  isStreaming?: boolean;
  /** Unique ID for keying memoized blocks (reserved for future use) */
  id?: string;
}

/**
 * Custom link renderer that handles download links and external links properly.
 * - Download links (files like .docx, .pdf, artifact URLs) open in new tab with download
 * - External links open in new tab
 * - Anchor links (#) work normally
 */
function MarkdownLink({ href, children }: { href?: string; children?: ReactNode }) {
  // Check if this is a downloadable file
  const isDownloadLink = href && (
    // Common document extensions
    /\.(docx?|xlsx?|pptx?|pdf|csv|zip|rar|txt|md)$/i.test(href) ||
    // Artifact download URLs
    href.includes("/artifacts/") ||
    href.includes("/download")
  );

  // Check if external link
  const isExternal = href && (href.startsWith("http://") || href.startsWith("https://"));
  const isAnchor = href && href.startsWith("#");

  const handleClick = (e: React.MouseEvent<HTMLAnchorElement>) => {
    if (!href) return;

    // For download links, always open in new tab to trigger download
    if (isDownloadLink) {
      e.preventDefault();
      window.open(href, "_blank", "noopener,noreferrer");
      return;
    }

    // For external links, open in new tab
    if (isExternal && !isAnchor) {
      e.preventDefault();
      window.open(href, "_blank", "noopener,noreferrer");
    }
    // Anchor links use default behavior
  };

  return (
    <a
      href={href}
      onClick={handleClick}
      className={`
        inline-flex items-center gap-1 text-primary hover:text-primary/80
        underline underline-offset-2 decoration-primary/30 hover:decoration-primary/60
        transition-colors
      `}
      rel={isExternal ? "noopener noreferrer" : undefined}
    >
      {children}
      {isDownloadLink && <FileDown className="h-3 w-3 inline-block" />}
      {isExternal && !isDownloadLink && <ExternalLink className="h-3 w-3 inline-block" />}
    </a>
  );
}

/**
 * Custom image renderer with support for base64 data URLs.
 * Provides loading state, error handling, and download capability.
 */
function MarkdownImage({ src, alt }: { src?: string; alt?: string }) {
  const [isLoading, setIsLoading] = useState(true);
  const [hasError, setHasError] = useState(false);
  const [isDownloading, setIsDownloading] = useState(false);
  const { t } = useTranslation();

  const isBase64 = src?.startsWith("data:");
  const displayAlt = alt || t("common.generatedImage");

  // Download image - supports both base64 and URL images
  const handleDownload = async () => {
    if (!src || isDownloading) return;

    const filename = `${displayAlt.replace(/\s+/g, "_")}_${Date.now()}.png`;

    if (isBase64) {
      // Direct download for base64 images
      const link = document.createElement("a");
      link.href = src;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    } else {
      // Fetch and download for URL images
      setIsDownloading(true);
      try {
        const response = await fetch(src);
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
      } catch (error) {
        console.error("Failed to download image:", error);
        // Fallback: open in new tab
        window.open(src, "_blank");
      } finally {
        setIsDownloading(false);
      }
    }
  };

  // Open in new tab - using DOM API instead of document.write to prevent XSS
  const handleOpenInNewTab = () => {
    if (!src) return;
    const newWindow = window.open("", "_blank");
    if (newWindow) {
      const doc = newWindow.document;
      doc.title = displayAlt;
      doc.body.style.cssText = "margin:0;display:flex;justify-content:center;align-items:center;min-height:100vh;background:#1a1a1a;";
      const img = doc.createElement("img");
      img.src = src;
      img.alt = displayAlt;
      img.style.cssText = "max-width:100%;max-height:100vh;object-fit:contain;";
      doc.body.appendChild(img);
    }
  };

  if (hasError) {
    return (
      <span className="flex flex-col items-center justify-center p-6 my-3 rounded-xl bg-slate-100 dark:bg-slate-800 border border-slate-200 dark:border-slate-700" style={{ display: 'flex' }}>
        <ImageIcon className="h-10 w-10 text-slate-400 mb-2" />
        <span className="text-sm text-slate-500">{t("common.imageLoadFailed")}</span>
      </span>
    );
  }

  return (
    <span className="relative group my-3 block">
      {/* Loading placeholder */}
      {isLoading && (
        <span className="absolute inset-0 flex items-center justify-center rounded-xl bg-slate-100 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 animate-pulse" style={{ display: 'flex' }}>
          <ImageIcon className="h-10 w-10 text-slate-400" />
        </span>
      )}

      {/* Image */}
      <img
        src={src}
        alt={displayAlt}
        className={`
          max-w-full rounded-xl shadow-lg border border-slate-200 dark:border-slate-700
          transition-all duration-300
          ${isLoading ? "opacity-0 h-[200px]" : "opacity-100"}
        `}
        onLoad={() => setIsLoading(false)}
        onError={() => {
          setIsLoading(false);
          setHasError(true);
        }}
      />

      {/* Hover actions for all images */}
      {!isLoading && !hasError && (
        <span className="absolute top-2 right-2 flex gap-1.5 opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            onClick={handleDownload}
            disabled={isDownloading}
            className="p-1.5 rounded-lg bg-black/50 hover:bg-black/70 text-white backdrop-blur-sm transition-colors disabled:opacity-50"
            title={t("common.downloadImage")}
          >
            <Download className={`h-4 w-4 ${isDownloading ? 'animate-pulse' : ''}`} />
          </button>
          <button
            onClick={handleOpenInNewTab}
            className="p-1.5 rounded-lg bg-black/50 hover:bg-black/70 text-white backdrop-blur-sm transition-colors"
            title={t("common.openInNewTab")}
          >
            <ExternalLink className="h-4 w-4" />
          </button>
        </span>
      )}
    </span>
  );
}

/**
 * Filter out JSON tool arguments and internal prompts that models might accidentally output to chat.
 * This happens when models output JSON before calling a tool or when internal
 * classification/routing results leak into the response.
 */
function filterToolJsonOutput(text: string): string {
  // Pattern 1: Code block with JSON tool arguments (```json ... ```)
  const jsonCodeBlockPattern = /```(?:json)?\s*\n?\s*\{[\s\S]*?"(?:slides|title|bullets|type|content)"[\s\S]*?\}\s*\n?```/gi;

  // Pattern 2: Raw JSON object with tool-like structure (at start of text or after newlines)
  const rawJsonPattern = /(?:^|\n)\s*\{\s*"(?:title|slides)"[\s\S]*?\}\s*(?=\n|$)/gi;

  // Pattern 3: JSON array of slides [ { "type": ... } ]
  const slidesArrayPattern = /\[\s*\{\s*"(?:type|title|bullets)"[\s\S]*?\}\s*(?:,\s*\{[\s\S]*?\}\s*)*\]/gi;

  // Pattern 4: Classification/routing JSON blobs leaking ANYWHERE
  // Matches: {"relevance":"offtopic","topic":"xxx"...}? with optional trailing ?
  // Uses non-greedy matching to capture the JSON object
  const classificationJsonPattern = /\{"(?:relevance|topic)":[^}]*(?:"(?:relevance|topic|sensitive|ambiguous|political|interfaith_attack|interpretation)":[^,}]*,?\s*)*\}\??/gi;

  // Pattern 5: Tool metadata JSON at start
  const toolMetaJsonPattern = /^\s*\{\s*"(?:tool_name|confidence|guidance|citations|queries|results_count|query_language|cross_language_enabled)"[\s\S]*?\}\s*(?:\n|$)/i;

  // Pattern 6: Router result JSON (router: xxx) or text before it
  const routerResultPattern = /\brouter:\s*\w+\s*(?:\n|$)/gi;

  // Pattern 7: Any standalone JSON object with known internal keys
  const internalJsonPattern = /\{[^{}]*"(?:relevance|tool_name|confidence)":\s*"[^"]*"[^{}]*\}\s*\??/gi;

  // Pattern 8: Internal prompt phrases that shouldn't be shown to users (with optional trailing content)
  // Be careful not to filter too aggressively - only filter specific internal prompts
  // NOTE: Do NOT filter "Let me" or "I'll" as these are common in normal responses
  const internalPromptsPattern = /(?:Here(?:'s| is) the JSON(?: requested)?|JSON output|Tool output|Generating response|Processing request)[:\s\n]*(?:```[\s\S]*?(?:```|$))?/gi;

  // Pattern 9: Empty code blocks (``` followed by ``` with only whitespace)
  const emptyCodeBlockPattern = /```(?:\w*)\s*\n?\s*```/gi;

  // Pattern 10: Code blocks with only JSON objects (tool arguments leaked as code)
  const jsonOnlyCodeBlockPattern = /```(?:json)?\s*\n\s*\{[\s\S]*?\}\s*\n```/gi;

  // Pattern 11: Incomplete/unclosed code blocks at the start (e.g., "Here is...\n```" without closing)
  // This handles the case where LLM outputs code block start but content continues normally
  const unclosedCodeBlockPattern = /^[^`]*```(?!\s*\n[\s\S]*?```)(?=\s*\n)/gm;
  
  // Pattern 12: Leading text with unclosed code fence (handles streaming partial output)
  // Only match specific internal prompt patterns, not general text
  const leadingUnclosedFencePattern = /^(?:Here(?:'s| is) the JSON[^`]*)\n?```\s*$/gm;

  // Pattern 13: Standalone unclosed code fences (just ``` at start or end)
  const standaloneCodeFence = /^```\s*$/gm;

  let filtered = text
    .replace(jsonCodeBlockPattern, '')
    .replace(rawJsonPattern, '')
    .replace(slidesArrayPattern, '')
    .replace(classificationJsonPattern, '')
    .replace(internalJsonPattern, '')
    .replace(toolMetaJsonPattern, '')
    .replace(routerResultPattern, '')
    .replace(internalPromptsPattern, '')
    .replace(emptyCodeBlockPattern, '')
    .replace(jsonOnlyCodeBlockPattern, '')
    .replace(unclosedCodeBlockPattern, '')
    .replace(leadingUnclosedFencePattern, '')
    .replace(standaloneCodeFence, '')
    .replace(/^(CONFIDENCE|GUIDANCE):.*$/gim, '')
    // Only remove specific "Here is the JSON" phrases, not general "Here is" which may be valid content
    .replace(/^Here(?:'s| is) the JSON[^\n]*\n?/gim, '');

  // Clean up excessive newlines left behind
  filtered = filtered.replace(/\n{3,}/g, '\n\n').trim();

  return filtered;
}

/**
 * Detect if text is predominantly RTL (Arabic, Persian, Urdu, Hebrew).
 * Strips markdown syntax before counting to avoid false negatives.
 */
function isRtlText(text: string): boolean {
  // Strip markdown formatting, URLs, code blocks
  const plain = text
    .replace(/```[\s\S]*?```/g, "")
    .replace(/`[^`]*`/g, "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[#*_~>\-|]/g, "");
  // Count RTL characters (Arabic, Hebrew, Persian/Urdu extended)
  const rtlChars = (plain.match(/[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF\u0590-\u05FF]/g) || []).length;
  const latinChars = (plain.match(/[a-zA-Z]/g) || []).length;
  // If RTL chars outnumber Latin chars, it's an RTL response
  return rtlChars > 0 && rtlChars > latinChars;
}

/**
 * High-performance streaming markdown renderer.
 *
 * Uses block-level memoization to achieve O(n) rendering instead of O(n²).
 * - Parses markdown into discrete blocks (paragraphs, headings, lists, etc.)
 * - Each completed block is memoized and won't re-render
 * - Only the last (incomplete) block re-renders during streaming
 *
 * This is the same approach used by ChatGPT, Vercel AI SDK, etc.
 *
 * @see https://ai-sdk.dev/cookbook/next/markdown-chatbot-with-memoization
 */
export const StreamOutput = memo(function StreamOutput({
  text,
  isStreaming = false,
}: StreamOutputProps) {
  // Enable LaTeX copy support - copies original LaTeX source when selecting formulas
  useLatexCopy();

  // Filter out accidental JSON tool output before parsing
  const filteredText = useMemo(() => filterToolJsonOutput(text), [text]);

  // Detect RTL content (Arabic, Persian, Urdu, Hebrew)
  const rtl = useMemo(() => isRtlText(filteredText), [filteredText]);

  if (!text) return null;

  return (
    <div dir={rtl ? "rtl" : undefined} className={`assistant-copy prose prose-slate dark:prose-invert max-w-none break-words prose-p:my-3 prose-p:leading-7 prose-p:text-[15px] sm:prose-p:text-[16px] prose-headings:mt-7 prose-headings:mb-3 prose-headings:font-semibold prose-headings:tracking-tight prose-ul:my-3 prose-ol:my-3 prose-li:my-1 prose-li:text-[15px] sm:prose-li:text-[16px] prose-pre:my-4 prose-pre:overflow-x-auto prose-pre:max-w-full prose-pre:whitespace-pre-wrap prose-pre:break-words prose-pre:rounded-xl prose-pre:border prose-pre:border-slate-200/70 dark:prose-pre:border-slate-700/60 prose-pre:bg-slate-100/70 dark:prose-pre:bg-slate-900/60 prose-code:whitespace-pre-wrap prose-code:break-words prose-code:text-[14px] prose-blockquote:border-l-slate-300 dark:prose-blockquote:border-l-slate-600${rtl ? " text-right" : ""}`}>
      <ReactMarkdown
        remarkPlugins={[
          remarkGfm,
          [remarkMath, { singleDollarTextMath: true }],
        ]}
        rehypePlugins={[
          [rehypeKatex, { throwOnError: false, strict: false, output: "htmlAndMathml" }],
        ]}
        urlTransform={allowDataUrlTransform}
        components={{
          img: ({ src, alt }) => <MarkdownImage src={src} alt={alt} />,
          a: ({ href, children }) => <MarkdownLink href={href}>{children}</MarkdownLink>,
        }}
      >
        {filteredText}
      </ReactMarkdown>
      {/* Streaming cursor - clean blinking bar */}
      {isStreaming && (
        <span className="inline-flex items-center ml-0.5 align-text-bottom">
          <span
            className="inline-block w-[2px] h-[18px] rounded-full bg-gradient-to-b from-violet-500 to-purple-500"
            style={{
              animation: 'cursor-blink 1s ease-in-out infinite',
            }}
          />
          <style>{`
            @keyframes cursor-blink {
              0%, 100% { opacity: 1; }
              50% { opacity: 0.15; }
            }
          `}</style>
        </span>
      )}
    </div>
  );
});
