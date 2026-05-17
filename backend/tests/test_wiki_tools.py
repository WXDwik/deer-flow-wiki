from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from deerflow.tools.builtins.wiki_tools import WIKI_TOOLS, _resolve_source_file


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


def test_wiki_tools_include_source_status_tool() -> None:
    tool_names = {tool.name for tool in WIKI_TOOLS}

    assert "wiki_source_status" in tool_names
    assert "wiki_sync_sources" in tool_names
    assert "wiki_add_source" in tool_names
    assert "wiki_add_sources" not in tool_names
    assert "wiki_plan_report" in tool_names
    assert "wiki_get_report_context" in tool_names
    assert "wiki_evolve_schema" in tool_names
