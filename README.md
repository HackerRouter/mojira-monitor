# Mojira 崩溃报告监控器

定期收集 Minecraft 最近更新的公开 bug 报告，筛选疑似服务端崩溃、客户端崩溃，保存到 SQLite，并复查 Duplicate、修复版本等状态。

## 已实现

- 官方站点 JQL 接口和 mojira.dev v1 API 两种数据源。
- 默认监控 `MC`（Java）、`MCPE`（基岩版）、`BDS`（基岩专用服务器）。
- 分页、更新时间检查点、重叠窗口、每日历史回扫。
- 中英文关键词、可扩展正则、ADF 富文本转纯文本。
- 可选 AI 分类，校验分类格式及证据是否来自原文；默认关闭。
- 按 issue key 更新记录，保留版本、状态、正文、来源 JSON、分类证据。
- Duplicate 从有效列表软删除；保留主报告编号，重开后可恢复。
- 404、403、超时、字段异常均保留本地报告，不作为删除依据。
- 限速、429/5xx 重试、Retry-After、任务日志、单写入进程锁。
- 命令行查询、JSON 导出、持续定时运行或 cron 运行。

环境要求：**Python 3.11+，Linux/macOS**。进程锁使用 `fcntl`，Windows 请用 WSL。

## 安装与启动

```bash
git clone https://github.com/HackerRouter/mojira-monitor.git
cd mojira-monitor

# 已有 uv 时，按锁文件安装（包含测试依赖）
uv sync --extra dev --frozen

# 或使用 Python 自带 venv
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'

# 先验证数据源
.venv/bin/mojira-monitor doctor

# 采集并复查一轮
.venv/bin/mojira-monitor once

# 持续定时运行，Ctrl+C 停止
.venv/bin/mojira-monitor run
```

`once` 不会常驻。`run` 在进程存活期间执行定时任务。长期运行请在持续在线的服务器上交给 systemd 等进程管理器；不要依赖临时开发会话的生命周期。

Git 仓库不包含数据库、采集结果和本地配置。程序首次运行会自动创建空数据库。本次交付的 ZIP 和开发工作区另附首轮真实采集的 `data/mojira.sqlite3`。

## 默认策略

完整配置见 [config.toml](config.toml)，路径相对于配置文件，不随 shell 工作目录变化。

| 配置 | 默认值 | 用途 |
|---|---:|---|
| `polling.collect_minutes` | 20 分钟 | 读取最近新增/更新报告 |
| `polling.recheck_hours` | 12 小时 | 复查已入库报告 |
| `polling.duplicate_recheck_hours` | 168 小时 | 低频复查软删除的 Duplicate，允许恢复 |
| `polling.page_size` | 50 | 每页条数，最多 100 |
| `polling.bootstrap_pages` | 2 | 首次每个项目最多采 100 条 |
| `polling.initial_hours` | 24 | 首次只考察最近 24 小时更新的报告 |
| `polling.overlap_hours` | 6 | 重叠采集，按 key 去重 |
| `polling.max_pages` | 30 | 每个项目每轮增量/回扫的页数上限 |
| `polling.backfill_hours` | 168 | 历史回扫时间范围 |
| `polling.backfill_interval_hours` | 24 | 持续运行模式的回扫间隔 |
| `polling.recheck_batch` | 100 | 每批复查条数；run 每分钟处理一批到期记录 |
| `http.min_interval_seconds` | 1 秒 | 请求最小间隔 |

首次初始化是有意截取最近的一部分，达到 `bootstrap_pages` 会输出 `bootstrap_truncated: true`。后续增量采集如果触及 `max_pages` 但还没读到检查点，会报错并保持旧检查点；请增大上限处理积压。不要把这类错误当成“没有新 bug”。

默认读取各项目的最近更新列表，在本地分析**标题、描述、环境**，避免只靠标题关键字检索漏掉正文。关键词规则输出的是候选报告，例如“卡死”和“崩溃”混用的用户描述仍可能命中；没有 AI 时不要视为已确认的崩溃结论。`include_hangs = false` 仅排除单纯命中卡死规则的报告。

分页接口不提供固定快照，采集期间数据可能移动。重叠窗口和每日回扫可以缓解遗漏，但不承诺完整复制全部历史数据。首次 `run` 从当前时间开始计算每日回扫周期；需要立即回扫时运行 `backfill`。

## 查询、复查和导出

```bash
# 查看有效报告（含正文、证据、版本和来源）
.venv/bin/mojira-monitor list --limit 10

# 包括已移除的 Duplicate / 已不再相关的报告
.venv/bin/mojira-monitor list --all --limit 10

# 任务结果、数量与检查点
.venv/bin/mojira-monitor status

# 不等 12 小时，立即复查最多 recheck_batch 条
.venv/bin/mojira-monitor recheck --force

# 立即回扫最近 backfill_hours 小时
.venv/bin/mojira-monitor backfill

# 导出有效报告；如需软删除记录，加 --all
.venv/bin/mojira-monitor export --output data/reports.json

# 使用独立配置（全局参数放在子命令之前）
.venv/bin/mojira-monitor --config config.local.toml once
```

导出拒绝覆盖已有文件，避免误覆盖数据库或其他资料。用新文件名重新导出即可。

数据库核心表：

- `bugs`：命中的报告及其当前状态；`active = 1` 为有效报告。
- `seen`：扫描过的 key、更新时间等指纹，减少重复分类，不保存未命中报告的完整正文。
- `metadata`：各数据源/项目的检查点。
- `events`：新增、状态更新、移除、恢复记录。
- `runs`：每轮运行的统计与失败原因。

Duplicate 判断只看来源的 `resolution`，不看评论关键词。`duplicates` 指向主报告；`is duplicated by` 表示别人重复了本报告，不能据此删除主报告。本程序保存 `duplicate_of`，暂不自动递归采集主报告。`Fixed` 不会因为已修复而删除。

## 数据源的实际验证

2026-10-07 在当前运行环境实测：

- 官方 `POST https://bugs.mojang.com/api/jql-search-post` 对 MC、MCPE、BDS 都返回成功。
- **官方分页参数为从 0 开始的 `page`**。实测传 `nextPageToken` 会重复第一页，因此本程序使用 `page`，并检查重复页和返回页码。
- mojira.dev API 在当前出口返回 Cloudflare challenge（HTTP 403）。此时程序明确报错，不将验证 HTML 解析为报告。

相关源码/文档：

- [mojira.dev OpenAPI](https://github.com/misode/mojira.dev/blob/main/openapi.yaml)
- [官方接口调用参考](https://github.com/misode/mojira.dev/blob/main/api/public.go)
- [mojira.dev API 返回结构](https://github.com/misode/mojira.dev/blob/main/views.go)

官方站点接口没有在本项目中被假设为稳定的公共开发者契约，接口变更可能需要更新适配器。mojira.dev 是社区镜像，有同步延迟；当前 v1 详情不包含 issue links、评论列表和附件列表。官方适配器保存公开返回的关联链接和附件元数据，**不下载附件、不采集评论、不访问私有报告**。

若使用可访问的镜像，将 `source = "mirror"`，并按需设置 `mirror_url`。切换数据源会使用独立检查点，但共用 bug key。比本地更新时间更旧的响应不会覆盖现有状态。

## 可选 AI 分类

关键词方案不需要任何密钥。若想加入语义分析，配置兼容 Chat Completions 请求格式的服务：

```toml
[ai]
mode = "candidates"
base_url = "https://YOUR_PROVIDER/v1"
model = "YOUR_MODEL"
api_key_env = "MOJIRA_AI_API_KEY"
min_confidence = 0.75
max_chars = 16000
```

密钥通过运行进程的 `MOJIRA_AI_API_KEY` 环境变量注入，不要写入配置文件。程序不会自动读取 `.env`。

- `candidates`：仅将关键词/卡死规则命中的候选报告交给模型，可减少误报，但无法找回关键词完全没命中的报告。
- `all`：所有新发现或变更的非 Duplicate 报告都交给模型，调用量更大。
- 输入只包含 issue key 和截断后的标题/正文/环境文本。启用意味着这些公开报告文本会发送到你配置的服务。
- 模型必须返回合法分类、数值置信度和原文证据。低置信度/uncertain 回退到关键词结论；格式或证据校验失败将保留重试机会，不缓存为“不相关”。
- 模型输出只参与内容分类。Duplicate 移除仍由数据源的 resolution 决定。
- 配置变更会改变分类缓存版本。运行 `backfill` 可以按新规则重评回扫范围内的报告；`recheck --force` 重评已入库报告。

此接口已通过模拟响应测试；当前未配置模型密钥，未执行付费或真实 AI 调用。

## 长期运行

可以用 [deploy/mojira-monitor.service](deploy/mojira-monitor.service) 作为 systemd 模板。将项目放入 `/opt/mojira-monitor`，安装依赖，创建具有该目录及 `data/` 写入权限的服务用户，再由管理员按机器环境调整 `User` 和路径后安装服务。模板没有自动安装或启用。

如果更习惯 cron，可配置：

```cron
*/20 * * * * cd /opt/mojira-monitor && .venv/bin/mojira-monitor once >> data/cron.log 2>&1
15 3 * * * cd /opt/mojira-monitor && .venv/bin/mojira-monitor backfill >> data/cron.log 2>&1
```

初始化数据库后再用上述日志路径。`once` 自身只执行一轮，复查仍遵守配置中的到期时间。cron 和常驻 `run` 二选一，写入进程锁会拒绝重叠运行。退出码 0 表示本轮成功，1 表示配置、请求或部分采集失败。需定期清理/轮转日志。

## 测试

```bash
.venv/bin/pytest -q
```

测试覆盖：真实格式的官方响应/ADF、官方与镜像适配、分页重复与积压、检查点失败恢复、去重、Duplicate 移除及重开恢复、不可访问报告保留、过期响应保护、429 重试、AI 原文证据校验等。自动化测试不访问互联网，真实接口通过 `doctor`、`once`、`recheck --force` 单独验证。
