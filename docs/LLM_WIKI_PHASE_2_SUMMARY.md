# LLM Wiki Phase 2 Summary

Date: 2026-05-16

## 阶段定位

阶段一证明了 LLM Wiki 的最小闭环：创建 wiki、导入文件、生成基础 source 页面、关键词搜索、结构检查。

阶段二把这个原型推进成了一个可被 Agent 实际使用的本地研究知识库系统。核心变化是：

- 导入资料时不再只复制文件，而是会读取 PDF / DOCX / PPT / Excel / Markdown 等资料并转换成 Markdown。
- 导入后会调用配置的聊天模型，把资料内容整理成 wiki 页面。
- 搜索从简单关键词升级为优先使用 QMD 的本地 Markdown 检索索引，并保留关键词兜底。
- 增加了 wiki 维护能力：状态检查、批量同步、轻量 / 深度 lint、LLM 修复。
- 增加了基于 wiki 的研究报告准备能力：报告规划、上下文包生成、配合 deep-research skill 分工写报告。
- 增加了回答回写机制：Agent 用 wiki 回答问题后，可以判断这次问答是否值得沉淀回 wiki。
- 修复了 Agent 上传路径和后端真实路径不一致的问题。

## 新增工具能力

阶段二后，LLM Wiki 对 Agent 暴露的工具已经扩展为：

```text
wiki_create
wiki_add_source
wiki_search
wiki_plan_report
wiki_get_report_context
wiki_source_status
wiki_sync_sources
wiki_archive_answer
wiki_lint
wiki_repair_lint
```

这些工具集中定义在：

```text
backend/packages/harness/deerflow/tools/builtins/wiki_tools.py
```

并通过以下文件注册到默认工具集中：

```text
backend/packages/harness/deerflow/tools/builtins/__init__.py
backend/packages/harness/deerflow/tools/tools.py
```

## 1. 文件读取与导入能力

### 完成的功能

`wiki_add_source` 现在已经不是简单复制文件。它同时处理单文件和多文件：传 1 个文件就是单文件导入，传多个文件就是整批资料共同导入。新的导入流程是：

1. 解析 Agent 传入的路径。
2. 把一个或多个原始文件保存到 wiki 的 `raw/sources/`。
3. 计算每个文件的 SHA256，用于追踪和去重判断。
4. 把整批源文件先转换或缓存成 Markdown，保存到 `raw/sources/.cache/`。
5. 调用配置的 LLM 一次性阅读这一批 Markdown 内容。
6. 生成每个 source 的 summary 页面，以及跨 source 的 entity / concept / query / synthesis / comparison 等分类页面。
7. 把 source 和生成页面写入 `.llm-wiki/index.json`。
8. 自动执行一次 light lint，把检查结果写入返回 metadata。

核心实现文件：

```text
backend/packages/harness/deerflow/wiki/ingest.py
backend/packages/harness/deerflow/utils/file_conversion.py
backend/packages/harness/deerflow/wiki/service.py
```

### 支持的文件类型

文件转换工具支持：

```text
.pdf
.ppt
.pptx
.xls
.xlsx
.doc
.docx
```

Markdown 文件 `.md` 会直接复制到 `.cache/`，不走外部转换。

### PDF 读取技术

PDF 转 Markdown 由 `file_conversion.py` 实现，采用两级策略：

1. 优先使用 `pymupdf4llm`。
   - 对文字型 PDF 更快，标题结构识别更好。
   - 可以提取 PDF 中的图片，并把图片保存到 `raw/assets/<source_id>/`。

2. 如果 `pymupdf4llm` 不可用，或输出内容过短，则回退到 `MarkItDown`。
   - 代码会用“每页字符数”判断 PDF 是否疑似扫描件 / 图片型 PDF。
   - 阈值是每页少于 50 字符时认为输出过稀疏。

大文件超过 1 MB 时，转换会放到 `asyncio.to_thread()` 中执行，避免阻塞事件循环。

### LLM 生成 wiki 页面

导入时会调用：

```python
create_chat_model(thinking_enabled=False)
```

模型需要返回 JSON：

```json
{
  "source_summary": "...",
  "tags": ["..."],
  "pages": [
    {
      "type": "entity|concept|query|synthesis|comparison",
      "title": "...",
      "tags": ["..."],
      "content": "..."
    }
  ]
}
```

系统会根据页面类型写入：

```text
wiki/entities/
wiki/concepts/
wiki/queries/
wiki/synthesis/
wiki/comparisons/
```

如果模型没有返回合法 JSON，会走 fallback：仍然生成 source summary 页面，并把转换出的 Markdown 前 1200 字左右作为预览保存下来，不会让整个导入失败。

## 2. 上传路径解析修复

### 完成的功能

阶段二修复了一个实际使用中很关键的问题：Agent 看到的路径和 gateway 后端进程看到的路径不一致。

支持的虚拟路径包括：

```text
/mnt/user-data/uploads/...
/mnt/user-data/workspace/...
/mnt/user-data/outputs/...
```

同时支持只传 uploads 目录下的文件名，例如：

```text
'paper with spaces.pdf'
```

实现位置：

```text
backend/packages/harness/deerflow/tools/builtins/wiki_tools.py
```

关键函数：

```python
_resolve_source_file(...)
_clean_source_path(...)
```

### 技术要点

- 从 `runtime.state["thread_data"]` 读取真实的 `uploads_path`、`workspace_path`、`outputs_path`。
- 把 `/mnt/user-data/...` 映射成后端真实本地路径。
- 自动去掉模型可能传入的外层单引号或双引号。
- 对映射后的路径执行 `relative_to(root)` 检查，防止 `../` 路径穿越。
- 如果 `thread_data` 不可用，则回退到 `get_paths().resolve_virtual_path(...)`。

这使得长文件名、空格、中文、特殊字符文件名的上传导入都更稳。

## 3. Wiki 搜索能力

### 完成的功能

`wiki_search` 现在优先使用 QMD，而不是只做简单关键词匹配。

实现位置：

```text
backend/packages/harness/deerflow/wiki/query.py
```

搜索流程：

1. 如果 `DEER_FLOW_WIKI_QMD_ENABLED` 没有关闭，则尝试使用 QMD。
2. 自动为当前 wiki 的 `wiki/**/*.md` 建立或复用 QMD collection。
3. 执行 QMD search / query / vsearch。
4. 解析 QMD JSON 输出，返回 `path`、`title`、`score`、`snippet`。
5. 如果 QMD 不可用、超时、失败或输出不可解析，则回退到原始关键词搜索。

### QMD 技术集成

Dockerfile 中安装了 QMD：

```text
npm install -g @tobilu/qmd
```

并在 Docker compose 中增加了 QMD 缓存卷：

```text
gateway-qmd-cache:/root/.cache/qmd
```

相关配置环境变量：

```text
DEER_FLOW_WIKI_QMD_ENABLED
DEER_FLOW_QMD_COMMAND
DEER_FLOW_WIKI_QMD_TIMEOUT_SECONDS
DEER_FLOW_WIKI_QMD_MODE
DEER_FLOW_WIKI_QMD_UPDATE
```

默认情况下，QMD 启用；如果 QMD 不可用，系统仍然可用，只是退回关键词搜索。

## 4. 原始资料状态检查与批量同步

### 完成的功能

新增：

```text
wiki_source_status
wiki_sync_sources
```

这解决了一个重要使用场景：用户可能手动把大量 PDF / Markdown 文件放到 `raw/sources/`，然后希望 wiki 自动识别哪些还没处理。

同一批资料现在会作为一个整体导入：系统会先把所有文件转换为 Markdown，再让模型同时阅读这些未处理内容。这样模型可以生成更好的 synthesis / comparison 页面，而不是把多篇论文割裂成一篇一篇的独立导入结果。

### `wiki_source_status`

它会对比：

```text
raw/sources/
.llm-wiki/index.json
```

并给每个文件标记状态：

```text
parsed
pending
stale
parsed_duplicate
missing_raw
```

判断依据包括：

- 文件路径是否在 index 中。
- SHA256 是否匹配。
- 是否有内容相同但路径不同的已解析文件。
- index 中记录的 raw source 是否已经丢失。

### `wiki_sync_sources`

它会批量处理 `pending` 文件：

- 只导入未解析文件。
- 同一批次中 SHA256 重复的文件会跳过。
- 支持 `limit` 限制本次处理数量。
- 所有本批唯一 pending 文件会复用标准批量 `add_sources` 导入流程。
- 模型会在同一次调用中综合分析整批 pending 文件。
- 返回 processed / skipped / failed 明细。

实现位置：

```text
backend/packages/harness/deerflow/wiki/service.py
```

## 5. Wiki 维护：Light Lint / Deep Lint

### 完成的功能

阶段二把 lint 分成了两类：

```text
light lint  = 本地结构检查
deep lint   = light lint + LLM 语义检查
```

实现位置：

```text
backend/packages/harness/deerflow/wiki/lint.py
```

### Light Lint

本地结构检查包括：

- broken-link：`[[wikilink]]` 指向不存在的页面。
- orphan：没有其他页面链接到当前页面。
- no-outlinks：当前页面没有链接到其他 wiki 页面。

`wiki/index.md` 和 `wiki/log.md` 等系统页面会按规则排除，避免产生无意义告警。

### Deep Lint

Deep lint 会在 light lint 基础上调用 LLM 做语义检查，支持发现：

- contradiction：概念或结论冲突。
- stale：内容可能过期。
- missing-page：重要概念缺少独立页面。
- suggestion：结构或内容改进建议。

LLM 输出采用结构化 lint block，再由代码解析为统一的 `LintIssue`。

### 自动模式

`wiki_lint` 支持：

```text
mode = light | deep | auto
```

`auto` 会根据触发原因决定是否升级为 deep lint。

轻量触发：

```text
add_source
page_edit
page_move
page_rename
page_delete
source_cleanup
manual
```

深度触发：

```text
batch_ingest
deep_manual
semantic_manual
important_source_delete
important_source_replace
core_page_update
chat_quality_drop
query_quality_drop
```

## 6. LLM 自动修复 Wiki

### 完成的功能

新增：

```text
wiki_repair_lint
```

它会读取 lint issue，构造维修指令，再让 LLM 给出精确的 Markdown 修改计划。

实现位置：

```text
backend/packages/harness/deerflow/wiki/repair.py
```

### 技术实现

维修流程：

1. 执行或接收 lint issues。
2. 把 issue 转成 repair instructions。
3. 读取相关 wiki 页面、`purpose.md`、`schema.md`、`overview.md` 等上下文。
4. 调用 LLM 返回 JSON 格式修改计划。
5. 校验修改路径和操作类型。
6. dry run 时只返回变更预览。
7. 非 dry run 时写入 Markdown，并追加 `wiki/log.md`。
8. 写入后再次执行 light lint 作为验证。

支持的编辑操作：

```text
replace
append
rewrite
```

### 安全边界

自动修复只能编辑：

```text
wiki/**/*.md
```

禁止编辑：

```text
raw/
raw/sources/
raw/assets/
.llm-wiki/
源文件本体
```

并且 `replace` 必须提供精确存在的 old text，否则会失败，避免模型误改。

## 7. 问答回写与知识沉淀

### 完成的功能

新增：

```text
wiki_archive_answer
```

这个工具用于“Agent 已经用 wiki_search 回答了用户问题之后”，判断这次问答是否值得写回 wiki。

实现位置：

```text
backend/packages/harness/deerflow/wiki/archive.py
```

### 回写决策

模型会判断：

```text
none
create_page
update_existing
create_and_update
```

含义：

- `none`：临时回答、重复内容、证据不足，不写回。
- `create_page`：创建一个新的 query / synthesis / comparison 等页面。
- `update_existing`：只补充已有 concept / entity / synthesis 页面。
- `create_and_update`：既创建新页面，也更新相关旧页面。

### 写回内容

如果创建新页面，系统会：

- 把聊天回答压缩成可独立阅读的 wiki 页面。
- 写入 `wiki/queries/`、`wiki/synthesis/`、`wiki/comparisons/` 等目录。
- 添加 YAML frontmatter。
- 更新 `.llm-wiki/index.json`。
- 更新 `wiki/index.md`。
- 追加 `wiki/log.md`。

如果更新已有页面，系统会让模型生成 append / replace / rewrite 级别的局部修改，并经过路径校验后应用。

## 8. 基于 Wiki 生成研究报告

### 完成的功能

新增：

```text
wiki_plan_report
wiki_get_report_context
```

这两个工具不是直接生成最终报告，而是给 DeerFlow 的 deep-research 流程提供本地知识库规划和证据包。

实现位置：

```text
backend/packages/harness/deerflow/wiki/report.py
skills/public/deep-research/SKILL.md
backend/packages/harness/deerflow/subagents/builtins/general_purpose.py
```

### `wiki_plan_report`

它会返回：

- wiki 标题、语言、根路径。
- purpose / schema / overview 摘要。
- source 数量。
- page 数量。
- page type 分布。
- tags 列表。
- sample pages。
- 根据报告目标生成的建议检索 query。
- 候选 wiki 页面。
- 推荐的 agent 工作流。

它的作用是让 lead agent 在正式写报告前先知道：

- 这个 wiki 里有什么。
- 哪些页面可能和报告目标有关。
- 报告应该从哪些方向拆分。

### `wiki_get_report_context`

它会为某个报告小节或子任务构造一个受预算限制的上下文包：

- 根据 research task 和 queries 搜索 wiki。
- 读取候选页面内容。
- 控制每页最大字符数。
- 控制总字符预算。
- 返回页面标题、路径、类型、tags、sources、snippet、content。
- 如果没有足够内容，会标记 `insufficient_context`。

这使得 lead agent 可以把不同小节的 wiki 证据包分发给 subagents，而不是让每个 subagent 盲目搜索整个 wiki。

### deep-research skill 集成

`skills/public/deep-research/SKILL.md` 已经加入 LLM Wiki 工作流：

1. 判断任务是否是 wiki-grounded research。
2. 使用 `wiki_plan_report` 检查 wiki 内容。
3. 设计报告结构。
4. 对每个小节使用 `wiki_get_report_context` 生成上下文包。
5. 把上下文包交给 subagent。
6. lead agent 最后综合，而不是简单拼接。

`general-purpose` subagent 的提示词也补充了约束：报告小节任务应该优先使用父 Agent 传入的 context pack，不要擅自扩大成完整报告。

## 9. 数据模型与存储

### 当前存储方式

主存储仍然是文件系统：

```text
.llm-wiki/config.json
.llm-wiki/index.json
.llm-wiki/queue.json
.llm-wiki/reviews.json
```

实现位置：

```text
backend/packages/harness/deerflow/wiki/repository.py
```

`index.json` 记录：

- sources
- generated pages

### 数据模型

阶段二扩展了领域模型：

```text
RawSource
WikiPage
IngestJob
ReviewItem
WikiCitation
WikiContextPage
AnswerDraft
ArchiveDecision
ArchiveDraft
WikiPageChange
QueryAnswer
```

定义位置：

```text
backend/packages/harness/deerflow/wiki/models.py
```

其中问答回写、页面变更、报告上下文都已经有明确 dataclass 表达。

### SQL / Memory 预留

新增了持久化包结构：

```text
backend/packages/harness/deerflow/persistence/wiki/
```

当前 `model.py`、`sql.py`、`memory.py` 还是占位实现，说明阶段二主要完成的是文件型存储和业务闭环，SQL 化还没有真正落地。

## 10. 测试覆盖

阶段二新增或扩展了以下测试：

```text
backend/tests/test_wiki_tools.py
backend/tests/test_wiki_ingest.py
backend/tests/test_wiki_query.py
backend/tests/test_wiki_lint.py
backend/tests/test_wiki_repair.py
backend/tests/test_wiki_report.py
backend/tests/test_wiki_archive.py
```

覆盖内容包括：

- 上传虚拟路径解析。
- 带空格的长文件名。
- 只传 uploads 文件名。
- PDF / Markdown 导入、缓存 Markdown、生成页面。
- 多文件批量导入只调用一次模型，并生成跨文件 comparison 页面。
- LLM 输出非法时 fallback。
- 导入后自动 light lint。
- raw/sources 状态识别。
- 批量同步 pending sources，并把唯一 pending 文件作为一批分析。
- QMD 搜索、collection 复用、QMD 不可用时关键词兜底。
- 语义 lint 输出解析。
- light / deep / auto lint 模式。
- repair dry run、真实写入、路径越界拒绝、replace old text 不存在时拒绝。
- 报告规划和上下文包。
- 问答回写决策、新建页面、更新旧页面、更新 index 和 log。

可以用以下命令集中验证：

```powershell
cd backend
uv run pytest tests/test_wiki_tools.py tests/test_wiki_ingest.py tests/test_wiki_query.py tests/test_wiki_lint.py tests/test_wiki_repair.py tests/test_wiki_report.py tests/test_wiki_archive.py
```

## 当前能力总览

阶段二完成后，LLM Wiki 已经具备以下闭环：

```text
创建 wiki
  ↓
导入 PDF / 文档 / Markdown
  ↓
转换为 Markdown 缓存
  ↓
LLM 生成 source / concept / entity / query / synthesis / comparison 页面
  ↓
QMD / 关键词搜索 wiki
  ↓
Agent 使用 wiki 回答问题
  ↓
高价值回答可回写到 wiki
  ↓
lint 检查结构和语义质量
  ↓
LLM 修复 wiki 页面
  ↓
基于 wiki 规划并生成研究报告
```

这已经从“文件夹模板 + 简单搜索”升级成“Agent 可维护的本地研究知识库”。

## 当前限制

阶段二虽然完成了主链路，但仍有一些明显限制：

- `.llm-wiki/index.json` 仍是文件型索引，不是数据库事务存储。
- `persistence/wiki/sql.py` 和 `memory.py` 还是占位，SQL repository 未实现。
- LLM 生成页面依赖模型返回 JSON，虽然有 fallback，但生成质量仍取决于模型。
- PDF 对扫描件的处理依赖 MarkItDown 回退能力，没有单独 OCR 流程。
- QMD 是本地 CLI 集成，部署环境必须安装或使用 Dockerfile 中的 npm 安装。
- wiki 页面生成、问答回写、repair 都是 Markdown 文件级操作，还没有前端可视化审核界面。
- `prompts/ingester.md`、`planner.md`、`writer.md`、`linter.md` 目录已经存在，但当前主要 prompt 仍写在 Python 代码里，prompt 文件化还没完成。

## 建议的阶段三方向

1. 把内联 prompt 迁移到 `wiki/prompts/*.md`，便于迭代和审查。
2. 实现 SQL repository，解决并发写入、历史版本、审计和更复杂查询。
3. 增加 OCR 支持，提升扫描 PDF 的可读性。
4. 增加前端 Wiki 管理界面：source 状态、lint issue、repair diff、archive review。
5. 增加人工审核队列，让 LLM 生成页面和 repair 修改先进入 review。
6. 对 QMD collection 增加更明确的生命周期管理和重建机制。
7. 增加 wiki 级别权限 / user scope / thread scope 的设计。
8. 增加 source replacement / deletion / stale page cleanup 的完整维护流程。
9. 让报告生成结果可自动引用 wiki 页面路径，并支持报告写回 wiki。
