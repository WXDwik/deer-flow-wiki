# LLM Wiki Phase 1 Summary

Date: 2026-05-14

## Scope

This phase adds a minimal LLM Wiki capability to DeerFlow. The goal is to prove the end-to-end tool path first, then expand ingestion, generation, search, and maintenance features in later phases.

## Completed

### Wiki domain package

Added the `deerflow.wiki` package under:

```text
backend/packages/harness/deerflow/wiki/
```

Current modules:

- `paths.py`: resolves wiki names/paths and creates the standard directory layout.
- `templates.py`: provides default Markdown and JSON templates.
- `scaffold.py`: creates a new wiki database.
- `repository.py`: reads/writes `.llm-wiki/*.json` metadata files.
- `ingest.py`: imports local source files into a wiki.
- `query.py`: performs simple keyword search over generated wiki Markdown pages.
- `lint.py`: checks empty pages, missing frontmatter, and dead wikilinks.
- `markdown.py`: builds frontmatter, wikilinks, and source summary pages.
- `service.py`: exposes the public service layer used by tools.

### Built-in wiki tools

Added built-in tools in:

```text
backend/packages/harness/deerflow/tools/builtins/wiki_tools.py
```

Exposed tools:

- `wiki_create`
- `wiki_add_source`
- `wiki_search`
- `wiki_lint`

These are registered through:

```text
backend/packages/harness/deerflow/tools/builtins/__init__.py
backend/packages/harness/deerflow/tools/tools.py
```

The tools now appear in the default lead agent tool set.

## Current Behavior

### `wiki_create`

Creates a wiki under:

```text
backend/.deer-flow/wiki/<wiki_name>/
```

Example:

```text
backend/.deer-flow/wiki/docker-smoke-wiki/
```

Generated structure includes:

```text
purpose.md
schema.md
raw/sources/
raw/assets/
wiki/index.md
wiki/log.md
wiki/overview.md
wiki/entities/
wiki/concepts/
wiki/sources/
wiki/queries/
wiki/synthesis/
wiki/comparisons/
.obsidian/
.llm-wiki/config.json
.llm-wiki/index.json
.llm-wiki/queue.json
.llm-wiki/reviews.json
.llm-wiki/chats/
```

### `wiki_add_source`

Copies a local source file into:

```text
backend/.deer-flow/wiki/<wiki_name>/raw/sources/
```

Then generates a fallback source summary page under:

```text
backend/.deer-flow/wiki/<wiki_name>/wiki/sources/
```

It also updates:

```text
backend/.deer-flow/wiki/<wiki_name>/.llm-wiki/index.json
```

Current ingestion does not yet parse PDF or fully summarize source content. It only copies the file, records metadata, computes SHA256, and creates a minimal Markdown page.

### `wiki_search`

Searches Markdown files under:

```text
backend/.deer-flow/wiki/<wiki_name>/wiki/
```

Current search is simple keyword scoring. It does not yet use vector search or embeddings.

### `wiki_lint`

Checks:

- Empty wiki pages.
- Missing YAML frontmatter on non-system pages.
- Dead `[[wikilink]]` references.

## Docker Validation Notes

Docker development mode was used.

The compose setup mounts:

```text
Windows: E:\AI_agent\deer-flow\backend
Container: /app/backend
```

Therefore wiki files created in Docker are visible on Windows.

Example mapping:

```text
Container:
/app/backend/.deer-flow/wiki/docker-smoke-wiki/

Windows:
E:\AI_agent\deer-flow\backend\.deer-flow\wiki\docker-smoke-wiki\
```

Agent-generated conversation artifacts are different from wiki files. Artifacts are per user and per thread:

```text
backend/.deer-flow/users/<user_id>/threads/<thread_id>/user-data/outputs/
```

Wiki files are currently global:

```text
backend/.deer-flow/wiki/<wiki_name>/
```

## Model Compatibility Finding

`mimo-v2-flash` failed during tool calls with:

```text
The reasoning_content in the thinking mode must be passed back to the API.
```

The same error occurred with DeerFlow's original `present_files` flow, so the issue is not specific to LLM Wiki.

Root cause:

- MiMo thinking mode returns provider-specific `reasoning_content`.
- Follow-up tool-call turns must send that field back.
- The current `langchain_openai:ChatOpenAI` adapter does not preserve MiMo's required reasoning field across tool-call turns.

Workaround used:

- Added an Alibaba DashScope/Qwen model in `config.yaml`.
- Tool calling worked with the Qwen model.

Future option:

- Implement a MiMo-specific provider adapter that preserves `reasoning_content` in `AIMessage.additional_kwargs` and serializes it back into future API requests.

## Verified Prompts

Create wiki:

```text
请完整测试 LLM Wiki 工具链：

1. 调用 wiki_create 创建一个名为 docker-smoke-wiki 的 LLM Wiki，标题为 Docker Smoke Wiki，语言 zh-CN。
2. 调用 wiki_lint 检查 docker-smoke-wiki。
3. 调用 wiki_search 在 docker-smoke-wiki 中搜索 Docker，limit=5。
4. 最后把每个工具返回的 JSON 结果原样展示给我。

必须调用 wiki_create、wiki_lint、wiki_search 工具，不要只用自然语言回答。
```

Import source:

```text
请把这个本地文件导入到 docker-smoke-wiki 这个 LLM Wiki 中：

/app/backend/wiki_test/docker-smoke-source.md

请调用 wiki_add_source 工具导入，然后调用 wiki_search 搜索 docker-smoke-wiki，limit=5，最后调用 wiki_lint 检查。请把每个工具返回的 JSON 原样展示给我。
```

## Next Work

Recommended next expansion order:

1. Add real source parsing for Markdown and plain text.
2. Add PDF parsing and source chunk extraction.
3. Add LLM-based source summary generation.
4. Generate entity, concept, query, synthesis, and comparison pages.
5. Add source deduplication based on SHA256.
6. Improve `wiki_search` with embeddings/vector search.
7. Make agent answers consult wiki before responding when relevant.
8. Decide whether wiki storage should remain global or become user-scoped.
9. Add tests for `wiki_create`, `wiki_add_source`, `wiki_search`, and `wiki_lint`.

## Open Design Questions

- Should wiki data be global, user-scoped, or thread-scoped?
- Should imported raw sources be immutable after ingestion?
- Should generated wiki pages be directly editable by users?
- Should search use only generated wiki pages, or both generated pages and raw sources?
- Should the wiki be exposed in the frontend as a first-class workspace view?
