"""LLM Wiki 项目脚手架。

这个模块负责创建一个完整的空 wiki 库：
- 创建目录结构
- 创建默认 Markdown 文件
- 创建 `.llm-wiki/` 内部状态文件
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from deerflow.wiki.paths import WikiPaths, ensure_wiki_layout, resolve_wiki_root
from deerflow.wiki.templates import (
    default_config,
    default_index,
    default_index_json,
    default_log,
    default_overview,
    default_purpose,
    default_queue_json,
    default_reviews_json,
    default_schema,
)


def write_text_if_missing(path: Path, content: str) -> None:
    """只在文件不存在时写入，避免覆盖用户已经编辑过的内容。"""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def write_json_if_missing(path: Path, data: dict) -> None:
    """只在 JSON 文件不存在时写入默认内容。"""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def create_wiki_database(
    wiki_name_or_path: str,
    *,
    title: str | None = None,
    language: str = "zh-CN",
) -> WikiPaths:
    """创建一个完整的 LLM Wiki 库。"""
    wiki_root = resolve_wiki_root(wiki_name_or_path)
    paths = ensure_wiki_layout(wiki_root)

    wiki_title = title or paths.root.name
    now = datetime.now(UTC).isoformat()

    write_text_if_missing(paths.purpose_file, default_purpose(wiki_title, language))
    write_text_if_missing(paths.schema_file, default_schema(language))
    write_text_if_missing(paths.wiki_index_file, default_index(wiki_title))
    write_text_if_missing(paths.wiki_log_file, default_log(now))
    write_text_if_missing(paths.wiki_overview_file, default_overview(wiki_title))

    write_json_if_missing(paths.llm_wiki_config_file, default_config(wiki_title, language, now))
    write_json_if_missing(paths.llm_wiki_index_file, default_index_json())
    write_json_if_missing(paths.llm_wiki_queue_file, default_queue_json())
    write_json_if_missing(paths.llm_wiki_reviews_file, default_reviews_json())

    return paths
