"""LLM Wiki search.

The primary backend is QMD when the `qmd` CLI is available. QMD provides a
local BM25/vector/hybrid search index for Markdown files. If QMD is not
installed or fails at runtime, search falls back to the original lightweight
keyword implementation so wiki_search remains usable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deerflow.wiki.paths import WikiPaths

logger = logging.getLogger(__name__)

_DISABLED_VALUES = {"0", "false", "no", "off"}
_QMD_SEARCH_MODES = {"search", "query", "vsearch"}
_RRF_K = 60


@dataclass
class SearchResult:
    """One wiki search hit."""

    path: str
    title: str
    score: float
    snippet: str


def _score_text(text: str, query_terms: list[str]) -> int:
    """Very small fallback keyword scorer."""
    lower = text.lower()
    score = 0
    for term in query_terms:
        score += lower.count(term)
    return score


def _snippet(text: str, query_terms: list[str], max_chars: int = 240) -> str:
    """Build a compact fallback hit snippet."""
    lower = text.lower()
    first_hit = min((lower.find(term) for term in query_terms if term in lower), default=0)
    start = max(first_hit - 60, 0)
    return text[start : start + max_chars].replace("\n", " ").strip()


def _is_qmd_enabled() -> bool:
    value = os.getenv("DEER_FLOW_WIKI_QMD_ENABLED", "true").strip().lower()
    return value not in _DISABLED_VALUES


def _qmd_command_parts() -> list[str] | None:
    raw = os.getenv("DEER_FLOW_QMD_COMMAND", "qmd").strip()
    if not raw:
        return None
    try:
        command = shlex.split(raw, posix=os.name != "nt")
    except ValueError:
        logger.warning("Invalid DEER_FLOW_QMD_COMMAND value: %r", raw)
        return None
    command = [part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in {"'", '"'} else part for part in command]
    if not command:
        return None
    executable = command[0]
    if Path(executable).exists() or shutil.which(executable):
        return command
    logger.debug("QMD command is not available: %s", executable)
    return None


def _qmd_timeout_seconds() -> float:
    raw = os.getenv("DEER_FLOW_WIKI_QMD_TIMEOUT_SECONDS", "30").strip()
    try:
        return max(float(raw), 1.0)
    except ValueError:
        logger.warning("Invalid DEER_FLOW_WIKI_QMD_TIMEOUT_SECONDS value: %r", raw)
        return 30.0


def _qmd_mode() -> str:
    mode = os.getenv("DEER_FLOW_WIKI_QMD_MODE", "search").strip().lower()
    if mode not in _QMD_SEARCH_MODES:
        logger.warning("Unsupported DEER_FLOW_WIKI_QMD_MODE=%r; using 'search'", mode)
        return "search"
    return mode


def _is_qmd_vector_enabled() -> bool:
    value = os.getenv("DEER_FLOW_WIKI_QMD_VECTOR_ENABLED", "true").strip().lower()
    return value not in _DISABLED_VALUES


def _qmd_collection_name(paths: WikiPaths) -> str:
    digest = hashlib.sha1(str(paths.root).encode("utf-8")).hexdigest()[:10]
    stem = re.sub(r"[^a-z0-9_-]+", "-", paths.root.name.lower()).strip("-") or "wiki"
    return f"deerflow-{stem}-{digest}"


def _run_qmd(args: list[str], *, timeout: float, cwd: Path) -> subprocess.CompletedProcess[str] | None:
    command = _qmd_command_parts()
    if command is None:
        return None

    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    try:
        return subprocess.run(
            [*command, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info("QMD command failed: %s", exc)
        return None


def _existing_qmd_collection_name(output: str) -> str | None:
    match = re.search(r"Name:\s*([^\s]+)\s*\(qmd://", output)
    if match:
        return match.group(1)
    return None


def _ensure_qmd_collection(paths: WikiPaths, collection: str, *, timeout: float) -> str | None:
    if not paths.wiki_dir.is_dir():
        return None

    add_result = _run_qmd(
        ["collection", "add", str(paths.wiki_dir), "--name", collection, "--mask", "**/*.md"],
        timeout=timeout,
        cwd=paths.root,
    )
    if add_result is None:
        return None
    search_collection = collection
    if add_result.returncode != 0:
        logger.debug("QMD collection add returned %s: %s%s", add_result.returncode, add_result.stdout, add_result.stderr)
        search_collection = _existing_qmd_collection_name(add_result.stdout + add_result.stderr) or collection

    update_enabled = os.getenv("DEER_FLOW_WIKI_QMD_UPDATE", "true").strip().lower() not in _DISABLED_VALUES
    if update_enabled:
        update_result = _run_qmd(["update"], timeout=timeout, cwd=paths.root)
        if update_result is not None and update_result.returncode != 0:
            logger.debug("QMD update returned %s: %s%s", update_result.returncode, update_result.stdout, update_result.stderr)

    return search_collection


def _score_from_qmd(value: Any, fallback: float) -> float:
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return fallback


def _path_from_qmd_file(paths: WikiPaths, file_value: object) -> str:
    raw = str(file_value or "").strip()
    if not raw:
        return ""

    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(paths.root).as_posix()
        except ValueError:
            return candidate.name

    normalized = raw.replace("\\", "/").lstrip("/")
    if normalized.startswith("qmd://"):
        _, _, normalized = normalized[len("qmd://") :].partition("/")
        normalized = normalized.lstrip("/")
    if (paths.root / normalized).is_file():
        return normalized
    if normalized.startswith("wiki/"):
        return normalized
    return f"wiki/{normalized}"


def _parse_qmd_results(paths: WikiPaths, output: str, *, limit: int) -> list[SearchResult] | None:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        logger.debug("QMD returned non-JSON output: %s", output[:500])
        return None

    if isinstance(payload, dict):
        raw_results = payload.get("results", [])
    else:
        raw_results = payload

    if not isinstance(raw_results, list):
        return None

    results: list[SearchResult] = []
    for index, item in enumerate(raw_results[:limit]):
        if not isinstance(item, dict):
            continue
        path = _path_from_qmd_file(paths, item.get("file") or item.get("path") or item.get("displayPath"))
        if not path:
            continue
        title = str(item.get("title") or Path(path).stem)
        snippet = str(item.get("snippet") or item.get("body") or "").replace("\n", " ").strip()
        results.append(
            SearchResult(
                path=path,
                title=title,
                score=_score_from_qmd(item.get("score"), float(limit - index)),
                snippet=snippet[:600],
            )
        )
    return results


def _qmd_search_mode(paths: WikiPaths, query: str, *, collection: str, mode: str, limit: int, timeout: float) -> list[SearchResult] | None:
    result = _run_qmd(
        [mode, query, "--json", "-n", str(limit), "--collection", collection],
        timeout=timeout,
        cwd=paths.root,
    )
    if result is None:
        return None
    if result.returncode != 0:
        logger.info("QMD %s failed with exit code %s: %s%s", mode, result.returncode, result.stdout, result.stderr)
        return None
    return _parse_qmd_results(paths, result.stdout, limit=limit)


def qmd_query_wiki(paths: WikiPaths, query: str, *, limit: int = 10) -> list[SearchResult] | None:
    """Run QMD's hybrid query mode without DeerFlow-side vsearch/RRF fusion."""
    if not query.strip() or not _is_qmd_enabled():
        return None

    timeout = _qmd_timeout_seconds()
    collection = _qmd_collection_name(paths)
    collection = _ensure_qmd_collection(paths, collection, timeout=timeout)
    if collection is None:
        return None
    return _qmd_search_mode(
        paths,
        query,
        collection=collection,
        mode="query",
        limit=limit,
        timeout=timeout,
    )


def _rrf_fuse_ranked_results(*ranked_lists: list[SearchResult]) -> list[SearchResult]:
    fused: dict[str, SearchResult] = {}
    scores: dict[str, float] = {}

    for ranked in ranked_lists:
        for rank, result in enumerate(ranked, start=1):
            existing = fused.get(result.path)
            if existing is None:
                fused[result.path] = result
            elif not existing.snippet and result.snippet:
                fused[result.path] = SearchResult(
                    path=existing.path,
                    title=existing.title or result.title,
                    score=existing.score,
                    snippet=result.snippet,
                )
            scores[result.path] = scores.get(result.path, 0.0) + 1.0 / (_RRF_K + rank)

    results = [SearchResult(path=item.path, title=item.title, score=scores[item.path], snippet=item.snippet) for item in fused.values()]
    results.sort(key=lambda item: (-item.score, item.path))
    return results


def _search_wiki_with_qmd(paths: WikiPaths, query: str, *, limit: int) -> list[SearchResult] | None:
    if not _is_qmd_enabled():
        return None

    timeout = _qmd_timeout_seconds()
    collection = _qmd_collection_name(paths)
    collection = _ensure_qmd_collection(paths, collection, timeout=timeout)
    if collection is None:
        return None

    mode = _qmd_mode()
    keyword_mode = "search" if mode == "vsearch" else mode
    keyword_results = _qmd_search_mode(
        paths,
        query,
        collection=collection,
        mode=keyword_mode,
        limit=limit,
        timeout=timeout,
    )

    vector_results: list[SearchResult] | None = None
    if _is_qmd_vector_enabled():
        vector_results = _qmd_search_mode(
            paths,
            query,
            collection=collection,
            mode="vsearch",
            limit=limit,
            timeout=timeout,
        )

    ranked_lists = [items for items in (keyword_results, vector_results) if items is not None]
    if not ranked_lists:
        return None
    if len(ranked_lists) == 1:
        return ranked_lists[0]
    return _rrf_fuse_ranked_results(*ranked_lists)[:limit]


def _search_wiki_with_keywords(paths: WikiPaths, query: str, *, limit: int = 10) -> list[SearchResult]:
    terms = [term.lower() for term in query.split() if term.strip()]
    if not terms:
        return []

    results: list[SearchResult] = []
    for path in paths.wiki_dir.rglob("*.md"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        title_score = _score_text(path.stem, terms) * 10
        score = title_score + _score_text(text, terms)
        if score <= 0:
            continue
        results.append(
            SearchResult(
                path=path.relative_to(paths.root).as_posix(),
                title=path.stem,
                score=float(score),
                snippet=_snippet(text, terms),
            )
        )

    results.sort(key=lambda item: item.score, reverse=True)
    return results[:limit]


def search_wiki(paths: WikiPaths, query: str, *, limit: int = 10) -> list[SearchResult]:
    """Search wiki Markdown pages."""
    if not query.strip():
        return []

    qmd_results = _search_wiki_with_qmd(paths, query, limit=limit)
    if qmd_results is not None:
        return qmd_results
    return _search_wiki_with_keywords(paths, query, limit=limit)
