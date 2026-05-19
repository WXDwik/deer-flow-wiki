from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage

from deerflow.wiki.ingest import ingest_file, ingest_files
from deerflow.wiki.repository import WikiRepository
from deerflow.wiki.scaffold import create_wiki_database
from deerflow.wiki.service import add_source, add_sources, source_status, sync_pending_sources


def test_ingest_file_copies_source_caches_markdown_and_writes_llm_pages(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    source_file = tmp_path / "paper.pdf"
    source_file.write_bytes(b"%PDF-1.4 fake")

    async def fake_convert(file_path: Path, *, output_dir: Path | None = None, pdf_image_dir: Path | None = None) -> Path:
        assert output_dir == paths.raw_sources_dir / ".cache"
        assert pdf_image_dir is not None
        assert pdf_image_dir.parent == paths.raw_assets_dir
        md_path = output_dir / file_path.with_suffix(".md").name
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("# Paper\n\nThis paper defines Retrieval Augmented Generation.", encoding="utf-8")
        return md_path

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "source_summary": "This source summarizes RAG.",
                "tags": ["rag"],
                "pages": [
                    {
                        "type": "concept",
                        "title": "Retrieval Augmented Generation",
                        "tags": ["rag"],
                        "content": "RAG combines retrieval with generation.",
                    }
                ],
            }
        )
    )

    with (
        patch("deerflow.wiki.ingest.convert_file_to_markdown", side_effect=fake_convert),
        patch("deerflow.wiki.ingest.create_chat_model", return_value=model),
    ):
        source = ingest_file(paths, source_file)

    raw_source = paths.raw_sources_dir / "paper.pdf"
    cached_md = paths.raw_sources_dir / ".cache" / "paper.md"
    assert raw_source.is_file()
    assert cached_md.read_text(encoding="utf-8").startswith("# Paper")
    assert source.metadata["cached_markdown"] == "raw/sources/.cache/paper.md"
    assert source.metadata["ingest_mode"] == "llm"
    assert {page["page_type"] for page in source.metadata["generated_pages"]} == {"source", "concept"}
    prompt = model.invoke.call_args.args[0]
    assert "Treat schema.md" in prompt
    assert "schema_version: 1" in prompt

    source_summary = paths.wiki_sources_dir / "paper.md"
    concept_page = paths.wiki_concepts_dir / "retrieval-augmented-generation.md"
    assert "This source summarizes RAG." in source_summary.read_text(encoding="utf-8")
    assert "RAG combines retrieval" in concept_page.read_text(encoding="utf-8")

    index = WikiRepository(paths).read_index()
    assert len(index["sources"]) == 1
    assert {page["page_type"] for page in index["pages"]} == {"source", "concept"}


def test_ingest_file_falls_back_when_model_output_is_invalid(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    source_file = tmp_path / "notes.md"
    source_file.write_text("# Notes\n\nImportant local content.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(content="not json")

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        source = ingest_file(paths, source_file)

    cached_md = paths.raw_sources_dir / ".cache" / "notes.md"
    assert cached_md.read_text(encoding="utf-8") == "# Notes\n\nImportant local content."
    assert source.metadata["ingest_mode"] == "fallback"
    assert "generation_error" in source.metadata

    source_summary = paths.wiki_sources_dir / "notes.md"
    text = source_summary.read_text(encoding="utf-8")
    assert "Important local content." in text


def test_ingest_files_analyzes_batch_with_one_model_call(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    first = tmp_path / "paper-a.md"
    second = tmp_path / "paper-b.md"
    first.write_text("# Paper A\n\nMethod A uses attention.", encoding="utf-8")
    second.write_text("# Paper B\n\nMethod B uses convolution.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "pages": [
                    {
                        "type": "comparison",
                        "title": "Method Comparison",
                        "tags": ["comparison"],
                        "content": "Method A and Method B should be compared together.",
                    }
                ],
            }
        )
    )

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        sources = ingest_files(paths, [first, second])

    assert len(sources) == 2
    assert model.invoke.call_count == 1
    assert {source.metadata["ingest_mode"] for source in sources} == {"llm_batch"}

    comparison = paths.wiki_comparisons_dir / "method-comparison.md"
    assert comparison.is_file()
    assert "Method A and Method B" in comparison.read_text(encoding="utf-8")

    index = WikiRepository(paths).read_index()
    comparison_pages = [page for page in index["pages"] if page["path"] == "wiki/comparisons/method-comparison.md"]
    assert len(comparison_pages) == 1
    assert len(comparison_pages[0]["sources"]) == 2


def test_ingest_files_passes_model_name_to_chat_factory(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    source_file = tmp_path / "paper.md"
    source_file.write_text("# Paper\n\nMethod content.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(content=json.dumps({"source_summary": "Summary.", "tags": [], "pages": []}))

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model) as factory:
        ingest_files(paths, [source_file], model_name="deepseek-v4-pro")

    factory.assert_called_once_with(name="deepseek-v4-pro", thinking_enabled=False)


def test_add_source_returns_light_lint_result(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    source_file = tmp_path / "notes.md"
    source_file.write_text("# Notes\n\nImportant local content.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "source_summary": "Notes summary.",
                "tags": ["notes"],
                "pages": [],
            }
        )
    )

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        source = add_source(str(paths.root), source_file)

    lint = source["metadata"]["lint"]
    assert lint["mode"] == "light"
    assert lint["trigger"] == "add_source"
    assert "issues" in lint
    assert lint["issue_count"] == len(lint["issues"])


def test_add_source_appends_operation_log(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    source_file = tmp_path / "notes.md"
    source_file.write_text("# Notes\n\nImportant local content.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "source_summary": "Notes summary.",
                "tags": ["notes"],
                "pages": [],
            }
        )
    )

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        add_source(str(paths.root), source_file)

    log = paths.wiki_log_file.read_text(encoding="utf-8")
    assert "source-ingest" in log
    assert "Imported 1 source." in log
    assert "`raw/sources/notes.md`" in log
    assert "`wiki/sources/notes.md` (source)" in log
    assert "trigger: `add_source`" in log


def test_add_sources_returns_batch_lint_result(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("# First\n\nImportant local content.", encoding="utf-8")
    second.write_text("# Second\n\nMore local content.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "sources": [],
                "pages": [],
            }
        )
    )

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        result = add_sources(str(paths.root), [first, second])

    assert result["source_count"] == 2
    assert result["metadata"]["ingest_mode"] == "batch"
    assert result["metadata"]["lint"]["mode"] == "light"
    assert result["metadata"]["lint"]["trigger"] == "batch_ingest"
    assert all(source["metadata"]["lint"]["trigger"] == "batch_ingest" for source in result["sources"])


def test_add_sources_passes_model_name_to_ingest(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    first = tmp_path / "first.md"
    first.write_text("# First\n\nImportant local content.", encoding="utf-8")

    with patch("deerflow.wiki.service.ingest_files", return_value=[]) as ingest:
        result = add_sources(str(paths.root), [first], model_name="deepseek-v4-pro")

    ingest.assert_called_once_with(paths, [first], model_name="deepseek-v4-pro")
    assert result["source_count"] == 0


def test_add_sources_appends_batch_operation_log(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("# First\n\nImportant local content.", encoding="utf-8")
    second.write_text("# Second\n\nMore local content.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "sources": [],
                "pages": [],
            }
        )
    )

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        add_sources(str(paths.root), [first, second])

    log = paths.wiki_log_file.read_text(encoding="utf-8")
    assert "source-ingest" in log
    assert "Imported 2 sources." in log
    assert "`raw/sources/first.md`" in log
    assert "`raw/sources/second.md`" in log
    assert "trigger: `batch_ingest`" in log


def test_source_status_marks_raw_files_pending_and_parsed(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    parsed = paths.raw_sources_dir / "parsed.md"
    pending = paths.raw_sources_dir / "pending.pdf"
    parsed.write_text("# Parsed\n\nAlready imported.", encoding="utf-8")
    pending.write_bytes(b"%PDF-1.4 pending")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "source_summary": "Parsed summary.",
                "tags": ["parsed"],
                "pages": [],
            }
        )
    )

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        add_source(str(paths.root), parsed)

    status = source_status(str(paths.root))
    by_name = {item["name"]: item for item in status["files"]}

    assert by_name["parsed.md"]["status"] == "parsed"
    assert by_name["pending.pdf"]["status"] == "pending"
    assert status["counts"]["parsed"] == 1
    assert status["counts"]["pending"] == 1


def test_add_source_parses_existing_raw_source_without_duplicate_copy(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    raw_source = paths.raw_sources_dir / "local-paper.md"
    raw_source.write_text("# Local Paper\n\nStored before ingest.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "source_summary": "Local paper summary.",
                "tags": ["local"],
                "pages": [],
            }
        )
    )

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        source = add_source(str(paths.root), raw_source)

    raw_files = sorted(path.name for path in paths.raw_sources_dir.iterdir() if path.is_file())
    assert raw_files == ["local-paper.md"]
    assert source["path"] == "raw/sources/local-paper.md"


def test_sync_pending_sources_batches_unique_pending_files(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    first = paths.raw_sources_dir / "first.md"
    second = paths.raw_sources_dir / "second.md"
    first.write_text("# First\n\nNeeds import.", encoding="utf-8")
    second.write_text("# Second\n\nAlso needs import.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "pages": [
                    {
                        "type": "comparison",
                        "title": "Pending Source Comparison",
                        "tags": ["sync"],
                        "content": "Compare the pending sources together.",
                    }
                ],
            }
        )
    )

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        result = sync_pending_sources(str(paths.root))

    assert result["processed_count"] == 2
    assert result["failed_count"] == 0
    assert model.invoke.call_count == 1
    assert (paths.wiki_comparisons_dir / "pending-source-comparison.md").is_file()


def test_sync_pending_sources_imports_only_unparsed_raw_files(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    parsed = paths.raw_sources_dir / "parsed.md"
    pending = paths.raw_sources_dir / "pending.md"
    duplicate = paths.raw_sources_dir / "duplicate.md"
    parsed.write_text("# Parsed\n\nAlready imported.", encoding="utf-8")
    pending.write_text("# Pending\n\nNeeds import.", encoding="utf-8")
    duplicate.write_text("# Pending\n\nNeeds import.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "source_summary": "Source summary.",
                "tags": ["sync"],
                "pages": [],
            }
        )
    )

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        add_source(str(paths.root), parsed)
        result = sync_pending_sources(str(paths.root))

    assert result["processed_count"] == 1
    assert result["processed"][0]["path"] in {"raw/sources/pending.md", "raw/sources/duplicate.md"}
    assert result["skipped_count"] == 1
    assert result["skipped"][0]["path"] in {"raw/sources/pending.md", "raw/sources/duplicate.md"}
    assert result["skipped"][0]["path"] != result["processed"][0]["path"]
    assert result["failed_count"] == 0

    status = source_status(str(paths.root))
    by_name = {item["name"]: item for item in status["files"]}
    processed_name = Path(str(result["processed"][0]["path"])).name
    skipped_name = Path(str(result["skipped"][0]["path"])).name
    assert by_name["parsed.md"]["status"] == "parsed"
    assert by_name[processed_name]["status"] == "parsed"
    assert by_name[skipped_name]["status"] == "parsed_duplicate"


def test_sync_pending_sources_respects_limit(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    first = paths.raw_sources_dir / "first.md"
    second = paths.raw_sources_dir / "second.md"
    first.write_text("# First\n\nNeeds import.", encoding="utf-8")
    second.write_text("# Second\n\nNeeds import.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "source_summary": "Source summary.",
                "tags": ["sync"],
                "pages": [],
            }
        )
    )

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        result = sync_pending_sources(str(paths.root), limit=1)

    assert result["processed_count"] == 1
    assert result["pending_count_before"] == 2

    status = source_status(str(paths.root))
    counts = status["counts"]
    assert counts["parsed"] == 1
    assert counts["pending"] == 1
