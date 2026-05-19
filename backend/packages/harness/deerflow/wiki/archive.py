"""Archival write-back for wiki-grounded answers."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from deerflow.models import create_chat_model
from deerflow.wiki.markdown import frontmatter
from deerflow.wiki.models import (
    AnswerDraft,
    ArchiveDecision,
    ArchiveDraft,
    QueryAnswer,
    WikiCitation,
    WikiContextPage,
    WikiPage,
    WikiPageChange,
)
from deerflow.wiki.paths import (
    WikiPaths,
    comparison_page_path,
    concept_page_path,
    entity_page_path,
    query_page_path,
    synthesis_page_path,
)
from deerflow.wiki.repository import WikiRepository
from deerflow.wiki.schema import schema_context

_MAX_CONTEXT_PAGE_CHARS = 8_000
_ARCHIVE_PAGE_TYPES = {"query", "synthesis", "comparison", "concept", "entity"}
_ARCHIVE_ACTIONS = {"none", "create_page", "update_existing", "create_and_update"}
_UPDATE_OPERATIONS = {"replace", "append", "rewrite"}


def _json_from_model_text(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _page_type_from_markdown(text: str) -> str | None:
    for line in text.splitlines()[:20]:
        if line.startswith("type:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return None


def _model_text(response: object) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        return "\n".join(str(item) for item in content)
    return str(content)


def _context_pages_from_citations(paths: WikiPaths, citations: list[WikiCitation]) -> list[WikiContextPage]:
    """Read cited wiki pages for archive judgment/update planning."""
    pages: list[WikiContextPage] = []
    seen: set[str] = set()
    for citation in citations:
        if not citation.path or citation.path in seen:
            continue
        seen.add(citation.path)
        path = (paths.root / citation.path.strip().replace("\\", "/")).resolve()
        try:
            path.relative_to(paths.wiki_dir.resolve())
        except ValueError:
            continue
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        content = text[:_MAX_CONTEXT_PAGE_CHARS]
        pages.append(
            WikiContextPage(
                title=citation.title or path.stem,
                path=path.relative_to(paths.root).as_posix(),
                score=0,
                snippet="",
                content=content,
                page_type=_page_type_from_markdown(text),
            )
        )
    return pages


def judge_archive_value(
    paths: WikiPaths,
    question: str,
    answer: AnswerDraft,
    context_pages: list[WikiContextPage],
    *,
    model_name: str | None = None,
) -> ArchiveDecision:
    """Decide whether the answer should be archived and how it should be written."""
    prompt = f"""You decide whether a wiki-grounded answer should be written back to the wiki.

Decide the archival action:
- none: temporary, duplicate, insufficiently sourced, or not reusable.
- create_page: the answer has independent long-term value as a standalone query,
  synthesis, or comparison page.
- update_existing: the answer only adds a local correction or small supplement to
  existing entity/concept/synthesis pages.
- create_and_update: the answer deserves a new page and should also update related
  existing pages with a short summary/link.

Prefer create_and_update only when both are clearly useful. Use concept/entity
page types cautiously; most standalone answer archives should be query,
synthesis, or comparison.

Follow the active schema.md contract below when deciding whether and how to
write back:
{schema_context(paths)}

Return ONLY valid JSON:
{{
  "should_archive": true,
  "reason": "short reason",
  "action": "none|create_page|update_existing|create_and_update",
  "page_type": "query|synthesis|comparison|concept|entity|null",
  "suggested_title": "concise title or null",
  "target_pages": ["wiki/concepts/example.md"],
  "tags": ["archived-query"],
  "cited_pages": ["wiki/sources/source.md"]
}}

Question:
{question}

Answer:
{answer.answer_markdown}

Citations:
{json.dumps([asdict(citation) for citation in answer.citations], ensure_ascii=False, indent=2)}

Retrieved context pages:
{json.dumps([asdict(page) for page in context_pages], ensure_ascii=False, indent=2)}
"""
    model = create_chat_model(name=model_name, thinking_enabled=False)
    data = _json_from_model_text(_model_text(model.invoke(prompt, config={"run_name": "wiki_archive_decision"})))
    if data is None:
        raise ValueError("Wiki archive decision model did not return valid JSON")

    action = str(data.get("action") or "none").strip()
    if action not in _ARCHIVE_ACTIONS:
        action = "none"
    should_archive = bool(data.get("should_archive")) and action != "none"

    raw_page_type = data.get("page_type")
    page_type = str(raw_page_type).strip() if raw_page_type else None
    if page_type == "null" or page_type not in _ARCHIVE_PAGE_TYPES:
        page_type = None

    return ArchiveDecision(
        should_archive=should_archive,
        reason=str(data.get("reason") or "").strip(),
        action=action,  # type: ignore[arg-type]
        page_type=page_type,  # type: ignore[arg-type]
        suggested_title=str(data.get("suggested_title") or "").strip() or None,
        target_pages=[str(item).strip() for item in data.get("target_pages", []) if str(item).strip()],
        tags=[str(item).strip() for item in data.get("tags", []) if str(item).strip()],
        cited_pages=[str(item).strip() for item in data.get("cited_pages", []) if str(item).strip()],
    )


def _default_page_type(question: str, context_pages: list[WikiContextPage]) -> str:
    lowered = question.lower()
    if any(term in lowered for term in ("compare", "comparison", "vs", "versus", "对比", "比较")):
        return "comparison"
    if len(context_pages) >= 2:
        return "synthesis"
    return "query"


def build_archive_page(
    paths: WikiPaths,
    question: str,
    answer: AnswerDraft,
    decision: ArchiveDecision,
    context_pages: list[WikiContextPage],
    *,
    model_name: str | None = None,
) -> ArchiveDraft:
    """Condense a conversational answer into a standalone wiki page draft."""
    page_type = decision.page_type or _default_page_type(question, context_pages)
    title = decision.suggested_title or question.strip()[:80] or "Archived query"
    cited_pages = decision.cited_pages or [citation.path for citation in answer.citations]
    tags = sorted({"archived-query", *decision.tags})

    prompt = f"""Turn this answer into a concise wiki page.

The page should be useful when read later outside the original chat. Condense,
structure, and preserve source references as slug wikilinks or page paths. Do
not add unsupported facts. Wikilinks must target lowercase kebab-case page slugs
matching Markdown filenames without .md, for example [[rag]], not [[RAG]].

Follow the active schema.md contract below. Return Markdown body only: no YAML
frontmatter and no duplicate top-level title.
{schema_context(paths)}

Return ONLY valid JSON:
{{
  "title": "Final page title",
  "markdown_body": "Markdown body without YAML frontmatter and without top-level title"
}}

Page type: {page_type}
Suggested title: {title}
Question:
{question}

Answer:
{answer.answer_markdown}

Cited pages:
{json.dumps(cited_pages, ensure_ascii=False, indent=2)}
"""
    model = create_chat_model(name=model_name, thinking_enabled=False)
    data = _json_from_model_text(_model_text(model.invoke(prompt, config={"run_name": "wiki_archive_page"})))
    if data is not None:
        title = str(data.get("title") or title).strip() or title
        body = str(data.get("markdown_body") or "").strip()
    else:
        body = ""
    if not body:
        body = f"## Original Question\n\n{question}\n\n## Answer\n\n{answer.answer_markdown}"

    now = datetime.now(UTC).isoformat()
    header = frontmatter(
        {
            "type": page_type,
            "title": title,
            "sources": cited_pages,
            "tags": tags,
            "created_at": now,
            "updated_at": now,
        }
    )
    markdown = f"{header}\n\n# {title}\n\n## Original Question\n\n{question}\n\n{body.strip()}\n"
    return ArchiveDraft(
        title=title,
        page_type=page_type,  # type: ignore[arg-type]
        markdown=markdown,
        tags=tags,
        cited_pages=cited_pages,
    )


def _archive_page_path(paths: WikiPaths, draft: ArchiveDraft) -> Path:
    if draft.page_type == "query":
        return query_page_path(paths, draft.title)
    if draft.page_type == "synthesis":
        return synthesis_page_path(paths, draft.title)
    if draft.page_type == "comparison":
        return comparison_page_path(paths, draft.title)
    if draft.page_type == "concept":
        return concept_page_path(paths, draft.title)
    if draft.page_type == "entity":
        return entity_page_path(paths, draft.title)
    raise ValueError(f"Unsupported archive page type: {draft.page_type}")


def write_archive_page(paths: WikiPaths, draft: ArchiveDraft) -> WikiPage:
    """Write a new archive Markdown page and register it in index.json."""
    path = _archive_page_path(paths, draft)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(draft.markdown, encoding="utf-8")
    now = datetime.now(UTC).isoformat()
    page = WikiPage(
        page_id=uuid4().hex,
        title=draft.title,
        path=path.relative_to(paths.root).as_posix(),
        page_type=draft.page_type,
        sources=draft.cited_pages,
        tags=draft.tags,
        created_at=now,
        updated_at=now,
    )
    WikiRepository(paths).add_page(page)
    return page


def _allowed_wiki_markdown_path(paths: WikiPaths, raw_path: str) -> Path:
    rel = raw_path.strip().replace("\\", "/")
    if not rel:
        raise ValueError("Wiki update path cannot be empty")
    if not rel.startswith("wiki/"):
        rel = f"wiki/{rel}"
    path = (paths.root / rel).resolve()
    try:
        path.relative_to(paths.wiki_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"Archive updates may only edit files under wiki/: {raw_path}") from exc
    if path.suffix.lower() != ".md":
        raise ValueError(f"Archive updates may only edit Markdown files: {raw_path}")
    return path


def build_existing_page_updates(
    paths: WikiPaths,
    question: str,
    answer: AnswerDraft,
    decision: ArchiveDecision,
    context_pages: list[WikiContextPage],
    archived_page: WikiPage | None,
    *,
    model_name: str | None = None,
) -> list[WikiPageChange]:
    """Ask the model for precise local edits to existing wiki pages."""
    target_pages = decision.target_pages or [page.path for page in context_pages if page.page_type in {"concept", "entity", "synthesis"}]
    target_pages = sorted(dict.fromkeys(target_pages))
    context: dict[str, str] = {}
    for rel in target_pages:
        try:
            path = _allowed_wiki_markdown_path(paths, rel)
        except ValueError:
            continue
        if path.is_file():
            context[path.relative_to(paths.root).as_posix()] = path.read_text(encoding="utf-8", errors="ignore")[:_MAX_CONTEXT_PAGE_CHARS]

    if not context:
        return []

    prompt = f"""Plan local Markdown edits that integrate a new wiki-grounded answer.

Use small, precise edits. Prefer append for adding a short note or link. Use
replace only when old text is an exact substring. Avoid full rewrite unless the
target page is very small and clearly needs it.
When adding wikilinks, use lowercase kebab-case page slugs matching Markdown
filenames without .md; do not write page-title wikilinks.

Follow the active schema.md contract below:
{schema_context(paths)}

Return ONLY valid JSON:
{{
  "changes": [
    {{
      "path": "wiki/concepts/example.md",
      "operation": "append|replace|rewrite",
      "reason": "why this page changes",
      "content": "for append/rewrite",
      "old": "for replace",
      "new": "for replace"
    }}
  ]
}}

Question:
{question}

Answer:
{answer.answer_markdown}

Archived page:
{json.dumps(asdict(archived_page) if archived_page else None, ensure_ascii=False, indent=2)}

Allowed target file context:
{json.dumps(context, ensure_ascii=False, indent=2)}
"""
    model = create_chat_model(name=model_name, thinking_enabled=False)
    data = _json_from_model_text(_model_text(model.invoke(prompt, config={"run_name": "wiki_archive_updates"})))
    if data is None:
        raise ValueError("Wiki archive update model did not return valid JSON")
    changes: list[WikiPageChange] = []
    for item in data.get("changes", []):
        if not isinstance(item, dict):
            continue
        raw_path = str(item.get("path") or "").strip()
        operation = str(item.get("operation") or "").strip().lower()
        if not raw_path or operation not in _UPDATE_OPERATIONS:
            continue
        path = _allowed_wiki_markdown_path(paths, raw_path)
        rel = path.relative_to(paths.root).as_posix()
        changes.append(
            WikiPageChange(
                path=rel,
                operation=operation,  # type: ignore[arg-type]
                reason=str(item.get("reason") or ""),
                old=item.get("old") if isinstance(item.get("old"), str) else None,
                new=item.get("new") if isinstance(item.get("new"), str) else None,
                content=item.get("content") if isinstance(item.get("content"), str) else None,
            )
        )
    return changes


def apply_page_changes(paths: WikiPaths, changes: list[WikiPageChange]) -> None:
    """Apply local wiki Markdown edits with simple path and operation validation."""
    for change in changes:
        path = _allowed_wiki_markdown_path(paths, change.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if change.operation == "rewrite":
            if change.content is None:
                raise ValueError(f"Rewrite change requires content: {change.path}")
            path.write_text(change.content, encoding="utf-8")
            continue

        current = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
        if change.operation == "append":
            if change.content is None:
                raise ValueError(f"Append change requires content: {change.path}")
            separator = "\n" if current.endswith("\n") or not current else "\n\n"
            path.write_text(f"{current}{separator}{change.content}", encoding="utf-8")
            continue

        if change.old is None or change.new is None or not change.old:
            raise ValueError(f"Replace change requires old and new text: {change.path}")
        if change.old not in current:
            raise ValueError(f"Replace text not found in {change.path}")
        path.write_text(current.replace(change.old, change.new, 1), encoding="utf-8")


def update_index_markdown(paths: WikiPaths, archived_page: WikiPage | None) -> None:
    """Add a simple link to wiki/index.md for newly archived pages."""
    if archived_page is None:
        return
    paths.wiki_index_file.parent.mkdir(parents=True, exist_ok=True)
    current = paths.wiki_index_file.read_text(encoding="utf-8", errors="ignore") if paths.wiki_index_file.exists() else "# Wiki Index\n"
    section = {
        "query": "## Queries",
        "synthesis": "## Synthesis",
        "comparison": "## Comparisons",
        "concept": "## Concepts",
        "entity": "## Entities",
        "source": "## Sources",
    }.get(archived_page.page_type, "## Queries")
    link = f"- [[{Path(archived_page.path).stem}]]"
    if link in current:
        return
    if section in current:
        current = current.replace(section, f"{section}\n\n{link}", 1)
    else:
        current = f"{current.rstrip()}\n\n{section}\n\n{link}\n"
    paths.wiki_index_file.write_text(current, encoding="utf-8")


def append_wiki_log(paths: WikiPaths, summary: str, changed_paths: list[str]) -> None:
    """Append an archive operation record to wiki/log.md."""
    now = datetime.now(UTC).isoformat()
    entry = f"\n## [{now}] query-archive\n\n{summary}\n"
    if changed_paths:
        entry += "\nChanged files:\n"
        for path in changed_paths:
            entry += f"- `{path}`\n"
    paths.wiki_log_file.parent.mkdir(parents=True, exist_ok=True)
    with paths.wiki_log_file.open("a", encoding="utf-8") as f:
        f.write(entry)


def archive_answer(
    paths: WikiPaths,
    question: str,
    answer_markdown: str,
    *,
    citations: list[WikiCitation] | None = None,
    auto_archive: bool = True,
    model_name: str | None = None,
) -> QueryAnswer:
    """Judge and optionally write back an answer that already used wiki_search."""
    answer = AnswerDraft(answer_markdown=answer_markdown, citations=citations or [])
    context_pages = _context_pages_from_citations(paths, answer.citations)
    decision = judge_archive_value(paths, question, answer, context_pages, model_name=model_name)
    archived_page: WikiPage | None = None
    page_changes: list[WikiPageChange] = []
    archive_applied = False

    if auto_archive and decision.should_archive:
        changed_paths: list[str] = []
        if decision.action in {"create_page", "create_and_update"}:
            draft = build_archive_page(paths, question, answer, decision, context_pages, model_name=model_name)
            archived_page = write_archive_page(paths, draft)
            update_index_markdown(paths, archived_page)
            changed_paths.append(archived_page.path)

        if decision.action in {"update_existing", "create_and_update"}:
            page_changes = build_existing_page_updates(paths, question, answer, decision, context_pages, archived_page, model_name=model_name)
            apply_page_changes(paths, page_changes)
            changed_paths.extend(change.path for change in page_changes)

        if changed_paths:
            append_wiki_log(paths, decision.reason or "Archived query answer.", changed_paths)
            archive_applied = True

    return QueryAnswer(
        question=question,
        answer_markdown=answer.answer_markdown,
        citations=answer.citations,
        archive_decision=decision,
        archived_page=archived_page,
        page_changes=page_changes,
        archive_applied=archive_applied,
    )
