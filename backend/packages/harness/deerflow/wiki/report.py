"""Research-report helpers for LLM Wiki.

This module does not generate a final report. It gives the lead agent the
wiki-side planning and context-packing primitives needed for a DeerFlow-style
deep research workflow.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
import re
from pathlib import Path
from typing import Any

from deerflow.wiki.markdown import extract_wikilinks
from deerflow.wiki.paths import WikiPaths
from deerflow.wiki.query import search_wiki
from deerflow.wiki.repository import WikiRepository

_DEFAULT_MAX_EXCERPT_CHARS = 3000
_DEFAULT_PAGE_CHARS = 6000
_DEFAULT_TOTAL_CHARS = 30000
_GRAPH_EXPANSION_LIMIT = 3
_GRAPH_MIN_RELEVANCE = 2.0

_TYPE_AFFINITY: dict[str, dict[str, float]] = {
    "entity": {"concept": 1.2, "entity": 0.8, "source": 1.0, "synthesis": 1.0, "query": 0.8},
    "concept": {"entity": 1.2, "concept": 0.8, "source": 1.0, "synthesis": 1.2, "query": 1.0},
    "source": {"entity": 1.0, "concept": 1.0, "source": 0.5, "query": 0.8, "synthesis": 1.0},
    "query": {"concept": 1.0, "entity": 0.8, "synthesis": 1.0, "source": 0.8, "query": 0.5},
    "synthesis": {"concept": 1.2, "entity": 1.0, "source": 1.0, "query": 1.0, "synthesis": 0.8},
    "comparison": {"concept": 1.0, "entity": 1.0, "source": 1.0, "query": 1.0, "synthesis": 1.2},
}


@dataclass(frozen=True)
class _GraphNode:
    title: str
    path: str
    page_type: str | None
    sources: tuple[str, ...]
    outlinks: frozenset[str]
    inlinks: frozenset[str]

_REPORT_DIMENSION_QUERY_PARTS = (
    "overview background research question",
    "method model approach architecture",
    "data dataset experiment metric evaluation",
    "comparison baseline alternative",
    "limitation challenge risk future work",
    "背景 研究问题",
    "方法 模型 技术路线",
    "实验 数据集 指标 评估",
    "对比 基线 替代方案",
    "局限 挑战 未来工作",
)


def _read_excerpt(path: Path, max_chars: int = _DEFAULT_MAX_EXCERPT_CHARS) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")[:max_chars].strip()


def _page_type_from_path(path: str) -> str | None:
    parts = path.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[0] != "wiki":
        return None
    return {
        "sources": "source",
        "entities": "entity",
        "concepts": "concept",
        "queries": "query",
        "synthesis": "synthesis",
        "comparisons": "comparison",
    }.get(parts[1])


def _index_pages_by_path(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    pages = index.get("pages", [])
    if not isinstance(pages, list):
        return {}
    return {str(item.get("path")): item for item in pages if isinstance(item, dict) and item.get("path")}


def _page_payload_from_index(page: dict[str, Any]) -> dict[str, Any]:
    path = str(page.get("path") or "")
    return {
        "title": str(page.get("title") or Path(path).stem),
        "path": path,
        "page_type": page.get("page_type") or _page_type_from_path(path),
        "tags": page.get("tags", []),
        "sources": page.get("sources", []),
    }


def _query_terms(query: str) -> list[str]:
    return [term.lower() for term in re.split(r"[\s,，。！？；;:：()\[\]{}<>\"'`/\\|-]+", query) if len(term.strip()) > 1]


def _candidate_title_match(candidate: dict[str, Any], query: str) -> bool:
    query_phrase = query.strip().lower()
    title_text = f"{candidate.get('title') or ''} {Path(str(candidate.get('path') or '')).stem}".lower()
    if query_phrase and query_phrase in title_text:
        return True
    return any(term in title_text for term in _query_terms(query))


def _first_heading(markdown: str) -> str | None:
    match = re.search(r"^#\s+(.+?)\s*$", markdown, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _frontmatter_list(markdown: str, key: str) -> list[str]:
    match = re.search(r"^---\n(.*?)\n---", markdown, flags=re.DOTALL)
    if not match:
        return []
    body = match.group(1)
    block = re.search(rf"^{re.escape(key)}:\s*\n((?:\s+-\s+.+\n?)*)", body, flags=re.MULTILINE)
    if block:
        return [
            item.group(1).strip().strip("\"'")
            for line in block.group(1).splitlines()
            if (item := re.match(r"^\s+-\s+(.+?)\s*$", line))
        ]
    inline = re.search(rf"^{re.escape(key)}:\s*\[([^\]]*)\]", body, flags=re.MULTILINE)
    if inline:
        return [part.strip().strip("\"'") for part in inline.group(1).split(",") if part.strip()]
    return []


def _normalize_link_key(value: str) -> str:
    text = value.strip().replace("\\", "/")
    if text.endswith(".md"):
        text = text[:-3]
    text = text.split("/")[-1]
    return re.sub(r"\s+", "-", text.lower())


def _build_retrieval_graph(paths: WikiPaths, index: dict[str, Any]) -> dict[str, _GraphNode]:
    pages_by_path = _index_pages_by_path(index)
    raw_nodes: dict[str, dict[str, Any]] = {}
    resolve_map: dict[str, str] = {}

    for page_path in sorted(paths.wiki_dir.rglob("*.md")):
        rel_path = page_path.relative_to(paths.root).as_posix()
        text = page_path.read_text(encoding="utf-8", errors="ignore")
        indexed = pages_by_path.get(rel_path, {})
        title = str(indexed.get("title") or _first_heading(text) or page_path.stem)
        page_type = indexed.get("page_type") or _page_type_from_path(rel_path)
        sources = indexed.get("sources")
        if not isinstance(sources, list):
            sources = _frontmatter_list(text, "sources")
        raw_nodes[rel_path] = {
            "title": title,
            "path": rel_path,
            "page_type": str(page_type) if page_type else None,
            "sources": tuple(str(item) for item in sources if isinstance(item, str)),
            "raw_links": extract_wikilinks(text),
        }
        for key in {rel_path, rel_path.removeprefix("wiki/"), page_path.stem, title}:
            resolve_map[_normalize_link_key(key)] = rel_path

    outlinks: dict[str, set[str]] = {path: set() for path in raw_nodes}
    inlinks: dict[str, set[str]] = {path: set() for path in raw_nodes}
    for rel_path, node in raw_nodes.items():
        for raw_link in node["raw_links"]:
            target = resolve_map.get(_normalize_link_key(raw_link))
            if target and target != rel_path:
                outlinks[rel_path].add(target)
                inlinks[target].add(rel_path)

    return {
        rel_path: _GraphNode(
            title=str(node["title"]),
            path=rel_path,
            page_type=node["page_type"],
            sources=node["sources"],
            outlinks=frozenset(outlinks[rel_path]),
            inlinks=frozenset(inlinks[rel_path]),
        )
        for rel_path, node in raw_nodes.items()
    }


def _neighbors(node: _GraphNode) -> set[str]:
    return set(node.outlinks) | set(node.inlinks)


def _graph_relevance(a: _GraphNode, b: _GraphNode, graph: dict[str, _GraphNode]) -> float:
    if a.path == b.path:
        return 0.0

    direct = (1 if b.path in a.outlinks else 0) + (1 if a.path in b.outlinks else 0)
    source_overlap = len(set(a.sources) & set(b.sources))
    common = _neighbors(a) & _neighbors(b)
    adamic_adar = 0.0
    for path in common:
        node = graph.get(path)
        if node:
            degree = len(node.outlinks) + len(node.inlinks)
            adamic_adar += 1.0 / math.log(max(degree, 2))
    affinity = _TYPE_AFFINITY.get(a.page_type or "", {}).get(b.page_type or "", 0.5)
    return direct * 3.0 + source_overlap * 4.0 + adamic_adar * 1.5 + affinity


def _expand_graph_candidates(
    paths: WikiPaths,
    index: dict[str, Any],
    seeds: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not seeds:
        return []
    graph = _build_retrieval_graph(paths, index)
    seed_paths = {str(seed.get("path") or "") for seed in seeds}
    expansions: dict[str, dict[str, Any]] = {}

    for seed in seeds:
        seed_path = str(seed.get("path") or "")
        seed_node = graph.get(seed_path)
        if seed_node is None:
            continue
        scored: list[tuple[float, _GraphNode]] = []
        for path, node in graph.items():
            if path in seed_paths:
                continue
            relevance = _graph_relevance(seed_node, node, graph)
            if relevance >= _GRAPH_MIN_RELEVANCE:
                scored.append((relevance, node))
        scored.sort(key=lambda item: (-item[0], item[1].path))
        for relevance, node in scored[:_GRAPH_EXPANSION_LIMIT]:
            existing = expansions.get(node.path)
            if existing is None or relevance > float(existing.get("score") or 0):
                expansions[node.path] = {
                    "title": node.title,
                    "path": node.path,
                    "page_type": node.page_type,
                    "score": relevance,
                    "snippet": "",
                    "matched_queries": [],
                    "retrieval_source": "graph",
                }

    return sorted(expansions.values(), key=lambda item: (-float(item.get("score") or 0), str(item.get("path") or "")))


def _suggest_queries(report_goal: str, max_queries: int) -> list[str]:
    goal = report_goal.strip()
    if not goal:
        base = [
            "overview",
            "summary",
            "method approach",
            "experiment evaluation",
            "comparison limitation",
            "概述 总结",
            "方法 实验 对比 局限",
        ]
        return base[:max_queries]

    queries = [goal]
    queries.extend(f"{goal} {part}" for part in _REPORT_DIMENSION_QUERY_PARTS)
    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        normalized = " ".join(query.split())
        if normalized and normalized not in seen:
            deduped.append(normalized)
            seen.add(normalized)
        if len(deduped) >= max_queries:
            break
    return deduped


def _wiki_page_path(paths: WikiPaths, rel_path: str) -> Path | None:
    normalized = rel_path.strip().replace("\\", "/").lstrip("/")
    if not normalized.startswith("wiki/"):
        return None
    candidate = (paths.root / normalized).resolve()
    try:
        candidate.relative_to(paths.wiki_dir.resolve())
    except ValueError:
        return None
    if not candidate.is_file() or candidate.suffix.lower() != ".md":
        return None
    return candidate


def _collect_search_candidates(
    paths: WikiPaths,
    queries: list[str],
    *,
    max_results_per_query: int,
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    matched_queries: dict[str, list[str]] = defaultdict(list)

    for query in queries:
        for result in search_wiki(paths, query, limit=max_results_per_query):
            page_path = _wiki_page_path(paths, result.path)
            if page_path is None:
                continue
            existing = candidates.get(result.path)
            if existing is None or result.score > existing["score"]:
                candidates[result.path] = {
                    "title": result.title,
                    "path": result.path,
                    "score": result.score,
                    "snippet": result.snippet,
                }
            matched_queries[result.path].append(query)

    ordered = sorted(candidates.values(), key=lambda item: item["score"], reverse=True)
    for item in ordered:
        item["matched_queries"] = matched_queries.get(item["path"], [])
    return ordered


def plan_report(
    paths: WikiPaths,
    report_goal: str,
    *,
    report_type: str = "deep_research",
    max_queries: int = 8,
    max_results_per_query: int = 4,
) -> dict[str, Any]:
    """Return wiki inventory and retrieval suggestions for report planning."""
    repo = WikiRepository(paths)
    config = repo.read_config()
    index = repo.read_index()
    pages_by_path = _index_pages_by_path(index)
    sources = [item for item in index.get("sources", []) if isinstance(item, dict)]
    pages = [item for item in index.get("pages", []) if isinstance(item, dict)]

    page_type_counts: dict[str, int] = {}
    tags: set[str] = set()
    for page in pages:
        page_type = str(page.get("page_type") or _page_type_from_path(str(page.get("path") or "")) or "unknown")
        page_type_counts[page_type] = page_type_counts.get(page_type, 0) + 1
        for tag in page.get("tags", []) if isinstance(page.get("tags"), list) else []:
            if isinstance(tag, str) and tag.strip():
                tags.add(tag.strip())

    queries = _suggest_queries(report_goal, max(1, max_queries))
    candidates = _collect_search_candidates(paths, queries, max_results_per_query=max(1, max_results_per_query))
    for candidate in candidates:
        indexed = pages_by_path.get(candidate["path"])
        if indexed:
            candidate.update(_page_payload_from_index(indexed))
        else:
            candidate["page_type"] = _page_type_from_path(candidate["path"])

    return {
        "root": str(paths.root),
        "title": config.get("title") or paths.root.name,
        "language": config.get("language"),
        "report_goal": report_goal,
        "report_type": report_type,
        "wiki_profile": {
            "purpose_excerpt": _read_excerpt(paths.purpose_file),
            "schema_excerpt": _read_excerpt(paths.schema_file),
            "overview_excerpt": _read_excerpt(paths.wiki_overview_file),
            "source_count": len(sources),
            "page_count": len(pages),
            "page_type_counts": page_type_counts,
            "tags": sorted(tags)[:50],
            "sample_pages": [_page_payload_from_index(page) for page in pages[:20]],
        },
        "suggested_wiki_queries": queries,
        "candidate_pages": candidates,
        "recommended_agent_flow": [
            "Lead agent loads the deep-research skill and designs the report sections.",
            "Lead agent uses candidate_pages as the wiki research map, then requests focused context packs per section.",
            "Lead agent delegates section tasks to subagents with the assigned context pack included in each prompt.",
            "Subagents draft their assigned sections and identify gaps or conflicts.",
            "Lead agent synthesizes the final report instead of concatenating section drafts.",
        ],
    }


def get_report_context(
    paths: WikiPaths,
    research_task: str,
    *,
    queries: list[str] | None = None,
    max_pages: int = 8,
    max_chars_per_page: int = _DEFAULT_PAGE_CHARS,
    total_char_budget: int = _DEFAULT_TOTAL_CHARS,
) -> dict[str, Any]:
    """Build a bounded wiki context pack for one research-report subtask."""
    repo = WikiRepository(paths)
    index = repo.read_index()
    pages_by_path = _index_pages_by_path(index)

    query_list: list[str] = []
    if research_task.strip():
        query_list.append(research_task.strip())
    query_list.extend(query.strip() for query in (queries or []) if query.strip())
    if not query_list:
        query_list = _suggest_queries("", max_pages)

    candidates = _collect_search_candidates(paths, query_list, max_results_per_query=max(1, max_pages))

    seed_candidates = candidates[:max(1, max_pages)]
    graph_expansions = _expand_graph_candidates(paths, index, seed_candidates)
    seed_paths = {str(item.get("path") or "") for item in seed_candidates}

    ordered_candidates: list[tuple[int, dict[str, Any]]] = []
    for candidate in seed_candidates:
        priority = 0 if _candidate_title_match(candidate, research_task) else 1
        candidate["retrieval_source"] = candidate.get("retrieval_source") or "search"
        ordered_candidates.append((priority, candidate))
    for candidate in graph_expansions:
        if str(candidate.get("path") or "") not in seed_paths:
            ordered_candidates.append((2, candidate))
    if not ordered_candidates and paths.wiki_overview_file.is_file():
        ordered_candidates.append(
            (
                3,
                {
                    "title": "Overview",
                    "path": paths.wiki_overview_file.relative_to(paths.root).as_posix(),
                    "page_type": "overview",
                    "score": None,
                    "matched_queries": [],
                    "snippet": "",
                    "retrieval_source": "fallback",
                },
            )
        )
    ordered_candidates.sort(key=lambda item: (item[0], -float(item[1].get("score") or 0), str(item[1].get("path") or "")))

    context_pages: list[dict[str, Any]] = []
    remaining = max(0, total_char_budget)
    seen_paths: set[str] = set()
    for priority, candidate in ordered_candidates:
        if len(context_pages) >= max_pages or remaining <= 0:
            break
        rel_path = str(candidate.get("path") or "")
        if rel_path in seen_paths:
            continue
        page_path = _wiki_page_path(paths, rel_path)
        if page_path is None:
            continue
        content_limit = min(max(1, max_chars_per_page), remaining)
        content = page_path.read_text(encoding="utf-8", errors="ignore")[:content_limit].strip()
        remaining -= len(content)
        seen_paths.add(rel_path)

        indexed = pages_by_path.get(rel_path, {})
        payload = {
            "title": candidate.get("title") or indexed.get("title") or page_path.stem,
            "path": rel_path,
            "page_type": indexed.get("page_type") or candidate.get("page_type") or _page_type_from_path(rel_path),
            "priority": priority,
            "retrieval_source": candidate.get("retrieval_source") or ("graph" if priority == 2 else "search"),
            "score": candidate.get("score"),
            "matched_queries": candidate.get("matched_queries", []),
            "snippet": candidate.get("snippet", ""),
            "tags": indexed.get("tags", []),
            "sources": indexed.get("sources", []),
            "content": content,
            "truncated": len(content) >= content_limit and page_path.stat().st_size > content_limit,
        }
        context_pages.append(payload)

    return {
        "root": str(paths.root),
        "research_task": research_task,
        "queries": query_list,
        "context_pages": context_pages,
        "page_count": len(context_pages),
        "total_chars": sum(len(page["content"]) for page in context_pages),
        "insufficient_context": len(context_pages) == 0,
        "retrieval": {
            "search_candidate_count": len(seed_candidates),
            "graph_expansion_count": len(graph_expansions),
            "priority_order": {
                "0": "title match search pages",
                "1": "content/snippet search pages",
                "2": "graph-expanded pages",
                "3": "overview fallback",
            },
        },
        "usage_guidance": [
            "Use this pack as the primary local wiki evidence for the delegated section.",
            "If the pack is thin or contradictory, report the gap instead of inventing missing evidence.",
            "Keep the output scoped to the delegated section so the lead agent can synthesize the final report.",
        ],
    }
