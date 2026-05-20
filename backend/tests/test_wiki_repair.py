from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from deerflow.wiki.repair import build_repair_instructions, repair_lint
from deerflow.wiki.scaffold import create_wiki_database


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_build_repair_instructions_for_broken_link(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    instructions = build_repair_instructions(
        paths,
        [
            {
                "type": "broken-link",
                "severity": "warning",
                "page": "concepts/rag.md",
                "detail": "Broken link: [[Retrieval]] - target page not found.",
                "affectedPages": [],
            }
        ],
    )

    repair = instructions[0]["repair"]
    assert repair["action"] == "fix_or_create_wikilink_target"
    assert "wiki/concepts/rag.md" in repair["read"]
    assert "wiki/concepts/*.md" in repair["allowedEdits"]


def test_repair_lint_dry_run_returns_changes_without_writing(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    page = paths.wiki_concepts_dir / "rag.md"
    original = "Broken [[Retrieval]]."
    _write(page, original)

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "summary": "Fix broken retrieval link.",
                "changes": [
                    {
                        "path": "wiki/concepts/rag.md",
                        "operation": "replace",
                        "reason": "Replace broken link with plain text.",
                        "old": "Broken [[Retrieval]].",
                        "new": "Retrieval is discussed here.",
                    }
                ],
            }
        )
    )

    with patch("deerflow.wiki.repair.create_chat_model", return_value=model):
        result = repair_lint(
            paths,
            dry_run=True,
            issues=[
                {
                    "type": "broken-link",
                    "severity": "warning",
                    "page": "concepts/rag.md",
                    "detail": "Broken link: [[Retrieval]] - target page not found.",
                    "affectedPages": [],
                }
            ],
        )

    assert result["dry_run"] is True
    assert result["repair"]["changes"][0]["path"] == "wiki/concepts/rag.md"
    assert result["repair"]["changes"][0]["operation"] == "replace"
    assert page.read_text(encoding="utf-8") == original
    assert result["post_lint"] is None


def test_repair_lint_replaces_title_wikilink_with_existing_file_stem(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    page = paths.wiki_concepts_dir / "activity-detection.md"
    target = paths.wiki_entities_dir / "Heterogeneous-Transformer-HT.md"
    _write(page, "Broken [[Heterogeneous Transformer (HT)]].")
    _write(target, "# Heterogeneous Transformer (HT)\n")

    with patch("deerflow.wiki.repair.create_chat_model") as factory:
        result = repair_lint(
            paths,
            dry_run=False,
            issues=[
                {
                    "type": "broken-link",
                    "severity": "warning",
                    "page": "concepts/activity-detection.md",
                    "detail": "Broken link: [[Heterogeneous Transformer (HT)]] - target page not found.",
                    "affectedPages": [],
                }
            ],
        )

    factory.assert_not_called()
    assert page.read_text(encoding="utf-8") == "Broken [[Heterogeneous-Transformer-HT|Heterogeneous Transformer (HT)]]."
    assert result["repair"]["changes"][0]["new_preview"] == "Broken [[Heterogeneous-Transformer-HT|Heterogeneous Transformer (HT)]]."
    assert result["post_lint"] is not None


def test_repair_lint_writes_markdown_and_verifies(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    page = paths.wiki_concepts_dir / "rag.md"
    _write(page, "Broken [[Retrieval]].")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "summary": "Fix broken retrieval link.",
                "changes": [
                    {
                        "path": "wiki/concepts/rag.md",
                        "operation": "replace",
                        "reason": "Replace broken link with plain text.",
                        "old": "Broken [[Retrieval]].",
                        "new": "Retrieval is discussed here.",
                    }
                ],
            }
        )
    )

    with patch("deerflow.wiki.repair.create_chat_model", return_value=model):
        result = repair_lint(
            paths,
            dry_run=False,
            issues=[
                {
                    "type": "broken-link",
                    "severity": "warning",
                    "page": "concepts/rag.md",
                    "detail": "Broken link: [[Retrieval]] - target page not found.",
                    "affectedPages": [],
                }
            ],
        )

    assert page.read_text(encoding="utf-8") == "Retrieval is discussed here."
    assert "lint-repair" in paths.wiki_log_file.read_text(encoding="utf-8")
    assert isinstance(result["post_lint"], list)


def test_repair_lint_rejects_non_wiki_edits(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    _write(paths.wiki_concepts_dir / "rag.md", "Broken [[Retrieval]].")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "summary": "Bad change.",
                "changes": [
                    {
                        "path": "raw/sources/source.md",
                        "operation": "rewrite",
                        "reason": "Not allowed.",
                        "content": "bad",
                    }
                ],
            }
        )
    )

    with patch("deerflow.wiki.repair.create_chat_model", return_value=model):
        with pytest.raises(ValueError, match="under wiki"):
            repair_lint(
                paths,
                dry_run=False,
                issues=[
                    {
                        "type": "broken-link",
                        "severity": "warning",
                        "page": "concepts/rag.md",
                        "detail": "Broken link: [[Retrieval]] - target page not found.",
                        "affectedPages": [],
                    }
                ],
            )


def test_repair_lint_rejects_replace_when_old_text_is_missing(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    page = paths.wiki_concepts_dir / "rag.md"
    _write(page, "Current content.")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "summary": "Bad replace.",
                "changes": [
                    {
                        "path": "wiki/concepts/rag.md",
                        "operation": "replace",
                        "reason": "Old text is wrong.",
                        "old": "Missing text.",
                        "new": "Replacement.",
                    }
                ],
            }
        )
    )

    with patch("deerflow.wiki.repair.create_chat_model", return_value=model):
        with pytest.raises(ValueError, match="Replace text not found"):
            repair_lint(
                paths,
                dry_run=False,
                issues=[
                    {
                        "type": "broken-link",
                        "severity": "warning",
                        "page": "concepts/rag.md",
                        "detail": "Broken link: [[Retrieval]] - target page not found.",
                        "affectedPages": [],
                    }
                ],
            )

    assert page.read_text(encoding="utf-8") == "Current content."
