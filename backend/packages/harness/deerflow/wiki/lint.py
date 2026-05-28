"""LLM Wiki lint checks.

Structural lint is local-only and checks wikilink graph health.
Semantic lint asks the configured chat model to find higher-level content issues.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from deerflow.models import create_chat_model
from deerflow.wiki.markdown import extract_wikilinks
from deerflow.wiki.paths import WikiPaths
from deerflow.wiki.schema import schema_context

LintType = str
Severity = str
LintMode = Literal["light", "deep"]

LIGHT_LINT_TRIGGERS = {
    "add_source",
    "page_edit",
    "page_move",
    "page_rename",
    "page_delete",
    "source_cleanup",
    "manual",
}
DEEP_LINT_TRIGGERS = {
    "batch_ingest",
    "deep_manual",
    "semantic_manual",
    "important_source_delete",
    "important_source_replace",
    "core_page_update",
    "chat_quality_drop",
    "query_quality_drop",
}

_SEMANTIC_LINT_RE = re.compile(
    r"---LINT:\s*([^\n|]+?)\s*\|\s*([^\n|]+?)\s*\|\s*([^\n-]+?)\s*---\n([\s\S]*?)---END LINT---",
    re.MULTILINE,
)
_MAX_SEMANTIC_PAGE_CHARS = 500


@dataclass
class LintIssue:
    """One lint result returned by wiki_lint."""

    type: LintType
    severity: Severity
    page: str
    detail: str
    affectedPages: list[str] = field(default_factory=list)


def resolve_lint_mode(*, mode: str = "light", trigger: str | None = None, include_semantic: bool | None = None) -> LintMode:
    """Resolve lint mode from an explicit mode, trigger reason, or legacy flag."""
    if include_semantic is True:
        return "deep"

    normalized_mode = (mode or "light").strip().lower()
    normalized_trigger = (trigger or "").strip().lower()

    if normalized_mode in {"deep", "semantic"}:
        return "deep"
    if normalized_mode in {"light", "structural"}:
        return "light"
    if normalized_mode == "auto":
        if normalized_trigger in DEEP_LINT_TRIGGERS:
            return "deep"
        return "light"

    raise ValueError("lint mode must be one of: light, deep, auto")


def _wiki_relative_path(paths: WikiPaths, path: Path) -> str:
    return path.relative_to(paths.wiki_dir).as_posix()


def _is_structural_system_page(paths: WikiPaths, path: Path) -> bool:
    return path.resolve() in {paths.wiki_index_file.resolve(), paths.wiki_log_file.resolve()}


def _is_semantic_system_page(paths: WikiPaths, path: Path) -> bool:
    return path.resolve() == paths.wiki_log_file.resolve()


def _markdown_pages(paths: WikiPaths, *, semantic: bool = False) -> list[Path]:
    pages = sorted(paths.wiki_dir.rglob("*.md"))
    if semantic:
        return [path for path in pages if not _is_semantic_system_page(paths, path)]
    return [path for path in pages if not _is_structural_system_page(paths, path)]


def _normalize_link_target(value: str) -> str:
    target = value.strip().replace("\\", "/")
    if target.lower().endswith(".md"):
        target = target[:-3]
    return target.strip("/").lower()


def _slug_map(paths: WikiPaths, pages: list[Path]) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    for path in pages:
        rel = _wiki_relative_path(paths, path)
        rel_slug = _normalize_link_target(Path(rel).with_suffix("").as_posix())
        basename_slug = _normalize_link_target(path.stem)
        mapping.setdefault(rel_slug, path)
        mapping.setdefault(basename_slug, path)
    return mapping


def structural_lint_wiki(paths: WikiPaths) -> list[LintIssue]:
    """Run local wikilink graph checks over wiki/**/*.md."""
    pages = _markdown_pages(paths)
    slug_map = _slug_map(paths, pages)
    page_links: dict[Path, list[str]] = {}
    inbound_count: dict[Path, int] = {path: 0 for path in pages}
    issues: list[LintIssue] = []

    for path in pages:
        text = path.read_text(encoding="utf-8", errors="ignore")
        links = extract_wikilinks(text)
        page_links[path] = links
        for link in links:
            target = slug_map.get(_normalize_link_target(link))
            if target is None:
                issues.append(
                    LintIssue(
                        type="broken-link",
                        severity="warning",
                        page=_wiki_relative_path(paths, path),
                        detail=f"Broken link: [[{link}]] - target page not found.",
                    )
                )
            elif target != path:
                inbound_count[target] += 1

    for path in pages:
        rel = _wiki_relative_path(paths, path)
        if inbound_count[path] == 0:
            issues.append(
                LintIssue(
                    type="orphan",
                    severity="info",
                    page=rel,
                    detail="No other pages link to this page.",
                )
            )
        if not page_links.get(path):
            issues.append(
                LintIssue(
                    type="no-outlinks",
                    severity="info",
                    page=rel,
                    detail="This page has no [[wikilink]] references to other pages.",
                )
            )

    return issues


def _semantic_page_summaries(paths: WikiPaths) -> str:
    sections: list[str] = ["## Wiki Pages"]
    for path in _markdown_pages(paths, semantic=True):
        rel = _wiki_relative_path(paths, path)
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        sections.append(f"### {rel}\n{text[:_MAX_SEMANTIC_PAGE_CHARS]}")
    return "\n\n".join(sections)


def _semantic_prompt(paths: WikiPaths) -> str:
    return f"""You are a Wiki quality analyst.

Find genuine semantic quality issues in this LLM Wiki. Supported issue types:
- contradiction
- stale
- missing-page
- suggestion

Supported severities:
- warning
- info

Output ONLY structured lint blocks in this format:
---LINT: type | severity | Short title---
Description of the issue.
PAGES: page1.md, page2.md
---END LINT---

Rules:
- Evaluate pages against the active schema.md contract.
- Only report genuine issues.
- Do not invent problems.
- Do not output any text outside lint blocks.

{schema_context(paths)}

{_semantic_page_summaries(paths)}
"""


def parse_semantic_lint_output(text: str) -> list[LintIssue]:
    """Parse ---LINT--- blocks returned by the semantic lint model."""
    issues: list[LintIssue] = []
    for match in _SEMANTIC_LINT_RE.finditer(text):
        raw_type = match.group(1).strip()
        raw_severity = match.group(2).strip().lower()
        title = match.group(3).strip()
        body = match.group(4).strip()
        pages: list[str] = []

        description_lines: list[str] = []
        for line in body.splitlines():
            if line.strip().lower().startswith("pages:"):
                raw_pages = line.split(":", 1)[1]
                pages = [page.strip() for page in raw_pages.split(",") if page.strip()]
            else:
                description_lines.append(line)

        description = "\n".join(description_lines).strip()
        severity = "warning" if raw_severity == "warning" else "info"
        issues.append(
            LintIssue(
                type="semantic",
                severity=severity,
                page=title,
                detail=f"[{raw_type}] {description}",
                affectedPages=pages,
            )
        )
    return issues


def semantic_lint_wiki(paths: WikiPaths, *, model_name: str | None = None) -> list[LintIssue]:
    """Run LLM-based semantic wiki quality checks."""
    model = create_chat_model(name=model_name, thinking_enabled=False)
    response = model.invoke(_semantic_prompt(paths), config={"run_name": "wiki_semantic_lint"})
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "\n".join(str(item) for item in content)
    return parse_semantic_lint_output(str(content))


def lint_wiki(
    paths: WikiPaths,
    *,
    mode: str = "light",
    trigger: str | None = None,
    include_semantic: bool | None = None,
    model_name: str | None = None,
) -> list[LintIssue]:
    """Run wiki lint checks."""
    lint_mode = resolve_lint_mode(mode=mode, trigger=trigger, include_semantic=include_semantic)
    issues = structural_lint_wiki(paths)
    if lint_mode == "deep":
        if model_name is None:
            issues.extend(semantic_lint_wiki(paths))
        else:
            issues.extend(semantic_lint_wiki(paths, model_name=model_name))
    return issues
