"""Research-report helpers for LLM Wiki.

This module does not generate a final report. It gives the lead agent the
wiki-side planning and context-packing primitives needed for a DeerFlow-style
deep research workflow.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deerflow.wiki.markdown import extract_wikilinks
from deerflow.wiki.paths import WikiPaths
from deerflow.wiki.query import qmd_query_wiki, search_wiki
from deerflow.wiki.repository import WikiRepository

_DEFAULT_PAGE_CHARS = 6000
_DEFAULT_TOTAL_CHARS = 30000
_GRAPH_EXPANSION_LIMIT = 3
_GRAPH_MIN_RELEVANCE = 2.0
_GRAPH_CONTEXT_RATIO = 0.30

_TYPE_AFFINITY: dict[str, dict[str, float]] = {
    "source": {"summary": 1.2, "idea": 1.2, "concept": 1.0, "synthesis": 1.0, "algorithm": 1.0, "dataset": 0.8},
    "background": {"summary": 1.0, "idea": 1.0, "concept": 1.0, "synthesis": 1.0, "source": 0.8},
    "idea": {"summary": 1.2, "algorithm": 1.0, "system_model": 1.0, "concept": 1.0, "synthesis": 1.2, "source": 1.0},
    "system_model": {"algorithm": 1.2, "idea": 1.0, "dataset": 1.0, "source": 1.0, "synthesis": 1.0},
    "algorithm": {"system_model": 1.2, "dataset": 1.0, "idea": 1.0, "concept": 1.0, "source": 1.0, "synthesis": 1.0},
    "dataset": {"algorithm": 1.0, "system_model": 1.0, "source": 1.0, "synthesis": 1.0},
    "summary": {"idea": 1.2, "background": 1.0, "source": 1.0, "synthesis": 1.0, "concept": 1.0},
    "concept": {"idea": 1.0, "algorithm": 1.0, "summary": 1.0, "source": 1.0, "synthesis": 1.2},
    "synthesis": {"summary": 1.2, "idea": 1.2, "concept": 1.2, "source": 1.0, "algorithm": 1.0, "dataset": 1.0},
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
    "baseline alternative tradeoff",
    "limitation challenge risk future work",
    "背景 研究问题",
    "方法 模型 技术路线",
    "实验 数据集 指标 评估",
    "对比 基线 替代方案",
    "局限 挑战 未来工作",
)


def _page_type_from_path(path: str) -> str | None:
    parts = path.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[0] != "wiki":
        return None
    return {
        "sources": "source",
        "background": "background",
        "idea": "idea",
        "system_model": "system_model",
        "algorithm": "algorithm",
        "datasets": "dataset",
        "summary": "summary",
        "concept": "concept",
        "synthesis": "synthesis",
    }.get(parts[1])


def _index_pages_by_path(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    pages = index.get("pages", [])
    if not isinstance(pages, list):
        return {}
    return {str(item.get("path")): item for item in pages if isinstance(item, dict) and item.get("path")}


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
        return [item.group(1).strip().strip("\"'") for line in block.group(1).splitlines() if (item := re.match(r"^\s+-\s+(.+?)\s*$", line))]
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
            "baseline limitation",
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


def _collect_research_candidates(
    paths: WikiPaths,
    queries: list[str],
    *,
    max_results_per_query: int,
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    matched_queries: dict[str, list[str]] = defaultdict(list)

    for query in queries:
        results = qmd_query_wiki(paths, query, limit=max_results_per_query)
        retrieval_source = "qmd_query"
        if results is None:
            results = search_wiki(paths, query, limit=max_results_per_query)
            retrieval_source = "fallback"
        for result in results:
            page_path = _wiki_page_path(paths, result.path)
            if page_path is None:
                continue
            existing = candidates.get(result.path)
            if existing is None or result.score > float(existing.get("score") or 0):
                candidates[result.path] = {
                    "title": result.title,
                    "path": result.path,
                    "score": result.score,
                    "snippet": result.snippet,
                    "retrieval_source": retrieval_source,
                }
            elif retrieval_source == "qmd_query" and existing.get("retrieval_source") == "fallback":
                existing["retrieval_source"] = "qmd_query"
            matched_queries[result.path].append(query)

    ordered = sorted(
        candidates.values(),
        key=lambda item: (
            0 if item.get("retrieval_source") == "qmd_query" else 1,
            -float(item.get("score") or 0),
            str(item.get("path") or ""),
        ),
    )
    for item in ordered:
        item["matched_queries"] = matched_queries.get(item["path"], [])
    return ordered


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _normalized_index(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    indexes: list[int] = []
    in_space = False
    for index, char in enumerate(text):
        if char.isspace():
            if not in_space:
                chars.append(" ")
                indexes.append(index)
                in_space = True
            continue
        chars.append(char.lower())
        indexes.append(index)
        in_space = False
    return "".join(chars).strip(), indexes


def _snippet_phrases(snippet: str) -> list[str]:
    normalized = _normalize_for_match(snippet)
    if not normalized:
        return []
    phrases = [normalized[:160], normalized[:100], normalized[:60]]
    words = [word for word in re.split(r"\s+", normalized) if len(word) > 2]
    for size in (12, 8, 5):
        if len(words) >= size:
            phrases.append(" ".join(words[:size]))
    deduped: list[str] = []
    seen: set[str] = set()
    for phrase in phrases:
        phrase = phrase.strip()
        if len(phrase) >= 20 and phrase not in seen:
            deduped.append(phrase)
            seen.add(phrase)
    return deduped


def _window_around(text: str, start: int, end: int, limit: int) -> str:
    if len(text) <= limit:
        return text.strip()
    match_len = max(end - start, 1)
    before = max((limit - match_len) // 2, 0)
    window_start = max(start - before, 0)
    window_end = min(window_start + limit, len(text))
    window_start = max(window_end - limit, 0)
    return text[window_start:window_end].strip()


def _content_from_snippet_window(text: str, snippet: str, limit: int) -> str | None:
    normalized_text, index_map = _normalized_index(text)
    if not normalized_text or not index_map:
        return None
    for phrase in _snippet_phrases(snippet):
        position = normalized_text.find(phrase)
        if position < 0:
            continue
        start = index_map[min(position, len(index_map) - 1)]
        end_position = min(position + len(phrase) - 1, len(index_map) - 1)
        end = index_map[end_position] + 1
        return _window_around(text, start, end, limit)
    return None


def _terms_from_texts(*texts: str) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for text in texts:
        for term in _query_terms(text):
            if term not in seen:
                seen.add(term)
                terms.append(term)
    return terms


def _content_from_paragraphs(text: str, terms: list[str], limit: int) -> str | None:
    if not terms:
        return None
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
    scored: list[tuple[int, int, str]] = []
    for index, paragraph in enumerate(paragraphs):
        lower = paragraph.lower()
        score = sum(lower.count(term) for term in terms)
        if paragraph.startswith("#"):
            score += sum(term in lower for term in terms)
        if score > 0:
            scored.append((score, index, paragraph))
    if not scored:
        return None
    selected = sorted(scored, key=lambda item: (-item[0], item[1]))[:6]
    selected.sort(key=lambda item: item[1])
    chunks: list[str] = []
    remaining = limit
    for _, _, paragraph in selected:
        if remaining <= 0:
            break
        chunk = paragraph[:remaining].strip()
        if chunk:
            chunks.append(chunk)
            remaining -= len(chunk) + 2
    return "\n\n".join(chunks).strip() or None


def _build_bounded_page_content(
    text: str,
    *,
    snippet: str,
    terms: list[str],
    max_chars: int,
) -> tuple[str, str, bool]:
    limit = max(1, max_chars)
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped, "full", False

    if snippet:
        snippet_content = _content_from_snippet_window(stripped, snippet, limit)
        if snippet_content:
            return snippet_content, "snippet_window", True

    paragraph_content = _content_from_paragraphs(stripped, terms, limit)
    if paragraph_content:
        return paragraph_content, "paragraph_match", True

    return stripped[:limit].strip(), "leading_excerpt", True


def research_context(
    paths: WikiPaths,
    research_task: str,
    *,
    queries: list[str] | None = None,
    max_pages: int = 8,
    max_chars_per_page: int = _DEFAULT_PAGE_CHARS,
    total_char_budget: int = _DEFAULT_TOTAL_CHARS,
) -> dict[str, Any]:
    """Build a unified QMD-query + graph-expanded context pack for complex research."""
    repo = WikiRepository(paths)
    index = repo.read_index()
    pages_by_path = _index_pages_by_path(index)

    query_list: list[str] = []
    if research_task.strip():
        query_list.append(research_task.strip())
    query_list.extend(query.strip() for query in (queries or []) if query.strip())
    if not query_list:
        query_list = _suggest_queries("", max_pages)

    page_limit = max(1, max_pages)
    search_candidates = _collect_research_candidates(paths, query_list, max_results_per_query=page_limit)
    seed_candidates = search_candidates[:page_limit]
    graph_expansions = _expand_graph_candidates(paths, index, seed_candidates)

    graph_quota = 0
    if graph_expansions:
        max_graph_pages = max(1, int(page_limit * _GRAPH_CONTEXT_RATIO))
        graph_quota = min(max_graph_pages, max(page_limit - 1, 0) if seed_candidates else page_limit)
    qmd_quota = max(page_limit - graph_quota, 0)
    selected: list[tuple[int, dict[str, Any]]] = []

    for candidate in seed_candidates[: qmd_quota or page_limit]:
        priority = 0 if _candidate_title_match(candidate, research_task) else 1
        selected.append((priority, candidate))

    graph_added = 0
    seed_paths = {str(item.get("path") or "") for item in seed_candidates}
    for candidate in graph_expansions:
        if graph_added >= graph_quota:
            break
        if str(candidate.get("path") or "") in seed_paths:
            continue
        selected.append((2, candidate))
        graph_added += 1

    if len(selected) < page_limit:
        selected_paths = {str(candidate.get("path") or "") for _, candidate in selected}
        for candidate in seed_candidates:
            if len(selected) >= page_limit:
                break
            if str(candidate.get("path") or "") in selected_paths:
                continue
            priority = 0 if _candidate_title_match(candidate, research_task) else 1
            selected.append((priority, candidate))
            selected_paths.add(str(candidate.get("path") or ""))

    if not selected and paths.wiki_overview_file.is_file():
        selected.append(
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

    selected.sort(key=lambda item: (item[0], -float(item[1].get("score") or 0), str(item[1].get("path") or "")))

    context_pages: list[dict[str, Any]] = []
    remaining = max(0, total_char_budget)
    seen_paths: set[str] = set()
    terms = _terms_from_texts(research_task, *query_list, *(str(item.get("title") or "") for _, item in selected))

    for priority, candidate in selected:
        if len(context_pages) >= page_limit or remaining <= 0:
            break
        rel_path = str(candidate.get("path") or "")
        if rel_path in seen_paths:
            continue
        page_path = _wiki_page_path(paths, rel_path)
        if page_path is None:
            continue
        content_limit = min(max(1, max_chars_per_page), remaining)
        text = page_path.read_text(encoding="utf-8", errors="ignore")
        content, content_strategy, truncated = _build_bounded_page_content(
            text,
            snippet=str(candidate.get("snippet") or ""),
            terms=terms,
            max_chars=content_limit,
        )
        remaining -= len(content)
        seen_paths.add(rel_path)

        indexed = pages_by_path.get(rel_path, {})
        retrieval_source = candidate.get("retrieval_source") or ("graph" if priority == 2 else "qmd_query")
        context_pages.append(
            {
                "title": candidate.get("title") or indexed.get("title") or page_path.stem,
                "path": rel_path,
                "page_type": indexed.get("page_type") or candidate.get("page_type") or _page_type_from_path(rel_path),
                "priority": priority,
                "retrieval_source": retrieval_source,
                "score": candidate.get("score"),
                "matched_queries": candidate.get("matched_queries", []),
                "snippet": candidate.get("snippet", ""),
                "tags": indexed.get("tags", []),
                "sources": indexed.get("sources", []),
                "content": content,
                "content_strategy": content_strategy,
                "truncated": truncated,
            }
        )

    return {
        "root": str(paths.root),
        "research_task": research_task,
        "queries": query_list,
        "context_pages": context_pages,
        "page_count": len(context_pages),
        "total_chars": sum(len(page["content"]) for page in context_pages),
        "insufficient_context": len(context_pages) == 0,
        "retrieval": {
            "mode": "qmd_query_graph_context",
            "qmd_candidate_count": len(seed_candidates),
            "graph_expansion_count": len(graph_expansions),
            "graph_quota": graph_quota,
            "priority_order": {
                "0": "title match qmd query pages",
                "1": "qmd query pages",
                "2": "graph-expanded pages",
                "3": "overview fallback",
            },
        },
        "usage_guidance": [
            "Use this pack as the primary local wiki evidence for complex answers or report sections.",
            "Treat snippets as retrieval previews and content as the bounded evidence body.",
            "If the pack is thin or contradictory, report the gap instead of inventing missing evidence.",
        ],
    }
