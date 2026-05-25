"""LLM Wiki 默认模板。

这个模块只负责生成新建 wiki 时需要的初始文件内容。
它不负责写文件，真正写文件由 `scaffold.py` 或 `repository.py` 完成。
"""

from __future__ import annotations


def default_purpose(title: str, language: str = "zh-CN") -> str:
    """生成 purpose.md 默认内容。

    purpose.md 表达“这个 wiki 为什么存在”，后续 ingest 和 query 都应该读取它。
    """
    if language.lower().startswith("zh"):
        return f"""# {title} 研究目标

## 目标

在这里描述这个 LLM Wiki 要服务的研究目标。

## 关键问题

- 问题 1
- 问题 2
- 问题 3

## 研究范围

说明哪些内容属于本 wiki，哪些内容不属于。

## 当前假设

记录当前阶段的研究判断、假设和待验证观点。
"""

    return f"""# {title} Purpose

## Goals

Describe the research goals this LLM Wiki should support.

## Key Questions

- Question 1
- Question 2
- Question 3

## Scope

Describe what belongs in this wiki and what is out of scope.

## Current Thesis

Record current assumptions, hypotheses, and open questions.
"""


def default_schema(language: str = "zh-CN") -> str:
    """生成 schema.md 默认内容。

    schema.md 表达“这个 wiki 应该怎么组织和写作”。
    """
    if language.lower().startswith("zh"):
        return """---
type: wiki-schema
schema_version: 1
language: zh-CN
status: active
---

# LLM Wiki Schema

这个文件是当前 wiki 的维护契约。LLM 在导入资料、回答查询、归档答案、修复 lint 问题和演化 schema 时，都必须先读取并遵守此文件。schema 可以演化，但演化后的版本继续作为后续所有 wiki 更新的依据。

## 目标

本 wiki 是一个由 LLM 维护的科研知识库。目标是把原始研究资料转化为可追溯、可链接、可维护、可持续积累的 Markdown 知识。

## 三层结构

- `raw/sources/`: 原始资料，原则上不可变。
- `raw/assets/`: 从原始资料中提取的图片、图表和附件，原则上不可变。
- `wiki/`: LLM 生成和维护的 Markdown 知识库。
- `.llm-wiki/`: 应用内部配置、索引、队列、审核项。

## 目录结构

- `purpose.md`: 研究目标、关键问题、范围和当前假设。
- `schema.md`: 当前 wiki 的维护规则和工作流。
- `wiki/index.md`: 人类可读索引。新增或重要修改页面后必须更新。
- `wiki/overview.md`: 全局研究概览。
- `wiki/log.md`: 追加式操作日志，不得重写历史记录。
- `wiki/sources/`: 每个原始资料对应的摘要页。
- `wiki/concepts/`: 可复用的概念、术语、方法和理论。
- `wiki/entities/`: 模型、系统、数据集、论文、组织等实体。
- `wiki/comparisons/`: 方法、模型、数据集或路线的对比页。
- `wiki/synthesis/`: 跨资料综合、研究脉络和阶段性结论。
- `wiki/queries/`: 有长期复用价值的问答归档。
- `wiki/deepresearch/`: Deep Research 生成的完整 Markdown 研究报告。
- `wiki/maintenance/`: lint 报告、修复记录、schema 演化记录。

## 文件规则

- 不得修改 `raw/` 下的文件。
- 不得修改 `.llm-wiki/` 下的内部状态文件，除非系统代码明确负责该操作。
- `wiki/log.md` 只允许追加，不得重写旧日志。
- 优先更新已有页面，避免创建重复页面。
- 新建页面或对页面做有意义修改后，必须更新 `wiki/index.md`。
- 事实性内容必须能追溯到 source summary 或 raw source。

## 页面类型

允许的页面类型：`source`、`entity`、`concept`、`query`、`synthesis`、`comparison`、`deepresearch`、`maintenance`。

每个 wiki 页面都必须在文件顶部包含且只包含一个 YAML frontmatter。

```yaml
---
type: source | entity | concept | query | synthesis | comparison | deepresearch | maintenance
title: 页面标题
sources: []
tags: []
generated_from_wiki_at: ""
created_at: ""
updated_at: ""
status: active
---
```

LLM 生成页面正文时不要再次生成 YAML frontmatter，也不要重复生成顶层标题；系统会负责写入 frontmatter 和 `# title`。

## 链接规则

页面之间使用 Obsidian 风格链接，链接目标必须是目标 Markdown 文件名去掉 `.md` 后的实际文件 stem。新链接应优先使用实际文件 stem 并保留其可读大小写；旧的小写 slug 链接仍可被系统解析以兼容已有 Wiki：

```markdown
[[Readable Page Stem]]
```

示例：目标文件 `wiki/entities/Lite Transformer for UAD.md` 应写作 `[[Lite Transformer for UAD]]`。
不要使用不存在的页面标题自造链接；只有真实文件 stem 存在时才写对应 wikilink。

## 语言与显示标题规则

- 本 Wiki 的说明性内容默认使用 schema 中声明的语言；`zh-CN` Wiki 使用中文说明。
- 英文专有名词、论文标题、模型名、方法名、数据集名、作者名、组织名和缩写必须保留原始或规范大小写，例如 `MIMO`、`UAD`、`Transformer`、`Deep Learning`。
- 用户可见的页面标题、frontmatter `title`、一级标题、索引展示名、图谱节点标签和正文中的英文术语必须使用人类可读显示名，不得直接使用全小写 slug。
- 文件名和 wikilink 目标必须使用安全、短而具体的可读文件名；保留英文专有名词、缩写、论文标题和术语的规范大小写。
- 如果只有 slug 而源材料中存在可读标题或术语写法，必须从源材料恢复可读写法；只有无法恢复时才保留 slug。

## 溯源规则

由资料生成的页面必须在 `sources` 字段记录来源。source summary 页面记录 raw source 路径；概念、实体、对比、综合和问答页面记录支持该页面的 source id，并在正文中尽量链接到相关 source summary。`deepresearch` 页面保存完整研究报告，应记录报告基于 wiki 生成的日期（例如 `generated_from_wiki_at` 或 `research_generated_at`），并保留完整正文；可复用的跨资料结论仍应进入 `synthesis`，不要把完整报告和综合结论页混用。

如果来源之间存在冲突，保留冲突并说明差异，不要为了简洁而制造确定性。

## 摄取工作流

导入新资料时：

1. 读取 `purpose.md`、`schema.md`、`wiki/index.md` 和 `wiki/overview.md`。
2. 将原始文件导入 `raw/sources/`。
3. 为每个原始资料创建 `wiki/sources/` 摘要页。
4. 提取关键问题、方法、实体、数据集、指标、结论和局限。
5. 优先更新已有页面；仅为可复用或重要主题创建新页面。
6. 添加必要的 wikilinks。
7. 更新 `wiki/index.md`。
8. 追加 `wiki/log.md`。
9. 运行 light lint。

## 查询与归档工作流

回答研究问题时：

1. 先搜索并阅读相关 wiki 页面。
2. 阅读完整相关页面，不只依赖片段。
3. 跟随重要 wikilinks。
4. 对不确定或有争议的事实回查 source summary 或 raw source。
5. 回答时给出引用。
6. 只有当答案具有长期复用价值时，才归档为 `query`、`synthesis` 或 `comparison` 页面。
7. 如果写回 wiki，必须更新 `wiki/index.md` 并追加 `wiki/log.md`。

## Lint 与修复工作流

light lint 检查 broken links、orphan pages、no outlinks、缺失索引、重复 frontmatter 和必需字段缺失。

deep lint 检查事实缺少来源、页面间矛盾、过时结论、重复概念、重要概念缺页和 source traceability 不清。

修复 lint 时只允许编辑 `wiki/` 下的 Markdown 文件；不得改动 `raw/` 或 `.llm-wiki/`。
修复 `wiki/deepresearch/` 页面时，只处理 frontmatter、wikilink 或索引结构问题，不应改写原始报告事实内容。

## Schema 演化规则

- schema 只沉淀稳定规则，不记录具体论文知识。
- 当同类维护问题反复出现、目录结构需要扩展、页面模板需要稳定化、引用规则需要收紧或用户形成长期偏好时，才演化 schema。
- schema 演化必须递增 `schema_version`。
- schema 演化必须记录到 `wiki/maintenance/schema-changelog.md`，并向 `wiki/log.md` 追加摘要。
- schema 演化后，后续所有 wiki 写入都以新版本为准。

## 命名规则

- 文件名使用安全、短而具体的可读名称，保留正常空格以及英文专有名词、缩写、论文标题和术语的规范大小写。
- wikilink 目标使用实际 Markdown 文件名去掉 `.md` 后的 stem；旧的小写 slug 链接仅作为兼容格式，不作为新生成内容的首选格式。
- frontmatter `title`、页面一级标题、索引显示名和图谱节点标签不得使用小写 slug 代替，必须保留人类可读大小写。
- 文件名短而具体。
- comparison 页面优先使用可读的 `A vs B.md` 风格名称。
- 不为一次性细节创建页面。
"""

    return """---
type: wiki-schema
schema_version: 1
language: en
status: active
---

# LLM Wiki Schema

This file is the maintenance contract for this wiki. The LLM must read and
follow it when ingesting sources, answering queries, archiving answers,
repairing lint issues, and evolving the schema. The schema may evolve, and the
evolved version becomes the rule source for future wiki updates.

## Purpose

This wiki is an LLM-maintained research knowledge base. Its goal is to convert raw research sources into traceable, interlinked, maintainable Markdown knowledge.

## Three Layers

- `raw/sources/`: immutable source documents.
- `raw/assets/`: extracted figures, images, and attachments. Treat as immutable.
- `wiki/`: LLM-generated Markdown knowledge base.
- `.llm-wiki/`: app config, indexes, queues, and review items.

## Directory Structure

- `purpose.md`: research goals, questions, scope, and current thesis.
- `schema.md`: current maintenance rules and workflows.
- `wiki/index.md`: human-readable index. Update after new or meaningfully changed pages.
- `wiki/overview.md`: global research overview.
- `wiki/log.md`: append-only operation log.
- `wiki/sources/`: one summary page per raw source.
- `wiki/concepts/`: reusable concepts, terms, methods, and theories.
- `wiki/entities/`: models, systems, datasets, papers, organizations, and other entities.
- `wiki/comparisons/`: comparisons between methods, models, datasets, or approaches.
- `wiki/synthesis/`: cross-source synthesis and research conclusions.
- `wiki/queries/`: archived reusable Q&A.
- `wiki/deepresearch/`: complete Markdown reports generated by Deep Research.
- `wiki/maintenance/`: lint reports, repair notes, and schema changelog.

## File Rules

- Never edit files under `raw/`.
- Never edit files under `.llm-wiki/` unless system code explicitly owns that operation.
- `wiki/log.md` is append-only.
- Prefer updating existing pages over creating duplicates.
- Update `wiki/index.md` after creating or meaningfully changing pages.
- Factual claims must trace back to source summaries or raw sources.

## Page Types

Allowed page types: `source`, `entity`, `concept`, `query`, `synthesis`, `comparison`, `deepresearch`, `maintenance`.

Every wiki page must include exactly one YAML frontmatter block at the top.

```yaml
---
type: source | entity | concept | query | synthesis | comparison | deepresearch | maintenance
title: Page title
sources: []
tags: []
generated_from_wiki_at: ""
created_at: ""
updated_at: ""
status: active
---
```

When generating page body content, do not include YAML frontmatter or a duplicate top-level title. The system writes frontmatter and `# title`.

## Link Rules

Use Obsidian-style links between pages. The link target must be the actual
filename stem from the target Markdown file without `.md`. New links should
prefer the actual stem and preserve its readable capitalization; old lowercase
slug links are still accepted by the system for compatibility:

```markdown
[[Readable Page Stem]]
```

Example: target file `wiki/entities/Lite Transformer for UAD.md` should be
linked as `[[Lite Transformer for UAD]]`.
Do not invent links from page titles; write a wikilink only when the actual
file stem exists.

## Language And Display Title Rules

- Use the schema language for explanatory prose; a `zh-CN` wiki should use Chinese explanations by default.
- Preserve the original or conventional capitalization of English proper nouns, paper titles, model names, method names, datasets, author names, organizations, and acronyms, such as `MIMO`, `UAD`, `Transformer`, and `Deep Learning`.
- User-visible page titles, frontmatter `title`, H1 headings, index display names, graph node labels, and English terms in prose must use reader-facing display names, not lowercase slugs.
- Filenames and wikilink targets must use safe, short, specific readable filenames; preserve conventional capitalization for English proper nouns, acronyms, paper titles, and technical terms.
- If only a slug is available but the source material contains a readable title or term spelling, recover the readable form from the source. Keep the slug only when no readable form can be recovered.

## Source Traceability

Pages generated from source material must record their sources. Source summary
pages record raw source paths. Concept, entity, comparison, synthesis, and query
pages record supporting source IDs and should link to related source summary
pages when useful.
Deep Research report pages must live under `wiki/deepresearch/`, preserve the
complete report body, and record the date the report was generated from the
wiki, for example `generated_from_wiki_at` or `research_generated_at`.
Use `synthesis` for reusable cross-source conclusions; do not mix complete
report archives with synthesis pages.

If sources conflict, preserve and explain the disagreement instead of inventing certainty.

## Ingest Workflow

When adding sources:

1. Read `purpose.md`, `schema.md`, `wiki/index.md`, and `wiki/overview.md`.
2. Import raw files into `raw/sources/`.
3. Create one source summary under `wiki/sources/` for each raw source.
4. Extract key questions, methods, entities, datasets, metrics, claims, and limitations.
5. Prefer updating existing pages; create new pages only for reusable or important topics.
6. Add useful wikilinks.
7. Update `wiki/index.md`.
8. Append to `wiki/log.md`.
9. Run light lint.

## Query And Archive Workflow

When answering research questions:

1. Search and read relevant wiki pages first.
2. Read full relevant pages, not only snippets.
3. Follow important wikilinks.
4. Verify uncertain or disputed claims against source summaries or raw sources.
5. Answer with citations.
6. Archive only reusable answers as `query`, `synthesis`, or `comparison` pages.
7. If the wiki changes, update `wiki/index.md` and append to `wiki/log.md`.

## Lint And Repair Workflow

Light lint checks broken links, orphan pages, no outlinks, missing index entries, duplicate frontmatter, and missing required fields.

Deep lint checks unsupported factual claims, contradictions, stale claims, duplicate concepts, missing important concept pages, and unclear source traceability.

Lint repair may edit only Markdown files under `wiki/`; never edit `raw/` or `.llm-wiki/`.
For `wiki/deepresearch/` pages, lint repair may fix frontmatter, wikilinks, or
index structure, but should not rewrite the report's factual body content.

## Schema Evolution Rules

- The schema stores stable maintenance rules, not paper-specific knowledge.
- Evolve the schema only when repeated maintenance problems appear, directory structure needs to expand, page templates need to stabilize, citation rules need tightening, or the user has a durable preference.
- Schema evolution must increment `schema_version`.
- Schema evolution must be recorded in `wiki/maintenance/schema-changelog.md` and summarized in `wiki/log.md`.
- After evolution, all future wiki writes follow the new schema version.

## Naming Rules

- Use safe, short, specific readable filenames that preserve normal spaces and conventional capitalization for English proper nouns, acronyms, paper titles, and technical terms.
- Wikilink targets should use the actual Markdown filename stem without `.md`; old lowercase slug links are compatibility-only and should not be preferred in newly generated content.
- Do not use lowercase slugs as frontmatter `title`, page H1 headings, index display names, or graph node labels; preserve readable capitalization there.
- Keep filenames short but specific.
- Prefer readable `A vs B.md` style names for comparison pages.
- Do not create pages for one-off details.
"""


def default_index(title: str) -> str:
    """生成 wiki/index.md 默认内容。"""
    return f"""# {title} Index

## Sources

## Entities

## Concepts

## Queries

## Synthesis

## Comparisons

## Deep Research

## Maintenance
"""


def default_log(created_at: str) -> str:
    """生成 wiki/log.md 默认内容。"""
    return f"""# Operation Log

- {created_at}: Wiki created.
"""


def default_overview(title: str) -> str:
    """生成 wiki/overview.md 默认内容。"""
    return f"""# {title} Overview

当前还没有导入资料。导入 PDF、Markdown 或其他文档后，这里会生成全局概要。
"""


def default_config(wiki_title: str, language: str, created_at: str) -> dict:
    """生成 .llm-wiki/config.json 默认内容。"""
    return {
        "title": wiki_title,
        "language": language,
        "created_at": created_at,
        "version": 1,
        "features": {
            "two_step_ingest": True,
            "wikilinks": True,
            "frontmatter": True,
            "obsidian_compatible": True,
            "vector_search": False,
        },
        "schema": {
            "current_version": 1,
            "path": "schema.md",
            "evolution_log": "wiki/maintenance/schema-changelog.md",
        },
    }


def default_index_json() -> dict:
    """生成 .llm-wiki/index.json 默认内容。"""
    return {"sources": [], "pages": []}


def default_queue_json() -> dict:
    """生成 .llm-wiki/queue.json 默认内容。"""
    return {"jobs": []}


def default_reviews_json() -> dict:
    """生成 .llm-wiki/reviews.json 默认内容。"""
    return {"items": []}
