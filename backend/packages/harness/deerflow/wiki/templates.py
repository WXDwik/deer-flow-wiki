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
    return _paper_schema_zh() if language.lower().startswith("zh") else _paper_schema_en()


def default_index(title: str) -> str:
    """生成 wiki/index.md 默认内容。"""
    return f"""# {title} Index

## Sources

## Background

## Idea

## System Model

## Algorithm

## Datasets

## Summary

## Concept

## Synthesis

## Maintenance
"""


def _paper_schema_zh() -> str:
    return """---
type: wiki-schema
schema_version: 1
language: zh-CN
status: active
---

# LLM Wiki Schema

这个文件是当前 wiki 的维护契约。LLM 在导入资料、回答查询、归档答案、修复 lint 问题和演化 schema 时，都必须先读取并遵守此文件。

## 目标

本 wiki 是一个由 LLM 维护的科研论文知识库。目标是把原始论文转化为可追溯、可链接、可维护、可直接服务论文写作的 Markdown 知识。

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
- `wiki/sources/`: 来源页。每篇原始论文或资料对应一个摘要页，并指向 raw source 和转换后的 Markdown 缓存。
- `wiki/background/`: 简略背景。记录论文解决的问题背景、已有方法脉络和研究动机。
- `wiki/idea/`: 创新点。记录论文的核心创新、关键做法和相对已有工作的改进。
- `wiki/system_model/`: 简要系统模型。记录任务建模、系统假设、符号设定、输入输出和问题定义。
- `wiki/algorithm/`: 算法。记录模型结构、算法流程、训练/推理步骤、损失函数和复杂度。
- `wiki/datasets/`: 数据集或实验设置。若论文有数据集、仿真配置、评价指标或实验数据来源，写入这里。
- `wiki/summary/`: 研究现状摘要。用一段话概括“这篇文献做了什么创新、完成了什么事”，要求可以直接改写进论文第一章相关工作/研究现状。
- `wiki/concept/`: 复用概念。记录跨论文复用的术语、指标、基础方法和理论。
- `wiki/synthesis/`: 跨来源综合分析和启发。几篇论文形成的共性启发、综合判断、可沉淀的有用回答都写入这里。
- `wiki/maintenance/`: lint 报告、修复记录、schema 演化记录等内部维护文件。

## 页面类型

允许的页面类型：`source`、`background`、`idea`、`system_model`、`algorithm`、`dataset`、`summary`、`concept`、`synthesis`、`maintenance`。

每个 wiki 页面都必须在文件顶部包含且只包含一个 YAML frontmatter。

```yaml
---
type: source | background | idea | system_model | algorithm | dataset | summary | concept | synthesis | maintenance
title: 页面标题
sources: []
tags: []
created_at: ""
updated_at: ""
status: active
---
```

LLM 生成页面正文时不要再次生成 YAML frontmatter，也不要重复生成顶层标题；系统会负责写入 frontmatter 和 `# title`。

## 写入规则

- 不得修改 `raw/` 下的文件。
- 不得修改 `.llm-wiki/` 下的内部状态文件，除非系统代码明确负责该操作。
- `wiki/log.md` 只允许追加，不得重写旧日志。
- 优先更新已有页面，避免创建重复页面。
- 新建页面或对页面做有意义修改后，必须更新 `wiki/index.md`。
- 事实性内容必须能追溯到 source summary 或 raw source。
- 如果来源之间存在冲突，保留冲突并说明差异，不要为了简洁而制造确定性。

## 论文导入重点

导入论文时，LLM 必须按论文写作需求抽取内容，而不是只写普通摘要：

1. `background`: 论文的研究背景、问题动机、已有工作不足。
2. `idea`: 论文的核心创新点、关键贡献和做了什么事。
3. `system_model`: 系统模型、问题定义、变量/符号、输入输出。
4. `algorithm`: 算法流程、模型结构、训练或推理步骤、损失函数、复杂度。
5. `datasets`: 数据集、仿真设置、评价指标、实验数据来源；没有数据集时不要编造。
6. `summary`: 一段可用于论文第一章研究现状的文字，必须说明“某文献提出/设计/验证了什么，解决了什么问题，有什么创新点”。
7. `concept`: 可复用概念、术语和指标。
8. `synthesis`: 跨论文综合启发、共性路线、可沉淀的有用问答。

## 链接与命名规则

- 页面之间使用 Obsidian 风格链接，链接目标必须是目标 Markdown 文件名去掉 `.md` 后的实际文件 stem。
- 新链接应优先使用实际文件 stem 并保留其可读大小写。
- 英文论文标题、模型名、方法名、数据集名、作者名、组织名和缩写必须保留原始或规范大小写，例如 `MIMO`、`UAD`、`Transformer`。
- 中文说明性内容使用中文；技术名词不要为了中文化而硬翻译。
- 文件名使用安全、短而具体的可读名称。

## 摄取工作流

导入新资料时：

1. 读取 `purpose.md`、`schema.md`、`wiki/index.md` 和 `wiki/overview.md`。
2. 将原始文件导入 `raw/sources/`，并读取完整转换后的 Markdown。
3. 为每个原始资料创建 `wiki/sources/` 来源页。
4. 按 `background`、`idea`、`system_model`、`algorithm`、`datasets`、`summary`、`concept`、`synthesis` 分类写入可复用页面。
5. 添加必要 wikilinks。
6. 更新 `wiki/index.md` 和 `wiki/overview.md`。
7. 追加 `wiki/log.md`。
8. 运行 lint。

## 查询、归档与修复

- 回答研究问题时，先搜索并阅读相关 wiki 页面，必要时回查 source summary 或 raw source。
- 有长期复用价值的回答、跨论文启发和综合判断应归档或更新到 `wiki/synthesis/`。
- 修复 lint 时只允许编辑 `wiki/` 下的 Markdown 文件；不得改动 `raw/` 或 `.llm-wiki/`。

## Schema 演化规则

- schema 只沉淀稳定规则，不记录具体论文知识。
- schema 演化必须递增 `schema_version`。
- schema 演化必须记录到 `wiki/maintenance/schema-changelog.md`，并向 `wiki/log.md` 追加摘要。
"""


def _paper_schema_en() -> str:
    return """---
type: wiki-schema
schema_version: 1
language: en
status: active
---

# LLM Wiki Schema

This is the maintenance contract for a paper-oriented research wiki. Convert raw papers into traceable, linked Markdown notes that directly support literature review and research writing.

## Directory Structure

- `purpose.md`: research goals, questions, scope, and current thesis.
- `schema.md`: current maintenance rules.
- `wiki/index.md`: human-readable index.
- `wiki/overview.md`: global overview.
- `wiki/log.md`: append-only operation log.
- `wiki/sources/`: source pages for imported papers and converted Markdown references.
- `wiki/background/`: concise background, motivation, and prior-work context.
- `wiki/idea/`: core innovations, contributions, and what the paper did.
- `wiki/system_model/`: system model, problem formulation, assumptions, variables, inputs, and outputs.
- `wiki/algorithm/`: algorithms, model architecture, training/inference procedure, loss functions, and complexity.
- `wiki/datasets/`: datasets, simulation settings, metrics, and experimental data sources.
- `wiki/summary/`: one-paragraph literature-review summaries suitable for a paper introduction.
- `wiki/concept/`: reusable concepts, terms, methods, and metrics.
- `wiki/synthesis/`: cross-source synthesis, insights, and durable useful answers.
- `wiki/maintenance/`: internal lint, repair, and schema-evolution records.

Allowed page types: `source`, `background`, `idea`, `system_model`, `algorithm`, `dataset`, `summary`, `concept`, `synthesis`, `maintenance`.

Every wiki page must have one YAML frontmatter block. The system writes frontmatter and the top-level title; model-generated bodies must not include either.

## Ingest Rules

- Read the complete converted Markdown.
- Do not assume a fixed paper structure.
- Extract background, innovation, system model, algorithm, datasets/experiments, reusable concepts, and synthesis opportunities.
- Write a `summary` page as one paragraph describing what the paper proposed/designed/validated and why it is innovative.
- Do not invent datasets, results, limitations, or claims that are absent from the source.
- Preserve original capitalization for technical names, acronyms, paper titles, model names, and datasets.
- Use Obsidian wikilinks to actual Markdown filename stems.
- Update `wiki/index.md`, `wiki/overview.md`, and append `wiki/log.md` after changes.

## Query And Repair

Search and read relevant wiki pages before answering. Archive durable cross-paper insights or useful answers into `wiki/synthesis/`. Lint repair may edit only Markdown files under `wiki/`; never edit `raw/` or `.llm-wiki/`.
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
