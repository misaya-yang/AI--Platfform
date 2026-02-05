import { memo, useMemo, useState, type ReactNode } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { marked } from "marked";
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
  /** Unique ID for keying memoized blocks */
  id?: string;
}

interface ParsedBlocks {
  blocks: string[];
  /** Global definitions (link references, footnotes) to prepend to each block */
  definitions: string;
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
 * Parse markdown into discrete blocks using marked lexer.
 * Extracts global definitions (link references, footnotes) and preserves them
 * so they can be injected into each block for proper rendering.
 */
function parseMarkdownIntoBlocks(markdown: string): ParsedBlocks {
  if (!markdown) return { blocks: [], definitions: "" };

  try {
    const tokens = marked.lexer(markdown);
    const definitions: string[] = [];
    const blocks: string[] = [];

    for (const token of tokens) {
      // Collect global definitions (link references, footnotes)
      if (token.type === "def" || token.type === "footnote") {
        definitions.push(token.raw);
      } else if (token.raw && token.raw.trim()) {
        blocks.push(token.raw);
      }
    }

    return {
      blocks,
      definitions: definitions.join("\n"),
    };
  } catch {
    // Fallback: render as single block to preserve structure
    // Don't split - it would break code blocks, lists, etc.
    return {
      blocks: [markdown],
      definitions: "",
    };
  }
}

/**
 * Memoized individual markdown block.
 * Once rendered, won't re-render unless content actually changes.
 */
const MemoizedMarkdownBlock = memo(
  function MemoizedMarkdownBlock({ content, definitions }: { content: string; definitions: string }) {
    // Prepend definitions to each block so references resolve correctly
    const fullContent = definitions ? `${definitions}\n\n${content}` : content;
    return (
      <ReactMarkdown
        remarkPlugins={[
          remarkGfm,
          // Configure remark-math to be more lenient with spacing
          [remarkMath, { singleDollarTextMath: true }],
        ]}
        rehypePlugins={[
          // Configure rehype-katex for better error handling
          // output: 'htmlAndMathml' includes <annotation> with original LaTeX for copy support
          [rehypeKatex, { throwOnError: false, strict: false, output: "htmlAndMathml" }],
        ]}
        // Allow data: URLs for base64 images (filtered by default for security)
        urlTransform={allowDataUrlTransform}
        components={{
          // Custom image renderer with base64 support
          img: ({ src, alt }) => <MarkdownImage src={src} alt={alt} />,
          // Custom link renderer for download/external links
          a: ({ href, children }) => <MarkdownLink href={href}>{children}</MarkdownLink>,
        }}
      >
        {fullContent}
      </ReactMarkdown>
    );
  },
  (prev, next) => prev.content === next.content && prev.definitions === next.definitions
);

/**
 * Filter out JSON tool arguments that models might accidentally output to chat.
 * This happens when models output JSON before calling a tool (e.g., PPTX slides array).
 *
 * Pattern: Detects code blocks containing JSON with "slides", "title", "bullets" etc.
 */
function filterToolJsonOutput(text: string): string {
  // Pattern 1: Code block with JSON tool arguments (```json ... ```)
  const jsonCodeBlockPattern = /```(?:json)?\s*\n?\s*\{[\s\S]*?"(?:slides|title|bullets|type|content)"[\s\S]*?\}\s*\n?```/gi;

  // Pattern 2: Raw JSON object with tool-like structure (at start of text or after newlines)
  const rawJsonPattern = /(?:^|\n)\s*\{\s*"(?:title|slides)"[\s\S]*?\}\s*(?=\n|$)/gi;

  // Pattern 3: JSON array of slides [ { "type": ... } ]
  const slidesArrayPattern = /\[\s*\{\s*"(?:type|title|bullets)"[\s\S]*?\}\s*(?:,\s*\{[\s\S]*?\}\s*)*\]/gi;

  // Pattern 4: Tool/router JSON blobs leaking into the answer
  const toolMetaJsonPattern = /^\s*\{\s*"(?:relevance|tool_name|confidence|guidance|citations|queries|results_count|query_language|cross_language_enabled)"[\s\S]*?\}\s*(?:\n|$)/i;

  let filtered = text
    .replace(jsonCodeBlockPattern, '')
    .replace(rawJsonPattern, '')
    .replace(slidesArrayPattern, '')
    .replace(toolMetaJsonPattern, '')
    .replace(/^(CONFIDENCE|GUIDANCE):.*$/gim, '');

  // Clean up excessive newlines left behind
  filtered = filtered.replace(/\n{3,}/g, '\n\n').trim();

  return filtered;
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
  id = "msg"
}: StreamOutputProps) {
  // Enable LaTeX copy support - copies original LaTeX source when selecting formulas
  useLatexCopy();

  // Filter out accidental JSON tool output before parsing
  const filteredText = useMemo(() => filterToolJsonOutput(text), [text]);

  // Parse markdown into blocks for memoization, preserving global definitions
  const { blocks, definitions } = useMemo(() => parseMarkdownIntoBlocks(filteredText), [filteredText]);

  if (!text) return null;

  return (
    <div className="prose prose-sm max-w-none dark:prose-invert break-words prose-p:my-2 prose-headings:my-3 prose-ul:my-2 prose-ol:my-2 prose-li:my-0.5 prose-pre:my-2 prose-pre:overflow-x-auto prose-pre:max-w-full prose-pre:whitespace-pre-wrap prose-pre:break-words prose-code:whitespace-pre-wrap prose-code:break-words">
      {blocks.map((block, index) => (
        <MemoizedMarkdownBlock
          key={`${id}-block-${index}`}
          content={block}
          definitions={definitions}
        />
      ))}
      {/* Streaming cursor with thinking animation */}
      {isStreaming && (
        <span className="inline-flex items-center gap-1 ml-1 align-text-bottom">
          {/* Animated cursor bar */}
          <span
            className="inline-block w-0.5 h-4 bg-violet-500 rounded-sm"
            style={{
              animation: 'cursor-blink 1s ease-in-out infinite',
            }}
          />
          {/* Pulsing dots for "thinking" effect */}
          <span className="inline-flex gap-0.5 ml-0.5">
            <span
              className="w-1 h-1 rounded-full bg-violet-400"
              style={{
                animation: 'thinking-dot 1.4s ease-in-out infinite',
                animationDelay: '0s',
              }}
            />
            <span
              className="w-1 h-1 rounded-full bg-purple-400"
              style={{
                animation: 'thinking-dot 1.4s ease-in-out infinite',
                animationDelay: '0.2s',
              }}
            />
            <span
              className="w-1 h-1 rounded-full bg-fuchsia-400"
              style={{
                animation: 'thinking-dot 1.4s ease-in-out infinite',
                animationDelay: '0.4s',
              }}
            />
          </span>
          <style>{`
            @keyframes cursor-blink {
              0%, 100% { opacity: 1; }
              50% { opacity: 0.3; }
            }
            @keyframes thinking-dot {
              0%, 100% { transform: scale(1); opacity: 0.5; }
              50% { transform: scale(1.3); opacity: 1; }
            }
          `}</style>
        </span>
      )}
    </div>
  );
});
