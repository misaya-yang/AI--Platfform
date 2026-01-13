/**
 * Constants for the Assistant page.
 */

import { BookOpen, Code, FileQuestion, TrendingUp, Lightbulb, Brain } from "lucide-react";
import type { SuggestedPrompt } from "./types";

export const SUGGESTED_PROMPTS: SuggestedPrompt[] = [
  {
    icon: <BookOpen className="h-5 w-5" />,
    title: "解释概念",
    prompt: "请用简单易懂的语言解释",
    category: "学习",
  },
  {
    icon: <Code className="h-5 w-5" />,
    title: "代码帮助",
    prompt: "帮我编写一个函数来实现",
    category: "开发",
  },
  {
    icon: <FileQuestion className="h-5 w-5" />,
    title: "文档分析",
    prompt: "分析这份文档的主要内容和关键信息",
    category: "分析",
  },
  {
    icon: <TrendingUp className="h-5 w-5" />,
    title: "数据洞察",
    prompt: "基于这些数据，提供关键洞察和建议",
    category: "分析",
  },
  {
    icon: <Lightbulb className="h-5 w-5" />,
    title: "创意头脑风暴",
    prompt: "帮我头脑风暴一些关于",
    category: "创意",
  },
  {
    icon: <Brain className="h-5 w-5" />,
    title: "问题解决",
    prompt: "帮我分析并解决这个问题：",
    category: "分析",
  },
];
