from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from deerflow.wiki import query as query_module
from deerflow.wiki import report as report_module
from deerflow.wiki.repository import WikiRepository
from deerflow.wiki.scaffold import create_wiki_database
from deerflow.wiki.service import research_context


def test_research_context_uses_qmd_query_without_vsearch(tmp_path: Path, monkeypatch) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    page = paths.wiki_concept_dir / "alpha.md"
    page.write_text("# Alpha\n\nAlpha method evidence.", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run_qmd(args: list[str], *, timeout: float, cwd: Path):
        calls.append(args)
        if args[:2] == ["collection", "add"] or args == ["update"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args and args[0] == "query":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "score": 0.9,
                            "file": "wiki/concept/alpha.md",
                            "title": "Alpha",
                            "snippet": "Alpha method evidence.",
                        }
                    ]
                ),
                stderr="",
            )
        raise AssertionError(f"unexpected qmd args: {args}")

    monkeypatch.setenv("DEER_FLOW_WIKI_QMD_ENABLED", "true")
    monkeypatch.setattr(query_module, "_run_qmd", fake_run_qmd)

    result = research_context(str(paths.root), "Alpha method", max_pages=2)

    assert result["retrieval"]["mode"] == "qmd_query_graph_context"
    assert result["context_pages"][0]["retrieval_source"] == "qmd_query"
    assert any(call and call[0] == "query" and "-n" in call and "2" in call for call in calls)
    assert not any(call and call[0] == "vsearch" for call in calls)


def test_research_context_does_not_depend_on_legacy_report_candidate_collection(tmp_path: Path, monkeypatch) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    (paths.wiki_concept_dir / "alpha.md").write_text("# Alpha\n\nAlpha evidence.", encoding="utf-8")

    def fake_qmd_query(paths_arg, query: str, *, limit: int):
        return [
            query_module.SearchResult(
                path="wiki/concept/alpha.md",
                title="Alpha",
                score=1.0,
                snippet="Alpha evidence.",
            )
        ]

    monkeypatch.setattr(report_module, "qmd_query_wiki", fake_qmd_query)
    assert not hasattr(report_module, "_collect_search_candidates")

    result = research_context(str(paths.root), "Alpha")

    assert result["context_pages"][0]["path"] == "wiki/concept/alpha.md"


def test_research_context_returns_full_short_page(tmp_path: Path, monkeypatch) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    text = "# Alpha\n\nShort page evidence."
    (paths.wiki_concept_dir / "alpha.md").write_text(text, encoding="utf-8")

    monkeypatch.setattr(
        report_module,
        "qmd_query_wiki",
        lambda *args, **kwargs: [
            query_module.SearchResult(
                path="wiki/concept/alpha.md",
                title="Alpha",
                score=1.0,
                snippet="Short page evidence.",
            )
        ],
    )

    result = research_context(str(paths.root), "Alpha evidence", max_chars_per_page=200)

    page = result["context_pages"][0]
    assert page["content"] == text
    assert page["content_strategy"] == "full"
    assert not page["truncated"]


def test_research_context_expands_content_around_qmd_snippet(tmp_path: Path, monkeypatch) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    before = "Intro text. " * 80
    target = "Critical transformer limitation evidence appears here."
    after = "Extra trailing text. " * 80
    (paths.wiki_concept_dir / "alpha.md").write_text(f"# Alpha\n\n{before}{target}{after}", encoding="utf-8")

    monkeypatch.setattr(
        report_module,
        "qmd_query_wiki",
        lambda *args, **kwargs: [
            query_module.SearchResult(
                path="wiki/concept/alpha.md",
                title="Alpha",
                score=1.0,
                snippet=target,
            )
        ],
    )

    result = research_context(str(paths.root), "transformer limitation", max_chars_per_page=220)

    page = result["context_pages"][0]
    assert target in page["content"]
    assert page["content_strategy"] == "snippet_window"
    assert page["truncated"]


def test_research_context_falls_back_to_query_matched_paragraph(tmp_path: Path, monkeypatch) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    page_text = (
        "# Alpha\n\n"
        "General introduction that is not enough.\n\n"
        "The evaluation metric paragraph explains anomaly detection precision and recall.\n\n"
        "Unrelated appendix."
    )
    (paths.wiki_concept_dir / "alpha.md").write_text(page_text, encoding="utf-8")

    monkeypatch.setattr(
        report_module,
        "qmd_query_wiki",
        lambda *args, **kwargs: [
            query_module.SearchResult(
                path="wiki/concept/alpha.md",
                title="Alpha",
                score=1.0,
                snippet="This snippet is not present in the markdown.",
            )
        ],
    )

    result = research_context(str(paths.root), "evaluation metric", max_chars_per_page=90)

    page = result["context_pages"][0]
    assert "evaluation metric paragraph" in page["content"]
    assert page["content_strategy"] == "paragraph_match"


def test_research_context_adds_bounded_graph_pages_with_quota(tmp_path: Path, monkeypatch) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    alpha = paths.wiki_concept_dir / "alpha.md"
    beta = paths.wiki_concept_dir / "beta.md"
    gamma = paths.wiki_concept_dir / "gamma.md"
    alpha.write_text("# Alpha\n\nAlpha keyword links to [[Beta]] and [[Gamma]].", encoding="utf-8")
    beta.write_text("# Beta\n\nBeta supporting Alpha evidence.\n\n" + ("long beta text " * 200), encoding="utf-8")
    gamma.write_text("# Gamma\n\nGamma supporting Alpha evidence.\n\n" + ("long gamma text " * 200), encoding="utf-8")
    WikiRepository(paths).write_index(
        {
            "sources": [],
            "pages": [
                {
                    "page_id": "alpha",
                    "title": "Alpha",
                    "path": "wiki/concept/alpha.md",
                    "page_type": "concept",
                    "sources": ["source-1"],
                },
                {
                    "page_id": "beta",
                    "title": "Beta",
                    "path": "wiki/concept/beta.md",
                    "page_type": "concept",
                    "sources": ["source-1"],
                },
                {
                    "page_id": "gamma",
                    "title": "Gamma",
                    "path": "wiki/concept/gamma.md",
                    "page_type": "concept",
                    "sources": ["source-1"],
                },
            ],
        }
    )

    monkeypatch.setattr(
        report_module,
        "qmd_query_wiki",
        lambda *args, **kwargs: [
            query_module.SearchResult(
                path="wiki/concept/alpha.md",
                title="Alpha",
                score=1.0,
                snippet="Alpha keyword links",
            )
        ],
    )

    result = research_context(
        str(paths.root),
        "Alpha evidence",
        max_pages=3,
        max_chars_per_page=120,
        total_char_budget=360,
    )

    graph_pages = [page for page in result["context_pages"] if page["retrieval_source"] == "graph"]
    assert [page["retrieval_source"] for page in result["context_pages"]][0] == "qmd_query"
    assert len(graph_pages) == 1
    assert len(result["context_pages"]) <= 3
    assert result["total_chars"] <= 360
    assert graph_pages[0]["content_strategy"] in {"paragraph_match", "leading_excerpt", "full"}

