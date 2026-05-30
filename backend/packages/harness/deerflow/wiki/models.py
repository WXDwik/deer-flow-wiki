"""LLM Wiki 领域数据模型。

这些模型描述 wiki 内部的数据结构，不直接处理文件读写。
后续 tools、service、repository 都应该优先使用这些模型传递数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

PageType = Literal[
    "source",
    "background",
    "idea",
    "system_model",
    "algorithm",
    "dataset",
    "summary",
    "concept",
    "synthesis",
    # Legacy aliases kept for older wiki data and callers.
    "entity",
    "query",
    "comparison",
    "deepresearch",
]
IngestStatus = Literal["pending", "processing", "completed", "failed", "skipped"]
ArchiveAction = Literal["none", "create_page", "update_existing", "create_and_update"]


@dataclass
class RawSource:
    """raw/sources/ 下的一份原始资料。"""

    source_id: str
    title: str
    path: str
    sha256: str
    mime_type: str | None = None
    size_bytes: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WikiPage:
    """wiki/ 下的一篇 Markdown 页面。"""

    page_id: str
    title: str
    path: str
    page_type: PageType
    sources: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestJob:
    """一次资料导入任务。"""

    job_id: str
    source_path: str
    status: IngestStatus = "pending"
    attempts: int = 0
    error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class ReviewItem:
    """需要人工判断的事项。"""

    item_id: str
    title: str
    reason: str
    action: str
    status: str = "open"
    created_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WikiCitation:
    """A wiki page cited by a generated answer."""

    title: str
    path: str


@dataclass
class WikiContextPage:
    """A searched wiki page with enough content for answer generation."""

    title: str
    path: str
    score: int
    snippet: str
    content: str
    page_type: str | None = None


@dataclass
class AnswerDraft:
    """The conversational answer generated for the current user question."""

    answer_markdown: str
    citations: list[WikiCitation] = field(default_factory=list)


@dataclass
class ArchiveDecision:
    """Decision about whether and how a query answer should be written back."""

    should_archive: bool
    reason: str
    action: ArchiveAction = "none"
    page_type: PageType | None = None
    suggested_title: str | None = None
    target_pages: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    cited_pages: list[str] = field(default_factory=list)


@dataclass
class ArchiveDraft:
    """A new wiki page draft derived from a high-value answer."""

    title: str
    page_type: PageType
    markdown: str
    tags: list[str] = field(default_factory=list)
    cited_pages: list[str] = field(default_factory=list)


@dataclass
class WikiPageChange:
    """A local Markdown edit under wiki/."""

    path: str
    operation: Literal["replace", "append", "rewrite"]
    reason: str = ""
    old: str | None = None
    new: str | None = None
    content: str | None = None


@dataclass
class QueryAnswer:
    """Full result of answering a question against a wiki."""

    question: str
    answer_markdown: str
    citations: list[WikiCitation] = field(default_factory=list)
    archive_decision: ArchiveDecision | None = None
    archived_page: WikiPage | None = None
    page_changes: list[WikiPageChange] = field(default_factory=list)
    archive_applied: bool = False
