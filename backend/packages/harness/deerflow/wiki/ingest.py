"""LLM Wiki source ingestion pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage

from deerflow.models import create_chat_model
from deerflow.utils.file_conversion import convert_file_to_markdown
from deerflow.wiki.markdown import build_source_summary_markdown, frontmatter
from deerflow.wiki.models import RawSource, WikiPage
from deerflow.wiki.paths import (
    WikiPaths,
    algorithm_page_path,
    background_page_path,
    concept_page_path,
    dataset_page_path,
    display_slugify_name,
    idea_page_path,
    raw_source_path,
    slugify_name,
    source_summary_path,
    summary_page_path,
    synthesis_page_path,
    system_model_page_path,
    unique_child_path,
)
from deerflow.wiki.purpose import wiki_language_instruction
from deerflow.wiki.repository import WikiRepository

_MAX_MARKDOWN_CHUNK_CHARS = 16_000
_MAX_NOTES_CONTEXT_CHARS = 80_000
_STRUCTURED_RETRY_ATTEMPTS = 3
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


def _string_schema() -> dict[str, Any]:
    return {"type": "string"}


def _string_array_schema() -> dict[str, Any]:
    return {"type": "array", "items": _string_schema()}


def _candidate_page_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["background", "idea", "system_model", "algorithm", "dataset", "summary", "concept", "synthesis"]},
            "title": _string_schema(),
            "reason": _string_schema(),
        },
        "required": ["type", "title", "reason"],
        "additionalProperties": False,
    }


def _wiki_source_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "source_id": _string_schema(),
            "display_title": _string_schema(),
            "source_summary": _string_schema(),
            "tags": _string_array_schema(),
        },
        "required": ["source_id", "display_title", "source_summary", "tags"],
        "additionalProperties": False,
    }


def _wiki_page_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["background", "idea", "system_model", "algorithm", "dataset", "summary", "concept", "synthesis"]},
            "title": _string_schema(),
            "source_ids": _string_array_schema(),
            "tags": _string_array_schema(),
            "content": _string_schema(),
        },
        "required": ["type", "title", "source_ids", "tags", "content"],
        "additionalProperties": False,
    }


def _final_wiki_content_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "sources": {"type": "array", "items": _wiki_source_schema()},
            "pages": {"type": "array", "items": _wiki_page_item_schema()},
        },
        "required": ["sources", "pages"],
        "additionalProperties": False,
    }


def _extract_tool_call_json(response: object, function_name: str) -> dict[str, Any] | None:
    tool_calls = getattr(response, "tool_calls", None) or []
    for tool_call in tool_calls:
        name = tool_call.get("name") if isinstance(tool_call, dict) else getattr(tool_call, "name", None)
        if name and name != function_name:
            continue
        args = tool_call.get("args") if isinstance(tool_call, dict) else getattr(tool_call, "args", None)
        if isinstance(args, dict):
            return args
        if isinstance(args, str):
            parsed = _json_from_model_text(args)
            if parsed is not None:
                return parsed

    additional = getattr(response, "additional_kwargs", {}) or {}
    for raw_call in additional.get("tool_calls") or []:
        function = raw_call.get("function", {}) if isinstance(raw_call, dict) else {}
        if function.get("name") and function["name"] != function_name:
            continue
        args = function.get("arguments")
        if isinstance(args, str):
            parsed = _json_from_model_text(args)
            if parsed is not None:
                return parsed
    return None


def _validate_required_shape(data: dict[str, Any], schema: dict[str, Any], run_name: str) -> None:
    required = schema.get("required") or []
    for key in required:
        if key not in data:
            raise ValueError(f"{run_name} missing required key: {key}")
    properties = schema.get("properties") or {}
    for key, value in data.items():
        spec = properties.get(key)
        if not spec:
            continue
        expected = spec.get("type")
        if expected == "object" and not isinstance(value, dict):
            raise ValueError(f"{run_name}.{key} must be object")
        if expected == "array" and not isinstance(value, list):
            raise ValueError(f"{run_name}.{key} must be array")
        if expected == "string" and not isinstance(value, str):
            raise ValueError(f"{run_name}.{key} must be string")


def _structured_tool_model(model: Any, function_name: str, schema: dict[str, Any]) -> Any | None:
    if type(model).__module__.startswith("unittest.mock"):
        return None
    bind_tools = getattr(model, "bind_tools", None)
    if not callable(bind_tools):
        return None
    tool = {
        "type": "function",
        "function": {
            "name": function_name,
            "description": f"Return validated JSON for {function_name}.",
            "parameters": schema,
            "strict": True,
        },
    }
    try:
        return bind_tools([tool], tool_choice=function_name)
    except TypeError:
        try:
            return bind_tools([tool])
        except Exception:
            return None
    except Exception:
        return None


def _repair_json_prompt(prompt: str, bad_text: str, schema: dict[str, Any], function_name: str) -> str:
    return f"""The previous response for `{function_name}` was not valid JSON for the required schema.

Return ONLY corrected JSON. Do not add prose, Markdown fences, or new facts.

Required JSON Schema:
{json.dumps(schema, ensure_ascii=False, indent=2)}

Original task:
{prompt[:12000]}

Invalid response:
{bad_text[:12000]}
"""


def _invoke_structured(model: Any, prompt: str, *, run_name: str, function_name: str, schema: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    structured_model = _structured_tool_model(model, function_name, schema)
    if structured_model is not None:
        try:
            response = structured_model.invoke([HumanMessage(content=prompt)], config={"run_name": run_name})
            data = _extract_tool_call_json(response, function_name)
            if data is not None:
                _validate_required_shape(data, schema, run_name)
                data.setdefault("_structured_output", {"mode": "strict_tool_call", "function": function_name})
                return data
            errors.append("strict tool call returned no parseable arguments")
        except Exception as exc:
            errors.append(f"strict_schema_failed: {exc}")

    current_prompt = prompt
    last_text = ""
    for attempt in range(1, _STRUCTURED_RETRY_ATTEMPTS + 1):
        response = model.invoke(current_prompt, config={"run_name": run_name})
        last_text = _model_text(response)
        data = _json_from_model_text(last_text)
        if data is not None:
            try:
                if not _looks_like_final_wiki_content(data):
                    _validate_required_shape(data, schema, run_name)
                data.setdefault(
                    "_structured_output",
                    {
                        "mode": "json_text",
                        "function": function_name,
                        "attempt": attempt,
                        "strict_errors": errors,
                    },
                )
                return data
            except Exception as exc:
                errors.append(str(exc))
        else:
            errors.append(f"attempt {attempt} returned invalid JSON")
        current_prompt = _repair_json_prompt(prompt, last_text, schema, function_name)

    raise ValueError(f"{run_name} model did not return valid structured JSON: {'; '.join(errors)}")


def _invoke_json(model: Any, prompt: str, *, run_name: str) -> dict[str, Any]:
    return _invoke_structured(
        model,
        prompt,
        run_name=run_name,
        function_name=run_name,
        schema={"type": "object", "properties": {}, "additionalProperties": True},
    )


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
- Wikilinks must target existing page filename stems without .md, or exact page titles returned in the same JSON response, for example [[Lite Transformer for UAD]].
- Do not create wikilinks to shorthand topic names unless a page with that exact filename stem exists or is returned in the same JSON response.
- If you mention a related idea but do not create a matching page for it, write plain text instead of a wikilink.
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

Imported sources:

Wiki context:
{_wiki_context(paths)}

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
    complete_markdown = str(notes.get("complete_markdown") or "").strip() if isinstance(notes, dict) else ""
    notes_text = json.dumps({key: value for key, value in notes.items() if key != "complete_markdown"}, ensure_ascii=False, indent=2) if isinstance(notes, dict) else json.dumps(notes, ensure_ascii=False, indent=2)
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
- Add useful wikilinks only when the target is an existing wiki page filename stem or an exact page title returned in this JSON response.
- Do not link to uncreated submodules, methods, datasets, or short concept names. If a linked target deserves navigation, return it as a page in `pages`; otherwise keep it as plain text.
- Base every statement on the paper notes.
- Page content must be Markdown body only: no YAML frontmatter and no duplicate top-level # title.

{_paper_ingest_guidance(paths)}

Wiki context:
{_wiki_context(paths)}

Imported source manifest:
{json.dumps(source_manifest, ensure_ascii=False, indent=2)}

Imported sources:
{complete_markdown}

Paper notes:
{notes_text[:_MAX_NOTES_CONTEXT_CHARS]}
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
- Add at least one durable generated page when the notes contain reusable concepts, ideas, methods, or synthesis-worthy findings.
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


def _source_ids_from_manifest(source_manifest: list[dict[str, str]]) -> list[str]:
    return [item["source_id"] for item in source_manifest if item.get("source_id")]


def _fallback_content_from_markdown(source_manifest: list[dict[str, str]], chunks: list[dict[str, Any]], *, reason: str) -> dict[str, Any]:
    source = source_manifest[0] if source_manifest else {}
    title = str(source.get("title") or "Imported Source")
    source_id = str(source.get("source_id") or "")
    markdown_preview = "\n\n".join(str(chunk.get("markdown") or "")[:1600] for chunk in chunks[:2]).strip()
    return {
        "sources": [
            {
                "source_id": source_id,
                "display_title": title,
                "source_summary": f"结构化生成失败，已保留转换后的 Markdown 内容供后续检索和重新解析。\n\n失败原因：`{reason}`\n\nMarkdown 预览：\n\n{markdown_preview}",
                "tags": ["source"],
            }
        ],
        "pages": [
            {
                "type": "summary",
                "title": f"{title} 导入摘要",
                "source_ids": [source_id],
                "tags": ["paper-summary"],
                "content": f"该论文已完整读取转换后的 Markdown，但结构化 wiki 页面生成失败。可基于 source 页面和缓存 Markdown 重新解析。\n\n失败原因：`{reason}`",
            }
        ],
    }


def generate_wiki_batch_content(paths: WikiPaths, prepared_sources: list[PreparedSource], *, model_name: str | None = None) -> dict[str, Any]:
    """Read complete Markdown in one structured pass and generate wiki pages."""
    model = create_chat_model(name=model_name, thinking_enabled=False, structured_output=True)
    source_manifest = _source_manifest(paths, prepared_sources)
    chunks = _source_markdown_chunks(paths, prepared_sources)
    total_markdown_chars = sum(len(str(chunk.get("markdown") or "")) for chunk in chunks)
    stage_status: dict[str, Any] = {
        "pipeline": "full_markdown_structured_generation",
        "chunk_count": len(chunks),
        "generation_failures": [],
        "fallback_level": None,
    }

    try:
        content = _invoke_structured(
            model,
            _final_wiki_content_prompt(paths, source_manifest, {"complete_markdown": _source_markdown_sections_from_chunks(chunks)}),
            run_name="wiki_ingest",
            function_name="wiki_ingest_full_paper",
            schema=_final_wiki_content_schema(),
        )
    except Exception as exc:
        stage_status["generation_failures"].append({"stage": "full_markdown_structured_generation", "error": str(exc)})
        stage_status["fallback_level"] = "structured_generation_failed"
        content = _fallback_content_from_markdown(source_manifest, chunks, reason=str(exc))

    initial_issues = _content_quality_issues(prepared_sources, content)
    if initial_issues:
        repaired = _repair_content_quality(paths, model, source_manifest, content, content, initial_issues)
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
        "pipeline": "full_markdown_structured_generation",
    }
    content.setdefault("_stage_status", stage_status)
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

    if prepared_sources and len(prepared_sources) == 1 and len(by_id) == 1 and prepared_sources[0].source.source_id not in by_id:
        by_id[prepared_sources[0].source.source_id] = next(iter(by_id.values()))

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


_WIKILINK_RE = re.compile(r"\[\[\s*([^\]|]+?)\s*(?:\|\s*([^\]]+?)\s*)?\]\]")


def _wikilink_match_keys(value: str) -> set[str]:
    raw = value.strip().replace("\\", "/")
    if raw.lower().endswith(".md"):
        raw = raw[:-3]
    raw = raw.strip("/")
    if raw.startswith("wiki/"):
        raw = raw[5:]

    candidates = {raw}
    if "/" in raw:
        candidates.add(Path(raw).name)

    keys: set[str] = set()
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        keys.add(candidate.lower())
        try:
            keys.add(display_slugify_name(candidate).lower())
        except ValueError:
            pass
        try:
            keys.add(slugify_name(candidate))
        except ValueError:
            pass
    return keys


def _add_wikilink_target(targets: dict[str, str], *, stem: str, aliases: list[str] | None = None) -> None:
    clean_stem = stem.strip()
    if not clean_stem:
        return
    values = [clean_stem, *(aliases or [])]
    for value in values:
        for key in _wikilink_match_keys(value):
            targets.setdefault(key, clean_stem)


def _planned_wikilink_targets(paths: WikiPaths, prepared_sources: list[PreparedSource], content: dict[str, Any]) -> dict[str, str]:
    targets: dict[str, str] = {}
    for path in paths.wiki_dir.rglob("*.md"):
        if not path.is_file():
            continue
        rel = path.relative_to(paths.wiki_dir).with_suffix("").as_posix()
        _add_wikilink_target(targets, stem=path.stem, aliases=[rel])

    source_payloads = _source_summary_payloads(prepared_sources, content)
    for item in prepared_sources:
        payload = source_payloads.get(item.source.source_id, {})
        display_title = str(payload.get("display_title") or item.source.title).strip() or item.source.title
        summary_path = _source_summary_page_path(paths, display_title)
        rel = summary_path.relative_to(paths.wiki_dir).with_suffix("").as_posix()
        _add_wikilink_target(targets, stem=summary_path.stem, aliases=[display_title, rel])

    for item in content.get("pages", []):
        if not isinstance(item, dict):
            continue
        page_type = str(item.get("type") or "").strip()
        if page_type == "datasets":
            page_type = "dataset"
        title = str(item.get("title") or "").strip()
        if page_type not in _PAGE_TYPE_PATHS or not title:
            continue
        path = _page_path(paths, page_type, title)
        rel = path.relative_to(paths.wiki_dir).with_suffix("").as_posix()
        _add_wikilink_target(targets, stem=path.stem, aliases=[title, rel])
    return targets


def _normalize_wikilinks(markdown: str, targets: dict[str, str]) -> tuple[str, dict[str, int]]:
    stats = {"checked": 0, "kept": 0, "rewritten": 0, "unlinked": 0}

    def replace(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        label = (match.group(2) or "").strip()
        stats["checked"] += 1
        resolved = None
        for key in _wikilink_match_keys(target):
            resolved = targets.get(key)
            if resolved is not None:
                break

        if resolved is None:
            stats["unlinked"] += 1
            return label or target

        if resolved == target and not label:
            stats["kept"] += 1
            return f"[[{resolved}]]"

        visible = label or target
        if visible == resolved:
            stats["rewritten"] += 1
            return f"[[{resolved}]]"
        stats["rewritten"] += 1
        return f"[[{resolved}|{visible}]]"

    return _WIKILINK_RE.sub(replace, markdown), stats


def _normalize_generated_content_wikilinks(paths: WikiPaths, prepared_sources: list[PreparedSource], content: dict[str, Any]) -> dict[str, int]:
    targets = _planned_wikilink_targets(paths, prepared_sources, content)
    totals = {"checked": 0, "kept": 0, "rewritten": 0, "unlinked": 0}

    def add_stats(stats: dict[str, int]) -> None:
        for key in totals:
            totals[key] += stats.get(key, 0)

    raw_sources = content.get("sources")
    if isinstance(raw_sources, list):
        seen_payloads: set[int] = set()
        for payload in _source_summary_payloads(prepared_sources, content).values():
            payload_id = id(payload)
            if payload_id in seen_payloads:
                continue
            seen_payloads.add(payload_id)
            summary = payload.get("source_summary")
            if isinstance(summary, str) and summary:
                normalized, stats = _normalize_wikilinks(summary, targets)
                payload["source_summary"] = normalized
                add_stats(stats)

    summary = content.get("source_summary")
    if isinstance(summary, str) and summary:
        normalized, stats = _normalize_wikilinks(summary, targets)
        content["source_summary"] = normalized
        add_stats(stats)

    for item in content.get("pages", []):
        if not isinstance(item, dict):
            continue
        body = item.get("content")
        if isinstance(body, str) and body:
            normalized, stats = _normalize_wikilinks(body, targets)
            item["content"] = normalized
            add_stats(stats)

    content["_link_normalization"] = totals
    return totals


def write_generated_batch_pages(
    paths: WikiPaths,
    prepared_sources: list[PreparedSource],
    content: dict[str, Any],
    now: str,
) -> list[WikiPage]:
    """Write model-generated source summaries and cross-source wiki pages."""
    pages: list[WikiPage] = []
    _normalize_generated_content_wikilinks(paths, prepared_sources, content)
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
        if isinstance(generated.get("_stage_status"), dict):
            item.source.metadata["stage_status"] = generated["_stage_status"]

    pages = write_generated_batch_pages(paths, prepared_sources, generated, now)
    sources = [item.source for item in prepared_sources]
    for source in sources:
        if isinstance(generated.get("_link_normalization"), dict):
            source.metadata["link_normalization"] = generated["_link_normalization"]
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
