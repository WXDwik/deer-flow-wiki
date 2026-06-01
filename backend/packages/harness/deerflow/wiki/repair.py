"""LLM-driven repair for LLM Wiki lint issues."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deerflow.models import create_chat_model
from deerflow.wiki.lint import LintIssue, lint_wiki, resolve_lint_mode
from deerflow.wiki.paths import WikiPaths, display_slugify_name, slugify_name

_MAX_FILE_CHARS = 12_000
_MAX_TOTAL_CONTEXT_CHARS = 60_000


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


def _issue_to_dict(issue: LintIssue | dict) -> dict:
    if is_dataclass(issue):
        return asdict(issue)
    return dict(issue)


def _root_relative_markdown_path(paths: WikiPaths, page: str) -> Path | None:
    raw = page.strip().replace("\\", "/")
    if not raw:
        return None
    if not raw.startswith("wiki/"):
        raw = f"wiki/{raw}"
    candidate = (paths.root / raw).resolve()
    try:
        candidate.relative_to(paths.wiki_dir.resolve())
    except ValueError:
        return None
    if candidate.suffix.lower() != ".md":
        return None
    return candidate


def _allowed_edit_path(paths: WikiPaths, raw_path: str) -> Path:
    candidate = (paths.root / raw_path.strip().replace("\\", "/")).resolve()
    try:
        candidate.relative_to(paths.wiki_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"Repair may only edit files under wiki/: {raw_path}") from exc
    if candidate.suffix.lower() != ".md":
        raise ValueError(f"Repair may only edit Markdown files: {raw_path}")
    return candidate


def _repair_action(issue: dict) -> str:
    issue_type = issue.get("type")
    detail = str(issue.get("detail") or "")
    if issue_type == "broken-link":
        return "fix_or_create_wikilink_target"
    if issue_type == "orphan":
        return "connect_orphan_page"
    if issue_type == "no-outlinks":
        return "add_relevant_outlinks"
    if issue_type == "semantic":
        lowered = detail.lower()
        if "[contradiction]" in lowered:
            return "reconcile_conflicting_claims"
        if "[stale]" in lowered:
            return "update_stale_claims"
        if "[missing-page]" in lowered:
            return "add_or_link_missing_concept_page"
        return "improve_semantic_quality"
    return "repair_markdown"


def _broken_link_target(issue: dict) -> str | None:
    if issue.get("type") != "broken-link":
        return None
    detail = str(issue.get("detail") or "")
    match = re.search(r"Broken link:\s*\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", detail)
    return match.group(1).strip() if match else None


def _link_match_key(value: str) -> str:
    try:
        return slugify_name(value)
    except ValueError:
        return display_slugify_name(value).lower()


def _existing_page_stems(paths: WikiPaths) -> dict[str, str]:
    return {_link_match_key(path.stem): path.stem for path in paths.wiki_dir.rglob("*.md") if path.is_file()}


def _deterministic_slug_link_repairs(paths: WikiPaths, issues: list[dict]) -> tuple[list[dict[str, str]], list[dict]]:
    stems_by_normalized = _existing_page_stems(paths)
    remaining: list[dict] = []
    originals: dict[Path, str] = {}
    repaired_by_path: dict[Path, str] = {}

    for issue in issues:
        target = _broken_link_target(issue)
        page = str(issue.get("page") or "")
        page_path = _root_relative_markdown_path(paths, page)
        if target is None or page_path is None:
            remaining.append(issue)
            continue

        try:
            key = _link_match_key(target)
        except ValueError:
            remaining.append(issue)
            continue

        actual_stem = stems_by_normalized.get(key)
        if actual_stem is None:
            remaining.append(issue)
            continue

        current = repaired_by_path.get(page_path)
        if current is None:
            current = page_path.read_text(encoding="utf-8", errors="ignore") if page_path.exists() else ""
            originals[page_path] = current
        pattern = re.compile(r"\[\[\s*" + re.escape(target) + r"\s*(?:\|[^\]]+)?\]\]")
        label = target.strip()
        replacement = f"[[{actual_stem}]]" if label == actual_stem else f"[[{actual_stem}|{label}]]"
        repaired = pattern.sub(replacement, current)
        if repaired == current:
            remaining.append(issue)
            continue

        repaired_by_path[page_path] = repaired

    changes = [
        {
            "path": page_path.relative_to(paths.root).as_posix(),
            "operation": "replace",
            "reason": "Replace title-style wikilinks with existing page filename stems.",
            "old": originals[page_path],
            "new": repaired,
        }
        for page_path, repaired in repaired_by_path.items()
        if repaired != originals[page_path]
    ]

    return changes, remaining


def build_repair_instructions(paths: WikiPaths, issues: list[LintIssue | dict]) -> list[dict[str, Any]]:
    """Convert lint issues into LLM-readable repair instructions."""
    instructions: list[dict[str, Any]] = []
    for index, raw_issue in enumerate(issues, 1):
        issue = _issue_to_dict(raw_issue)
        read_paths: list[str] = []

        if issue.get("type") != "semantic" and issue.get("page"):
            path = _root_relative_markdown_path(paths, str(issue["page"]))
            if path is not None:
                read_paths.append(path.relative_to(paths.root).as_posix())

        for page in issue.get("affectedPages") or []:
            path = _root_relative_markdown_path(paths, str(page))
            if path is not None:
                read_paths.append(path.relative_to(paths.root).as_posix())

        for path in (paths.wiki_index_file, paths.wiki_overview_file):
            if path.is_file():
                read_paths.append(path.relative_to(paths.root).as_posix())

        read_paths = sorted(dict.fromkeys(read_paths))
        instructions.append(
            {
                "id": f"lint-{index:03d}",
                "issue": issue,
                "repair": {
                    "mode": "semantic" if issue.get("type") == "semantic" else "structural",
                    "action": _repair_action(issue),
                    "read": read_paths,
                    "allowedEdits": [
                        "wiki/index.md",
                        "wiki/overview.md",
                        "wiki/log.md",
                        "wiki/sources/*.md",
                        "wiki/background/*.md",
                        "wiki/idea/*.md",
                        "wiki/system_model/*.md",
                        "wiki/algorithm/*.md",
                        "wiki/datasets/*.md",
                        "wiki/summary/*.md",
                        "wiki/concept/*.md",
                        "wiki/synthesis/*.md",
                    ],
                    "instruction": (
                        "Repair this issue by editing Markdown files under wiki/ only. Do not modify raw/, .llm-wiki/, binary assets, or source documents. Prefer updating existing pages. Create new Markdown pages only when needed."
                    ),
                    "verify": [
                        "Run light lint again.",
                        "The repaired structural issues should disappear or be reduced.",
                    ],
                },
            }
        )
    return instructions


def _read_context(paths: WikiPaths, repair_instructions: list[dict[str, Any]]) -> dict[str, str]:
    wanted = set()
    for item in repair_instructions:
        wanted.update(item["repair"].get("read", []))
    if paths.schema_file.is_file():
        wanted.add(paths.schema_file.relative_to(paths.root).as_posix())
    if paths.purpose_file.is_file():
        wanted.add(paths.purpose_file.relative_to(paths.root).as_posix())

    context: dict[str, str] = {}
    total = 0
    for rel in sorted(wanted):
        path = (paths.root / rel).resolve()
        try:
            path.relative_to(paths.root)
        except ValueError:
            continue
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")[:_MAX_FILE_CHARS]
        if total + len(text) > _MAX_TOTAL_CONTEXT_CHARS:
            break
        context[rel] = text
        total += len(text)
    return context


def _repair_prompt(paths: WikiPaths, repair_instructions: list[dict[str, Any]], context: dict[str, str]) -> str:
    return f"""You are maintaining an LLM Wiki.

Repair the lint issues by editing Markdown files only.

Hard constraints:
- You may edit or create files only under wiki/.
- Every edited or created file must be Markdown (.md).
- Do not modify raw/, raw/sources/, raw/assets/, .llm-wiki/, or source documents.
- Follow schema.md from the available file context as the active wiki contract.
- Preserve source-grounded nuance. If sources conflict, document the disagreement instead of inventing certainty.
- Do not add unrelated content just to satisfy lint.
- Wikilinks must target existing page filename stems without .md. Preserve
  readable filename capitalization when the file stem uses it.
- Use aliases such as [[Page-Stem|Readable Label]] when visible link text should
  remain human-readable. Preserve Chinese explanatory prose where appropriate
  and preserve the original or conventional capitalization of English proper
  nouns, paper titles, model names, methods, datasets, authors, organizations,
  and acronyms in visible titles, headings, labels, and prose.
- For broken links, if a slugified target page exists, replace the link with
  that slug. Create pages only for genuinely missing, source-supported topics.

Return ONLY valid JSON:
{{
  "summary": "Short repair summary",
  "changes": [
    {{
      "path": "wiki/concept/example.md",
      "operation": "replace",
      "reason": "Why this file changes",
      "old": "Exact existing Markdown substring to replace",
      "new": "Replacement Markdown"
    }}
  ]
}}

Change operations:
- Use "replace" by default for precise local edits. The old text must be an exact substring of the current file.
- Use "append" only when adding a small new section or link to an existing page.
- Use "rewrite" only when the page structure or synthesis needs a full-file rewrite.

For rewrite changes, use:
{{"path": "...", "operation": "rewrite", "reason": "...", "content": "Full new Markdown content"}}

Lint repair instructions:
{json.dumps(repair_instructions, ensure_ascii=False, indent=2)}

Available file context:
{json.dumps(context, ensure_ascii=False, indent=2)}
"""


def _validate_model_changes(paths: WikiPaths, data: dict[str, Any]) -> list[dict[str, str]]:
    changes = data.get("changes")
    if not isinstance(changes, list):
        raise ValueError("Repair model response must contain a changes list")

    validated: list[dict[str, str]] = []
    for item in changes:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "").strip().replace("\\", "/")
        operation = str(item.get("operation") or "replace").strip().lower()
        reason = str(item.get("reason") or "")
        if not rel:
            continue
        if operation not in {"replace", "append", "rewrite"}:
            raise ValueError(f"Unsupported repair operation: {operation}")
        path = _allowed_edit_path(paths, rel)
        change = {
            "path": path.relative_to(paths.root).as_posix(),
            "operation": operation,
            "reason": reason,
        }
        if operation == "replace":
            old = item.get("old")
            new = item.get("new")
            if not isinstance(old, str) or not isinstance(new, str) or not old:
                raise ValueError("Replace repair changes require non-empty old and string new fields")
            change["old"] = old
            change["new"] = new
        elif operation == "append":
            content = item.get("content")
            if not isinstance(content, str):
                raise ValueError("Append repair changes require a content field")
            change["content"] = content
        else:
            content = item.get("content")
            if not isinstance(content, str):
                raise ValueError("Rewrite repair changes require a content field")
            change["content"] = content
        validated.append(change)
    return validated


def _apply_change(paths: WikiPaths, change: dict[str, str]) -> None:
    path = (paths.root / change["path"]).resolve()
    _allowed_edit_path(paths, change["path"])
    path.parent.mkdir(parents=True, exist_ok=True)

    operation = change["operation"]
    if operation == "rewrite":
        path.write_text(change["content"], encoding="utf-8")
        return

    current = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    if operation == "append":
        separator = "\n" if current.endswith("\n") or not current else "\n\n"
        path.write_text(f"{current}{separator}{change['content']}", encoding="utf-8")
        return

    old = change["old"]
    if old not in current:
        raise ValueError(f"Replace text not found in {change['path']}")
    path.write_text(current.replace(old, change["new"], 1), encoding="utf-8")


def _append_repair_log(paths: WikiPaths, summary: str, changed_paths: list[str]) -> None:
    now = datetime.now(UTC).isoformat()
    entry = f"\n## [{now}] lint-repair\n\n{summary}\n\nChanged files:\n"
    for path in changed_paths:
        entry += f"- `{path}`\n"
    paths.wiki_log_file.parent.mkdir(parents=True, exist_ok=True)
    with paths.wiki_log_file.open("a", encoding="utf-8") as f:
        f.write(entry)


def repair_lint(
    paths: WikiPaths,
    *,
    mode: str = "light",
    trigger: str = "manual",
    dry_run: bool = True,
    issues: list[LintIssue | dict] | None = None,
    model_name: str | None = None,
) -> dict[str, Any]:
    """Use the configured chat model to repair wiki lint issues."""
    lint_mode = resolve_lint_mode(mode=mode, trigger=trigger)
    lint_issues = issues if issues is not None else lint_wiki(paths, mode=lint_mode, trigger=trigger, model_name=model_name)
    issue_dicts = [_issue_to_dict(issue) for issue in lint_issues]
    if not issue_dicts:
        return {
            "ok": True,
            "mode": lint_mode,
            "trigger": trigger,
            "dry_run": dry_run,
            "issues": [],
            "repair": {"summary": "No lint issues to repair.", "changes": []},
            "post_lint": None,
        }

    deterministic_changes, remaining_issues = _deterministic_slug_link_repairs(paths, issue_dicts)
    model_changes: list[dict[str, str]] = []
    summary_parts: list[str] = []
    if deterministic_changes:
        summary_parts.append("Replaced title-style wikilinks with existing page filename stems.")

    if remaining_issues:
        instructions = build_repair_instructions(paths, remaining_issues)
        context = _read_context(paths, instructions)
        model = create_chat_model(name=model_name, thinking_enabled=False)
        response = model.invoke(_repair_prompt(paths, instructions, context), config={"run_name": "wiki_lint_repair"})
        content = getattr(response, "content", response)
        if isinstance(content, list):
            content = "\n".join(str(item) for item in content)
        data = _json_from_model_text(str(content))
        if data is None:
            raise ValueError("Repair model did not return valid JSON")
        model_changes = _validate_model_changes(paths, data)
        summary_parts.append(str(data.get("summary") or "Applied lint repair."))

    changes = deterministic_changes + model_changes
    summary = " ".join(summary_parts) or "Applied lint repair."
    if not dry_run:
        for change in changes:
            _apply_change(paths, change)
        _append_repair_log(paths, summary, [change["path"] for change in changes])
        post_lint = [asdict(issue) for issue in lint_wiki(paths, mode="light", trigger="repair_verify", model_name=model_name)]
    else:
        post_lint = None

    return {
        "ok": True,
        "mode": lint_mode,
        "trigger": trigger,
        "dry_run": dry_run,
        "issues": issue_dicts,
        "repair": {
            "summary": summary,
            "changes": [
                {
                    "path": change["path"],
                    "operation": change["operation"],
                    "reason": change["reason"],
                    "old_preview": change.get("old", "")[:500],
                    "new_preview": change.get("new", change.get("content", ""))[:1000],
                    "content_length": len(change.get("new", change.get("content", ""))),
                }
                for change in changes
            ],
        },
        "post_lint": post_lint,
    }
