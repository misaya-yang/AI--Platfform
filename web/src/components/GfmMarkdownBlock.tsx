import type { ReactNode } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

const REMARK_PLUGINS = [remarkGfm] as const;

function allowDataUrlTransform(url: string): string {
  return url.startsWith("data:image/") ? url : defaultUrlTransform(url);
}

export function GfmMarkdownBlock({
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
    <ReactMarkdown remarkPlugins={REMARK_PLUGINS} urlTransform={allowDataUrlTransform} components={components}>
      {text}
    </ReactMarkdown>
  );
}
