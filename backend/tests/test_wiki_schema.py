from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage

from deerflow.wiki.repository import WikiRepository
from deerflow.wiki.scaffold import create_wiki_database
from deerflow.wiki.schema import evolve_schema, parse_schema_version, schema_context


def test_new_wiki_has_versioned_schema_contract(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")

    schema = paths.schema_file.read_text(encoding="utf-8")
    config = WikiRepository(paths).read_config()

    assert parse_schema_version(schema) == 1
    assert "schema_version: 1" in schema
    assert "Schema 演化规则" in schema
    assert "语言与显示标题规则" in schema
    assert "frontmatter `title`、页面一级标题、索引显示名和图谱节点标签" in schema
    assert "[[lite-transformer-for-uad|Lite Transformer for UAD]]" in schema
    assert paths.wiki_maintenance_dir.is_dir()
    assert config["schema"]["current_version"] == 1
    assert config["schema"]["evolution_log"] == "wiki/maintenance/schema-changelog.md"


def test_schema_context_reads_current_schema_each_time(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    paths.schema_file.write_text("schema_version: 7\n\n# Custom Schema\n\nraw/ .llm-wiki/ log.md", encoding="utf-8")

    context = schema_context(paths)

    assert "schema_version: 7" in context
    assert "Custom Schema" in context


def test_evolve_schema_dry_run_does_not_write_files(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    original = paths.schema_file.read_text(encoding="utf-8")
    proposed = "schema_version: 2\n\n# Updated Schema\n\nKeep raw/ immutable, .llm-wiki/ internal, and log.md append-only."

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "schema_markdown": proposed,
                "summary": "Add stricter citation rules.",
                "version": 2,
            }
        )
    )

    with patch("deerflow.wiki.schema.create_chat_model", return_value=model):
        result = evolve_schema(
            paths,
            change_request="Add stricter citation rules.",
            evidence=["Two pages lacked sources."],
            dry_run=True,
        )

    assert result["dry_run"] is True
    assert result["previous_version"] == 1
    assert result["proposed_version"] == 2
    assert result["schema_markdown"] == proposed
    assert paths.schema_file.read_text(encoding="utf-8") == original
    assert not (paths.wiki_maintenance_dir / "schema-changelog.md").exists()


def test_evolve_schema_apply_updates_schema_config_and_logs(tmp_path: Path) -> None:
    paths = create_wiki_database(str(tmp_path / "wiki"), title="Research Wiki")
    proposed = "schema_version: 2\n\n# Updated Schema\n\nKeep raw/ immutable, .llm-wiki/ internal, and log.md append-only."

    model = MagicMock()
    model.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "schema_markdown": proposed,
                "summary": "Add stable page creation rules.",
                "version": 2,
            }
        )
    )

    with patch("deerflow.wiki.schema.create_chat_model", return_value=model):
        result = evolve_schema(
            paths,
            change_request="Add stable page creation rules.",
            evidence=["Duplicate concept pages appeared."],
            dry_run=False,
        )

    assert result["changed_paths"] == ["schema.md", "wiki/maintenance/schema-changelog.md", "wiki/log.md"]
    assert parse_schema_version(paths.schema_file.read_text(encoding="utf-8")) == 2
    assert "Add stable page creation rules." in (paths.wiki_maintenance_dir / "schema-changelog.md").read_text(encoding="utf-8")
    assert "schema-evolution" in paths.wiki_log_file.read_text(encoding="utf-8")
    assert WikiRepository(paths).read_config()["schema"]["current_version"] == 2
