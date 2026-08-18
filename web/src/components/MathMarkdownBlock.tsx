import type { ReactNode } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";

const REMARK_PLUGINS = [remarkGfm, [remarkMath, { singleDollarTextMath: true }]] as const;
const REHYPE_PLUGINS = [
  [rehypeKatex, { throwOnError: false, strict: false, output: "htmlAndMathml" }],
] as const;

function allowDataUrlTransform(url: string): string {
  return url.startsWith("data:image/") ? url : defaultUrlTransform(url);
}

export function MathMarkdownBlock({
  text,
  components,
}: {
  text: string;
  components: {
    img: (props: { src?: string; alt?: string }) => ReactNode;
    a: (props: { href?: string; children?: ReactNode }) => ReactNode;
  };
}) {
  return (
    <ReactMarkdown
      remarkPlugins={REMARK_PLUGINS}
      rehypePlugins={REHYPE_PLUGINS}
      urlTransform={allowDataUrlTransform}
      components={components}
    >
      {text}
    </ReactMarkdown>
  );
}
