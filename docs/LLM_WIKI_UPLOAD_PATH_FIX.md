# LLM Wiki 上传文件路径问题总结

## 问题

测试 `wiki_add_source` 导入 PDF 时，文件已经上传到 `/mnt/user-data/uploads/`，Agent 也能看到文件，但工具仍然报“找不到文件”。

这个问题在文件名较长、包含空格、中文或特殊字符时更容易暴露，例如论文文件名 `Sheng et al - 2024 - UADFormer...pdf`。

## 原因

`wiki_add_source` 原来直接把 Agent 传入的路径交给 `Path(...).resolve()`：

```python
service.add_source(wiki_name_or_path, Path(source_file))
```

但 `/mnt/user-data/uploads/...` 是 Agent / sandbox 视角下的虚拟路径，不是 gateway 后端进程可直接访问的真实路径。

因此后端会在错误的位置查找文件，导致实际已上传的文件无法被识别。

## 解决方案

在 `wiki_tools.py` 的工具层新增路径解析逻辑：

- 支持 `/mnt/user-data/uploads/...`、`/mnt/user-data/workspace/...`、`/mnt/user-data/outputs/...` 虚拟路径。
- 根据当前 thread 的 `thread_data` 映射到真实本地路径。
- 支持只传 uploads 目录中的文件名。
- 自动去掉模型可能传入的外层引号。
- 保留路径越界检查，防止通过 `../` 访问 thread 目录外的文件。

修复后，`wiki_add_source` 会先解析路径，再调用业务逻辑：

```python
result = service.add_source(wiki_name_or_path, _resolve_source_file(runtime, source_file))
```

## 验证

新增测试覆盖：

- `/mnt/user-data/uploads/` 下包含空格的长文件名。
- 只传带引号的上传文件名。

已通过：

```powershell
cd backend
uv run pytest tests/test_wiki_tools.py tests/test_wiki_ingest.py tests/test_wiki_lint.py
```

结果：

```text
10 passed
```
