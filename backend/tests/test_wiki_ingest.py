from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from deerflow.wiki.ingest import ingest_file, ingest_files
from deerflow.wiki.lint import lint_wiki
from deerflow.wiki.paths import display_slugify_name
from deerflow.wiki.repository import WikiRepository
from deerflow.wiki.scaffold import create_wiki_database
from deerflow.wiki.service import add_source, add_sources, source_status, sync_pending_sources


@pytest.fixture(autouse=True)
def _skip_purpose_updates_by_default(monkeypatch) -> None:
    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "should_update": False,
                "purpose_markdown": "",
                "summary": "No purpose-level change.",
            }
        )
    )
    monkeypatch.setattr("deerflow.wiki.purpose.create_chat_model", lambda *args, **kwargs: model)


def test_display_slugify_name_preserves_readable_spaces_and_cleans_path_chars() -> None:
    assert display_slugify_name("Cell-Free Massive MIMO") == "Cell-Free Massive MIMO"
    assert display_slugify_name("CF-RAN协议栈与接口设计") == "CF-RAN协议栈与接口设计"
    assert display_slugify_name("CF/RAN*Design.") == "CF-RAN-Design"


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
    assert "display_title" in prompt
    assert "reader-facing labels" in prompt
    assert "[[Lite Transformer for UAD]]" in prompt
    assert "hyphenated slug links are accepted for compatibility" in prompt

    source_summary = paths.wiki_sources_dir / "paper.md"
    concept_page = paths.wiki_concept_dir / "Retrieval Augmented Generation.md"
    assert "This source summarizes RAG." in source_summary.read_text(encoding="utf-8")
    assert "RAG combines retrieval" in concept_page.read_text(encoding="utf-8")

    index = WikiRepository(paths).read_index()
    assert len(index["sources"]) == 1
    assert {page["page_type"] for page in index["pages"]} == {"source", "concept"}


def test_ingest_source_summary_uses_readable_display_title_from_model(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    source_file = tmp_path / "deep-learning-based-activity-detection-for-uad.md"
    source_file.write_text("# Deep Learning-Based Activity Detection for UAD\n\nThe paper studies MIMO systems.", encoding="utf-8")

    model = MagicMock()

    def inject_real_source_id(prompt: str, *args, **kwargs):
        manifest = json.loads(prompt.split("Imported source manifest:\n", 1)[1].split("\n\nImported sources:", 1)[0])
        payload = {
            "sources": [
                {
                    "source_id": manifest[0]["source_id"],
                    "display_title": "Deep Learning-Based Activity Detection for UAD",
                    "source_summary": "中文说明保留 `UAD` 和 `MIMO` 的大小写。",
                    "tags": ["uad"],
                }
            ],
            "pages": [],
        }
        return AIMessage(content=json.dumps(payload))

    model.invoke.side_effect = inject_real_source_id

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        source = ingest_file(paths, source_file)

    source_page = paths.wiki_sources_dir / "Deep Learning-Based Activity Detection for UAD.md"
    text = source_page.read_text(encoding="utf-8")
    assert "# Deep Learning-Based Activity Detection for UAD" in text
    assert 'title: "Deep Learning-Based Activity Detection for UAD"' in text
    assert "中文说明保留 `UAD` 和 `MIMO`" in text
    assert source.metadata["generated_pages"][0]["title"] == "Deep Learning-Based Activity Detection for UAD"


def test_ingest_file_preserves_space_filename_wikilinks_for_lint(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    source_file = tmp_path / "cell-free-notes.md"
    source_file.write_text("# Cell-Free Notes\n\nCell-Free Massive MIMO is central.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "source_summary": "Notes summary.",
                "tags": ["cell-free"],
                "pages": [
                    {
                        "type": "idea",
                        "title": "Cell-Free Massive MIMO",
                        "tags": ["mimo"],
                        "content": "A distributed massive MIMO architecture.",
                    },
                    {
                        "type": "concept",
                        "title": "CF-mMIMO Random Access",
                        "tags": ["random-access"],
                        "content": "Random access builds on [[Cell-Free Massive MIMO]].",
                    },
                ],
            }
        )
    )

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        ingest_file(paths, source_file)

    entity_page = paths.wiki_idea_dir / "Cell-Free Massive MIMO.md"
    concept_page = paths.wiki_concept_dir / "CF-mMIMO Random Access.md"
    assert entity_page.is_file()
    assert "[[Cell-Free Massive MIMO]]" in concept_page.read_text(encoding="utf-8")
    assert [issue for issue in lint_wiki(paths) if issue.type == "broken-link"] == []


def test_ingest_unlinks_unresolved_model_wikilinks_before_writing(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    source_file = tmp_path / "paper.md"
    source_file.write_text("# Paper\n\nThe paper describes a pilot module and JCEDD method.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "sources": [
                    {
                        "source_id": "ignored-by-single-source-compat",
                        "display_title": "Paper",
                        "source_summary": "Summary links [[导频长度自适应模块]] and [[Real Page]].",
                        "tags": ["paper"],
                    }
                ],
                "pages": [
                    {
                        "type": "concept",
                        "title": "Real Page",
                        "source_ids": [],
                        "tags": ["paper"],
                        "content": "Keep [[Real Page]] but unlink [[异步交替迭代JCEDD]].",
                    }
                ],
            }
        )
    )

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        source = ingest_file(paths, source_file)

    source_text = (paths.wiki_sources_dir / "Paper.md").read_text(encoding="utf-8")
    page_text = (paths.wiki_concept_dir / "Real Page.md").read_text(encoding="utf-8")
    assert "[[Real Page]]" in source_text
    assert "[[导频长度自适应模块]]" not in source_text
    assert "导频长度自适应模块" in source_text
    assert "[[异步交替迭代JCEDD]]" not in page_text
    assert "异步交替迭代JCEDD" in page_text
    assert not (paths.root / "导频长度自适应模块.md").exists()
    assert source.metadata["link_normalization"]["unlinked"] == 2
    assert [issue for issue in lint_wiki(paths) if issue.type == "broken-link"] == []


def test_ingest_rewrites_title_style_wikilinks_to_existing_page_stems(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    source_file = tmp_path / "paper.md"
    source_file.write_text("# Paper\n\nMethod content.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "sources": [
                    {
                        "source_id": "ignored-by-single-source-compat",
                        "display_title": "Paper",
                        "source_summary": "Summary links [[Method: A/B]].",
                        "tags": ["paper"],
                    }
                ],
                "pages": [
                    {
                        "type": "algorithm",
                        "title": "Method: A/B",
                        "source_ids": [],
                        "tags": ["paper"],
                        "content": "A method page.",
                    }
                ],
            }
        )
    )

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        source = ingest_file(paths, source_file)

    source_text = (paths.wiki_sources_dir / "Paper.md").read_text(encoding="utf-8")
    assert "[[Method- A-B|Method: A/B]]" in source_text
    assert (paths.wiki_algorithm_dir / "Method- A-B.md").is_file()
    assert source.metadata["link_normalization"]["rewritten"] == 1


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
    assert source.metadata["ingest_mode"] == "llm"
    assert source.metadata["stage_status"]["fallback_level"] == "structured_generation_failed"

    source_summary = paths.wiki_sources_dir / "notes.md"
    text = source_summary.read_text(encoding="utf-8")
    assert "Important local content." in text
    assert (paths.wiki_summary_dir / "notes 导入摘要.md").is_file()


def test_ingest_files_analyzes_batch_together(tmp_path: Path) -> None:
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
                        "type": "synthesis",
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
    assert model.invoke.call_count >= 1
    assert {source.metadata["ingest_mode"] for source in sources} == {"llm_batch"}

    comparison = paths.wiki_synthesis_dir / "Method Comparison.md"
    assert comparison.is_file()
    assert "Method A and Method B" in comparison.read_text(encoding="utf-8")

    index = WikiRepository(paths).read_index()
    comparison_pages = [page for page in index["pages"] if page["path"] == "wiki/synthesis/Method Comparison.md"]
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

    factory.assert_called_once_with(name="deepseek-v4-pro", thinking_enabled=False, structured_output=True)


def test_ingest_prompt_uses_configured_zh_language_for_english_source(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki", language="zh-CN")
    source_file = tmp_path / "english-paper.md"
    source_file.write_text("# English Paper\n\nThis paper studies retrieval augmented generation.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "source_summary": "这篇论文讨论检索增强生成。",
                "tags": ["rag"],
                "pages": [
                    {
                        "type": "concept",
                        "title": "Retrieval Augmented Generation",
                        "tags": ["rag"],
                        "content": "检索增强生成结合检索与生成能力。",
                    }
                ],
            }
        )
    )

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        ingest_file(paths, source_file)

    prompt = model.invoke.call_args.args[0]
    assert "Target wiki language: `zh-CN`" in prompt
    assert "use Chinese explanatory prose even when imported sources are English" in prompt
    assert "Source language does not override the wiki language" in prompt
    assert "这篇论文讨论检索增强生成" in (paths.wiki_sources_dir / "english-paper.md").read_text(encoding="utf-8")
    assert "检索增强生成结合检索与生成能力" in (paths.wiki_concept_dir / "Retrieval Augmented Generation.md").read_text(encoding="utf-8")


def test_ingest_prompt_uses_configured_en_language(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki", language="en")
    source_file = tmp_path / "paper.md"
    source_file.write_text("# Paper\n\nMethod content.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(content=json.dumps({"source_summary": "Summary.", "tags": [], "pages": []}))

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        ingest_file(paths, source_file)

    prompt = model.invoke.call_args.args[0]
    assert "Target wiki language: `en`" in prompt
    assert "Write explanatory prose, source summaries, generated page content" in prompt
    assert "Target wiki language: `zh-CN`" not in prompt


def test_add_source_updates_purpose_when_model_requests_it(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    source_file = tmp_path / "notes.md"
    source_file.write_text("# Notes\n\nRAG is now the central research topic.", encoding="utf-8")

    ingest_model = MagicMock()
    ingest_model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "source_summary": "RAG 是新的研究重点。",
                "tags": ["rag"],
                "pages": [
                    {
                        "type": "concept",
                        "title": "Retrieval Augmented Generation",
                        "tags": ["rag"],
                        "content": "RAG 是当前知识库的核心主题。",
                    }
                ],
            }
        )
    )
    purpose_model = MagicMock()
    purpose_model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "should_update": True,
                "purpose_markdown": "# Research Wiki 研究目标\n\n## 目标\n\n聚焦 RAG 研究。\n",
                "summary": "Updated purpose for RAG focus.",
            }
        )
    )

    with (
        patch("deerflow.wiki.ingest.create_chat_model", return_value=ingest_model),
        patch("deerflow.wiki.purpose.create_chat_model", return_value=purpose_model),
    ):
        result = add_source(str(paths.root), source_file)

    assert result["metadata"]["purpose_update"]["updated"] is True
    assert result["metadata"]["purpose_update"]["summary"] == "Updated purpose for RAG focus."
    assert "聚焦 RAG 研究" in paths.purpose_file.read_text(encoding="utf-8")
    log = paths.wiki_log_file.read_text(encoding="utf-8")
    assert "Purpose update:" in log
    assert "Changed files:" in log
    assert "`purpose.md`" in log


def test_add_source_keeps_purpose_when_update_not_needed(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    original = paths.purpose_file.read_text(encoding="utf-8")
    source_file = tmp_path / "notes.md"
    source_file.write_text("# Notes\n\nSmall supporting note.", encoding="utf-8")

    ingest_model = MagicMock()
    ingest_model.invoke.return_value = AIMessage(content=json.dumps({"source_summary": "补充说明。", "tags": [], "pages": []}))
    purpose_model = MagicMock()
    purpose_model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "should_update": False,
                "purpose_markdown": "",
                "summary": "No purpose-level change.",
            }
        )
    )

    with (
        patch("deerflow.wiki.ingest.create_chat_model", return_value=ingest_model),
        patch("deerflow.wiki.purpose.create_chat_model", return_value=purpose_model),
    ):
        result = add_source(str(paths.root), source_file)

    assert result["metadata"]["purpose_update"]["updated"] is False
    assert result["metadata"]["purpose_update"]["summary"] == "No purpose-level change."
    assert paths.purpose_file.read_text(encoding="utf-8") == original
    assert "No purpose-level change." in paths.wiki_log_file.read_text(encoding="utf-8")


def test_add_source_survives_invalid_purpose_update_response(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    original = paths.purpose_file.read_text(encoding="utf-8")
    source_file = tmp_path / "notes.md"
    source_file.write_text("# Notes\n\nImportant local content.", encoding="utf-8")

    ingest_model = MagicMock()
    ingest_model.invoke.return_value = AIMessage(content=json.dumps({"source_summary": "Notes summary.", "tags": [], "pages": []}))
    purpose_model = MagicMock()
    purpose_model.invoke.return_value = AIMessage(content="not json")

    with (
        patch("deerflow.wiki.ingest.create_chat_model", return_value=ingest_model),
        patch("deerflow.wiki.purpose.create_chat_model", return_value=purpose_model),
    ):
        result = add_source(str(paths.root), source_file)

    assert result["metadata"]["purpose_update"]["updated"] is False
    assert "error" in result["metadata"]["purpose_update"]
    assert paths.purpose_file.read_text(encoding="utf-8") == original
    assert (paths.wiki_sources_dir / "notes.md").is_file()
    assert len(WikiRepository(paths).read_index()["sources"]) == 1


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
    assert "Maintenance:" in log
    assert "wiki/index.md" in log
    assert "wiki/overview.md" in log


def test_add_source_rebuilds_index_and_overview(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    source_file = tmp_path / "notes.md"
    source_file.write_text("# Notes\n\nImportant local content.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "source_summary": "Notes summary linking [[Notes Concept]].",
                "tags": ["notes"],
                "pages": [
                    {
                        "type": "concept",
                        "title": "Notes Concept",
                        "tags": ["notes"],
                        "content": "A durable concept linked back to [[notes]].",
                    }
                ],
            }
        )
    )

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        result = add_source(str(paths.root), source_file)

    index_text = paths.wiki_index_file.read_text(encoding="utf-8")
    overview_text = paths.wiki_overview_file.read_text(encoding="utf-8")
    assert "[[notes]]" in index_text
    assert "[[Notes Concept]]" in index_text
    assert "当前 wiki 已导入 1 份资料" in overview_text
    assert "[[Notes Concept]]" in overview_text
    assert result["metadata"]["maintenance"]["index"]["updated"] is True
    assert result["metadata"]["maintenance"]["overview"]["updated"] is True


def test_ingest_long_markdown_processes_content_after_40k_chars(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    source_file = tmp_path / "long-paper.md"
    late_marker = "LATE_RESULT_MARKER improves the final error rate."
    source_file.write_text("# Long Paper\n\n" + ("filler text\n" * 9000) + f"\n## Results\n\n{late_marker}\n", encoding="utf-8")

    model = MagicMock()

    def respond(prompt: str, *args, **kwargs):
        assert kwargs.get("config", {}).get("run_name") == "wiki_ingest"
        assert late_marker in prompt
        return AIMessage(
            content=json.dumps(
                {
                    "sources": [
                        {
                            "source_id": json.loads(prompt.split("Imported source manifest:\n", 1)[1].split("\n\nImported sources:", 1)[0])[0]["source_id"],
                            "display_title": "Long Paper",
                            "source_summary": f"完整导入后保留后段结果：{late_marker} 相关页面 [[Long Context]]。",
                            "tags": ["long"],
                        }
                    ],
                    "pages": [
                        {
                            "type": "concept",
                            "title": "Long Context",
                            "source_ids": [],
                            "tags": ["long"],
                            "content": f"Long-document processing captures late evidence from [[Long Paper]]: {late_marker}",
                        }
                    ],
                }
            )
        )

    model.invoke.side_effect = respond

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        source = ingest_file(paths, source_file)

    source_page = paths.wiki_sources_dir / "Long Paper.md"
    assert late_marker in source_page.read_text(encoding="utf-8")
    assert source.metadata["ingest_input"]["complete_markdown_processed"] is True
    assert source.metadata["ingest_input"]["chunk_count"] > 1


def test_ingest_uses_strict_tool_call_arguments_when_available(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    source_file = tmp_path / "strict-paper.md"
    source_file.write_text("# Strict Paper\n\nA strict structured output test.", encoding="utf-8")

    class BoundStrictModel:
        def __init__(self, function_name: str) -> None:
            self.function_name = function_name

        def invoke(self, *args, **kwargs):
            assert self.function_name == "wiki_ingest_full_paper"
            payload = {
                "sources": [
                    {
                        "source_id": "strict-source",
                        "display_title": "Strict Paper",
                        "source_summary": "Strict tool-call summary with [[Strict Output Summary]].",
                        "tags": ["strict"],
                    }
                ],
                "pages": [
                    {
                        "type": "summary",
                        "title": "Strict Output Summary",
                        "source_ids": ["strict-source"],
                        "tags": ["strict"],
                        "content": "Strict schema generated this summary.",
                    }
                ],
            }
            return AIMessage(content="", tool_calls=[{"name": self.function_name, "args": payload, "id": f"call-{self.function_name}"}])

    class StrictModel:
        def __init__(self) -> None:
            self.bound_functions: list[str] = []

        def bind_tools(self, tools, **kwargs):
            function_name = tools[0]["function"]["name"]
            assert tools[0]["function"]["strict"] is True
            self.bound_functions.append(function_name)
            return BoundStrictModel(function_name)

        def invoke(self, *args, **kwargs):
            raise AssertionError("strict tool call path should be used")

    model = StrictModel()
    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        source = ingest_file(paths, source_file)

    assert model.bound_functions == ["wiki_ingest_full_paper"]
    assert (paths.wiki_sources_dir / "Strict Paper.md").is_file()
    assert (paths.wiki_summary_dir / "Strict Output Summary.md").is_file()
    assert source.metadata["stage_status"]["pipeline"] == "full_markdown_structured_generation"


def test_ingest_structured_failure_keeps_more_than_single_source_page(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    source_file = tmp_path / "partial-paper.md"
    source_file.write_text("# Partial Paper\n\nA full-paper fallback test.", encoding="utf-8")

    model = MagicMock()
    model.invoke.return_value = AIMessage(content="not json")
    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        source = ingest_file(paths, source_file)

    assert source.metadata["ingest_mode"] == "llm"
    assert source.metadata["stage_status"]["fallback_level"] == "structured_generation_failed"
    assert (paths.wiki_sources_dir / "partial-paper.md").is_file()
    assert (paths.wiki_summary_dir / "partial-paper 导入摘要.md").is_file()


def test_ingest_quality_retry_repairs_placeholder_summary(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    source_file = tmp_path / "paper.md"
    source_file.write_text("# Paper\n\nThis paper studies a reusable method.", encoding="utf-8")

    model = MagicMock()
    model.invoke.side_effect = [
        AIMessage(content=json.dumps({"source_summary": "待补充", "tags": [], "pages": []})),
        AIMessage(
            content=json.dumps(
                {
                    "source_summary": "Better source summary with [[Reusable Method]].",
                    "tags": ["method"],
                    "pages": [
                        {
                            "type": "concept",
                            "title": "Reusable Method",
                            "tags": ["method"],
                            "content": "Reusable Method is linked to [[paper]].",
                        }
                    ],
                }
            )
        ),
    ]

    with patch("deerflow.wiki.ingest.create_chat_model", return_value=model):
        source = ingest_file(paths, source_file)

    assert "Better source summary" in (paths.wiki_sources_dir / "paper.md").read_text(encoding="utf-8")
    assert source.metadata["quality"]["initial_issues"]
    assert source.metadata["quality"]["resolved"] is True
    assert model.invoke.call_count == 2


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
                        "type": "synthesis",
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
    assert model.invoke.call_count >= 1
    assert (paths.wiki_synthesis_dir / "Pending Source Comparison.md").is_file()


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
