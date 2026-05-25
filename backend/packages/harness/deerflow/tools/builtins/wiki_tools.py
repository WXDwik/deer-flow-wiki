"""LLM Wiki built-in tools.

这些工具是 agent 调用 LLM Wiki 能力的入口。
注意：tools 层只负责参数接收和结果返回，真正业务逻辑在 `deerflow.wiki.service`。
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain.tools import ToolRuntime, tool
from langgraph.config import get_config
from langgraph.typing import ContextT

from deerflow.agents.thread_state import ThreadState
from deerflow.config.paths import VIRTUAL_PATH_PREFIX, get_paths
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.wiki import service
from deerflow.wiki.lint import resolve_lint_mode
from deerflow.wiki.paths import resolve_wiki_root


def _json_result(data: object) -> str:
    """把 Python 结果转换成稳定的 JSON 字符串，方便 LLM 阅读和后续解析。"""
    return json.dumps(data, ensure_ascii=False, indent=2)


def _get_thread_id(runtime: ToolRuntime[ContextT, ThreadState]) -> str | None:
    """Resolve the current thread id from runtime context or RunnableConfig."""
    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if thread_id:
        return thread_id

    runtime_config = getattr(runtime, "config", None) or {}
    thread_id = runtime_config.get("configurable", {}).get("thread_id")
    if thread_id:
        return thread_id

    try:
        return get_config().get("configurable", {}).get("thread_id")
    except RuntimeError:
        return None


def _get_runtime_model_name(runtime: ToolRuntime[ContextT, ThreadState]) -> str | None:
    """Resolve the model selected for the current run, if available."""
    context = runtime.context or {}
    model_name = context.get("model_name") or context.get("model")
    if model_name:
        return str(model_name)

    runtime_config = getattr(runtime, "config", None) or {}
    configurable = runtime_config.get("configurable", {})
    model_name = configurable.get("model_name") or configurable.get("model")
    if model_name:
        return str(model_name)

    metadata = runtime_config.get("metadata", {})
    model_name = metadata.get("model_name") or metadata.get("model")
    if model_name and model_name != "default":
        return str(model_name)

    try:
        config = get_config()
    except RuntimeError:
        return None

    configurable = config.get("configurable", {})
    model_name = configurable.get("model_name") or configurable.get("model")
    if model_name:
        return str(model_name)

    metadata = config.get("metadata", {})
    model_name = metadata.get("model_name") or metadata.get("model")
    if model_name and model_name != "default":
        return str(model_name)
    return None


def _clean_source_path(source_file: str) -> str:
    value = source_file.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _resolve_source_file(runtime: ToolRuntime[ContextT, ThreadState], source_file: str) -> Path:
    """Resolve source paths visible to the agent into gateway-local paths."""
    cleaned = _clean_source_path(source_file)
    stripped = cleaned.lstrip("/")
    virtual_prefix = VIRTUAL_PATH_PREFIX.lstrip("/")
    thread_data = runtime.state.get("thread_data") if runtime.state else None

    if stripped == virtual_prefix or stripped.startswith(virtual_prefix + "/"):
        relative = stripped[len(virtual_prefix) :].lstrip("/")
        root_segment, _, remainder = relative.partition("/")
        state_path_key = {
            "workspace": "workspace_path",
            "uploads": "uploads_path",
            "outputs": "outputs_path",
        }.get(root_segment)
        state_root = thread_data.get(state_path_key) if thread_data and state_path_key else None
        if state_root:
            root = Path(state_root).resolve()
            actual = (root / remainder).resolve()
            try:
                actual.relative_to(root)
            except ValueError as exc:
                raise ValueError("Access denied: path traversal detected") from exc
            return actual

        thread_id = _get_thread_id(runtime)
        if not thread_id:
            raise ValueError("Thread ID is not available; cannot resolve /mnt/user-data source path.")
        try:
            return get_paths().resolve_virtual_path(thread_id, cleaned, user_id=get_effective_user_id())
        except TypeError:
            return get_paths().resolve_virtual_path(thread_id, cleaned)

    path = Path(cleaned).expanduser()
    if path.is_absolute() or path.exists():
        return path.resolve()

    uploads_path = thread_data.get("uploads_path") if thread_data else None
    if uploads_path:
        upload_candidate = (Path(uploads_path) / cleaned).resolve()
        if upload_candidate.is_file():
            return upload_candidate

    return path.resolve()


def _resolve_source_files(runtime: ToolRuntime[ContextT, ThreadState], source_files: list[str]) -> list[Path]:
    if not source_files:
        raise ValueError("source_files cannot be empty")
    return [_resolve_source_file(runtime, source_file) for source_file in source_files]


def _resolve_wiki_name_or_path(runtime: ToolRuntime[ContextT, ThreadState], wiki_name_or_path: str) -> str:
    """Resolve plain wiki names into the current user's shared wiki directory."""
    user_id = get_effective_user_id()
    root = resolve_wiki_root(wiki_name_or_path, base_dir=get_paths().user_wiki_dir(user_id))
    return str(root)


@tool("wiki_create", parse_docstring=True)
def wiki_create_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    wiki_name_or_path: str,
    title: str | None = None,
    language: str = "zh-CN",
) -> str:
    """Create a local LLM Wiki database with the standard research-wiki structure.

    Use this tool when the user asks to create, initialize, scaffold, or set up
    an LLM Wiki / research wiki / knowledge base.

    The created wiki contains purpose.md, schema.md, raw/sources, raw/assets,
    wiki/index.md, wiki/log.md, wiki/overview.md, wiki page folders, .obsidian,
    and .llm-wiki internal state files.

    Args:
        wiki_name_or_path: Wiki name or filesystem path. A plain name is created under the current user's shared wiki directory.
        title: Optional human-readable wiki title. If omitted, the folder name is used.
        language: Initial wiki language, usually `zh-CN` or `en`.
    """
    resolved_wiki = _resolve_wiki_name_or_path(runtime, wiki_name_or_path)
    result = service.create_wiki(resolved_wiki, title=title, language=language)
    return _json_result({"ok": True, "wiki": result})


@tool("wiki_delete", parse_docstring=True)
def wiki_delete_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    wiki_name_or_path: str,
) -> str:
    """Delete an existing local LLM Wiki database and all of its contents.

    Use this tool only after the user explicitly confirms deletion. This is
    irreversible. The target must be a complete LLM Wiki layout, not an
    arbitrary directory.

    Args:
        wiki_name_or_path: Existing wiki name or filesystem path. A plain name is resolved under the current user's shared wiki directory.
    """
    resolved_wiki = _resolve_wiki_name_or_path(runtime, wiki_name_or_path)
    result = service.delete_wiki(resolved_wiki)
    return _json_result({"ok": True, **result})


@tool("wiki_add_source", parse_docstring=True)
def wiki_add_source_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    wiki_name_or_path: str,
    source_files: list[str] | None = None,
    source_file: str | None = None,
) -> str:
    """Import one or more local source files into an existing LLM Wiki.

    Use this tool when the user provides PDFs, Markdown files, DOCX files, or
    other local files and wants them added to an LLM Wiki. Pass one file for a
    single-source import, or pass multiple files when the sources should be
    analyzed together.

    Current behavior:
    - Copies each original file into raw/sources/
    - Converts or reads all sources into Markdown under raw/sources/.cache/
    - Uses the configured chat model once to summarize/classify the whole batch
    - Generates one source summary per file plus cross-source wiki pages
    - Updates .llm-wiki/index.json with raw sources and generated pages

    Args:
        wiki_name_or_path: Existing wiki name or filesystem path.
        source_files: One or more source file paths. Each item supports local
            paths, `/mnt/user-data/...` virtual paths, or a filename in the
            current thread uploads directory.
        source_file: Backward-compatible single source file path. Prefer
            source_files for new calls, even when there is only one file.
    """
    files = list(source_files or [])
    if source_file:
        files.append(source_file)
    resolved_wiki = _resolve_wiki_name_or_path(runtime, wiki_name_or_path)
    result = service.add_sources(
        resolved_wiki,
        _resolve_source_files(runtime, files),
        model_name=_get_runtime_model_name(runtime),
    )
    return _json_result({"ok": True, **result})


@tool("wiki_search", parse_docstring=True)
def wiki_search_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    wiki_name_or_path: str,
    query: str,
    limit: int = 10,
) -> str:
    """Search Markdown pages in an LLM Wiki.

    Use this tool before answering questions that should be grounded in the
    user's LLM Wiki knowledge base. After drafting a normal conversational
    answer from these results, call wiki_archive_answer with the question,
    answer, and cited wiki pages so the wiki can decide whether to write back.

    Args:
        wiki_name_or_path: Existing wiki name or filesystem path.
        query: Search query text.
        limit: Maximum number of results to return.
    """
    resolved_wiki = _resolve_wiki_name_or_path(runtime, wiki_name_or_path)
    result = service.search(resolved_wiki, query, limit=limit)
    return _json_result({"ok": True, "results": result})


@tool("wiki_research_context", parse_docstring=True)
def wiki_research_context_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    wiki_name_or_path: str,
    research_task: str,
    queries: list[str] | None = None,
    max_pages: int = 8,
    max_chars_per_page: int = 6000,
    total_char_budget: int = 30000,
) -> str:
    """Build a grounded wiki evidence pack for complex questions and research reports.

    Use this tool for complex wiki-grounded questions, systematic analysis,
    deep research, and report preparation. It runs QMD query-mode retrieval,
    expands the selected pages through the wiki link/source graph, and returns
    bounded page content under a total character budget. Use wiki_search for
    simple quick questions; use this tool when snippets alone are not enough.

    Args:
        wiki_name_or_path: Existing wiki name or filesystem path.
        research_task: The complex question, report objective, or section task.
        queries: Optional focused retrieval queries to run in addition to
            research_task.
        max_pages: Maximum wiki pages to include in the evidence pack.
        max_chars_per_page: Maximum characters to include from each selected page.
        total_char_budget: Maximum total wiki content characters in the pack.
    """
    resolved_wiki = _resolve_wiki_name_or_path(runtime, wiki_name_or_path)
    result = service.research_context(
        resolved_wiki,
        research_task,
        queries=queries,
        max_pages=max_pages,
        max_chars_per_page=max_chars_per_page,
        total_char_budget=total_char_budget,
    )
    return _json_result({"ok": True, **result})


@tool("wiki_source_status", parse_docstring=True)
def wiki_source_status_tool(runtime: ToolRuntime[ContextT, ThreadState], wiki_name_or_path: str) -> str:
    """List raw source files and whether each one has already been parsed.

    Use this before importing a batch of files that were manually placed under
    raw/sources/. It compares raw/sources/ with .llm-wiki/index.json and reports:
    parsed, pending, stale, parsed_duplicate, and missing_raw.

    Args:
        wiki_name_or_path: Existing wiki name or filesystem path.
    """
    resolved_wiki = _resolve_wiki_name_or_path(runtime, wiki_name_or_path)
    result = service.source_status(resolved_wiki)
    return _json_result({"ok": True, **result})


@tool("wiki_sync_sources", parse_docstring=True)
def wiki_sync_sources_tool(runtime: ToolRuntime[ContextT, ThreadState], wiki_name_or_path: str, limit: int = 0) -> str:
    """Import all unparsed files already stored under raw/sources/.

    Use this when the user has manually placed files into a wiki's raw/sources/
    folder and wants the wiki to process only files that have not been parsed.
    It imports status=pending files, skips already parsed/duplicate/stale files,
    and analyzes the remaining pending files together through the same batch
    ingestion path used by wiki_add_source.

    Args:
        wiki_name_or_path: Existing wiki name or filesystem path.
        limit: Maximum pending files to import. Use 0 to import all pending files.
    """
    resolved_wiki = _resolve_wiki_name_or_path(runtime, wiki_name_or_path)
    result = service.sync_pending_sources(resolved_wiki, limit=limit, model_name=_get_runtime_model_name(runtime))
    return _json_result({"ok": True, **result})


@tool("wiki_archive_answer", parse_docstring=True)
def wiki_archive_answer_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    wiki_name_or_path: str,
    question: str,
    answer_markdown: str,
    citations: list[dict] | None = None,
    auto_archive: bool = True,
) -> str:
    """Judge and optionally archive an answer that already used wiki_search.

    Use this tool after wiki_search has been used and after you have drafted the
    normal conversational answer. This tool does not answer the user question.
    It only decides whether the answer should be written back to the wiki and,
    if auto_archive is true, applies the archive/update changes.

    Args:
        wiki_name_or_path: Existing wiki name or filesystem path.
        question: Original user question.
        answer_markdown: The answer that will be returned to the user.
        citations: Wiki pages used by the answer, each with title and path.
        auto_archive: If true, apply archive writes when the answer is worth archiving.
    """
    resolved_wiki = _resolve_wiki_name_or_path(runtime, wiki_name_or_path)
    result = service.archive_answer(
        resolved_wiki,
        question,
        answer_markdown,
        citations=citations,
        auto_archive=auto_archive,
        model_name=_get_runtime_model_name(runtime),
    )
    return _json_result({"ok": True, **result})


@tool("wiki_lint", parse_docstring=True)
def wiki_lint_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    wiki_name_or_path: str,
    mode: str = "light",
    trigger: str = "manual",
    include_semantic: bool = False,
) -> str:
    """Run health checks on an LLM Wiki.

    Use light lint for structural checks only. Use deep lint for Structural +
    Semantic Lint. Use mode="auto" with a trigger to apply the standard trigger
    policy: add_source/page changes/source cleanup/manual are light; batch
    ingest, explicit deep/semantic requests, important source replacement, core
    page updates, and chat/query quality drops are deep.

    Args:
        wiki_name_or_path: Existing wiki name or filesystem path.
        mode: Lint mode: "light", "deep", or "auto".
        trigger: Reason for lint, for example "manual", "add_source", "batch_ingest", "page_edit",
            "source_cleanup", "important_source_replace", "core_page_update", or "chat_quality_drop".
        include_semantic: Backward-compatible flag; true forces deep lint.
    """
    lint_mode = resolve_lint_mode(mode=mode, trigger=trigger, include_semantic=include_semantic)
    resolved_wiki = _resolve_wiki_name_or_path(runtime, wiki_name_or_path)
    result = service.lint(resolved_wiki, mode=lint_mode, trigger=trigger, model_name=_get_runtime_model_name(runtime))
    return _json_result({"ok": True, "mode": lint_mode, "trigger": trigger, "issues": result, "issue_count": len(result)})


@tool("wiki_repair_lint", parse_docstring=True)
def wiki_repair_lint_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    wiki_name_or_path: str,
    mode: str = "light",
    trigger: str = "manual",
    dry_run: bool = True,
) -> str:
    """Repair LLM Wiki lint issues by asking the configured chat model to edit Markdown.

    Use this after wiki_lint finds issues and the user wants the LLM to maintain
    the wiki. The repair model is constrained to Markdown files under wiki/.
    It never edits raw sources, raw assets, or .llm-wiki internal state.

    Args:
        wiki_name_or_path: Existing wiki name or filesystem path.
        mode: Lint mode used before repair: "light", "deep", or "auto".
        trigger: Reason for lint/repair, for example "manual", "batch_ingest", or "chat_quality_drop".
        dry_run: If true, return the proposed Markdown changes without writing files.
    """
    resolved_wiki = _resolve_wiki_name_or_path(runtime, wiki_name_or_path)
    result = service.repair(resolved_wiki, mode=mode, trigger=trigger, dry_run=dry_run, model_name=_get_runtime_model_name(runtime))
    return _json_result(result)


@tool("wiki_evolve_schema", parse_docstring=True)
def wiki_evolve_schema_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    wiki_name_or_path: str,
    change_request: str,
    evidence: list[str] | None = None,
    dry_run: bool = True,
) -> str:
    """Evolve the wiki's schema.md contract through a controlled LLM workflow.

    Use this when repeated maintenance issues, stable user preferences, new
    page categories, or stronger citation/lint rules should become permanent
    wiki operating rules. This tool updates only schema.md and maintenance logs
    when dry_run is false; it never edits raw sources or ordinary knowledge
    pages.

    Args:
        wiki_name_or_path: Existing wiki name or filesystem path.
        change_request: Durable rule change to incorporate into schema.md.
        evidence: Optional repeated issues, examples, or lint findings that justify the change.
        dry_run: If true, return the proposed schema without writing files.
    """
    resolved_wiki = _resolve_wiki_name_or_path(runtime, wiki_name_or_path)
    result = service.evolve_schema(
        resolved_wiki,
        change_request=change_request,
        evidence=evidence,
        dry_run=dry_run,
        model_name=_get_runtime_model_name(runtime),
    )
    return _json_result(result)


WIKI_TOOLS = [
    wiki_create_tool,
    wiki_delete_tool,
    wiki_add_source_tool,
    wiki_search_tool,
    wiki_research_context_tool,
    wiki_source_status_tool,
    wiki_sync_sources_tool,
    wiki_archive_answer_tool,
    wiki_lint_tool,
    wiki_repair_lint_tool,
    wiki_evolve_schema_tool,
]
