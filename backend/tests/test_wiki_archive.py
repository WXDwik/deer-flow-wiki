from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from deerflow.wiki.archive import archive_answer
from deerflow.wiki.models import WikiCitation
from deerflow.wiki.repository import WikiRepository
from deerflow.wiki.scaffold import create_wiki_database
from deerflow.wiki.service import archive_answer as service_archive_answer


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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


def test_archive_answer_judges_without_writing_when_auto_archive_disabled(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    _write(
        paths.wiki_concept_dir / "rag.md",
        "---\ntype: concept\n---\n# RAG\n\nRAG combines retrieval with generation.",
    )

    model = MagicMock()
    model.invoke.side_effect = [
        AIMessage(
            content=json.dumps(
                {
                    "should_archive": True,
                    "reason": "Reusable definition.",
                    "action": "create_page",
                    "page_type": "synthesis",
                    "suggested_title": "RAG Definition",
                    "target_pages": [],
                    "tags": ["rag"],
                    "cited_pages": ["wiki/concept/rag.md"],
                }
            )
        ),
    ]

    with patch("deerflow.wiki.archive.create_chat_model", return_value=model):
        result = archive_answer(
            paths,
            "What is RAG?",
            "RAG combines retrieval with generation. See [[rag]].",
            citations=[WikiCitation(title="rag", path="wiki/concept/rag.md")],
            auto_archive=False,
        )

    assert result.archive_decision is not None
    assert result.archive_decision.should_archive is True
    assert result.archive_decision.action == "create_page"
    assert result.archive_applied is False
    assert result.archived_page is None
    assert not (paths.wiki_synthesis_dir / "RAG Definition.md").exists()


def test_archive_answer_auto_archive_creates_page_updates_index_and_log(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    _write(
        paths.wiki_concept_dir / "rag.md",
        "---\ntype: concept\n---\n# RAG\n\nRAG combines retrieval with generation.",
    )

    model = MagicMock()
    model.invoke.side_effect = [
        AIMessage(
            content=json.dumps(
                {
                    "should_archive": True,
                    "reason": "Reusable definition.",
                    "action": "create_page",
                    "page_type": "synthesis",
                    "suggested_title": "RAG Definition",
                    "target_pages": [],
                    "tags": ["rag"],
                    "cited_pages": ["wiki/concept/rag.md"],
                }
            )
        ),
        AIMessage(
            content=json.dumps(
                {
                    "title": "RAG Definition",
                    "markdown_body": "## Conclusion\n\nRAG combines retrieval with generation.\n\n## Evidence\n\n- [[rag]]",
                }
            )
        ),
    ]

    with patch("deerflow.wiki.archive.create_chat_model", return_value=model):
        result = archive_answer(
            paths,
            "What is RAG?",
            "RAG combines retrieval with generation. See [[rag]].",
            citations=[WikiCitation(title="rag", path="wiki/concept/rag.md")],
            auto_archive=True,
        )

    archived = paths.wiki_synthesis_dir / "RAG Definition.md"
    assert archived.is_file()
    assert "## Conclusion" in archived.read_text(encoding="utf-8")
    assert result.archive_applied is True
    assert result.archived_page is not None
    assert result.archived_page.path == "wiki/synthesis/RAG Definition.md"
    assert "[[RAG Definition]]" in paths.wiki_index_file.read_text(encoding="utf-8")
    assert "query-archive" in paths.wiki_log_file.read_text(encoding="utf-8")
    prompts = [call.args[0] for call in model.invoke.call_args_list]
    assert any("Target wiki language: `zh-CN`" in prompt for prompt in prompts)
    assert any("use Chinese explanatory prose even when imported sources are English" in prompt for prompt in prompts)

    index = WikiRepository(paths).read_index()
    assert any(page["path"] == "wiki/synthesis/RAG Definition.md" for page in index["pages"])


def test_archive_answer_auto_archive_updates_existing_page(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    page = paths.wiki_concept_dir / "rag.md"
    _write(
        page,
        "---\ntype: concept\n---\n# RAG\n\nRAG combines retrieval with generation.",
    )

    model = MagicMock()
    model.invoke.side_effect = [
        AIMessage(
            content=json.dumps(
                {
                    "should_archive": True,
                    "reason": "Small local supplement.",
                    "action": "update_existing",
                    "page_type": None,
                    "suggested_title": None,
                    "target_pages": ["wiki/concept/rag.md"],
                    "tags": ["rag"],
                    "cited_pages": ["wiki/concept/rag.md"],
                }
            )
        ),
        AIMessage(
            content=json.dumps(
                {
                    "changes": [
                        {
                            "path": "wiki/concept/rag.md",
                            "operation": "append",
                            "reason": "Add evaluation note.",
                            "content": "## Evaluation\n\nRAG answers should be evaluated for retrieval quality.",
                        }
                    ]
                }
            )
        ),
    ]

    with patch("deerflow.wiki.archive.create_chat_model", return_value=model):
        result = archive_answer(
            paths,
            "What should we add about RAG evaluation?",
            "RAG needs evaluation notes. See [[rag]].",
            citations=[WikiCitation(title="rag", path="wiki/concept/rag.md")],
            auto_archive=True,
        )

    text = page.read_text(encoding="utf-8")
    assert "## Evaluation" in text
    assert result.archive_applied is True
    assert result.archived_page is None
    assert result.page_changes[0].path == "wiki/concept/rag.md"
    assert "wiki/concept/rag.md" in paths.wiki_log_file.read_text(encoding="utf-8")


def test_archive_answer_updates_purpose_after_writeback(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    _write(paths.wiki_concept_dir / "rag.md", "---\ntype: concept\n---\n# RAG\n\nretrieval generation")

    model = MagicMock()
    model.invoke.side_effect = [
        AIMessage(
            content=json.dumps(
                {
                    "should_archive": True,
                    "reason": "Reusable RAG scope change.",
                    "action": "create_page",
                    "page_type": "synthesis",
                    "suggested_title": "RAG Scope",
                    "target_pages": [],
                    "tags": ["rag"],
                    "cited_pages": ["wiki/concept/rag.md"],
                }
            )
        ),
        AIMessage(content=json.dumps({"title": "RAG Scope", "markdown_body": "RAG scope details."})),
    ]
    purpose_model = MagicMock()
    purpose_model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "should_update": True,
                "purpose_markdown": "# Research Wiki 研究目标\n\n## 目标\n\n归档 RAG 范围问题。\n",
                "summary": "Updated purpose from archive.",
            }
        )
    )

    with (
        patch("deerflow.wiki.archive.create_chat_model", return_value=model),
        patch("deerflow.wiki.purpose.create_chat_model", return_value=purpose_model),
    ):
        result = archive_answer(
            paths,
            "What RAG scope should be tracked?",
            "Track reusable RAG scope.",
            citations=[WikiCitation(title="rag", path="wiki/concept/rag.md")],
            auto_archive=True,
        )

    assert result.archive_applied is True
    assert "归档 RAG 范围问题" in paths.purpose_file.read_text(encoding="utf-8")
    log = paths.wiki_log_file.read_text(encoding="utf-8")
    assert "Purpose update:" in log
    assert "`purpose.md`" in log


def test_service_archive_answer_returns_plain_dict(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    _write(paths.wiki_concept_dir / "rag.md", "# RAG\n\nretrieval generation")

    model = MagicMock()
    model.invoke.side_effect = [
        AIMessage(
            content=json.dumps(
                {
                    "should_archive": False,
                    "reason": "Too small.",
                    "action": "none",
                    "page_type": None,
                    "suggested_title": None,
                    "target_pages": [],
                    "tags": [],
                    "cited_pages": [],
                }
            )
        ),
    ]

    with patch("deerflow.wiki.archive.create_chat_model", return_value=model):
        result = service_archive_answer(
            str(paths.root),
            "RAG?",
            "See [[rag]].",
            citations=[{"title": "rag", "path": "wiki/concept/rag.md"}],
            auto_archive=False,
        )

    assert result["answer_markdown"] == "See [[rag]]."
    assert result["archive_decision"]["should_archive"] is False
