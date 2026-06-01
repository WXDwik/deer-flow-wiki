"""Deterministic maintenance for human-readable wiki system pages."""

# ruff: noqa: E501

from __future__ import annotations

from pathlib import Path
from typing import Any

from deerflow.wiki.paths import WikiPaths
from deerflow.wiki.purpose import wiki_language
from deerflow.wiki.repository import WikiRepository

_SECTION_TITLES = {
    "source": "Sources",
    "background": "Background",
    "idea": "Idea",
    "system_model": "System Model",
    "algorithm": "Algorithm",
    "dataset": "Datasets",
    "summary": "Summary",
    "concept": "Concept",
    "synthesis": "Synthesis",
    "maintenance": "Maintenance",
}

_SECTION_ORDER = ("source", "background", "idea", "system_model", "algorithm", "dataset", "summary", "concept", "synthesis", "maintenance")


def _wiki_title(paths: WikiPaths) -> str:
    config = WikiRepository(paths).read_config()
    return str(config.get("title") or paths.root.name).strip() or paths.root.name


def _page_link(page: dict[str, Any]) -> str | None:
    path = str(page.get("path") or "").strip()
    title = str(page.get("title") or "").strip()
    if not path:
        return None
    stem = Path(path).stem
    label = title or stem
    target = stem if stem == label else f"{stem}|{label}"
    return f"[[{target}]]"


def _group_pages(paths: WikiPaths) -> dict[str, list[dict[str, Any]]]:
    index = WikiRepository(paths).read_index()
    grouped: dict[str, list[dict[str, Any]]] = {section: [] for section in _SECTION_ORDER}
    for page in index.get("pages", []):
        if not isinstance(page, dict):
            continue
        page_type = str(page.get("page_type") or page.get("type") or "").strip()
        if page_type in grouped:
            grouped[page_type].append(page)
    for pages in grouped.values():
        pages.sort(key=lambda item: str(item.get("title") or item.get("path") or "").lower())
    return grouped


def rebuild_index_markdown(paths: WikiPaths) -> dict[str, Any]:
    """Rebuild wiki/index.md from .llm-wiki/index.json."""
    title = _wiki_title(paths)
    grouped = _group_pages(paths)
    lines = [f"# {title} Index", ""]
    page_count = 0

    for page_type in _SECTION_ORDER:
        lines.append(f"## {_SECTION_TITLES[page_type]}")
        lines.append("")
        for page in grouped[page_type]:
            link = _page_link(page)
            if link is None:
                continue
            tags = page.get("tags") if isinstance(page.get("tags"), list) else []
            tag_text = f" — {', '.join(str(tag) for tag in tags[:4])}" if tags else ""
            lines.append(f"- {link}{tag_text}")
            page_count += 1
        lines.append("")

    paths.wiki_index_file.parent.mkdir(parents=True, exist_ok=True)
    paths.wiki_index_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return {"path": "wiki/index.md", "updated": True, "page_count": page_count}


def update_overview_markdown(paths: WikiPaths) -> dict[str, Any]:
    """Write a compact overview from the current page index."""
    title = _wiki_title(paths)
    language = wiki_language(paths)
    grouped = _group_pages(paths)
    sources = grouped["source"]
    background = grouped["background"]
    ideas = grouped["idea"]
    system_models = grouped["system_model"]
    algorithms = grouped["algorithm"]
    datasets = grouped["dataset"]
    summaries = grouped["summary"]
    concepts = grouped["concept"]
    synthesis = grouped["synthesis"]

    def links(pages: list[dict[str, Any]], limit: int = 8) -> list[str]:
        result = []
        for page in pages[:limit]:
            link = _page_link(page)
            if link:
                result.append(link)
        return result

    source_links = links(sources, 6)
    background_links = links(background, 6)
    idea_links = links(ideas, 6)
    system_model_links = links(system_models, 6)
    algorithm_links = links(algorithms, 6)
    dataset_links = links(datasets, 6)
    summary_links = links(summaries, 6)
    concept_links = links(concepts, 8)
    synthesis_links = links(synthesis, 6)

    if language.lower().startswith("zh"):
        lines = [
            f"# {title} Overview",
            "",
            f"当前 wiki 已导入 {len(sources)} 份资料，维护 {len(background)} 个背景页面、{len(ideas)} 个创新点页面、{len(system_models)} 个系统模型页面、{len(algorithms)} 个算法页面、{len(datasets)} 个数据集页面、{len(summaries)} 个研究现状摘要、{len(concepts)} 个复用概念和 {len(synthesis)} 个综合启发页面。",
            "",
            "## 资料",
            "",
            *(f"- {link}" for link in source_links),
            "",
            "## 背景与创新点",
            "",
            *(f"- {link}" for link in background_links + idea_links),
            "",
            "## 系统模型、算法与数据集",
            "",
            *(f"- {link}" for link in system_model_links + algorithm_links + dataset_links),
            "",
            "## 研究现状摘要与复用概念",
            "",
            *(f"- {link}" for link in summary_links + concept_links),
            "",
            "## 综合启发",
            "",
            *(f"- {link}" for link in synthesis_links),
        ]
    else:
        lines = [
            f"# {title} Overview",
            "",
            (
                f"This wiki currently contains {len(sources)} imported sources, "
                f"{len(background)} background pages, {len(ideas)} idea pages, "
                f"{len(system_models)} system-model pages, {len(algorithms)} algorithm pages, "
                f"{len(datasets)} dataset pages, {len(summaries)} summary pages, "
                f"{len(concepts)} concept pages, and {len(synthesis)} synthesis pages."
            ),
            "",
            "## Sources",
            "",
            *(f"- {link}" for link in source_links),
            "",
            "## Background And Ideas",
            "",
            *(f"- {link}" for link in background_links + idea_links),
            "",
            "## System Models, Algorithms, And Datasets",
            "",
            *(f"- {link}" for link in system_model_links + algorithm_links + dataset_links),
            "",
            "## Summaries And Concepts",
            "",
            *(f"- {link}" for link in summary_links + concept_links),
            "",
            "## Synthesis",
            "",
            *(f"- {link}" for link in synthesis_links),
        ]

    paths.wiki_overview_file.parent.mkdir(parents=True, exist_ok=True)
    paths.wiki_overview_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return {
        "path": "wiki/overview.md",
        "updated": True,
        "source_count": len(sources),
        "background_count": len(background),
        "idea_count": len(ideas),
        "system_model_count": len(system_models),
        "algorithm_count": len(algorithms),
        "dataset_count": len(datasets),
        "summary_count": len(summaries),
        "concept_count": len(concepts),
        "synthesis_count": len(synthesis),
    }
