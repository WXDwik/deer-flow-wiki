"""LLM Wiki 文件型仓储。

这个模块负责读写 `.llm-wiki/` 下的 JSON 状态文件。
当前先使用文件作为最小可用实现；以后如果需要 SQL，可以在这里再抽接口。
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from deerflow.wiki.paths import WikiPaths


class WikiRepository:
    """基于 `.llm-wiki/*.json` 的轻量仓储。"""

    def __init__(self, paths: WikiPaths) -> None:
        self.paths = paths

    def read_json(self, path: Path, default: dict | None = None) -> dict:
        """读取 JSON 文件；不存在时返回 default。"""
        if not path.exists():
            return dict(default or {})
        return json.loads(path.read_text(encoding="utf-8") or "{}")

    def write_json(self, path: Path, data: dict) -> None:
        """写入 JSON 文件，自动创建父目录。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def read_config(self) -> dict:
        """读取当前 wiki 配置。"""
        return self.read_json(self.paths.llm_wiki_config_file, {})

    def write_config(self, data: dict) -> None:
        """写入当前 wiki 配置。"""
        self.write_json(self.paths.llm_wiki_config_file, data)

    def read_index(self) -> dict:
        """读取 source/page 索引。"""
        return self.read_json(self.paths.llm_wiki_index_file, {"sources": [], "pages": []})

    def write_index(self, data: dict) -> None:
        """写入 source/page 索引。"""
        data.setdefault("sources", [])
        data.setdefault("pages", [])
        self.write_json(self.paths.llm_wiki_index_file, data)

    def read_queue(self) -> dict:
        """读取导入队列。"""
        return self.read_json(self.paths.llm_wiki_queue_file, {"jobs": []})

    def write_queue(self, data: dict) -> None:
        """写入导入队列。"""
        data.setdefault("jobs", [])
        self.write_json(self.paths.llm_wiki_queue_file, data)

    def read_reviews(self) -> dict:
        """读取人工审核项。"""
        return self.read_json(self.paths.llm_wiki_reviews_file, {"items": []})

    def write_reviews(self, data: dict) -> None:
        """写入人工审核项。"""
        data.setdefault("items", [])
        self.write_json(self.paths.llm_wiki_reviews_file, data)

    def add_source(self, source: Any) -> None:
        """向 index.json 追加或更新一个 source。"""
        index = self.read_index()
        payload = asdict(source) if is_dataclass(source) else dict(source)
        sources = [item for item in index.get("sources", []) if item.get("source_id") != payload.get("source_id")]
        sources.append(payload)
        index["sources"] = sources
        self.write_index(index)

    def add_page(self, page: Any) -> None:
        """向 index.json 追加或更新一个 wiki page。"""
        index = self.read_index()
        payload = asdict(page) if is_dataclass(page) else dict(page)
        pages = [item for item in index.get("pages", []) if item.get("page_id") != payload.get("page_id")]
        pages.append(payload)
        index["pages"] = pages
        self.write_index(index)
