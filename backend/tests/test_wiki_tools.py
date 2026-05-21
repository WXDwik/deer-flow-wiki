from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from deerflow.config.paths import Paths
from deerflow.tools.builtins.wiki_tools import WIKI_TOOLS, _get_runtime_model_name, _resolve_source_file, _resolve_wiki_name_or_path
from deerflow.wiki import service
from deerflow.wiki.scaffold import create_wiki_database


def test_resolve_source_file_maps_virtual_uploads_path_with_spaces(tmp_path: Path) -> None:
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    source_file = uploads_dir / "Sheng et al - 2024 - UADFormer long name.pdf"
    source_file.write_bytes(b"%PDF-1.4")

    runtime = SimpleNamespace(
        context=None,
        config={"configurable": {"thread_id": "thread-1"}},
        state={"thread_data": {"uploads_path": str(uploads_dir)}},
    )

    resolved = _resolve_source_file(
        runtime,
        "/mnt/user-data/uploads/Sheng et al - 2024 - UADFormer long name.pdf",
    )

    assert resolved == source_file.resolve()


def test_resolve_source_file_accepts_quoted_uploaded_filename(tmp_path: Path) -> None:
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    source_file = uploads_dir / "paper with spaces.pdf"
    source_file.write_bytes(b"%PDF-1.4")

    runtime = SimpleNamespace(
        context=None,
        config={},
        state={"thread_data": {"uploads_path": str(uploads_dir)}},
    )

    resolved = _resolve_source_file(runtime, "'paper with spaces.pdf'")

    assert resolved == source_file.resolve()


def test_resolve_wiki_name_maps_plain_name_to_user_wiki_dir(tmp_path: Path, monkeypatch) -> None:
    outputs_dir = tmp_path / "outputs"
    paths = Paths(base_dir=tmp_path)
    monkeypatch.setattr("deerflow.tools.builtins.wiki_tools.get_paths", lambda: paths)

    runtime = SimpleNamespace(
        context=None,
        config={"configurable": {"thread_id": "thread-1"}},
        state={"thread_data": {"outputs_path": str(outputs_dir)}},
    )

    resolved = _resolve_wiki_name_or_path(runtime, "Research Wiki")

    assert Path(resolved) == (tmp_path / "users" / "test-user-autouse" / "wiki" / "research-wiki").resolve()


def test_resolve_wiki_name_is_shared_across_threads_for_same_user(tmp_path: Path, monkeypatch) -> None:
    paths = Paths(base_dir=tmp_path)
    monkeypatch.setattr("deerflow.tools.builtins.wiki_tools.get_paths", lambda: paths)

    runtime_one = SimpleNamespace(
        context=None,
        config={"configurable": {"thread_id": "thread-1"}},
        state={"thread_data": {"outputs_path": str(tmp_path / "thread-1" / "outputs")}},
    )
    runtime_two = SimpleNamespace(
        context=None,
        config={"configurable": {"thread_id": "thread-2"}},
        state={"thread_data": {"outputs_path": str(tmp_path / "thread-2" / "outputs")}},
    )

    assert _resolve_wiki_name_or_path(runtime_one, "Research Wiki") == _resolve_wiki_name_or_path(runtime_two, "Research Wiki")


def test_resolve_wiki_name_keeps_explicit_path(tmp_path: Path, monkeypatch) -> None:
    outputs_dir = tmp_path / "outputs"
    explicit_path = tmp_path / "external-wiki"
    paths = Paths(base_dir=tmp_path)
    monkeypatch.setattr("deerflow.tools.builtins.wiki_tools.get_paths", lambda: paths)

    runtime = SimpleNamespace(
        context=None,
        config={"configurable": {"thread_id": "thread-1"}},
        state={"thread_data": {"outputs_path": str(outputs_dir)}},
    )

    resolved = _resolve_wiki_name_or_path(runtime, str(explicit_path))

    assert Path(resolved) == explicit_path.resolve()


def test_get_runtime_model_name_prefers_context() -> None:
    runtime = SimpleNamespace(
        context={"model_name": "deepseek-v4-pro"},
        config={"metadata": {"model_name": "qwen3.6-flash"}},
        state={},
    )

    assert _get_runtime_model_name(runtime) == "deepseek-v4-pro"


def test_get_runtime_model_name_uses_metadata() -> None:
    runtime = SimpleNamespace(
        context=None,
        config={"metadata": {"model_name": "deepseek-v4-flash"}},
        state={},
    )

    assert _get_runtime_model_name(runtime) == "deepseek-v4-flash"


def test_wiki_lint_rejects_missing_wiki(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Wiki not found or incomplete"):
        service.lint(str(tmp_path / "missing-wiki"))


def test_wiki_lint_rejects_incomplete_wiki_layout(tmp_path: Path) -> None:
    incomplete = tmp_path / "incomplete-wiki"
    (incomplete / "wiki").mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="Wiki not found or incomplete"):
        service.lint(str(incomplete))


def test_wiki_lint_keeps_existing_wiki_behavior(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")

    result = service.lint(str(paths.root))

    assert isinstance(result, list)


def test_wiki_tools_include_source_status_tool() -> None:
    tool_names = {tool.name for tool in WIKI_TOOLS}

    assert "wiki_source_status" in tool_names
    assert "wiki_sync_sources" in tool_names
    assert "wiki_add_source" in tool_names
    assert "wiki_add_sources" not in tool_names
    assert "wiki_plan_report" in tool_names
    assert "wiki_get_report_context" in tool_names
    assert "wiki_evolve_schema" in tool_names
