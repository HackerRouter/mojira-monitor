# Mojira Monitor

按**项目、关键词、正则表达式和 issue 标签**定时监控 Minecraft bug 报告。支持多个独立监控项、SQLite 存储、可选 AI 分类、Duplicate 清理与重开恢复。

既可以追踪崩服/闪退，也可以追踪性能、红石、存档损坏等任意主题，或者收集指定项目的全部近期报告。当前数据源支持 Mojang 官方站点和 mojira.dev，尚未接入其他平台。

## 快速开始

环境：**Python 3.11+，Linux/macOS**；Windows 使用 WSL（进程锁依赖 `fcntl`）。

```bash
git clone https://github.com/HackerRouter/mojira-monitor.git
cd mojira-monitor
uv sync --extra dev --frozen

# 查看全部监控项，包括尚未启用的示例
.venv/bin/mojira-monitor monitors

# 只读检查数据源，无需 AI 密钥
.venv/bin/mojira-monitor doctor

# 采集、复查一次
.venv/bin/mojira-monitor once

# 常驻运行所有 enabled=true 的监控项
.venv/bin/mojira-monitor run
```

没有 uv 时：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

默认仅启用 `crashes` 崩溃监控。`config.toml` 同时提供基岩性能、Java 红石两个可修改的示例，改为 `enabled=true` 即可一起运行。数据库、采集结果、密钥和本地配置不会提交到 Git。

## 多个独立监控配置

顶层配置作为公共默认值，每个 `[[monitors]]` 可以覆盖项目、数据源、采集频率、筛选与 AI 设置。配置节按字段继承，数组整体替换；需要清除继承的关键词时显式写 `[]`。

```toml
database = "data/mojira.sqlite3"
source = "official"
projects = ["MC", "MCPE", "BDS"]

[polling]
collect_minutes = 20
recheck_hours = 12

[[monitors]]
id = "crashes"
name = "崩溃报告"
database = "data/mojira.sqlite3"
tags = ["stability"]

[monitors.classification]
mode = "crash"
include_hangs = false

[[monitors]]
id = "bedrock-performance"
name = "基岩性能问题"
projects = ["MCPE", "BDS"]
tags = ["performance", "bedrock"]

[monitors.polling]
collect_minutes = 30

[monitors.classification]
mode = "keywords"
category = "performance"
keywords_any = ["fps", "stutter", "帧率", "卡顿"]
patterns_any = ['\blag(?:gy|ging)?\b']
keywords_all = []
exclude_keywords = ["feature request"]
fields = ["summary", "description"]

[monitors.filters]
labels_any = []
labels_all = []
exclude_labels = []
```

每个监控项拥有**独立数据库、检查点、分类缓存、复查时间和事件记录**。同一报告可以同时被多个监控项保存；某条规则不再命中，不会影响其他监控项的结果。没有指定 `database` 时，以顶层数据库名生成独立路径，例如 `data/mojira.bedrock-performance.sqlite3`。

同一配置中不允许重复 id 或数据库路径。数据库首次使用时会绑定监控 id，防止另一个配置文件误用并覆盖其分类。`id` 应保持稳定，显示名称可通过 `name` 修改。各监控项按各自频率串行执行，耗时任务可能推迟后续任务，不提供严格实时调度。

原有单监控 TOML 仍然可用，不写 `[[monitors]]` 即视为一个监控项（默认 id 为 `default`，也可设置 `monitor_id`）。独立示例：

- [examples/general.toml](examples/general.toml)：存档/区块数据损坏监控。
- [examples/all-java.toml](examples/all-java.toml)：收集 Java 近期报告，不要求崩溃关键词。

数据库等相对路径均以 **TOML 文件所在目录**为基准。

## 项目、标签与匹配方式

`MC`、`MCPE`、`BDS` 是**项目代码**，放在 `projects`；也可配置其他有效 Mojira 项目代码。项目是否存在可以用 `doctor` 检查。

| 配置 | 含义 |
|---|---|
| `projects` | 监控哪些 Mojira 项目，例如 `["MC", "MCPE"]` |
| `tags` | 给本地监控项添加自定义分组标签，输出为 `monitor_tags`；不修改远端 issue |
| `filters.labels_any` | issue 自身的 labels 至少包含其中一个 |
| `filters.labels_all` | issue 自身的 labels 必须包含这里的全部标签 |
| `filters.exclude_labels` | issue 命中任意一个就排除 |
| `classification.keywords_any` | 字面关键词，至少命中一个（与 `patterns_any` 合并为同一个 OR 组） |
| `classification.patterns_any` | Python 正则表达式，支持词边界等高级匹配 |
| `classification.keywords_all` | 还必须包含这里的全部字面关键词 |
| `classification.exclude_keywords` | 命中任意字面关键词即排除 |
| `classification.exclude_patterns` | 命中任意正则即排除 |
| `classification.fields` | 搜索哪些字段：`summary`、`description`、`environment` |
| `classification.case_sensitive` | 自定义关键词/正则是否区分大小写，默认 false |
| `classification.category` | 通用模式命中后的本地分类名，例如 `performance` |

Issue labels **区分大小写、精确匹配**。所有非空标签条件需要同时满足。标签名称请使用报告中真实存在的值。

字面关键词采用子串匹配，例如 `corrupt` 可命中 `corrupted`。要避免 `lag` 误命中 `villager`，使用 `patterns_any = ['\blag\b']`。多个字段用换行连接后匹配；不检索评论或附件内容。

三种分类模式：

- `crash`：保留原内置中英文崩溃分类，输出 `server_crash` / `client_crash` / `crash_unspecified` / `hang`。自定义正向关键词条件可进一步缩小范围；`extra_patterns` 继续作为补充崩溃规则。
- `keywords`：通用主题匹配，不要求提到崩溃。至少配置一项正向关键词、正则或标签条件。
- `all`：保存通过筛选的全部非 Duplicate 报告。仍遵守配置中的关键词、标签和排除条件；不想限制关键词时将对应数组清空。

排除词和排除标签优先。规则模式只代表候选匹配，不代表 Mojang 已确认问题。崩溃监控中，描述同时使用“卡死”和“崩溃”仍可能命中；`include_hangs=false` 只排除单纯命中卡死规则的报告。

## 运行、查询与导出

全局参数 `--config`、`--monitor` 放在子命令之前。默认操作所有启用的监控项；显式 `--monitor` 可以选择未启用的示例，并可重复指定多个。

```bash
.venv/bin/mojira-monitor --monitor bedrock-performance once
.venv/bin/mojira-monitor --monitor crashes --monitor java-redstone run
.venv/bin/mojira-monitor --config examples/general.toml once

.venv/bin/mojira-monitor --monitor crashes list --limit 10
.venv/bin/mojira-monitor --monitor crashes list --all --limit 10
.venv/bin/mojira-monitor status
.venv/bin/mojira-monitor --monitor crashes recheck --force
.venv/bin/mojira-monitor --monitor crashes backfill
.venv/bin/mojira-monitor export --output data/reports.json
```

`list --limit` 是每个监控项的上限。`list` / `export` 的每条记录包含监控 id、名称、分组标签、正文、版本、来源与分类证据。`--all` 包括软删除记录。导出拒绝覆盖已有文件。多监控命令结果包含各监控项的统计；退出码 1 表示至少一项失败，0 表示全部成功。

修改关键词、标签或 AI 配置会使分类缓存失效，但不会立即重扫全部历史。运行 `backfill` 重评回扫范围，运行 `recheck --force` 重评已入库报告。`--force` 仍遵守每批复查上限。移除一个项目不会物理删除该项目之前的记录，但之后只会采集/复查当前配置的项目；旧记录仍可在数据库和导出中查看。

## 增量与状态维护

| 公共默认配置 | 值 |
|---|---:|
| `polling.collect_minutes` | 20 分钟 |
| `polling.recheck_hours` | 12 小时 |
| `polling.duplicate_recheck_hours` | 168 小时 |
| `polling.recheck_batch` | 100 条 |
| `polling.page_size` | 50 条，最大 100 |
| `polling.bootstrap_pages` / `initial_hours` | 首次每项目最多 2 页，只考察最近 24 小时更新 |
| `polling.overlap_hours` | 6 小时 |
| `polling.max_pages` | 增量或回扫每项目每轮最多 30 页 |
| `polling.backfill_hours` / `backfill_interval_hours` | 每 24 小时回扫最近 168 小时 |
| `http.min_interval_seconds` | 每个 HTTP 客户端最小请求间隔 1 秒 |

首次初始化是有意截取最近的一部分，达到上限时输出 `bootstrap_truncated=true`。后续增量达到 `max_pages` 但仍未读到检查点会报错，保持旧检查点；请增大上限处理积压。逐项失败也不推进该项目检查点。

分页没有固定快照。重叠窗口、key 去重和每日回扫缓解并发更新与镜像延迟造成的遗漏，但不保证完整复制全部历史。`run` 首次启动后按配置间隔进行第一次历史回扫；需要立即回扫用 `backfill`。

只有来源的 `resolution == Duplicate` 会触发 Duplicate 软删除，不看评论里的字样。主报告链接取 `duplicates` 方向，不能因 `is duplicated by` 而删除主报告。保存 `duplicate_of`，暂不递归采集主报告。重开后再次匹配可恢复；`Fixed` 不会仅因已修复而删除。

404、403、超时、响应结构错误均保留本地记录并记下失败。比本地更旧的响应不会覆盖现有状态。未变化报告在常规复查中复用分类结果，避免重复 AI 调用。

原有 SQLite 表保持兼容；新默认配置的 `crashes` 继续使用 `data/mojira.sqlite3`。没有监控绑定的旧数据库会被首次使用它的监控项接管，不删除任何报告。核心表：`bugs`、`seen`、`metadata`、`events`、`runs`。

## 可选 AI

默认 `ai.mode="off"`，无需密钥。兼容 Chat Completions 格式的服务可以通过公共 `[ai]` 或单项 `[monitors.ai]` 配置：

```toml
[ai]
mode = "candidates"
base_url = "https://YOUR_PROVIDER/v1"
model = "YOUR_MODEL"
api_key_env = "MOJIRA_AI_API_KEY"
min_confidence = 0.75
max_chars = 16000
criteria = "只关注游戏本身导致的存档或区块数据丢失。"
```

密钥由运行进程的环境变量注入，不写进 TOML，不自动读取 `.env`。启用后将公开报告的 key、labels 和截断的所选字段文本发送给你配置的服务。

- `candidates`：只分析规则候选，可减少误报，不能找回关键词完全漏掉的报告。
- `all`：分析通过项目、标签、排除条件的全部新/变更报告，允许语义判断超出字面正向关键词匹配；调用量更大。
- `crash` 模式做崩溃分类；通用模式按关键词规则和可选 `criteria` 判断相关性，命中后使用自定义 `category`。
- AI 不能推翻项目、标签和排除条件。Duplicate 处理始终依据数据源状态。
- 模型必须返回合法分类、置信度及原文证据。低置信度/uncertain 回退规则；格式或证据错误保留重试机会，不缓存为无关。

当前测试使用模拟 AI 响应，未配置真实模型密钥或发起付费调用。

## 数据源、部署和扩展

默认使用官方 `POST https://bugs.mojang.com/api/jql-search-post`。2026-10-07 实测 MC、MCPE、BDS 均可访问；分页使用从 0 开始的 `page`，传 `nextPageToken` 会重复第一页。程序检查重复页与返回页码。

mojira.dev 在当前开发环境触发 Cloudflare 验证；可以在可访问的环境使用 `source="mirror"`，或修改 `mirror_url` 指向自托管实例。镜像有同步延迟，v1 详情没有关联链接、评论列表和附件列表；官方适配器保存公开关联链接及附件元数据，不下载附件或访问私有报告。

参考：[mojira.dev OpenAPI](https://github.com/misode/mojira.dev/blob/main/openapi.yaml)、[官方接口实现](https://github.com/misode/mojira.dev/blob/main/api/public.go)。官方站点接口可能变更，并非本项目承诺稳定的外部开发者 API。

`once` 只执行一轮，`run` 在进程存活期间调度各监控项。长期运行可使用 [systemd 模板](deploy/mojira-monitor.service)，修改服务用户和路径后安装；模板没有自动启用。也可用 cron：

```cron
*/20 * * * * cd /opt/mojira-monitor && .venv/bin/mojira-monitor once >> data/cron.log 2>&1
15 3 * * * cd /opt/mojira-monitor && .venv/bin/mojira-monitor backfill >> data/cron.log 2>&1
```

先运行 `init` 创建数据目录。cron 中 `once` 的采集频率由 cron 决定，复查仍遵守到期时间；想让多项采用不同采集频率，请使用 `run` 或独立 cron 行加 `--monitor`。不要同时用 cron 和 run 操作同一数据库，文件锁会拒绝冲突；不同数据库可以分别运行。请按部署环境轮转日志。

开发扩展说明见 [docs/EXTENDING.md](docs/EXTENDING.md)。测试：

```bash
.venv/bin/pytest -q
```

自动化测试不联网，覆盖数据源适配、分页恢复、Duplicate 生命周期、多配置继承与隔离、关键词/标签语义、AI 证据校验、命令行导出和不同调度频率。真实接口用 `doctor`、`once` 单独验证。
