"""Purpose maintenance helpers for LLM Wiki write workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from deerflow.models import create_chat_model
from deerflow.wiki.paths import WikiPaths
from deerflow.wiki.repository import WikiRepository

_MAX_PURPOSE_CONTEXT_CHARS = 24_000
_MAX_CHANGED_FILE_CHARS = 4_000


def wiki_language(paths: WikiPaths) -> str:
    """Return the configured wiki language, falling back to zh-CN."""
    config = WikiRepository(paths).read_config()
    language = str(config.get("language") or "").strip()
    return language or "zh-CN"


def wiki_language_instruction(paths: WikiPaths) -> str:
    """Return prompt rules that make the wiki's configured language explicit."""
    language = wiki_language(paths)
    return f"""## Target Wiki Language

Target wiki language: `{language}`

Hard language rules:
- Write explanatory prose, source summaries, generated page content, purpose updates, and maintenance updates in `{language}` by default.
- If the target wiki language starts with `zh`, use Chinese explanatory prose even when imported sources are English.
- Preserve original or conventional capitalization for paper titles, proper nouns, model names, method names, datasets, authors, organizations, and acronyms.
- Do not translate names or technical identifiers when translation would make them less recognizable."""


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


def _model_text(response: object) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        return "\n".join(str(item) for item in content)
    return str(content)


def _read_file(path: Path, *, max_chars: int) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]


def _purpose_context(paths: WikiPaths, changed_paths: list[str]) -> str:
    parts: list[str] = []
    for path in (paths.purpose_file, paths.wiki_index_file, paths.wiki_overview_file):
        if path.is_file():
            rel = path.relative_to(paths.root).as_posix()
            parts.append(f"## {rel}\n{_read_file(path, max_chars=6_000)}")

    for rel in changed_paths:
        if not rel or rel == "purpose.md":
            continue
        path = (paths.root / rel).resolve()
        try:
            path.relative_to(paths.root.resolve())
        except ValueError:
            continue
        if path.is_file() and path.suffix.lower() == ".md":
            parts.append(f"## Changed file: {rel}\n{_read_file(path, max_chars=_MAX_CHANGED_FILE_CHARS)}")

    return "\n\n".join(parts)[:_MAX_PURPOSE_CONTEXT_CHARS]


def update_purpose_after_wiki_change(
    paths: WikiPaths,
    *,
    change_summary: str,
    changed_paths: list[str],
    model_name: str | None = None,
) -> dict[str, Any]:
    """Update purpose.md after knowledge-bearing wiki changes, without failing the caller."""
    result: dict[str, Any] = {
        "updated": False,
        "summary": "Purpose update skipped.",
    }
    if not paths.purpose_file.is_file():
        result["error"] = "purpose.md not found"
        return result

    try:
        prompt = f"""You maintain purpose.md for an LLM-maintained research wiki.

Decide whether the latest wiki change should update purpose.md. The purpose
file should stay concise and express the wiki's current research goals, key
questions, scope, and current thesis/assumptions.

{wiki_language_instruction(paths)}

Return ONLY valid JSON:
{{
  "should_update": true,
  "purpose_markdown": "Full updated purpose.md Markdown, or empty string when should_update is false",
  "summary": "Short summary of the purpose decision"
}}

Rules:
- Update purpose.md when new content changes the research goals, key questions,
  scope, current thesis, assumptions, or open questions.
- Keep the existing purpose.md structure and intent unless the new content
  clearly requires a focused adjustment.
- Do not add unsupported facts. Base updates on the provided wiki context and
  changed files.
- If no durable purpose-level change is needed, return should_update false.

Latest wiki change:
{change_summary}

Changed paths:
{json.dumps(changed_paths, ensure_ascii=False, indent=2)}

Current wiki context:
{_purpose_context(paths, changed_paths)}
"""
        model = create_chat_model(name=model_name, thinking_enabled=False)
        data = _json_from_model_text(_model_text(model.invoke(prompt, config={"run_name": "wiki_purpose_update"})))
        if data is None:
            result["error"] = "Purpose update model did not return valid JSON"
            return result

        summary = str(data.get("summary") or result["summary"]).strip() or result["summary"]
        result["summary"] = summary
        if not bool(data.get("should_update")):
            return result

        purpose_markdown = str(data.get("purpose_markdown") or "").strip()
        if not purpose_markdown:
            result["error"] = "Purpose update requested but returned empty purpose_markdown"
            return result

        paths.purpose_file.write_text(purpose_markdown + "\n", encoding="utf-8")
        result["updated"] = True
        result["path"] = "purpose.md"
        return result
    except Exception as exc:  # Purpose updates must not make ingest/archive fail.
        result["error"] = str(exc)
        return result
