import { useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { ContentItem } from "@/types/gateway";
import { Paperclip, Send } from "lucide-react";

export function MultimodalInput({
  onSend,
  disabled,
  includeFiles = false,
}: {
  onSend: (inputs: ContentItem[]) => void;
  disabled?: boolean;
  includeFiles?: boolean;
}) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const fileRef = useRef<HTMLInputElement | null>(null);

  async function handleSend() {
    const inputs: ContentItem[] = [];
    if (text.trim()) {
      inputs.push({ type: "text", data: text.trim() });
    }
    // 附件 UI 先做出来，但默认不发送到后端
    if (includeFiles) {
      for (const file of files) {
        inputs.push({
          type: file.type.startsWith("image/") ? "image" : "file",
          url: undefined,
          data: undefined,
          mime_type: file.type,
          metadata: { filename: file.name },
        });
      }
    }
    onSend(inputs);
    setText("");
    setFiles([]);
  }

  return (
    <div className="w-full">
      {files.length > 0 && (
        <div className="px-4 pt-3 pb-2 flex flex-wrap gap-2 border-b border-border/50">
          {files.map((f) => (
            <div
              key={f.name}
              className="rounded-lg bg-muted px-2.5 py-1 text-xs text-muted-foreground"
            >
              {f.name}
            </div>
          ))}
        </div>
      )}
      <div className="flex items-end gap-2 p-3">
        <input
          ref={fileRef}
          type="file"
          multiple
          className="hidden"
          disabled={disabled}
          onChange={(e) =>
            setFiles(Array.from(e.target.files || []).slice(0, 3))
          }
        />
        <Button
          variant="ghost"
          size="icon"
          type="button"
          disabled={disabled}
          onClick={() => fileRef.current?.click()}
          aria-label="添加附件"
          className="h-10 w-10 shrink-0 text-muted-foreground hover:text-foreground"
        >
          <Paperclip className="h-5 w-5" />
        </Button>
        <Textarea
          placeholder="输入消息..."
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={1}
          disabled={disabled}
          className="min-h-[44px] max-h-[200px] resize-none border-0 bg-transparent focus-visible:ring-0 text-base placeholder:text-muted-foreground/60"
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              if (!disabled && (text.trim() || files.length)) handleSend();
            }
          }}
        />
        <Button
          size="icon"
          type="button"
          disabled={disabled || (!text.trim() && files.length === 0)}
          onClick={handleSend}
          aria-label="发送"
          className="h-10 w-10 shrink-0 rounded-xl bg-primary hover:bg-primary/90 shadow-md shadow-primary/25"
        >
          <Send className="h-5 w-5" />
        </Button>
      </div>
    </div>
  );
}
