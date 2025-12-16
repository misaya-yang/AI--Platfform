import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function StreamOutput({ text }: { text: string }) {
  return (
    <div className="prose prose-sm max-w-none dark:prose-invert">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}

