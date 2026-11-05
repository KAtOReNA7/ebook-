# 原运行电脑：番茄出版飙升榜接续说明

2026-11-05 已完成官方完整100名采集及每日入口的代码接入。无需 App、模拟器、
WSL 或新环境。当前仍保持 `enabled=false / status=user_paused`，没有在新电脑
部署定时任务、修改业务 Base 或发送通知。代码接入、恢复采集和生产验收分别处理。

## 更新原电脑

先读取 README.md、docs/DEVELOPMENT.md、AGENTS.md、本机私有 RUNBOOK.md 和
config/runtime.json。确认没有正在运行的任务，保留私有配置、冻结观察和注册表：

```powershell
git status --short --branch
git fetch origin
git pull --ff-only origin main
python -m unittest discover -s tests -t .
python tools/check_public_tree.py
python tools/doctor.py
```

存在本地改动或分叉时先保留并核对，不能 reset、clean、强推或恢复历史 stash。
Git 本机代理失效且直连适用时，可对单条 Git 命令使用
`git -c http.proxy= -c https.proxy= ...`，不必修改全局代理。

## 已实现的每日行为

`daily_run.py` 在原有进程锁内执行 `fanqie_rising_daily.run`，位于番茄详情采集后、
版权方最近上架前。沿用原电脑每日10点入口，不新建第二个调度任务。

- 暂停时不请求榜单，不访问 Base；配置同时写暂停和启用会报错。
- 成功后冻结当天独立榜单及原始响应。使用响应 SHA256 重算榜单身份、电子书
  类型、100个唯一ID和连续名次，不能只相信 `complete=true`。
- 向现有“榜单快照”写100名；只关联已匹配的电子书营销项目，不创建100个营销项目。
  榜单请求实际时间单独保留；平台未提供更新时间，“平台榜单日期”留空。
- 按已读取的 `FANQIE_RANK` 规则，只为营销项目生成进入榜单候选。
  仅比较相邻两天已完成、同口径的完整观察。首次、缺前日、失败或残榜均不推测新上榜。
  不增加下榜通知。营销作品下榜后仍由现有番茄详情任务继续采集。
- 飙升值和原始读者标签保存在证据中，不写入累计阅读人数或每日详情指标。
- `--dry-run` 只读来源和 Base、保存本地计划；不写表、不提交完成状态。
  成功写入并完整回读后才标记完成，同日再次执行不写入。
- 未知写入以 `runs/<day>/fanqie-rising-state.json` 保护，阻止跨日重试及后续通知。
  先对账，再决定如何处理；不得删除 guard 后重新写入。

目前平台未提供明确更新时间，而现有通知规则要求核实更新时间，因此上榜候选仅
本地留档。`notifications_enabled=false` 保持不变；即使误改为 true，缺更新时间
仍不能排入通知事件。未来取得该证据时需修改并测试来源解析，不得手填推定时间。

## 尚未执行的部署步骤

当前用户提供的项目边界仍要求榜单暂停，本次不恢复线上运行。
收到明确恢复指令后，由唯一运行电脑核对现场 guard、身份映射及本地运行政策；
将 `config/fanqie-ranking.json` 改为 `enabled=true / status=web_daily`，通知保持关闭。
`tools/doctor.py` 当前也会检查暂停政策；恢复时须同步复核该政策检查，不能忽略报错。

随后在原电脑执行：

```powershell
python scripts/daily_run.py --run-kind local --dry-run
```

核对 `runs/<实际日期>/fanqie-rising-plan.json` 的100行、关联、真实采样时间和零写入。
在已授权的唯一写入端，继续沿用原来的 scheduled 入口；写后全量回读榜单快照、
运行日志及相关项目。不得拿本机旧观察冒充当天10点数据，不回放旧 outbox，
调试通知仍仅允许已核实 owner。跨日实际稳定性与原电脑首次写入尚待现场验证。

## 营销作品身份补充

网页查找和证据核验流程见 [营销覆盖核验](FANQIE_COVERAGE.md)。私有映射和现场
留档不随 Git 发布，须单独私下交接。先运行身份链接的只读计划，核对记录ID与服务
关联；原电脑完成受控链接并回读后，已匹配作品会由现有 `fanqie_daily.py` 自动纳入。
不要把“发现候选”或“本地确认映射”报告为线上覆盖已提升。

可转交原电脑的任务文本：

> 更新 main 并保留私有现场状态。读取本说明和本机 RUNBOOK。每日榜单代码已接入，
> 无需重写，当前暂停和通知关闭保持不变。先跑测试、检查未知写入 guard；
> 核对私下收到的营销作品身份映射，生成只读链接计划。仅在原电脑作为唯一写入端
> 的条件下执行已授权的身份接入及全量回读。榜单线上恢复须遵守最新用户指令，
> 不恢复旧测试 Base、不新建替代注册表、不发送测试消息。
