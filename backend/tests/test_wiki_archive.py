from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage

from deerflow.wiki.archive import archive_answer
from deerflow.wiki.models import WikiCitation
from deerflow.wiki.repository import WikiRepository
from deerflow.wiki.scaffold import create_wiki_database
from deerflow.wiki.service import archive_answer as service_archive_answer


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_archive_answer_judges_without_writing_when_auto_archive_disabled(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    _write(
        paths.wiki_concepts_dir / "rag.md",
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
                    "page_type": "query",
                    "suggested_title": "RAG definition",
                    "target_pages": [],
                    "tags": ["rag"],
                    "cited_pages": ["wiki/concepts/rag.md"],
                }
            )
        ),
    ]

    with patch("deerflow.wiki.archive.create_chat_model", return_value=model):
        result = archive_answer(
            paths,
            "What is RAG?",
            "RAG combines retrieval with generation. See [[rag]].",
            citations=[WikiCitation(title="rag", path="wiki/concepts/rag.md")],
            auto_archive=False,
        )

    assert result.archive_decision is not None
    assert result.archive_decision.should_archive is True
    assert result.archive_decision.action == "create_page"
    assert result.archive_applied is False
    assert result.archived_page is None
    assert not (paths.wiki_queries_dir / "rag-definition.md").exists()


def test_archive_answer_auto_archive_creates_page_updates_index_and_log(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    _write(
        paths.wiki_concepts_dir / "rag.md",
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
                    "page_type": "query",
                    "suggested_title": "RAG definition",
                    "target_pages": [],
                    "tags": ["rag"],
                    "cited_pages": ["wiki/concepts/rag.md"],
                }
            )
        ),
        AIMessage(
            content=json.dumps(
                {
                    "title": "RAG definition",
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
            citations=[WikiCitation(title="rag", path="wiki/concepts/rag.md")],
            auto_archive=True,
        )

    archived = paths.wiki_queries_dir / "rag-definition.md"
    assert archived.is_file()
    assert "## Conclusion" in archived.read_text(encoding="utf-8")
    assert result.archive_applied is True
    assert result.archived_page is not None
    assert result.archived_page.path == "wiki/queries/rag-definition.md"
    assert "[[rag-definition|RAG definition]]" in paths.wiki_index_file.read_text(encoding="utf-8")
    assert "query-archive" in paths.wiki_log_file.read_text(encoding="utf-8")

    index = WikiRepository(paths).read_index()
    assert any(page["path"] == "wiki/queries/rag-definition.md" for page in index["pages"])


def test_archive_answer_auto_archive_updates_existing_page(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    page = paths.wiki_concepts_dir / "rag.md"
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
                    "target_pages": ["wiki/concepts/rag.md"],
                    "tags": ["rag"],
                    "cited_pages": ["wiki/concepts/rag.md"],
                }
            )
        ),
        AIMessage(
            content=json.dumps(
                {
                    "changes": [
                        {
                            "path": "wiki/concepts/rag.md",
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
            citations=[WikiCitation(title="rag", path="wiki/concepts/rag.md")],
            auto_archive=True,
        )

    text = page.read_text(encoding="utf-8")
    assert "## Evaluation" in text
    assert result.archive_applied is True
    assert result.archived_page is None
    assert result.page_changes[0].path == "wiki/concepts/rag.md"
    assert "wiki/concepts/rag.md" in paths.wiki_log_file.read_text(encoding="utf-8")


def test_service_archive_answer_returns_plain_dict(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    _write(paths.wiki_concepts_dir / "rag.md", "# RAG\n\nretrieval generation")

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
            citations=[{"title": "rag", "path": "wiki/concepts/rag.md"}],
            auto_archive=False,
        )

    assert result["answer_markdown"] == "See [[rag]]."
    assert result["archive_decision"]["should_archive"] is False
