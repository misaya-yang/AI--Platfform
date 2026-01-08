import { memo, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { marked } from "marked";

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
      <ReactMarkdown remarkPlugins={[remarkGfm]}>
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
