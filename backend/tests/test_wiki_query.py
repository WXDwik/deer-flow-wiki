from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from deerflow.wiki import query as query_module
from deerflow.wiki.query import search_wiki
from deerflow.wiki.scaffold import create_wiki_database


def test_qmd_command_parts_preserves_windows_backslashes(monkeypatch) -> None:
    monkeypatch.setenv("DEER_FLOW_QMD_COMMAND", r'node "C:\Temp\qmd package\dist\cli\qmd.js"')
    monkeypatch.setattr(query_module.os, "name", "nt")
    monkeypatch.setattr(query_module.shutil, "which", lambda executable: executable if executable == "node" else None)

    assert query_module._qmd_command_parts() == ["node", r"C:\Temp\qmd package\dist\cli\qmd.js"]


def test_search_wiki_uses_qmd_when_available(tmp_path: Path, monkeypatch) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    page = paths.wiki_concept_dir / "uadformer.md"
    page.write_text("# UADFormer\n\nTransformer based user action detection.", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run_qmd(args: list[str], *, timeout: float, cwd: Path):
        calls.append(args)
        if args[:2] == ["collection", "add"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args == ["update"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args and args[0] == "search":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "docid": "#abc123",
                            "score": 0.91,
                            "file": "qmd://deerflow-test/concept/uadformer.md",
                            "title": "UADFormer",
                            "snippet": "Transformer based user action detection.",
                        }
                    ]
                ),
                stderr="",
            )
        if args and args[0] == "vsearch":
            return SimpleNamespace(returncode=0, stdout=json.dumps([]), stderr="")
        raise AssertionError(f"unexpected qmd args: {args}")

    monkeypatch.setenv("DEER_FLOW_WIKI_QMD_ENABLED", "true")
    monkeypatch.setenv("DEER_FLOW_WIKI_QMD_MODE", "search")
    monkeypatch.setattr(query_module, "_run_qmd", fake_run_qmd)

    results = search_wiki(paths, "user action detection", limit=3)

    assert results[0].path == "wiki/concept/uadformer.md"
    assert results[0].title == "UADFormer"
    assert results[0].score > 0
    search_call = [call for call in calls if call and call[0] == "search"][-1]
    assert search_call[:2] == ["search", "user action detection"]
    assert search_call[-2:] == ["--collection", query_module._qmd_collection_name(paths)]


def test_search_wiki_reuses_existing_qmd_collection_for_same_path(tmp_path: Path, monkeypatch) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    (paths.wiki_synthesis_dir / "uadformer.md").write_text("# UADFormer\n\nExisting qmd collection.", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run_qmd(args: list[str], *, timeout: float, cwd: Path):
        calls.append(args)
        if args[:2] == ["collection", "add"]:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="A collection already exists for this path and pattern:\n  Name: deerflow-test (qmd://deerflow-test/)\n",
            )
        if args == ["update"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args and args[0] == "search":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([{"score": 0.5, "file": "qmd://deerflow-test/synthesis/uadformer.md", "title": "UADFormer", "snippet": "Existing qmd collection."}]),
                stderr="",
            )
        if args and args[0] == "vsearch":
            return SimpleNamespace(returncode=0, stdout=json.dumps([]), stderr="")
        raise AssertionError(f"unexpected qmd args: {args}")

    monkeypatch.setenv("DEER_FLOW_WIKI_QMD_ENABLED", "true")
    monkeypatch.setattr(query_module, "_run_qmd", fake_run_qmd)

    results = search_wiki(paths, "UADFormer", limit=3)

    assert results[0].path == "wiki/synthesis/uadformer.md"
    search_call = [call for call in calls if call and call[0] == "search"][-1]
    assert search_call[-2:] == ["--collection", "deerflow-test"]


def test_search_wiki_rrf_fuses_qmd_search_and_vsearch(tmp_path: Path, monkeypatch) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    (paths.wiki_concept_dir / "exact.md").write_text("# Exact\n\nkeyword match", encoding="utf-8")
    (paths.wiki_concept_dir / "semantic.md").write_text("# Semantic\n\nmeaning match", encoding="utf-8")

    def fake_run_qmd(args: list[str], *, timeout: float, cwd: Path):
        if args[:2] == ["collection", "add"] or args == ["update"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args and args[0] == "search":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {"score": 20, "file": "wiki/concept/exact.md", "title": "Exact", "snippet": "keyword match"},
                        {"score": 10, "file": "wiki/concept/semantic.md", "title": "Semantic", "snippet": "meaning match"},
                    ]
                ),
                stderr="",
            )
        if args and args[0] == "vsearch":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {"score": 0.9, "file": "wiki/concept/semantic.md", "title": "Semantic", "snippet": "meaning match"},
                        {"score": 0.8, "file": "wiki/concept/exact.md", "title": "Exact", "snippet": "keyword match"},
                    ]
                ),
                stderr="",
            )
        raise AssertionError(f"unexpected qmd args: {args}")

    monkeypatch.setenv("DEER_FLOW_WIKI_QMD_ENABLED", "true")
    monkeypatch.setenv("DEER_FLOW_WIKI_QMD_VECTOR_ENABLED", "true")
    monkeypatch.setattr(query_module, "_run_qmd", fake_run_qmd)

    results = search_wiki(paths, "hybrid question", limit=5)

    assert [item.path for item in results[:2]] == [
        "wiki/concept/exact.md",
        "wiki/concept/semantic.md",
    ]
    assert results[0].score == results[1].score


def test_search_wiki_falls_back_to_keywords_when_qmd_unavailable(tmp_path: Path, monkeypatch) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    page = paths.wiki_concept_dir / "rag.md"
    page.write_text("# RAG\n\nRetrieval Augmented Generation combines retrieval with generation.", encoding="utf-8")

    monkeypatch.setenv("DEER_FLOW_WIKI_QMD_ENABLED", "true")
    monkeypatch.setattr(query_module, "_run_qmd", lambda *args, **kwargs: None)

    results = search_wiki(paths, "Retrieval Generation", limit=5)

    assert len(results) == 1
    assert results[0].path == "wiki/concept/rag.md"
    assert results[0].score > 0
    assert "Retrieval Augmented Generation" in results[0].snippet


def test_parse_qmd_results_accepts_results_object(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    payload = {
        "results": [
            {
                "score": "0.75",
                "file": "wiki/synthesis/uadformer.md",
                "title": "UADFormer synthesis",
                "snippet": "A synthesized page.",
            }
        ]
    }

    results = query_module._parse_qmd_results(paths, json.dumps(payload), limit=10)

    assert results is not None
    assert results[0].path == "wiki/synthesis/uadformer.md"
    assert results[0].score == 0.75
