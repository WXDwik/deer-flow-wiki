"""LLM Wiki 本地路径管理模块。

这个模块只负责“文件系统路径和目录结构”相关的事情：
- 解析用户传入的 wiki 名称或 wiki 路径
- 创建科研型 LLM Wiki 的标准目录结构
- 生成安全的文件名和目标路径
- 检查目录结构是否已经存在
- 防止传入路径逃出当前 wiki 根目录

注意：
这里不处理 PDF，不调用 LLM，也不读写 wiki 元数据。
后续 PDF 解析、Markdown 生成、schema 更新都应该放到其他模块里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


# 默认 wiki 根目录。
# 如果用户只输入一个 wiki 名称，比如 "rag-research"，
# 系统会默认创建到 `.deer-flow/wiki/rag-research/`。
DEFAULT_WIKI_BASE_DIR = Path(".deer-flow") / "wiki"

# wiki 根目录下的两个顶层说明文件。
PURPOSE_FILE_NAME = "purpose.md"  # 记录研究目标、关键问题、研究范围。
SCHEMA_FILE_NAME = "schema.md"  # 记录 wiki 写作规则、页面类型、链接规则。

# raw/ 用来保存原始资料，原则上不可变。
RAW_DIR_NAME = "raw"
RAW_SOURCES_DIR_NAME = "sources"  # PDF、DOCX、网页剪藏等原始文件。
RAW_ASSETS_DIR_NAME = "assets"  # 图片、截图、PDF 中提取出的图表等。

# wiki/ 用来保存 LLM 生成和维护的 Markdown 知识库。
WIKI_DIR_NAME = "wiki"
WIKI_INDEX_FILE_NAME = "index.md"  # wiki 导航目录。
WIKI_LOG_FILE_NAME = "log.md"  # 操作历史。
WIKI_OVERVIEW_FILE_NAME = "overview.md"  # 全局概要。

# wiki/ 下的论文导向页面分类目录。
WIKI_BACKGROUND_DIR_NAME = "background"  # 简略背景、问题背景、已有路线。
WIKI_IDEA_DIR_NAME = "idea"  # 创新点、核心做法、论文贡献。
WIKI_SYSTEM_MODEL_DIR_NAME = "system_model"  # 简要系统模型、任务建模、符号设定。
WIKI_ALGORITHM_DIR_NAME = "algorithm"  # 算法流程、模型结构、训练/推理步骤。
WIKI_DATASETS_DIR_NAME = "datasets"  # 数据集、仿真设置、实验数据来源。
WIKI_SUMMARY_DIR_NAME = "summary"  # 可写入论文第一章研究现状的一段式总结。
WIKI_CONCEPT_DIR_NAME = "concept"  # 复用概念、术语、指标、基础方法。
WIKI_SOURCES_DIR_NAME = "sources"  # 每个原始资料对应的摘要页。
WIKI_SYNTHESIS_DIR_NAME = "synthesis"  # 跨来源综合分析、启发和可沉淀答案。
WIKI_MAINTENANCE_DIR_NAME = "maintenance"  # lint 报告、schema 演化记录等维护文件。

# Obsidian 配置目录，让生成的 wiki 可以直接作为 Obsidian vault 使用。
OBSIDIAN_DIR_NAME = ".obsidian"

# .llm-wiki/ 是应用内部状态目录，不建议用户手动编辑。
LLM_WIKI_DIR_NAME = ".llm-wiki"
LLM_WIKI_CONFIG_FILE_NAME = "config.json"  # 当前 wiki 的配置。
LLM_WIKI_INDEX_FILE_NAME = "index.json"  # 文件索引、页面索引、source-page 映射等。
LLM_WIKI_QUEUE_FILE_NAME = "queue.json"  # 后续 ingest 队列可以放这里。
LLM_WIKI_REVIEWS_FILE_NAME = "reviews.json"  # 需要人工审核的事项。
LLM_WIKI_CHATS_DIR_NAME = "chats"  # 和这个 wiki 相关的聊天记录。


@dataclass(frozen=True)
class WikiPaths:
    """一个 LLM Wiki 数据库的所有标准路径。

    frozen=True 表示这个对象创建后不能被修改。
    这样可以避免后续代码不小心改掉路径，导致文件写到错误位置。
    """

    # wiki 根目录，比如 `.deer-flow/wiki/rag-research/`。
    root: Path

    # 顶层说明文件。
    purpose_file: Path
    schema_file: Path

    # 原始资料目录。
    raw_dir: Path
    raw_sources_dir: Path
    raw_assets_dir: Path

    # LLM 生成的 wiki 内容目录。
    wiki_dir: Path
    wiki_index_file: Path
    wiki_log_file: Path
    wiki_overview_file: Path

    # wiki 页面分类目录。
    wiki_background_dir: Path
    wiki_idea_dir: Path
    wiki_system_model_dir: Path
    wiki_algorithm_dir: Path
    wiki_datasets_dir: Path
    wiki_summary_dir: Path
    wiki_concept_dir: Path
    wiki_sources_dir: Path
    wiki_synthesis_dir: Path
    wiki_maintenance_dir: Path
    # Obsidian 配置目录。
    obsidian_dir: Path

    # LLM Wiki 应用内部状态目录。
    llm_wiki_dir: Path
    llm_wiki_config_file: Path
    llm_wiki_index_file: Path
    llm_wiki_queue_file: Path
    llm_wiki_reviews_file: Path
    llm_wiki_chats_dir: Path


def slugify_name(name: str) -> str:
    """把用户输入的名称转换成适合做文件夹名/文件名的安全字符串。

    示例：
    - "RAG Research" -> "rag-research"
    - "我的论文库" -> ValueError

    这里先只支持英文、数字、点、下划线、短横线。
    如果后续你想支持中文文件名，可以单独调整这个函数。
    """
    value = name.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value)
    value = value.strip("-._")
    if not value:
        raise ValueError("Wiki name cannot be empty.")
    return value


def display_slugify_name(name: str) -> str:
    """Convert a display title into a safe, readable Wiki page filename stem.

    Unlike ``slugify_name``, this preserves readable capitalization and
    non-path-separator Unicode text for user-facing Markdown page filenames.
    It keeps normal spaces intact and removes path-unsafe characters so the
    result is safe to use as a single filename component.
    """
    value = name.strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", value)
    value = re.sub(r"-+", "-", value)
    value = value.strip(" .-_")
    if not value:
        raise ValueError("Wiki page filename cannot be empty.")
    return value


def resolve_wiki_root(
    wiki_name_or_path: str,
    *,
    base_dir: str | Path | None = None,
) -> Path:
    """解析 wiki 根目录。

    这个函数支持两种输入：
    1. 用户输入具体路径，比如 `E:/wiki/my-wiki`
       这种情况直接使用这个路径。
    2. 用户输入一个名称，比如 `rag-research`
       这种情况默认放到 `.deer-flow/wiki/rag-research/`。
    """
    raw = wiki_name_or_path.strip()
    if not raw:
        raise ValueError("wiki_name_or_path cannot be empty.")

    candidate = Path(raw).expanduser()

    # 判断用户传进来的是“路径”还是“普通 wiki 名称”。
    # 只要包含路径分隔符、是绝对路径、或以 "." 开头，就当成路径处理。
    looks_like_path = (
        candidate.is_absolute()
        or "/" in raw
        or "\\" in raw
        or raw.startswith(".")
    )

    if looks_like_path:
        return candidate.resolve()

    root_base = Path(base_dir).expanduser() if base_dir is not None else DEFAULT_WIKI_BASE_DIR
    return (root_base / slugify_name(raw)).resolve()


def build_wiki_paths(wiki_root: str | Path) -> WikiPaths:
    """根据 wiki 根目录计算所有标准路径，但不创建任何文件或目录。

    这个函数只做“路径拼接”，不会产生副作用。
    真正创建目录的逻辑在 `ensure_wiki_layout()`。
    """
    root = Path(wiki_root).expanduser().resolve()

    raw_dir = root / RAW_DIR_NAME
    raw_sources_dir = raw_dir / RAW_SOURCES_DIR_NAME
    raw_assets_dir = raw_dir / RAW_ASSETS_DIR_NAME

    wiki_dir = root / WIKI_DIR_NAME
    llm_wiki_dir = root / LLM_WIKI_DIR_NAME

    return WikiPaths(
        root=root,
        purpose_file=root / PURPOSE_FILE_NAME,
        schema_file=root / SCHEMA_FILE_NAME,
        raw_dir=raw_dir,
        raw_sources_dir=raw_sources_dir,
        raw_assets_dir=raw_assets_dir,
        wiki_dir=wiki_dir,
        wiki_index_file=wiki_dir / WIKI_INDEX_FILE_NAME,
        wiki_log_file=wiki_dir / WIKI_LOG_FILE_NAME,
        wiki_overview_file=wiki_dir / WIKI_OVERVIEW_FILE_NAME,
        wiki_background_dir=wiki_dir / WIKI_BACKGROUND_DIR_NAME,
        wiki_idea_dir=wiki_dir / WIKI_IDEA_DIR_NAME,
        wiki_system_model_dir=wiki_dir / WIKI_SYSTEM_MODEL_DIR_NAME,
        wiki_algorithm_dir=wiki_dir / WIKI_ALGORITHM_DIR_NAME,
        wiki_datasets_dir=wiki_dir / WIKI_DATASETS_DIR_NAME,
        wiki_summary_dir=wiki_dir / WIKI_SUMMARY_DIR_NAME,
        wiki_concept_dir=wiki_dir / WIKI_CONCEPT_DIR_NAME,
        wiki_sources_dir=wiki_dir / WIKI_SOURCES_DIR_NAME,
        wiki_synthesis_dir=wiki_dir / WIKI_SYNTHESIS_DIR_NAME,
        wiki_maintenance_dir=wiki_dir / WIKI_MAINTENANCE_DIR_NAME,
        obsidian_dir=root / OBSIDIAN_DIR_NAME,
        llm_wiki_dir=llm_wiki_dir,
        llm_wiki_config_file=llm_wiki_dir / LLM_WIKI_CONFIG_FILE_NAME,
        llm_wiki_index_file=llm_wiki_dir / LLM_WIKI_INDEX_FILE_NAME,
        llm_wiki_queue_file=llm_wiki_dir / LLM_WIKI_QUEUE_FILE_NAME,
        llm_wiki_reviews_file=llm_wiki_dir / LLM_WIKI_REVIEWS_FILE_NAME,
        llm_wiki_chats_dir=llm_wiki_dir / LLM_WIKI_CHATS_DIR_NAME,
    )


def ensure_wiki_layout(wiki_root: str | Path) -> WikiPaths:
    """创建科研型 LLM Wiki 的标准目录结构。

    如果目录已经存在，不会报错，也不会删除已有内容。
    这个函数只创建目录，不创建 purpose.md、schema.md 等默认文件。
    默认文件建议后续交给 `templates.py` 或 `service.py` 处理。
    """
    paths = build_wiki_paths(wiki_root)

    directories = (
        paths.root,
        paths.raw_dir,
        paths.raw_sources_dir,
        paths.raw_assets_dir,
        paths.wiki_dir,
        paths.wiki_background_dir,
        paths.wiki_idea_dir,
        paths.wiki_system_model_dir,
        paths.wiki_algorithm_dir,
        paths.wiki_datasets_dir,
        paths.wiki_summary_dir,
        paths.wiki_concept_dir,
        paths.wiki_sources_dir,
        paths.wiki_synthesis_dir,
        paths.wiki_maintenance_dir,
        paths.obsidian_dir,
        paths.llm_wiki_dir,
        paths.llm_wiki_chats_dir,
    )

    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

    return paths


def wiki_layout_exists(wiki_root: str | Path) -> bool:
    """检查 wiki 的核心目录结构是否已经存在。

    这里只检查目录，不检查默认 Markdown/JSON 文件。
    因为有些文件可能还没有初始化，或者由后续模块创建。
    """
    paths = build_wiki_paths(wiki_root)

    required_directories = (
        paths.root,
        paths.raw_sources_dir,
        paths.raw_assets_dir,
        paths.wiki_dir,
        paths.wiki_background_dir,
        paths.wiki_idea_dir,
        paths.wiki_system_model_dir,
        paths.wiki_algorithm_dir,
        paths.wiki_datasets_dir,
        paths.wiki_summary_dir,
        paths.wiki_concept_dir,
        paths.wiki_sources_dir,
        paths.wiki_synthesis_dir,
        paths.wiki_maintenance_dir,
        paths.obsidian_dir,
        paths.llm_wiki_dir,
        paths.llm_wiki_chats_dir,
    )

    return all(path.is_dir() for path in required_directories)


def safe_filename(original_name: str) -> str:
    """把原始文件名转换成安全文件名。

    用途：
    - 导入 PDF 到 raw/sources/
    - 生成 Markdown 页面到 wiki/sources/ 等目录

    这个函数会保留合法后缀，比如 `.pdf`、`.md`。
    如果后缀里有奇怪字符，则丢弃后缀。
    """
    path = Path(original_name)
    stem = slugify_name(path.stem)
    suffix = path.suffix.lower()

    if suffix and not re.fullmatch(r"\.[a-z0-9]+", suffix):
        suffix = ""

    return f"{stem}{suffix}"


def safe_display_filename(original_name: str) -> str:
    """Convert a filename to a safe display-preserving filename.

    This is for generated Wiki Markdown pages, where capitalization is part of
    the human-readable title. Raw source files keep using ``safe_filename`` so
    their historical lowercase import behavior stays unchanged.
    """
    path = Path(original_name)
    stem = display_slugify_name(path.stem)
    suffix = path.suffix.lower()

    if suffix and not re.fullmatch(r"\.[A-Za-z0-9]+", suffix):
        suffix = ""

    return f"{stem}{suffix}"


def unique_child_path(directory: str | Path, filename: str) -> Path:
    """在指定目录下生成一个不会重名的文件路径。

    如果 `paper.pdf` 已经存在，就自动生成：
    - `paper-2.pdf`
    - `paper-3.pdf`
    - ...
    """
    directory_path = Path(directory).expanduser().resolve()
    safe_name = safe_filename(filename)

    candidate = directory_path / safe_name
    if not candidate.exists():
        return candidate

    original = Path(safe_name)
    stem = original.stem
    suffix = original.suffix

    counter = 2
    while True:
        candidate = directory_path / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def unique_display_child_path(directory: str | Path, filename: str) -> Path:
    """Generate a unique child path while preserving readable filename casing."""
    directory_path = Path(directory).expanduser().resolve()
    safe_name = safe_display_filename(filename)

    candidate = directory_path / safe_name
    if not candidate.exists():
        return candidate

    original = Path(safe_name)
    stem = original.stem
    suffix = original.suffix

    counter = 2
    while True:
        candidate = directory_path / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def raw_source_path(paths: WikiPaths, original_name: str) -> Path:
    """为导入的原始资料生成 raw/sources/ 下的目标路径。"""
    return unique_child_path(paths.raw_sources_dir, original_name)


def raw_asset_path(paths: WikiPaths, original_name: str) -> Path:
    """为图片、截图、提取图表等资源生成 raw/assets/ 下的目标路径。"""
    return unique_child_path(paths.raw_assets_dir, original_name)


def source_summary_path(paths: WikiPaths, title: str) -> Path:
    """为资料摘要页生成 wiki/sources/ 下的 Markdown 路径。"""
    filename = f"{display_slugify_name(title)}.md"
    return unique_display_child_path(paths.wiki_sources_dir, filename)


def background_page_path(paths: WikiPaths, title: str) -> Path:
    """为简略背景页生成 wiki/background/ 下的 Markdown 路径。"""
    filename = f"{display_slugify_name(title)}.md"
    return unique_display_child_path(paths.wiki_background_dir, filename)


def idea_page_path(paths: WikiPaths, title: str) -> Path:
    """为创新点页生成 wiki/idea/ 下的 Markdown 路径。"""
    filename = f"{display_slugify_name(title)}.md"
    return unique_display_child_path(paths.wiki_idea_dir, filename)


def system_model_page_path(paths: WikiPaths, title: str) -> Path:
    """为系统模型页生成 wiki/system_model/ 下的 Markdown 路径。"""
    filename = f"{display_slugify_name(title)}.md"
    return unique_display_child_path(paths.wiki_system_model_dir, filename)


def algorithm_page_path(paths: WikiPaths, title: str) -> Path:
    """为算法页生成 wiki/algorithm/ 下的 Markdown 路径。"""
    filename = f"{display_slugify_name(title)}.md"
    return unique_display_child_path(paths.wiki_algorithm_dir, filename)


def dataset_page_path(paths: WikiPaths, title: str) -> Path:
    """为数据集/仿真设置页生成 wiki/datasets/ 下的 Markdown 路径。"""
    filename = f"{display_slugify_name(title)}.md"
    return unique_display_child_path(paths.wiki_datasets_dir, filename)


def summary_page_path(paths: WikiPaths, title: str) -> Path:
    """为研究现状摘要页生成 wiki/summary/ 下的 Markdown 路径。"""
    filename = f"{display_slugify_name(title)}.md"
    return unique_display_child_path(paths.wiki_summary_dir, filename)


def concept_page_path(paths: WikiPaths, title: str) -> Path:
    """为概念页生成 wiki/concept/ 下的 Markdown 路径。"""
    filename = f"{display_slugify_name(title)}.md"
    return unique_display_child_path(paths.wiki_concept_dir, filename)


def synthesis_page_path(paths: WikiPaths, title: str) -> Path:
    """为综合分析页生成 wiki/synthesis/ 下的 Markdown 路径。"""
    filename = f"{display_slugify_name(title)}.md"
    return unique_display_child_path(paths.wiki_synthesis_dir, filename)


def assert_inside_wiki(paths: WikiPaths, target: str | Path) -> Path:
    """确保目标路径位于当前 wiki 根目录内部。

    这是一个安全检查，防止后续代码误把文件写到 wiki 外面。
    比如用户传入 `../../somewhere` 时，这里会抛出错误。
    """
    resolved = Path(target).expanduser().resolve()

    try:
        resolved.relative_to(paths.root)
    except ValueError as exc:
        raise ValueError(f"Path is outside wiki root: {resolved}") from exc

    return resolved
