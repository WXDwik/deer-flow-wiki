"""LLM Wiki Markdown 辅助函数。

这里放和 Markdown 文本格式有关的纯函数：
- 生成 YAML frontmatter
- 生成 Obsidian 风格 wikilink
- 生成最小资料摘要页
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any


def wikilink(title: str, label: str | None = None) -> str:
    """生成 Obsidian 风格链接。"""
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("wikilink title cannot be empty.")
    if label:
        return f"[[{clean_title}|{label.strip()}]]"
    return f"[[{clean_title}]]"


def yaml_scalar(value: Any) -> str:
    """把简单 Python 值转换成 YAML 标量。

    这里先实现最小版本，避免引入新的 YAML 写入依赖。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).replace('"', '\\"')
    return f'"{text}"'


def frontmatter(data: dict[str, Any]) -> str:
    """生成 Markdown 页面顶部的 YAML frontmatter。"""
    lines = ["---"]
    for key, value in data.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {yaml_scalar(item)}")
        else:
            lines.append(f"{key}: {yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def extract_wikilinks(markdown: str) -> list[str]:
    """从 Markdown 中提取 `[[wikilink]]` 链接目标。"""
    links = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", markdown)
    return sorted({link.strip() for link in links if link.strip()})


def build_source_summary_markdown(
    *,
    title: str,
    source_path: str,
    summary: str = "待生成资料摘要。",
    tags: list[str] | None = None,
) -> str:
    """生成一个最小可用的资料摘要页。

    后续接入 LLM 后，summary 可以替换成模型生成的结构化内容。
    """
    now = datetime.now(UTC).isoformat()
    header = frontmatter(
        {
            "type": "source",
            "title": title,
            "sources": [source_path],
            "tags": tags or [],
            "created_at": now,
            "updated_at": now,
        }
    )
    return f"""{header}

# {title}

## Summary

{summary}

## Key Points

- 待补充。

## Source

- `{source_path}`
"""
