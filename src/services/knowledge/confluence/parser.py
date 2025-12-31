"""
Confluence Storage Format Parser.

Converts Confluence Storage Format (XHTML-like) to plain text or Markdown
for use in knowledge base vector embedding.

Storage Format is Confluence's internal representation of page content,
containing standard HTML elements plus Confluence-specific macros prefixed
with "ac:" namespace.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from io import StringIO
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class StorageFormatParser(HTMLParser):
    """
    Confluence Storage Format (XHTML) 解析器

    将 Confluence 的存储格式转换为 Markdown 或纯文本。

    支持的元素：
    - 标准 HTML: h1-h6, p, br, strong, em, code, pre, ul, ol, li, a, table, blockquote
    - Confluence 宏: ac:structured-macro (code, info, warning, note, expand, toc)
    """

    def __init__(self, output_format: str = "markdown"):
        """
        初始化解析器

        Args:
            output_format: 输出格式 ("markdown" | "text")
        """
        super().__init__()
        self.output = StringIO()
        self.output_format = output_format

        # 状态跟踪
        self.in_code_block = False
        self.code_language = ""
        self.list_stack: List[str] = []  # 用于跟踪嵌套列表
        self.current_depth = 0
        self.in_table = False
        self.table_row: List[str] = []
        self.in_link = False
        self.current_link_href = ""

        # 要忽略的标签
        self.ignore_tags = {"ac:placeholder", "ac:parameter", "ri:attachment", "ri:page"}
        self.in_ignored_tag = 0

        # 当前宏信息
        self.current_macro = ""
        self.macro_params: Dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        attrs_dict = {k: v for k, v in attrs if v is not None}

        # 忽略某些标签
        if tag in self.ignore_tags:
            self.in_ignored_tag += 1
            return

        if self.in_ignored_tag > 0:
            return

        # 处理 Confluence 宏
        if tag.startswith("ac:"):
            self._handle_macro_start(tag, attrs_dict)
            return

        # 标准 HTML 标签处理
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            if self.output_format == "markdown":
                self.output.write("\n\n" + "#" * level + " ")
            else:
                self.output.write("\n\n")

        elif tag == "p":
            self.output.write("\n\n")

        elif tag == "br":
            self.output.write("\n")

        elif tag in ("strong", "b"):
            if self.output_format == "markdown":
                self.output.write("**")

        elif tag in ("em", "i"):
            if self.output_format == "markdown":
                self.output.write("*")

        elif tag == "code" and not self.in_code_block:
            if self.output_format == "markdown":
                self.output.write("`")

        elif tag == "pre":
            self.in_code_block = True
            if self.output_format == "markdown":
                self.output.write("\n```")
                if self.code_language:
                    self.output.write(self.code_language)
                self.output.write("\n")
            else:
                self.output.write("\n")

        elif tag == "ul":
            self.list_stack.append("ul")
            self.current_depth += 1

        elif tag == "ol":
            self.list_stack.append("ol")
            self.current_depth += 1

        elif tag == "li":
            indent = "  " * (self.current_depth - 1)
            if self.output_format == "markdown":
                if self.list_stack and self.list_stack[-1] == "ol":
                    self.output.write(f"\n{indent}1. ")
                else:
                    self.output.write(f"\n{indent}- ")
            else:
                self.output.write(f"\n{indent}* ")

        elif tag == "a":
            self.in_link = True
            self.current_link_href = attrs_dict.get("href", "")
            if self.output_format == "markdown":
                self.output.write("[")

        elif tag == "table":
            self.in_table = True
            self.output.write("\n\n")

        elif tag == "tr":
            self.table_row = []

        elif tag in ("th", "td"):
            pass  # 内容在 handle_data 中处理

        elif tag == "blockquote":
            if self.output_format == "markdown":
                self.output.write("\n> ")
            else:
                self.output.write("\n")

        elif tag == "hr":
            if self.output_format == "markdown":
                self.output.write("\n\n---\n\n")
            else:
                self.output.write("\n\n")

    def handle_endtag(self, tag: str):
        if tag in self.ignore_tags:
            self.in_ignored_tag = max(0, self.in_ignored_tag - 1)
            return

        if self.in_ignored_tag > 0:
            return

        # 处理 Confluence 宏结束
        if tag.startswith("ac:"):
            self._handle_macro_end(tag)
            return

        if tag in ("strong", "b"):
            if self.output_format == "markdown":
                self.output.write("**")

        elif tag in ("em", "i"):
            if self.output_format == "markdown":
                self.output.write("*")

        elif tag == "code" and not self.in_code_block:
            if self.output_format == "markdown":
                self.output.write("`")

        elif tag == "pre":
            self.in_code_block = False
            if self.output_format == "markdown":
                self.output.write("\n```\n")
            else:
                self.output.write("\n")
            self.code_language = ""

        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
            self.current_depth = max(0, self.current_depth - 1)
            if self.current_depth == 0:
                self.output.write("\n")

        elif tag == "a":
            if self.output_format == "markdown" and self.current_link_href:
                self.output.write(f"]({self.current_link_href})")
            self.in_link = False
            self.current_link_href = ""

        elif tag == "tr":
            if self.table_row:
                self.output.write("| " + " | ".join(self.table_row) + " |\n")
            self.table_row = []

        elif tag == "table":
            self.in_table = False

    def handle_data(self, data: str):
        if self.in_ignored_tag > 0:
            return

        # 在代码块中保留原始格式
        if self.in_code_block:
            self.output.write(data)
            return

        # 在表格单元格中
        if self.in_table and self.table_row is not None:
            cleaned = " ".join(data.split())
            if cleaned:
                self.table_row.append(cleaned)
            return

        # 清理空白
        cleaned = " ".join(data.split())
        if cleaned:
            self.output.write(cleaned)

    def _handle_macro_start(self, tag: str, attrs: Dict[str, str]):
        """处理 Confluence 宏开始标签"""
        if tag == "ac:structured-macro":
            self.current_macro = attrs.get("ac:name", "")

            # 根据宏类型添加前缀
            if self.current_macro == "code":
                # 代码宏会在遇到 ac:plain-text-body 时处理
                pass
            elif self.current_macro == "info":
                if self.output_format == "markdown":
                    self.output.write("\n> **Info:** ")
                else:
                    self.output.write("\n[Info] ")
            elif self.current_macro == "warning":
                if self.output_format == "markdown":
                    self.output.write("\n> **Warning:** ")
                else:
                    self.output.write("\n[Warning] ")
            elif self.current_macro == "note":
                if self.output_format == "markdown":
                    self.output.write("\n> **Note:** ")
                else:
                    self.output.write("\n[Note] ")
            elif self.current_macro == "tip":
                if self.output_format == "markdown":
                    self.output.write("\n> **Tip:** ")
                else:
                    self.output.write("\n[Tip] ")
            elif self.current_macro == "expand":
                # 展开面板，正常处理内容
                pass
            elif self.current_macro == "toc":
                # 目录宏，忽略
                self.in_ignored_tag += 1
            elif self.current_macro == "panel":
                self.output.write("\n")

        elif tag == "ac:parameter":
            param_name = attrs.get("ac:name", "")
            self.macro_params[param_name] = ""

        elif tag == "ac:plain-text-body":
            # 代码块内容
            if self.current_macro == "code":
                lang = self.macro_params.get("language", "")
                if self.output_format == "markdown":
                    self.output.write(f"\n```{lang}\n")
                else:
                    self.output.write("\n")
                self.in_code_block = True

        elif tag == "ac:rich-text-body":
            # 富文本内容，正常处理
            pass

    def _handle_macro_end(self, tag: str):
        """处理 Confluence 宏结束标签"""
        if tag == "ac:structured-macro":
            if self.current_macro == "toc":
                self.in_ignored_tag = max(0, self.in_ignored_tag - 1)
            self.current_macro = ""
            self.macro_params = {}

        elif tag == "ac:plain-text-body":
            if self.in_code_block:
                self.in_code_block = False
                if self.output_format == "markdown":
                    self.output.write("\n```\n")
                else:
                    self.output.write("\n")

    def get_output(self) -> str:
        """获取解析结果"""
        result = self.output.getvalue()

        # 清理多余空行
        result = re.sub(r"\n{3,}", "\n\n", result)

        # 清理行首尾空白
        lines = [line.strip() for line in result.split("\n")]
        result = "\n".join(lines)

        return result.strip()


def parse_storage_format(content: str, output_format: str = "markdown") -> str:
    """
    解析 Confluence Storage Format

    Args:
        content: Confluence 存储格式内容 (XHTML)
        output_format: 输出格式 ("markdown" | "text")

    Returns:
        转换后的内容
    """
    if not content:
        return ""

    try:
        parser = StorageFormatParser(output_format=output_format)
        parser.feed(content)
        result = parser.get_output()
        return result
    except Exception as e:
        logger.warning(f"Failed to parse storage format: {e}")
        # 降级处理：去除所有 HTML 标签
        result = re.sub(r"<[^>]+>", " ", content)
        result = re.sub(r"\s+", " ", result)
        return result.strip()


def extract_plain_text(content: str) -> str:
    """
    提取纯文本 (用于向量化)

    移除所有格式，只保留纯文本内容。

    Args:
        content: Confluence 存储格式内容

    Returns:
        纯文本内容
    """
    result = parse_storage_format(content, output_format="text")

    # 进一步清理 Markdown 语法残留
    result = re.sub(r"[#*`>\[\]()]", " ", result)
    result = re.sub(r"\s+", " ", result)

    return result.strip()


def extract_markdown(content: str) -> str:
    """
    提取 Markdown 格式 (用于展示)

    保留基本格式，便于阅读。

    Args:
        content: Confluence 存储格式内容

    Returns:
        Markdown 格式内容
    """
    return parse_storage_format(content, output_format="markdown")


def extract_headings(content: str) -> List[Dict[str, str]]:
    """
    提取标题结构

    用于生成文档大纲或分块参考。

    Args:
        content: Confluence 存储格式内容

    Returns:
        标题列表，每个包含 level 和 text
    """
    headings = []

    # 匹配 h1-h6 标签
    pattern = r"<h([1-6])[^>]*>(.*?)</h\1>"
    for match in re.finditer(pattern, content, re.IGNORECASE | re.DOTALL):
        level = int(match.group(1))
        text = re.sub(r"<[^>]+>", "", match.group(2))  # 去除内部标签
        text = " ".join(text.split())  # 清理空白
        if text:
            headings.append({"level": level, "text": text})

    return headings


def estimate_reading_time(content: str, words_per_minute: int = 200) -> int:
    """
    估算阅读时间

    Args:
        content: Confluence 存储格式内容
        words_per_minute: 每分钟阅读字数

    Returns:
        预计阅读时间（分钟）
    """
    text = extract_plain_text(content)

    # 计算字数（考虑中文）
    # 英文按空格分词，中文按字符计数
    english_words = len(re.findall(r"[a-zA-Z]+", text))
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))

    total_words = english_words + chinese_chars

    minutes = max(1, total_words // words_per_minute)
    return minutes
