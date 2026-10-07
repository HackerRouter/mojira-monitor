# 扩展开发

本项目把监控配置、来源适配、文本分类、存储、调度分开。添加新的监控主题通常只需 TOML，不需要改代码；添加新平台则需要适配实现。

## 配置与监控项

`config.py` 中的 `load_configs()` 解析一个或多个 `Config`。公共节按字段继承，监控项覆盖自己的字段；数组整体替换。`select_configs()` 处理 enabled 和 CLI 选择。

一个监控项使用一个 SQLite 数据库。`Monitor` 仍然只负责单项采集与复查，多项启动和调度在 `cli.py`。数据库绑定 id，避免两个不同配置误用同一数据库。

## 分类扩展

`Classifier.classify(issue)` 返回 `Decision(category, relevant, evidence, method, confidence)`。`relevant` 决定是否进入该监控项的有效库；证据应是原文片段或明确标注的标签条件。

添加内置规则时，同步更新配置校验、分类逻辑和测试。`Config.classifier_version()` 用规则、项目、过滤器和 AI 设置生成缓存版本；内置实现发生语义变化时递增 `rules_version`。

现有 AI 适配器仅接受内容分类 JSON，不提供工具调用。输入报告按不可信数据处理，模型证据必须出现在送入模型的文本中。软删除与恢复仍由 `Monitor` 和数据库依据来源状态执行。

## 来源扩展

`Monitor` 依赖以下接口：

```python
source.identity: str  # 稳定的来源身份，用于检查点命名空间
source.page(project: str, page: int, limit: int) -> Page
source.issue(key: str) -> Issue
```

- 内部页码从 1 开始，由适配器转换为上游分页方式。
- `Page.issues` 必须按更新时间倒序；`Page.has_more` 必须反映真实分页状态。
- 列表只包含摘要时，设置 `Issue.full=False`，引擎会按需补全详情。
- 必须提供合法 key、带时区的更新时间和明确的 resolution。缺少字段应报 `SourceError`，不能默认为未解决或 Duplicate。
- 暂不可访问应报 `NotFound` / `SourceError`，由引擎保留本地记录。
- 使用 `JsonHTTP` 处理限速、重试和错误，不记录访问凭据或整份错误响应。

当前 `Source` 支持 official/mirror 两个分支，`Issue` 的 key 格式和 URL 仍针对 Mojira/Jira 项目代码。接入其他平台时，需要扩展 source 配置校验、适配器选择、Issue 标识与链接生成，而非仅替换 URL。

## 状态兼容

本次扩展沿用第一版的数据库表，通过 metadata 绑定监控 id，不需要删除或重建旧数据库。未来引入表结构变更时应使用显式 schema version 和事务迁移，迁移前备份 SQLite。

`seen` 缓存不含未命中报告的完整正文；改规则后只能通过增量扫描、backfill 或已入库报告复查重新评估。历史全部内容不会自动保存在本地。
