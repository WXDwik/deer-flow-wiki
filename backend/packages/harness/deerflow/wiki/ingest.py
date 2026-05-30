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
    algorithm_page_path,
    background_page_path,
    comparison_page_path,
    concept_page_path,
    dataset_page_path,
    entity_page_path,
    idea_page_path,
    query_page_path,
    raw_source_path,
    source_summary_path,
    summary_page_path,
    synthesis_page_path,
    system_model_page_path,
    unique_child_path,
)
from deerflow.wiki.purpose import wiki_language_instruction
from deerflow.wiki.repository import WikiRepository

_MAX_INLINE_MARKDOWN_CHARS = 80_000
_MAX_MARKDOWN_CHUNK_CHARS = 24_000
_MAX_NOTES_CONTEXT_CHARS = 80_000
_QUALITY_PLACEHOLDERS = ("待补充", "TODO", "TBD", "Pending source summary.")
_PAGE_TYPE_PATHS = {
    "background": background_page_path,
    "idea": idea_page_path,
    "system_model": system_model_page_path,
    "algorithm": algorithm_page_path,
    "dataset": dataset_page_path,
    "datasets": dataset_page_path,
    "summary": summary_page_path,
    "concept": concept_page_path,
    "synthesis": synthesis_page_path,
    # Legacy aliases.
    "entity": entity_page_path,
    "query": query_page_path,
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


def _chunk_markdown(text: str, *, max_chars: int = _MAX_MARKDOWN_CHUNK_CHARS) -> list[str]:
    """Split Markdown in document order without dropping any content."""
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current and current_len + len(line) > max_chars:
            chunks.append("".join(current))
            current = []
            current_len = 0
        if len(line) > max_chars:
            for index in range(0, len(line), max_chars):
                part = line[index : index + max_chars]
                if current:
                    chunks.append("".join(current))
                    current = []
                    current_len = 0
                chunks.append(part)
            continue
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


def _source_markdown_chunks(paths: WikiPaths, prepared_sources: list[PreparedSource]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for item in prepared_sources:
        markdown = item.cached_markdown.read_text(encoding="utf-8", errors="ignore")
        source_chunks = _chunk_markdown(markdown)
        for index, chunk in enumerate(source_chunks, 1):
            chunks.append(
                {
                    "source_id": item.source.source_id,
                    "title": item.source.title,
                    "raw_path": item.source.path,
                    "cached_markdown_path": item.cached_markdown.relative_to(paths.root).as_posix(),
                    "chunk_index": index,
                    "chunk_count": len(source_chunks),
                    "markdown": chunk,
                }
            )
    return chunks


def _source_markdown_sections_from_chunks(chunks: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for chunk in chunks:
        suffix = f" chunk {chunk['chunk_index']}/{chunk['chunk_count']}" if chunk["chunk_count"] > 1 else ""
        sections.append(
            f"""## Source: {chunk['title']}{suffix}
source_id: {chunk['source_id']}
raw_path: {chunk['raw_path']}
cached_markdown_path: {chunk['cached_markdown_path']}

```markdown
{chunk['markdown']}
```"""
        )

    return "\n\n".join(sections)


def _model_text(response: object) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        return "\n".join(str(item) for item in content)
    return str(content)


def _invoke_json(model: Any, prompt: str, *, run_name: str) -> dict[str, Any]:
    data = _json_from_model_text(_model_text(model.invoke(prompt, config={"run_name": run_name})))
    if data is None:
        raise ValueError(f"{run_name} model did not return valid JSON")
    return data


def _looks_like_final_wiki_content(data: dict[str, Any]) -> bool:
    if "source_summary" in data or "pages" in data:
        return "paper_notes" not in data and "chunk_notes" not in data
    sources = data.get("sources")
    if isinstance(sources, list) and any(isinstance(item, dict) and "source_summary" in item for item in sources):
        return "paper_notes" not in data and "chunk_notes" not in data
    return False


def _paper_ingest_guidance(paths: WikiPaths) -> str:
    return f"""## Built-in wiki-paper-ingest guidance

- Read the complete converted Markdown. Do not rely only on the beginning or abstract.
- Paper structures are not fixed. Identify title, abstract, body, method, experiments, results, conclusion, references, and figure/table captions from the available Markdown when present.
- Prefer durable research notes over a short abstract.
- Preserve original capitalization for English paper titles, model names, method names, acronyms, datasets, and metrics.
- For a zh-CN wiki, explain in Chinese, but do not force-translate technical identifiers.
- Every factual claim must be grounded in the imported Markdown. Do not add background knowledge that is not in the source.
- If experiments, results, or limitations are absent from the Markdown, mark them as not found instead of inventing them.
- Return strict JSON only: no Markdown fence, no prose wrapper, no code block.
- Treat schema.md in the wiki context as the active contract for directory structure, page types, source traceability, and write-back behavior.
- Source language does not override the wiki language.
- Source display_title and page title are reader-facing labels; preserve readable titles and conventional capitalization.
- Wikilinks must target existing page filename stems without .md, for example [[Lite Transformer for UAD]].
- Old lowercase or hyphenated slug links are accepted for compatibility, but newly generated links should use the actual page stem when it is available.

{wiki_language_instruction(paths)}"""


def _source_manifest(paths: WikiPaths, prepared_sources: list[PreparedSource]) -> list[dict[str, str]]:
    return [
        {
            "source_id": item.source.source_id,
            "title": item.source.title,
            "raw_path": item.source.path,
            "cached_markdown": item.cached_markdown.relative_to(paths.root).as_posix(),
        }
        for item in prepared_sources
    ]


def _notes_prompt(paths: WikiPaths, source_manifest: list[dict[str, str]], chunks: list[dict[str, Any]]) -> str:
    return f"""You are reading imported academic sources for a local LLM Wiki.

Return ONLY valid JSON:
{{
  "paper_notes": [
    {{
      "source_id": "exact source_id from the manifest",
      "display_title": "reader-facing title",
      "metadata": {{"authors": [], "venue": "", "year": "", "doi": "", "keywords": []}},
      "research_problem": "",
      "core_contributions": [],
      "method": "",
      "experiments": "",
      "results": "",
      "limitations": "",
      "important_terms": [],
      "candidate_pages": [{{"type": "background|idea|system_model|algorithm|dataset|summary|concept|synthesis", "title": "", "reason": ""}}],
      "coverage_warnings": []
    }}
  ]
}}

Rules:
- Produce paper-level structured notes only; do not write final wiki pages yet.
- Cover the complete provided Markdown, including later sections if present.
- Do not assume a fixed paper structure.
- Use source_id values exactly as listed.

{_paper_ingest_guidance(paths)}

Wiki context:
{_wiki_context(paths)}

Imported source manifest:
{json.dumps(source_manifest, ensure_ascii=False, indent=2)}

Imported sources:
{_source_markdown_sections_from_chunks(chunks)}
"""


def _chunk_notes_prompt(paths: WikiPaths, source_manifest: list[dict[str, str]], chunk: dict[str, Any]) -> str:
    return f"""You are reading one sequential Markdown chunk from an imported academic source.

Return ONLY valid JSON:
{{
  "chunk_note": {{
    "source_id": "{chunk['source_id']}",
    "chunk_index": {chunk['chunk_index']},
    "chunk_count": {chunk['chunk_count']},
    "findings": [],
    "metadata_seen": {{}},
    "methods_seen": [],
    "experiments_seen": [],
    "results_seen": [],
    "limitations_seen": [],
    "candidate_terms": [],
    "coverage_warnings": []
  }}
}}

Rules:
- Analyze this chunk in document order. It may be any part of the paper.
- Do not summarize only the abstract if later content is present in the chunk.
- Do not invent missing sections.

{_paper_ingest_guidance(paths)}

Imported source manifest:
{json.dumps(source_manifest, ensure_ascii=False, indent=2)}

## Source: {chunk['title']} chunk {chunk['chunk_index']}/{chunk['chunk_count']}
source_id: {chunk['source_id']}
raw_path: {chunk['raw_path']}

```markdown
{chunk['markdown']}
```
"""


def _aggregate_notes_prompt(paths: WikiPaths, source_manifest: list[dict[str, str]], chunk_notes: list[dict[str, Any]]) -> str:
    return f"""Merge sequential chunk notes into paper-level notes for wiki ingestion.

Return ONLY valid JSON using this shape:
{{
  "paper_notes": [
    {{
      "source_id": "exact source_id from the manifest",
      "display_title": "reader-facing title",
      "metadata": {{"authors": [], "venue": "", "year": "", "doi": "", "keywords": []}},
      "research_problem": "",
      "core_contributions": [],
      "method": "",
      "experiments": "",
      "results": "",
      "limitations": "",
      "important_terms": [],
      "candidate_pages": [{{"type": "background|idea|system_model|algorithm|dataset|summary|concept|synthesis", "title": "", "reason": ""}}],
      "coverage_warnings": []
    }}
  ]
}}

Rules:
- Use all chunk notes. Later chunks may contain the important results and conclusion.
- Preserve uncertainty and missing-section warnings.
- Do not invent facts beyond the chunk notes.

{_paper_ingest_guidance(paths)}

Imported source manifest:
{json.dumps(source_manifest, ensure_ascii=False, indent=2)}

Chunk notes:
{json.dumps(chunk_notes, ensure_ascii=False, indent=2)[:_MAX_NOTES_CONTEXT_CHARS]}
"""


def _final_wiki_content_prompt(paths: WikiPaths, source_manifest: list[dict[str, str]], notes: dict[str, Any]) -> str:
    return f"""You are maintaining a local LLM Wiki from complete paper notes.

Return ONLY valid JSON:
{{
  "sources": [
    {{
      "source_id": "exact source_id from the manifest",
      "display_title": "Human-readable source title, preserving original capitalization",
      "source_summary": "High-quality Markdown source page body",
      "tags": ["short-tag"]
    }}
  ],
  "pages": [
    {{
      "type": "background|idea|system_model|algorithm|dataset|summary|concept|synthesis",
      "title": "Human-readable page title, preserving original capitalization",
      "source_ids": ["source ids that support this page"],
      "tags": ["short-tag"],
      "content": "Markdown body. Use wikilinks targeting existing or generated page filename stems when useful."
    }}
  ]
}}

Rules:
- Return one source summary for each imported source.
- Source summaries must include metadata when available, research problem, core contributions, method, experiments/results, conclusions, limitations, and links to key generated pages.
- Prefer page types by paper-reading purpose: background for concise background, idea for innovations, system_model for problem/system model, algorithm for method or model procedure, dataset for datasets/simulation settings, summary for one-paragraph research-status text suitable for a paper introduction, concept for reusable terms, and synthesis for cross-source insights or useful archived answers.
- Do not output placeholder text such as 待补充, TODO, TBD, or Pending.
- Create reusable background/idea/system_model/algorithm/dataset/summary/concept/synthesis pages for important paper content, methods, datasets, metrics, literature-review statements, and cross-source insights.
- Add useful wikilinks. Only link to pages that already exist or pages returned in this JSON response.
- Base every statement on the paper notes.
- Page content must be Markdown body only: no YAML frontmatter and no duplicate top-level # title.

{_paper_ingest_guidance(paths)}

Wiki context:
{_wiki_context(paths)}

Imported source manifest:
{json.dumps(source_manifest, ensure_ascii=False, indent=2)}

Paper notes:
{json.dumps(notes, ensure_ascii=False, indent=2)[:_MAX_NOTES_CONTEXT_CHARS]}
"""


def _content_quality_issues(prepared_sources: list[PreparedSource], content: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    source_payloads = _source_summary_payloads(prepared_sources, content)
    for item in prepared_sources:
        payload = source_payloads.get(item.source.source_id, {})
        summary = str(payload.get("source_summary") or "").strip()
        if not summary:
            issues.append(f"missing source_summary for {item.source.source_id}")
        if any(placeholder.lower() in summary.lower() for placeholder in _QUALITY_PLACEHOLDERS):
            issues.append(f"placeholder source_summary for {item.source.source_id}")

    pages = [page for page in content.get("pages", []) if isinstance(page, dict)]
    if not pages:
        issues.append("no generated knowledge pages")
    return issues


def _repair_content_quality(
    paths: WikiPaths,
    model: Any,
    source_manifest: list[dict[str, str]],
    notes: dict[str, Any],
    content: dict[str, Any],
    issues: list[str],
) -> dict[str, Any] | None:
    prompt = f"""Repair this wiki ingest JSON so it passes the quality requirements.

Issues:
{json.dumps(issues, ensure_ascii=False, indent=2)}

Return ONLY the repaired final wiki JSON with the same shape:
{{
  "sources": [{{"source_id": "", "display_title": "", "source_summary": "", "tags": []}}],
  "pages": [{{"type": "background|idea|system_model|algorithm|dataset|summary|concept|synthesis", "title": "", "source_ids": [], "tags": [], "content": ""}}]
}}

Rules:
- Remove placeholders.
- Add at least one durable generated page when the notes contain reusable concepts/entities.
- Add useful wikilinks between generated pages/source summaries where supported.
- Do not invent facts beyond the notes.

{_paper_ingest_guidance(paths)}

Imported source manifest:
{json.dumps(source_manifest, ensure_ascii=False, indent=2)}

Paper notes:
{json.dumps(notes, ensure_ascii=False, indent=2)[:_MAX_NOTES_CONTEXT_CHARS]}

Current final wiki JSON:
{json.dumps(content, ensure_ascii=False, indent=2)[:_MAX_NOTES_CONTEXT_CHARS]}
"""
    try:
        return _invoke_json(model, prompt, run_name="wiki_ingest_quality_repair")
    except Exception:
        return None


def generate_wiki_batch_content(paths: WikiPaths, prepared_sources: list[PreparedSource], *, model_name: str | None = None) -> dict[str, Any]:
    """Ask the configured chat model to read complete Markdown and generate wiki pages."""
    model = create_chat_model(name=model_name, thinking_enabled=False)
    source_manifest = _source_manifest(paths, prepared_sources)
    chunks = _source_markdown_chunks(paths, prepared_sources)
    total_markdown_chars = sum(len(str(chunk.get("markdown") or "")) for chunk in chunks)

    notes: dict[str, Any]
    if total_markdown_chars <= _MAX_INLINE_MARKDOWN_CHARS:
        notes = _invoke_json(model, _notes_prompt(paths, source_manifest, chunks), run_name="wiki_ingest_notes")
        if _looks_like_final_wiki_content(notes):
            content = notes
        else:
            content = _invoke_json(
                model,
                _final_wiki_content_prompt(paths, source_manifest, notes),
                run_name="wiki_ingest",
            )
    else:
        chunk_notes: list[dict[str, Any]] = []
        for chunk in chunks:
            chunk_notes.append(
                _invoke_json(
                    model,
                    _chunk_notes_prompt(paths, source_manifest, chunk),
                    run_name="wiki_ingest_chunk_notes",
                )
            )
        notes = _invoke_json(
            model,
            _aggregate_notes_prompt(paths, source_manifest, chunk_notes),
            run_name="wiki_ingest_notes",
        )
        if _looks_like_final_wiki_content(notes):
            content = notes
        else:
            content = _invoke_json(
                model,
                _final_wiki_content_prompt(paths, source_manifest, notes),
                run_name="wiki_ingest",
            )

    initial_issues = _content_quality_issues(prepared_sources, content)
    if initial_issues:
        repaired = _repair_content_quality(paths, model, source_manifest, notes if "notes" in locals() else content, content, initial_issues)
        if repaired is not None:
            repaired_issues = _content_quality_issues(prepared_sources, repaired)
            if len(repaired_issues) <= len(initial_issues):
                content = repaired
                content["_quality_retry"] = {
                    "initial_issues": initial_issues,
                    "remaining_issues": repaired_issues,
                    "resolved": not repaired_issues,
                }
            else:
                content["_quality_retry"] = {
                    "initial_issues": initial_issues,
                    "remaining_issues": initial_issues,
                    "resolved": False,
                }
        else:
            content["_quality_retry"] = {
                "initial_issues": initial_issues,
                "remaining_issues": initial_issues,
                "resolved": False,
            }
    else:
        content["_quality_retry"] = {"initial_issues": [], "remaining_issues": [], "resolved": True}

    content["_ingest_input"] = {
        "cached_markdown_chars": total_markdown_chars,
        "chunk_count": len(chunks),
        "complete_markdown_processed": True,
    }
    return content


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
            "background": paths.wiki_background_dir,
            "idea": paths.wiki_idea_dir,
            "system_model": paths.wiki_system_model_dir,
            "algorithm": paths.wiki_algorithm_dir,
            "dataset": paths.wiki_datasets_dir,
            "datasets": paths.wiki_datasets_dir,
            "summary": paths.wiki_summary_dir,
            "concept": paths.wiki_concept_dir,
            "synthesis": paths.wiki_synthesis_dir,
            "entity": paths.wiki_entities_dir,
            "query": paths.wiki_queries_dir,
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
        if page_type == "datasets":
            page_type = "dataset"
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
        if isinstance(generated.get("_ingest_input"), dict):
            item.source.metadata["ingest_input"] = generated["_ingest_input"]
        if isinstance(generated.get("_quality_retry"), dict):
            item.source.metadata["quality"] = generated["_quality_retry"]

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
