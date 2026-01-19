import { memo, useMemo, useState, type ReactNode } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { marked } from "marked";
import { ImageIcon, Download, ExternalLink, FileDown } from "lucide-react";
import "katex/dist/katex.min.css";

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

  const isBase64 = src?.startsWith("data:");
  const displayAlt = alt || "Generated Image";

  // Download base64 image
  const handleDownload = () => {
    if (!src) return;

    const link = document.createElement("a");
    link.href = src;
    link.download = `${displayAlt.replace(/\s+/g, "_")}_${Date.now()}.png`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  // Open in new tab
  const handleOpenInNewTab = () => {
    if (!src) return;
    const newWindow = window.open();
    if (newWindow) {
      newWindow.document.write(`
        <html>
          <head><title>${displayAlt}</title></head>
          <body style="margin:0;display:flex;justify-content:center;align-items:center;min-height:100vh;background:#1a1a1a;">
            <img src="${src}" alt="${displayAlt}" style="max-width:100%;max-height:100vh;object-fit:contain;" />
          </body>
        </html>
      `);
    }
  };

  if (hasError) {
    return (
      <span className="flex flex-col items-center justify-center p-6 my-3 rounded-xl bg-slate-100 dark:bg-slate-800 border border-slate-200 dark:border-slate-700" style={{ display: 'flex' }}>
        <ImageIcon className="h-10 w-10 text-slate-400 mb-2" />
        <span className="text-sm text-slate-500">图片加载失败</span>
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

      {/* Hover actions for base64 images */}
      {!isLoading && isBase64 && (
        <span className="absolute top-2 right-2 flex gap-1.5 opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            onClick={handleDownload}
            className="p-1.5 rounded-lg bg-black/50 hover:bg-black/70 text-white backdrop-blur-sm transition-colors"
            title="下载图片"
          >
            <Download className="h-4 w-4" />
          </button>
          <button
            onClick={handleOpenInNewTab}
            className="p-1.5 rounded-lg bg-black/50 hover:bg-black/70 text-white backdrop-blur-sm transition-colors"
            title="在新标签页打开"
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
          [rehypeKatex, { throwOnError: false, strict: false }],
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
  // Parse markdown into blocks for memoization, preserving global definitions
  const { blocks, definitions } = useMemo(() => parseMarkdownIntoBlocks(text), [text]);

  if (!text) return null;

  return (
    <div className="prose prose-sm max-w-none dark:prose-invert prose-p:my-2 prose-headings:my-3 prose-ul:my-2 prose-ol:my-2 prose-li:my-0.5 prose-pre:my-2">
      {blocks.map((block, index) => (
        <MemoizedMarkdownBlock
          key={`${id}-block-${index}`}
          content={block}
          definitions={definitions}
        />
      ))}
      {/* Streaming cursor */}
      {isStreaming && (
        <span className="inline-block w-1.5 h-4 ml-0.5 bg-primary/60 animate-pulse rounded-sm align-text-bottom" />
      )}
    </div>
  );
});
