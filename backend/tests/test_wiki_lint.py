from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage

from deerflow.wiki.lint import lint_wiki, parse_semantic_lint_output, resolve_lint_mode, semantic_lint_wiki, structural_lint_wiki
from deerflow.wiki.scaffold import create_wiki_database


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_structural_lint_detects_orphan_no_outlinks_and_broken_links(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    _write(paths.wiki_concept_dir / "Transformer.md", "Links to [[Attention Mechanism|attention]] and [[Missing Page]].")
    _write(paths.wiki_concept_dir / "Attention Mechanism.md", "No links here.")
    _write(paths.wiki_idea_dir / "OpenAI.md", "No links here either.")

    issues = structural_lint_wiki(paths)
    by_type = {}
    for issue in issues:
        by_type.setdefault(issue.type, []).append(issue)

    assert any(issue.page == "concept/Transformer.md" and "[[Missing Page]]" in issue.detail for issue in by_type["broken-link"])
    assert {issue.page for issue in by_type["orphan"]} == {"overview.md", "concept/Transformer.md", "idea/OpenAI.md"}
    assert {issue.page for issue in by_type["no-outlinks"]} == {"overview.md", "concept/Attention Mechanism.md", "idea/OpenAI.md"}
    assert all(issue.page not in {"index.md", "log.md"} for issue in issues)


def test_structural_lint_resolves_display_stem_and_legacy_slug_wikilinks(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    _write(paths.wiki_idea_dir / "Deep-Learning-Based-Activity-Detection-for-UAD.md", "# Deep Learning-Based Activity Detection for UAD\n")
    _write(
        paths.wiki_concept_dir / "Activity-Detection.md",
        "Links to [[Deep-Learning-Based-Activity-Detection-for-UAD]], "
        "[[deep-learning-based-activity-detection-for-uad]], and "
        "[[idea/Deep-Learning-Based-Activity-Detection-for-UAD|Deep Learning-Based Activity Detection for UAD]].",
    )

    issues = structural_lint_wiki(paths)

    broken = [issue for issue in issues if issue.type == "broken-link"]
    assert broken == []


def test_parse_semantic_lint_output_returns_unified_results() -> None:
    text = """---LINT: contradiction | warning | Conflicting definition of RAG---
Two pages describe RAG differently.
PAGES: concept/rag.md, sources/paper-a.md
---END LINT---

---LINT: suggestion | info | Add retrieval evaluation question---
The wiki would benefit from a saved research question about evaluation.
PAGES: concept/rag.md
---END LINT---
"""

    issues = parse_semantic_lint_output(text)

    assert issues[0].type == "semantic"
    assert issues[0].severity == "warning"
    assert issues[0].page == "Conflicting definition of RAG"
    assert issues[0].detail == "[contradiction] Two pages describe RAG differently."
    assert issues[0].affectedPages == ["concept/rag.md", "sources/paper-a.md"]
    assert issues[1].severity == "info"
    assert issues[1].detail.startswith("[suggestion]")


def test_semantic_lint_invokes_model_with_page_summaries(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    _write(paths.wiki_concept_dir / "rag.md", "---\ntype: concept\n---\n# RAG\n\nRetrieval content.")

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content="""---LINT: missing-page | info | Missing Evaluation page---
Evaluation appears important but has no dedicated page.
PAGES: concept/rag.md
---END LINT---"""
    )

    with patch("deerflow.wiki.lint.create_chat_model", return_value=model):
        issues = semantic_lint_wiki(paths)

    prompt = model.invoke.call_args.args[0]
    assert "### concept/rag.md" in prompt
    assert "### log.md" not in prompt
    assert issues[0].type == "semantic"
    assert issues[0].detail.startswith("[missing-page]")


def test_lint_wiki_includes_semantic_only_when_requested(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    _write(paths.wiki_concept_dir / "lonely.md", "No links.")

    with patch("deerflow.wiki.lint.semantic_lint_wiki", return_value=[]) as semantic:
        lint_wiki(paths)
        semantic.assert_not_called()

    with patch("deerflow.wiki.lint.semantic_lint_wiki", return_value=[]) as semantic:
        lint_wiki(paths, include_semantic=True)
        semantic.assert_called_once_with(paths)


def test_resolve_lint_mode_applies_trigger_policy() -> None:
    assert resolve_lint_mode(mode="auto", trigger="add_source") == "light"
    assert resolve_lint_mode(mode="auto", trigger="page_delete") == "light"
    assert resolve_lint_mode(mode="auto", trigger="batch_ingest") == "deep"
    assert resolve_lint_mode(mode="auto", trigger="chat_quality_drop") == "deep"
    assert resolve_lint_mode(mode="light", trigger="batch_ingest") == "light"
    assert resolve_lint_mode(mode="deep", trigger="manual") == "deep"
    assert resolve_lint_mode(mode="light", include_semantic=True) == "deep"

