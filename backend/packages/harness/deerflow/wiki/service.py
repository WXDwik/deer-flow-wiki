"""LLM Wiki 对外业务入口。

tools 层后续应该只调用这里，不要直接操作 paths/repository/ingest。
这样可以保证业务逻辑集中在 wiki 包内部。
"""

from __future__ import annotations

import shutil
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from deerflow.wiki.archive import archive_answer as archive_wiki_answer
from deerflow.wiki.ingest import ingest_files, sha256_file
from deerflow.wiki.lint import lint_wiki
from deerflow.wiki.maintenance import rebuild_index_markdown, update_overview_markdown
from deerflow.wiki.models import RawSource
from deerflow.wiki.paths import WikiPaths, build_wiki_paths, resolve_wiki_root, wiki_layout_exists
from deerflow.wiki.purpose import update_purpose_after_wiki_change
from deerflow.wiki.query import search_wiki
from deerflow.wiki.repair import repair_lint
from deerflow.wiki.report import research_context as build_research_context
from deerflow.wiki.repository import WikiRepository
from deerflow.wiki.scaffold import create_wiki_database
from deerflow.wiki.schema import evolve_schema as evolve_wiki_schema


def create_wiki(
    wiki_name_or_path: str,
    *,
    title: str | None = None,
    language: str = "zh-CN",
) -> dict:
    """创建一个完整 LLM Wiki，并返回关键路径。"""
    paths = create_wiki_database(wiki_name_or_path, title=title, language=language)
    return summarize_paths(paths)


def open_wiki(wiki_name_or_path: str) -> WikiPaths:
    """打开已有 wiki，返回路径对象。"""
    root = resolve_wiki_root(wiki_name_or_path)
    if not wiki_layout_exists(root):
        raise FileNotFoundError(f"Wiki not found or incomplete: {root}")
    return build_wiki_paths(root)


def delete_wiki(wiki_name_or_path: str) -> dict:
    """Delete a complete LLM Wiki directory and all of its contents."""
    paths = open_wiki(wiki_name_or_path)
    if paths.root.is_symlink():
        raise ValueError(f"Refusing to delete symlinked wiki root: {paths.root}")

    root = paths.root
    shutil.rmtree(root)
    return {
        "root": str(root),
        "deleted": True,
    }


def add_source(wiki_name_or_path: str, source_file: str | Path, *, model_name: str | None = None) -> dict:
    """向 wiki 导入一个原始资料文件。"""
    result = add_sources(wiki_name_or_path, [source_file], model_name=model_name)
    return result["sources"][0]


def _post_ingest_lint(paths: WikiPaths, *, trigger: str, model_name: str | None = None) -> dict:
    lint_issues = [asdict(issue) for issue in lint_wiki(paths, mode="light", trigger=trigger, model_name=model_name)]
    return {
        "mode": "light",
        "trigger": trigger,
        "issues": lint_issues,
        "issue_count": len(lint_issues),
    }


def _append_ingest_log(
    paths: WikiPaths,
    sources: list[RawSource],
    lint_result: dict,
    purpose_update: dict | None = None,
    maintenance_update: dict | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    source_word = "source" if len(sources) == 1 else "sources"
    entry = f"\n## [{now}] source-ingest\n\nImported {len(sources)} {source_word}.\n\nSources:\n"

    generated_pages = []
    for source in sources:
        source_id = getattr(source, "source_id", "")
        path = getattr(source, "path", "")
        metadata = getattr(source, "metadata", {}) or {}
        ingest_mode = metadata.get("ingest_mode", "unknown")
        pages = metadata.get("generated_pages", [])
        entry += f"- `{path}`"
        if source_id:
            entry += f" (`{source_id}`)"
        entry += f" via `{ingest_mode}`"
        if pages:
            entry += f", generated {len(pages)} pages"
        entry += "\n"
        generated_pages.extend(page for page in pages if isinstance(page, dict))

    if generated_pages:
        entry += "\nGenerated pages:\n"
        for page in generated_pages:
            page_path = str(page.get("path") or "").strip()
            page_type = str(page.get("page_type") or "").strip()
            title = str(page.get("title") or "").strip()
            label = page_path or title
            if not label:
                continue
            entry += f"- `{label}`"
            if page_type:
                entry += f" ({page_type})"
            entry += "\n"

    entry += (
        "\nLint:\n"
        f"- mode: `{lint_result.get('mode', 'light')}`\n"
        f"- trigger: `{lint_result.get('trigger', 'add_source')}`\n"
        f"- issues: `{lint_result.get('issue_count', 0)}`\n"
    )

    quality_records = []
    for source in sources:
        quality = (getattr(source, "metadata", {}) or {}).get("quality")
        if isinstance(quality, dict):
            quality_records.append(quality)
    if quality_records:
        unresolved = sum(1 for item in quality_records if not item.get("resolved"))
        initial_issue_count = sum(len(item.get("initial_issues", [])) for item in quality_records)
        entry += "\nIngest quality:\n"
        entry += f"- checked: `{len(quality_records)}`\n"
        entry += f"- initial issues: `{initial_issue_count}`\n"
        entry += f"- unresolved records: `{unresolved}`\n"

    if maintenance_update is not None:
        index_update = maintenance_update.get("index") if isinstance(maintenance_update.get("index"), dict) else {}
        overview_update = maintenance_update.get("overview") if isinstance(maintenance_update.get("overview"), dict) else {}
        entry += "\nMaintenance:\n"
        entry += f"- index: `{bool(index_update.get('updated'))}`"
        if index_update.get("path"):
            entry += f" ({index_update['path']})"
        entry += "\n"
        entry += f"- overview: `{bool(overview_update.get('updated'))}`"
        if overview_update.get("path"):
            entry += f" ({overview_update['path']})"
        entry += "\n"

    if purpose_update is not None:
        entry += "\nPurpose update:\n"
        entry += f"- updated: `{bool(purpose_update.get('updated'))}`\n"
        if purpose_update.get("summary"):
            entry += f"- summary: {purpose_update['summary']}\n"
        if purpose_update.get("error"):
            entry += f"- error: `{purpose_update['error']}`\n"
        if purpose_update.get("updated"):
            entry += "\nChanged files:\n- `purpose.md`\n"

    paths.wiki_log_file.parent.mkdir(parents=True, exist_ok=True)
    with paths.wiki_log_file.open("a", encoding="utf-8") as f:
        f.write(entry)


def add_sources(wiki_name_or_path: str, source_files: list[str | Path], *, model_name: str | None = None) -> dict:
    """Import a batch of source files into one wiki generation pass."""
    if not source_files:
        raise ValueError("source_files cannot be empty")

    paths = open_wiki(wiki_name_or_path)
    sources = ingest_files(paths, source_files, model_name=model_name)
    repo = WikiRepository(paths)

    changed_paths = sorted(
        {
            str(page.get("path"))
            for source in sources
            for page in source.metadata.get("generated_pages", [])
            if isinstance(page, dict) and page.get("path")
        }
    )
    index_update = rebuild_index_markdown(paths)
    overview_update = update_overview_markdown(paths)
    maintenance_update = {"index": index_update, "overview": overview_update}
    for rel_path in (index_update.get("path"), overview_update.get("path")):
        if isinstance(rel_path, str) and rel_path:
            changed_paths.append(rel_path)

    purpose_update = update_purpose_after_wiki_change(
        paths,
        change_summary=f"Imported {len(sources)} source{'s' if len(sources) != 1 else ''}.",
        changed_paths=changed_paths,
        model_name=model_name,
    )
    if purpose_update.get("updated") and purpose_update.get("path"):
        changed_paths.append(str(purpose_update["path"]))

    lint_trigger = "batch_ingest" if len(sources) > 1 else "add_source"
    lint_result = _post_ingest_lint(paths, trigger=lint_trigger, model_name=model_name)
    for source in sources:
        source.metadata["lint"] = lint_result
        source.metadata["purpose_update"] = purpose_update
        source.metadata["maintenance"] = maintenance_update
        repo.add_source(source)
    _append_ingest_log(paths, sources, lint_result, purpose_update, maintenance_update)

    return {
        "root": str(paths.root),
        "source_count": len(sources),
        "sources": [asdict(source) for source in sources],
        "metadata": {
            "ingest_mode": "batch" if len(sources) > 1 else "single",
            "lint": lint_result,
            "purpose_update": purpose_update,
            "maintenance": maintenance_update,
        },
    }


def source_status(wiki_name_or_path: str) -> dict:
    """Compare raw/sources files with .llm-wiki/index.json ingestion state."""
    paths = open_wiki(wiki_name_or_path)
    repo = WikiRepository(paths)
    index = repo.read_index()
    sources = [item for item in index.get("sources", []) if isinstance(item, dict)]

    by_path = {str(item.get("path")): item for item in sources if item.get("path")}
    by_hash: dict[str, list[dict]] = {}
    for item in sources:
        digest = item.get("sha256")
        if isinstance(digest, str) and digest:
            by_hash.setdefault(digest, []).append(item)

    files = []
    raw_files = []
    if paths.raw_sources_dir.is_dir():
        raw_files = [
            path
            for path in paths.raw_sources_dir.rglob("*")
            if path.is_file() and ".cache" not in path.relative_to(paths.raw_sources_dir).parts
        ]

    seen_index_paths: set[str] = set()
    for raw_file in sorted(raw_files, key=lambda item: item.relative_to(paths.raw_sources_dir).as_posix()):
        rel_path = raw_file.relative_to(paths.root).as_posix()
        digest = sha256_file(raw_file)
        indexed = by_path.get(rel_path)
        hash_matches = by_hash.get(digest, [])

        if indexed and indexed.get("sha256") == digest:
            status = "parsed"
            reason = "raw file matches the indexed source path and sha256"
        elif indexed:
            status = "stale"
            reason = "raw file path exists in the index but sha256 has changed"
        elif hash_matches:
            indexed = hash_matches[0]
            status = "parsed_duplicate"
            reason = "raw file content matches an indexed source with a different path"
        else:
            status = "pending"
            reason = "raw file is not present in the index"

        if indexed and indexed.get("path"):
            seen_index_paths.add(str(indexed["path"]))

        files.append(
            {
                "path": rel_path,
                "name": raw_file.name,
                "status": status,
                "reason": reason,
                "sha256": digest,
                "size_bytes": raw_file.stat().st_size,
                "source_id": indexed.get("source_id") if indexed else None,
                "title": indexed.get("title") if indexed else raw_file.stem,
                "indexed_path": indexed.get("path") if indexed else None,
                "generated_pages": indexed.get("metadata", {}).get("generated_pages", []) if indexed else [],
            }
        )

    missing_raw = []
    for item in sources:
        rel_path = item.get("path")
        if not isinstance(rel_path, str) or rel_path in seen_index_paths:
            continue
        raw_path = paths.root / rel_path
        if not raw_path.is_file():
            missing_raw.append(
                {
                    "path": rel_path,
                    "status": "missing_raw",
                    "reason": "source is indexed but the raw file is missing",
                    "source_id": item.get("source_id"),
                    "title": item.get("title"),
                    "sha256": item.get("sha256"),
                }
            )

    counts: dict[str, int] = {}
    for item in files + missing_raw:
        status = str(item["status"])
        counts[status] = counts.get(status, 0) + 1

    return {
        "root": str(paths.root),
        "raw_sources": str(paths.raw_sources_dir),
        "counts": counts,
        "files": files,
        "missing_raw": missing_raw,
    }


def sync_pending_sources(wiki_name_or_path: str, *, limit: int = 0, model_name: str | None = None) -> dict:
    """Import raw/sources files that are not yet present in the wiki index."""
    paths = open_wiki(wiki_name_or_path)
    status = source_status(str(paths.root))
    pending = [item for item in status["files"] if item.get("status") == "pending"]
    if limit > 0:
        pending = pending[:limit]

    processed = []
    skipped = []
    failed = []
    seen_hashes: set[str] = set()
    batch_items = []
    batch_paths = []

    for item in pending:
        digest = str(item.get("sha256") or "")
        if digest in seen_hashes:
            skipped.append(
                {
                    "path": item.get("path"),
                    "status": "skipped_duplicate_in_batch",
                    "reason": "another pending file with the same sha256 was already imported in this sync",
                    "sha256": digest,
                }
            )
            continue
        seen_hashes.add(digest)

        rel_path = item.get("path")
        if not isinstance(rel_path, str):
            failed.append({"path": rel_path, "error": "invalid source path"})
            continue

        batch_items.append(item)
        batch_paths.append(paths.root / rel_path)

    if batch_paths:
        try:
            result = add_sources(str(paths.root), batch_paths, model_name=model_name)
            for source in result["sources"]:
                processed.append(
                    {
                        "path": source.get("path"),
                        "source_id": source.get("source_id"),
                        "title": source.get("title"),
                        "sha256": source.get("sha256"),
                    }
                )
        except Exception as exc:
            for item in batch_items:
                failed.append({"path": item.get("path"), "error": str(exc)})

    return {
        "root": str(paths.root),
        "raw_sources": str(paths.raw_sources_dir),
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "processed_count": len(processed),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "pending_count_before": len([item for item in status["files"] if item.get("status") == "pending"]),
        "limit": limit,
    }


def search(wiki_name_or_path: str, query: str, *, limit: int = 10) -> list[dict]:
    """搜索 wiki 内容。"""
    paths = open_wiki(wiki_name_or_path)
    return [asdict(result) for result in search_wiki(paths, query, limit=limit)]


def research_context(
    wiki_name_or_path: str,
    research_task: str,
    *,
    queries: list[str] | None = None,
    max_pages: int = 8,
    max_chars_per_page: int = 6000,
    total_char_budget: int = 30000,
) -> dict:
    """Build a unified QMD-query and graph-expanded context pack for complex research."""
    paths = open_wiki(wiki_name_or_path)
    return build_research_context(
        paths,
        research_task,
        queries=queries,
        max_pages=max_pages,
        max_chars_per_page=max_chars_per_page,
        total_char_budget=total_char_budget,
    )


def archive_answer(
    wiki_name_or_path: str,
    question: str,
    answer_markdown: str,
    *,
    citations: list[dict] | None = None,
    auto_archive: bool = True,
    model_name: str | None = None,
) -> dict:
    """Judge and optionally archive an answer that was grounded in wiki_search."""
    paths = open_wiki(wiki_name_or_path)
    from deerflow.wiki.models import WikiCitation

    normalized_citations = []
    for item in citations or []:
        title = str(item.get("title") or "").strip()
        path = str(item.get("path") or "").strip()
        if title and path:
            normalized_citations.append(WikiCitation(title=title, path=path))
    return asdict(archive_wiki_answer(paths, question, answer_markdown, citations=normalized_citations, auto_archive=auto_archive, model_name=model_name))


def lint(
    wiki_name_or_path: str,
    *,
    mode: str = "light",
    trigger: str | None = None,
    include_semantic: bool | None = None,
    model_name: str | None = None,
) -> list[dict]:
    """运行 wiki 健康检查。"""
    paths = open_wiki(wiki_name_or_path)
    return [asdict(issue) for issue in lint_wiki(paths, mode=mode, trigger=trigger, include_semantic=include_semantic, model_name=model_name)]


def repair(
    wiki_name_or_path: str,
    *,
    mode: str = "light",
    trigger: str = "manual",
    dry_run: bool = True,
    model_name: str | None = None,
) -> dict:
    """Use the configured chat model to repair lint issues by editing wiki Markdown."""
    paths = open_wiki(wiki_name_or_path)
    return repair_lint(paths, mode=mode, trigger=trigger, dry_run=dry_run, model_name=model_name)


def evolve_schema(
    wiki_name_or_path: str,
    *,
    change_request: str,
    evidence: list[str] | None = None,
    dry_run: bool = True,
    model_name: str | None = None,
) -> dict:
    """Propose or apply a controlled schema.md evolution."""
    paths = open_wiki(wiki_name_or_path)
    return evolve_wiki_schema(paths, change_request=change_request, evidence=evidence, dry_run=dry_run, model_name=model_name)


def summarize_paths(paths: WikiPaths) -> dict:
    """返回适合 tools/API 输出的关键路径摘要。"""
    return {
        "root": str(paths.root),
        "purpose": str(paths.purpose_file),
        "schema": str(paths.schema_file),
        "raw_sources": str(paths.raw_sources_dir),
        "raw_assets": str(paths.raw_assets_dir),
        "wiki": str(paths.wiki_dir),
        "llm_wiki": str(paths.llm_wiki_dir),
        "schema_evolution_log": str(paths.wiki_maintenance_dir / "schema-changelog.md"),
    }
