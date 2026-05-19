"""Schema contract helpers for LLM Wiki maintenance.

The schema is a Markdown file written for the LLM, but the runtime still needs
small mechanical hooks: read the current contract on every write workflow,
track its version, and evolve it through a constrained write path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from deerflow.models import create_chat_model
from deerflow.wiki.paths import WikiPaths
from deerflow.wiki.repository import WikiRepository

_SCHEMA_VERSION_RE = re.compile(r"^\s*schema_version\s*:\s*[\"']?(\d+)[\"']?\s*$", re.MULTILINE)
_MAX_CONTEXT_CHARS = 24_000


@dataclass(frozen=True)
class WikiSchemaContract:
    """The current schema contract read from `schema.md`."""

    version: int
    text: str


def parse_schema_version(text: str) -> int:
    """Parse `schema_version` from schema Markdown, defaulting old schemas to v1."""
    match = _SCHEMA_VERSION_RE.search(text)
    if not match:
        return 1
    return int(match.group(1))


def read_schema_contract(paths: WikiPaths) -> WikiSchemaContract:
    """Read the current schema contract from disk on demand."""
    text = paths.schema_file.read_text(encoding="utf-8", errors="ignore") if paths.schema_file.is_file() else ""
    return WikiSchemaContract(version=parse_schema_version(text), text=text)


def schema_context(paths: WikiPaths, *, max_chars: int = 8_000) -> str:
    """Return a bounded prompt section that makes the active schema explicit."""
    contract = read_schema_contract(paths)
    text = contract.text[:max_chars] if contract.text else "No schema.md found."
    return f"""## Active Wiki Schema

Path: schema.md
schema_version: {contract.version}

```markdown
{text}
```"""


def _json_from_model_text(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _read_evolution_context(paths: WikiPaths) -> str:
    parts: list[str] = []
    for path in (paths.purpose_file, paths.schema_file, paths.wiki_index_file, paths.wiki_overview_file):
        if not path.is_file():
            continue
        rel = path.relative_to(paths.root).as_posix()
        parts.append(f"## {rel}\n{path.read_text(encoding='utf-8', errors='ignore')[:6_000]}")
    return "\n\n".join(parts)[:_MAX_CONTEXT_CHARS]


def _evolution_prompt(paths: WikiPaths, change_request: str, evidence: list[str], next_version: int) -> str:
    return f"""You evolve the schema.md contract for an LLM-maintained research wiki.

Return ONLY valid JSON:
{{
  "schema_markdown": "Full updated schema.md Markdown",
  "summary": "Short summary of what changed",
  "version": {next_version}
}}

Hard rules:
- Update schema.md only; do not modify wiki knowledge content.
- Increment schema_version to exactly {next_version}.
- Keep durable maintenance rules only. Do not add facts from specific papers.
- Preserve hard safety rules: never edit raw/, never edit .llm-wiki/ manually, log.md append-only.
- Keep the schema practical for future ingest, query, archive, lint, repair, and schema evolution workflows.
- If the change request is too temporary or content-specific, keep the schema mostly unchanged but still return a coherent v{next_version} schema.

Schema evolution request:
{change_request}

Evidence or repeated issues:
{json.dumps(evidence, ensure_ascii=False, indent=2)}

Current wiki context:
{_read_evolution_context(paths)}
"""


def _validate_schema_markdown(markdown: str, expected_version: int) -> None:
    if not markdown.strip():
        raise ValueError("Schema evolution returned an empty schema")
    actual_version = parse_schema_version(markdown)
    if actual_version != expected_version:
        raise ValueError(f"Schema evolution must set schema_version to {expected_version}, got {actual_version}")
    for required in ("raw/", ".llm-wiki/", "log.md"):
        if required not in markdown:
            raise ValueError(f"Schema evolution dropped required safety rule reference: {required}")


def _append_schema_changelog(paths: WikiPaths, *, previous_version: int, new_version: int, summary: str, change_request: str) -> None:
    now = datetime.now(UTC).isoformat()
    paths.wiki_maintenance_dir.mkdir(parents=True, exist_ok=True)
    changelog = paths.wiki_maintenance_dir / "schema-changelog.md"
    if not changelog.exists():
        changelog.write_text("# Schema Changelog\n", encoding="utf-8")
    with changelog.open("a", encoding="utf-8") as f:
        f.write(
            f"\n## [{now}] v{previous_version} -> v{new_version}\n\n"
            f"{summary}\n\n"
            f"Change request:\n\n{change_request}\n"
        )


def _append_schema_log(paths: WikiPaths, *, previous_version: int, new_version: int, summary: str) -> None:
    now = datetime.now(UTC).isoformat()
    entry = (
        f"\n## [{now}] schema-evolution\n\n"
        f"Updated schema from v{previous_version} to v{new_version}.\n\n"
        f"{summary}\n\n"
        "Changed files:\n"
        "- `schema.md`\n"
        "- `wiki/maintenance/schema-changelog.md`\n"
    )
    paths.wiki_log_file.parent.mkdir(parents=True, exist_ok=True)
    with paths.wiki_log_file.open("a", encoding="utf-8") as f:
        f.write(entry)


def evolve_schema(
    paths: WikiPaths,
    *,
    change_request: str,
    evidence: list[str] | None = None,
    dry_run: bool = True,
    model_name: str | None = None,
) -> dict[str, Any]:
    """Propose or apply a controlled schema.md evolution."""
    if not change_request.strip():
        raise ValueError("change_request cannot be empty")

    current = read_schema_contract(paths)
    next_version = current.version + 1
    model = create_chat_model(name=model_name, thinking_enabled=False)
    response = model.invoke(
        _evolution_prompt(paths, change_request, evidence or [], next_version),
        config={"run_name": "wiki_schema_evolution"},
    )
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "\n".join(str(item) for item in content)
    data = _json_from_model_text(str(content))
    if data is None:
        raise ValueError("Schema evolution model did not return valid JSON")

    schema_markdown = str(data.get("schema_markdown") or "").strip()
    summary = str(data.get("summary") or "Updated schema.").strip()
    _validate_schema_markdown(schema_markdown, next_version)

    changed_paths: list[str] = []
    if not dry_run:
        paths.schema_file.write_text(schema_markdown + "\n", encoding="utf-8")
        _append_schema_changelog(
            paths,
            previous_version=current.version,
            new_version=next_version,
            summary=summary,
            change_request=change_request,
        )
        _append_schema_log(paths, previous_version=current.version, new_version=next_version, summary=summary)
        changed_paths = ["schema.md", "wiki/maintenance/schema-changelog.md", "wiki/log.md"]

        repo = WikiRepository(paths)
        config = repo.read_config()
        schema_config = dict(config.get("schema") or {})
        schema_config.update(
            {
                "current_version": next_version,
                "path": "schema.md",
                "evolution_log": "wiki/maintenance/schema-changelog.md",
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        config["schema"] = schema_config
        repo.write_config(config)

    return {
        "ok": True,
        "dry_run": dry_run,
        "previous_version": current.version,
        "proposed_version": next_version,
        "summary": summary,
        "schema_markdown": schema_markdown,
        "changed_paths": changed_paths,
    }
