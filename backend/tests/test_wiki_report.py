from __future__ import annotations

from pathlib import Path

from deerflow.wiki.repository import WikiRepository
from deerflow.wiki.scaffold import create_wiki_database
from deerflow.wiki.service import get_report_context, plan_report


def _seed_report_wiki(tmp_path: Path):
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Anomaly Detection Wiki")
    page = paths.wiki_concepts_dir / "uadformer.md"
    page.write_text(
        "# UADFormer\n\n"
        "UADFormer is a transformer method for time series anomaly detection. "
        "It discusses model architecture, datasets, experiments, comparisons, "
        "limitations, and future work.",
        encoding="utf-8",
    )
    WikiRepository(paths).write_index(
        {
            "sources": [
                {
                    "source_id": "source-1",
                    "title": "UADFormer paper",
                    "path": "raw/sources/uadformer.pdf",
                    "sha256": "abc",
                }
            ],
            "pages": [
                {
                    "page_id": "page-1",
                    "title": "UADFormer",
                    "path": "wiki/concepts/uadformer.md",
                    "page_type": "concept",
                    "tags": ["anomaly-detection"],
                    "sources": ["source-1"],
                }
            ],
        }
    )
    return paths


def test_plan_report_returns_wiki_inventory_and_candidate_pages(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DEER_FLOW_WIKI_QMD_ENABLED", "0")
    paths = _seed_report_wiki(tmp_path)

    result = plan_report(str(paths.root), "UADFormer anomaly detection")

    assert result["title"] == "Anomaly Detection Wiki"
    assert result["wiki_profile"]["source_count"] == 1
    assert result["wiki_profile"]["page_type_counts"]["concept"] == 1
    assert "UADFormer anomaly detection" in result["suggested_wiki_queries"]
    assert result["candidate_pages"][0]["path"] == "wiki/concepts/uadformer.md"
    assert result["recommended_agent_flow"]


def test_get_report_context_builds_bounded_context_pack(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DEER_FLOW_WIKI_QMD_ENABLED", "0")
    paths = _seed_report_wiki(tmp_path)

    result = get_report_context(
        str(paths.root),
        "model architecture and limitations",
        queries=["UADFormer transformer limitation"],
        max_pages=3,
        max_chars_per_page=80,
        total_char_budget=80,
    )

    assert result["page_count"] == 1
    page = result["context_pages"][0]
    assert page["path"] == "wiki/concepts/uadformer.md"
    assert page["page_type"] == "concept"
    assert page["sources"] == ["source-1"]
    assert len(page["content"]) <= 80
    assert not result["insufficient_context"]


def test_get_report_context_adds_graph_expansion_after_search_hits(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DEER_FLOW_WIKI_QMD_ENABLED", "0")
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    main = paths.wiki_concepts_dir / "alpha.md"
    related = paths.wiki_concepts_dir / "beta.md"
    main.write_text("# Alpha\n\nAlpha keyword links to [[Beta]].", encoding="utf-8")
    related.write_text("# Beta\n\nBeta adds supporting context.", encoding="utf-8")
    WikiRepository(paths).write_index(
        {
            "sources": [],
            "pages": [
                {
                    "page_id": "alpha",
                    "title": "Alpha",
                    "path": "wiki/concepts/alpha.md",
                    "page_type": "concept",
                    "sources": ["source-1"],
                },
                {
                    "page_id": "beta",
                    "title": "Beta",
                    "path": "wiki/concepts/beta.md",
                    "page_type": "concept",
                    "sources": ["source-1"],
                },
            ],
        }
    )

    result = get_report_context(
        str(paths.root),
        "Alpha keyword",
        max_pages=3,
        max_chars_per_page=1000,
        total_char_budget=3000,
    )

    assert [page["path"] for page in result["context_pages"]] == [
        "wiki/concepts/alpha.md",
        "wiki/concepts/beta.md",
    ]
    assert result["context_pages"][0]["priority"] == 0
    assert result["context_pages"][1]["priority"] == 2
    assert result["context_pages"][1]["retrieval_source"] == "graph"
