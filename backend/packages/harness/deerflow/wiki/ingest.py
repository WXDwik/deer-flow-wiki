"""LLM Wiki source ingestion pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from deerflow.models import create_chat_model
from deerflow.utils.file_conversion import convert_file_to_markdown
from deerflow.wiki.markdown import build_source_summary_markdown, frontmatter
from deerflow.wiki.models import RawSource, WikiPage
from deerflow.wiki.paths import (
    WikiPaths,
    comparison_page_path,
    concept_page_path,
    entity_page_path,
    query_page_path,
    raw_source_path,
    source_summary_path,
    synthesis_page_path,
    unique_child_path,
)
from deerflow.wiki.purpose import wiki_language_instruction
from deerflow.wiki.repository import WikiRepository

_MAX_LLM_MARKDOWN_CHARS = 40_000
_MAX_BATCH_LLM_MARKDOWN_CHARS = 120_000
_PAGE_TYPE_PATHS = {
    "entity": entity_page_path,
    "concept": concept_page_path,
    "query": query_page_path,
    "synthesis": synthesis_page_path,
    "comparison": comparison_page_path,
}


@dataclass(frozen=True)
class PreparedSource:
    """A raw source plus the Markdown cache prepared for LLM ingestion."""

    source: RawSource
    cached_markdown: Path


def sha256_file(path: Path) -> str:
    """Calculate a file SHA256 for import traceability and deduplication."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_source_to_raw(paths: WikiPaths, source_file: str | Path) -> tuple[Path, str]:
    """Copy the source file into raw/sources/ and return target path + SHA256."""
    source_path = Path(source_file).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Source file not found: {source_path}")

    file_hash = sha256_file(source_path)
    raw_sources_dir = paths.raw_sources_dir.resolve()
    try:
        relative_to_raw = source_path.relative_to(raw_sources_dir)
    except ValueError:
        relative_to_raw = None
    if relative_to_raw is not None and ".cache" not in relative_to_raw.parts:
        return source_path, file_hash

    target = raw_source_path(paths, source_path.name)
    shutil.copy2(source_path, target)
    return target, file_hash


def _run_async(coro):
    """Run a coroutine from the synchronous tool/service path."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def _cache_dir(paths: WikiPaths) -> Path:
    path = paths.raw_sources_dir / ".cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _source_asset_dir(paths: WikiPaths, source_id: str) -> Path:
    path = paths.raw_assets_dir / source_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_source_markdown(paths: WikiPaths, source_path: Path, source_id: str) -> Path:
    """Convert a raw source into cached Markdown under raw/sources/.cache/."""
    cache_dir = _cache_dir(paths)
    cache_path = unique_child_path(cache_dir, source_path.with_suffix(".md").name)

    if source_path.suffix.lower() == ".md":
        cache_path.write_text(source_path.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
        return cache_path

    md_path = _run_async(
        convert_file_to_markdown(
            source_path,
            output_dir=cache_dir,
            pdf_image_dir=_source_asset_dir(paths, source_id),
        )
    )
    if md_path is not None:
        return md_path

    text = source_path.read_text(encoding="utf-8", errors="ignore")
    cache_path.write_text(text, encoding="utf-8")
    return cache_path


def _json_from_model_text(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
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


def _wiki_context(paths: WikiPaths) -> str:
    parts = []
    for path in (paths.purpose_file, paths.schema_file, paths.wiki_index_file, paths.wiki_overview_file):
        if path.is_file():
            rel = path.relative_to(paths.root)
            text = path.read_text(encoding="utf-8", errors="ignore")[:4000]
            parts.append(f"## {rel}\n{text}")
    return "\n\n".join(parts)


def _source_markdown_sections(paths: WikiPaths, prepared_sources: list[PreparedSource]) -> str:
    sections: list[str] = []
    remaining = _MAX_BATCH_LLM_MARKDOWN_CHARS
    per_source_cap = min(
        _MAX_LLM_MARKDOWN_CHARS,
        max(8_000, _MAX_BATCH_LLM_MARKDOWN_CHARS // max(len(prepared_sources), 1)),
    )

    for item in prepared_sources:
        if remaining <= 0:
            break
        source = item.source
        cap = min(per_source_cap, remaining)
        markdown = item.cached_markdown.read_text(encoding="utf-8", errors="ignore")[:cap]
        remaining -= len(markdown)
        sections.append(
            f"""## Source: {source.title}
source_id: {source.source_id}
raw_path: {source.path}
cached_markdown_path: {item.cached_markdown.relative_to(paths.root).as_posix()}

```markdown
{markdown}
```"""
        )

    return "\n\n".join(sections)


def generate_wiki_batch_content(paths: WikiPaths, prepared_sources: list[PreparedSource], *, model_name: str | None = None) -> dict[str, Any]:
    """Ask the configured chat model to summarize and classify a batch of sources together."""
    source_manifest = [
        {
            "source_id": item.source.source_id,
            "title": item.source.title,
            "raw_path": item.source.path,
            "cached_markdown": item.cached_markdown.relative_to(paths.root).as_posix(),
        }
        for item in prepared_sources
    ]
    prompt = f"""You are maintaining a local LLM Wiki.

Read the wiki context and ALL imported source Markdown together. Return ONLY valid JSON:
{{
  "sources": [
    {{
      "source_id": "exact source_id from the manifest",
      "display_title": "Human-readable source title, preserving original capitalization",
      "source_summary": "Markdown summary for this source page",
      "tags": ["short-tag"]
    }}
  ],
  "pages": [
    {{
      "type": "entity|concept|query|synthesis|comparison",
      "title": "Human-readable page title, preserving original capitalization",
      "source_ids": ["source ids that support this page"],
      "tags": ["short-tag"],
      "content": "Markdown body. Use wikilinks targeting existing page filename stems, like [[Readable Page Stem]], when useful."
    }}
  ]
}}

Rules:
- Treat schema.md in the wiki context as the active contract for directory
  structure, page types, source traceability, and write-back behavior.
- Follow the target wiki language rules below for every generated source
  summary and page body. Source language does not override the wiki language.
- Use source_id values exactly as listed in the manifest.
- Return one source summary for each imported source.
- Base every statement on the imported sources.
- Prefer a compact source summary per source plus high-value classification pages.
- When multiple sources are imported, actively look for cross-source synthesis,
  comparison, shared concepts, contradictions, and complementary evidence.
- For a page supported by multiple sources, include all relevant source_ids.
- Page content must be Markdown body only: no YAML frontmatter and no duplicate
  top-level # title.
- Source display_title and page title are reader-facing labels. Preserve the
  source language and the original or conventional capitalization of paper
  titles, proper nouns, model names, methods, datasets, authors, organizations,
  and acronyms (for example MIMO, UAD, Transformer, Deep Learning). Do not
  return lowercase slugs as visible titles when a readable title can be
  recovered from the imported source.
- Wikilinks must target existing page filename stems without .md. New generated
  page filenames preserve normal spaces from their readable titles, so link to
  a page titled "Lite Transformer for UAD" as [[Lite Transformer for UAD]].
  Old lowercase or hyphenated slug links are accepted for compatibility, but
  newly generated links should use the actual page stem when it is available.
- Only link to pages that already exist or pages returned in this JSON response.
- Do not invent facts.

{wiki_language_instruction(paths)}

Wiki context:
{_wiki_context(paths)}

Imported source manifest:
{json.dumps(source_manifest, ensure_ascii=False, indent=2)}

Imported sources:
{_source_markdown_sections(paths, prepared_sources)}
"""
    model = create_chat_model(name=model_name, thinking_enabled=False)
    response = model.invoke(prompt, config={"run_name": "wiki_ingest"})
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "\n".join(str(item) for item in content)
    data = _json_from_model_text(str(content))
    if data is None:
        raise ValueError("Wiki ingest model did not return valid JSON")
    return data


def generate_wiki_content(paths: WikiPaths, source: RawSource, cached_markdown: Path, *, model_name: str | None = None) -> dict[str, Any]:
    """Ask the configured chat model to summarize and classify one cached source."""
    return generate_wiki_batch_content(paths, [PreparedSource(source=source, cached_markdown=cached_markdown)], model_name=model_name)


def _fallback_wiki_content(source: RawSource, cached_markdown: Path) -> dict[str, Any]:
    text = cached_markdown.read_text(encoding="utf-8", errors="ignore").strip()
    preview = text[:1200] if text else "No readable Markdown content was extracted."
    return {
        "source_summary": f"Imported source `{source.path}` and cached it as `{cached_markdown.name}`.\n\n{preview}",
        "tags": ["source"],
        "pages": [],
        "generation_error": "llm_generation_failed",
    }


def _fallback_wiki_batch_content(prepared_sources: list[PreparedSource]) -> dict[str, Any]:
    return {
        "sources": [
            {
                "source_id": item.source.source_id,
                "source_summary": _fallback_wiki_content(item.source, item.cached_markdown)["source_summary"],
                "tags": ["source"],
            }
            for item in prepared_sources
        ],
        "pages": [],
        "generation_error": "llm_generation_failed",
    }


def _page_markdown(*, page_type: str, title: str, source_ids: list[str], tags: list[str], content: str, now: str) -> str:
    header = frontmatter(
        {
            "type": page_type,
            "title": title,
            "sources": source_ids,
            "tags": tags,
            "created_at": now,
            "updated_at": now,
        }
    )
    return f"{header}\n\n# {title}\n\n{content.strip()}\n"


def _page_path(paths: WikiPaths, page_type: str, title: str) -> Path:
    try:
        return _PAGE_TYPE_PATHS[page_type](paths, title)
    except ValueError:
        digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
        directory = {
            "entity": paths.wiki_entities_dir,
            "concept": paths.wiki_concepts_dir,
            "query": paths.wiki_queries_dir,
            "synthesis": paths.wiki_synthesis_dir,
            "comparison": paths.wiki_comparisons_dir,
        }[page_type]
        return unique_child_path(directory, f"page-{digest}.md")


def _source_summary_page_path(paths: WikiPaths, title: str) -> Path:
    try:
        return source_summary_path(paths, title)
    except ValueError:
        digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
        return unique_child_path(paths.wiki_sources_dir, f"source-{digest}.md")


def _source_summary_payloads(prepared_sources: list[PreparedSource], content: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    raw_sources = content.get("sources")
    if isinstance(raw_sources, list):
        for item in raw_sources:
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("source_id") or "").strip()
            if source_id:
                by_id[source_id] = item

    if not by_id and prepared_sources:
        first = prepared_sources[0].source.source_id
        by_id[first] = {
            "source_id": first,
            "source_summary": content.get("source_summary"),
            "tags": content.get("tags", []),
        }

    return by_id


def _clean_tags(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(tag).strip() for tag in value if str(tag).strip()]


def _page_source_ids(item: dict[str, Any], valid_source_ids: set[str], default_source_ids: list[str]) -> list[str]:
    raw = item.get("source_ids", item.get("sources", item.get("source_id")))
    if isinstance(raw, str):
        source_ids = [raw]
    elif isinstance(raw, list):
        source_ids = [str(source_id) for source_id in raw]
    else:
        source_ids = []

    filtered = [source_id.strip() for source_id in source_ids if source_id.strip() in valid_source_ids]
    return filtered or default_source_ids


def write_generated_batch_pages(
    paths: WikiPaths,
    prepared_sources: list[PreparedSource],
    content: dict[str, Any],
    now: str,
) -> list[WikiPage]:
    """Write model-generated source summaries and cross-source wiki pages."""
    pages: list[WikiPage] = []
    source_payloads = _source_summary_payloads(prepared_sources, content)

    for item in prepared_sources:
        source = item.source
        payload = source_payloads.get(source.source_id, {})
        display_title = str(payload.get("display_title") or source.title).strip() or source.title
        summary_path = _source_summary_page_path(paths, display_title)
        summary = str(payload.get("source_summary") or "Pending source summary.")
        tags = _clean_tags(payload.get("tags", []))
        summary_md = build_source_summary_markdown(title=display_title, source_path=source.path, summary=summary, tags=tags)
        summary_path.write_text(summary_md, encoding="utf-8")
        pages.append(
            WikiPage(
                page_id=uuid4().hex,
                title=display_title,
                path=summary_path.relative_to(paths.root).as_posix(),
                page_type="source",
                sources=[source.source_id],
                tags=tags,
                created_at=now,
                updated_at=now,
            )
        )

    default_source_ids = [item.source.source_id for item in prepared_sources]
    valid_source_ids = set(default_source_ids)
    for item in content.get("pages", []):
        if not isinstance(item, dict):
            continue
        page_type = str(item.get("type") or "").strip()
        if page_type not in _PAGE_TYPE_PATHS:
            continue
        title = str(item.get("title") or "").strip()
        body = str(item.get("content") or "").strip()
        if not title or not body:
            continue
        item_tags = _clean_tags(item.get("tags", []))
        source_ids = _page_source_ids(item, valid_source_ids, default_source_ids)
        path = _page_path(paths, page_type, title)
        path.write_text(
            _page_markdown(page_type=page_type, title=title, source_ids=source_ids, tags=item_tags, content=body, now=now),
            encoding="utf-8",
        )
        pages.append(
            WikiPage(
                page_id=uuid4().hex,
                title=title,
                path=path.relative_to(paths.root).as_posix(),
                page_type=page_type,  # type: ignore[arg-type]
                sources=source_ids,
                tags=item_tags,
                created_at=now,
                updated_at=now,
            )
        )

    return pages


def write_generated_pages(paths: WikiPaths, source: RawSource, content: dict[str, Any], now: str) -> list[WikiPage]:
    """Write model-generated pages for a single source."""
    cached_markdown = Path(str(source.metadata.get("cached_markdown") or ""))
    prepared = PreparedSource(source=source, cached_markdown=paths.root / cached_markdown)
    return write_generated_batch_pages(paths, [prepared], content, now)


def prepare_source(paths: WikiPaths, source_file: str | Path, now: str) -> PreparedSource:
    """Copy/cache one source before batch LLM ingestion."""
    original_source_path = Path(source_file).expanduser().resolve()
    target, file_hash = copy_source_to_raw(paths, source_file)
    mime_type, _ = mimetypes.guess_type(target)
    source_id = uuid4().hex

    source = RawSource(
        source_id=source_id,
        title=original_source_path.stem,
        path=target.relative_to(paths.root).as_posix(),
        sha256=file_hash,
        mime_type=mime_type,
        size_bytes=target.stat().st_size,
        created_at=now,
        updated_at=now,
    )

    cached_markdown = cache_source_markdown(paths, target, source.source_id)
    source.metadata["cached_markdown"] = cached_markdown.relative_to(paths.root).as_posix()
    return PreparedSource(source=source, cached_markdown=cached_markdown)


def ingest_files(paths: WikiPaths, source_files: list[str | Path], *, model_name: str | None = None) -> list[RawSource]:
    """Import a batch of files and generate wiki pages from their combined content."""
    if not source_files:
        raise ValueError("source_files cannot be empty")

    now = datetime.now(UTC).isoformat()
    prepared_sources = [prepare_source(paths, source_file, now) for source_file in source_files]
    try:
        generated = generate_wiki_batch_content(paths, prepared_sources, model_name=model_name)
        ingest_mode = "llm_batch" if len(prepared_sources) > 1 else "llm"
    except Exception as exc:
        generated = _fallback_wiki_batch_content(prepared_sources)
        ingest_mode = "fallback"
        for item in prepared_sources:
            item.source.metadata["generation_error"] = str(exc)

    for item in prepared_sources:
        item.source.metadata["ingest_mode"] = ingest_mode

    pages = write_generated_batch_pages(paths, prepared_sources, generated, now)
    sources = [item.source for item in prepared_sources]
    for source in sources:
        source.metadata["generated_pages"] = [
            {
                "title": page.title,
                "path": page.path,
                "page_type": page.page_type,
            }
            for page in pages
            if source.source_id in page.sources
        ]

    repo = WikiRepository(paths)
    for source in sources:
        repo.add_source(source)
    for page in pages:
        repo.add_page(page)
    return sources


def ingest_file(paths: WikiPaths, source_file: str | Path, *, model_name: str | None = None) -> RawSource:
    """Import one file, cache Markdown, and generate wiki pages."""
    return ingest_files(paths, [source_file], model_name=model_name)[0]
