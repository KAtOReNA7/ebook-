# 原运行电脑：番茄出版飙升榜接续说明

## 本次交付与授权范围

用户于 2026-11-05 明确要求继续解决番茄出版飙升榜获取，并提交代码供原电脑接续。
独立只读采集已成功，不再需要 Android、模拟器、WSL 或签名服务。
实现与来源依据见 [采集验证说明](FANQIE_PUBLICATION_RISING.md)。

本次交付完成“取得官方榜单并留档”。尚未完成“接入原每日流水线、追踪作品、
比较上下榜、同步飞书”的业务集成。仅修改 `config/fanqie-ranking.json` 的开关
不会完成接入，也不应据此宣称原采集任务全部完成。
本次不迁移运行电脑：原电脑继续保管现场私有配置和状态，新电脑没有启用业务写入。

## 在原电脑更新并采集

先进入原来的 ebook 项目目录，读取 README.md、docs/DEVELOPMENT.md，
以及原电脑自己的 RUNBOOK.md 和 config/runtime.json。使用原有 Python 3.11+ 环境。
更新代码时确认没有正在执行的采集/同步进程。先检查分支与工作区：

```powershell
git status --short --branch
git remote -v
git fetch origin
```

目标仓库是 `https://github.com/KAtOReNA7/ebook-`。原目录在 main、没有未处理的
代码改动且可以快进时执行：

```powershell
git pull --ff-only origin main
```

若存在本地改动或分支分叉，先比较并保留现场改动，受控合并所需提交；不要 reset、
clean、强制覆盖，也不要恢复历史 stash。被忽略的私有文件不需要用新电脑的示例替换。
若 Git 因失效的本机代理无法连接，只在确认直连适用后，单次使用
`git -c http.proxy= -c https.proxy= pull --ff-only origin main`；不全局改代理。

依次执行，前一步失败则先处理，不继续：

```powershell
python -m unittest discover -s tests -t .
python tools/check_public_tree.py
python scripts/collect_fanqie_rising.py
```

本次提交前全量测试为 241 项通过。采集命令无需飞书登录，也不写飞书。
它打印 `snapshot` 的实际绝对路径；默认每次建立新目录，不复用已有输出目录。
正常结果须同时满足 `status=success`、`complete=true`、`count=100`，进程退出码 0。
失败则保留 `snapshot.json` 中的错误和原始响应；没有榜单数据不等于当天空榜。

输出目录 `snapshots/fanqie-rising/<实际时间>/` 包含：

- `directory.json` 和 `ranking.json`：原始官方响应。
- `*.source.json`：实际采样时间、请求来源、响应摘要。
- `snapshot.json`：经过校验的 100 本作品及名次。
- `ranking.csv`：可查阅的榜单；长作品 ID 按文本导入电子表格。

本机 2026-11-05 19:53 与 19:55 两次成功采集的 100 本作品顺序一致。
这只是当时的验证，不是原电脑应当硬编码的预期书单。原电脑须取得自己的新观察，
不将另一台电脑当晚的观察回填为当天 10 点样本。

## 如何完成原采集任务的剩余集成

1. **恢复原需求口径。** 从原电脑现场记录确认监测 Top50 还是全榜、作品追踪期限、
   原表字段和已授权通知规则。接口当前返回 100 本不代表原业务范围自动扩为 100 本；
   若原需求是 Top50，保留完整响应，校验通过后才选名次 1–50。
2. **确认唯一写入端与现场状态。** 沿用原电脑现有 Base、身份映射、入表时间、
   通知注册表和冻结观察。检查所有未完成写入 guard；未知结果先只读核对。
   不新建替代 Base、不执行旧测试系统、不回放历史 outbox。
3. **接入已有每日锁。** 在 `scripts/daily_run.py` 的现有锁内编排独立采集器，
   将输出作为当天独立来源观察保存。不能只改暂停状态；必须实际读取并验证
   `fanqie.publication-rising.v1` 输出。不要用它覆盖已有营销作品详情快照。
4. **分开榜单与详情。** 榜单用于作品 ID、名次、飙升值。需要点评人数、书评数、
   累计阅读等详情时，使用已有 `collect_fanqie.py` 按榜单返回的真实 ID 采集。
   `rising_value_display` 不能写入累计阅读字段；`reader_label_raw` 不能未经口径
   与精度校验就直接作为精确数。出版榜样本不能自动变为营销项目。
5. **完整观察才推进比较。** 仅用完整且同范围、时间有序的两次有效榜单比较上下榜。
   若没有可信旧榜单，首次成功观察只建立当前基线，不把所有存量作品当新增事件。
   失败、重复 ID、缺位、书目数变化或未知媒介都应冻结比较，不能推导全榜下榜。
   下榜后的追踪期限沿用原需求，采样缺日保留缺失，不补造历史数据。
6. **为接入补测试。** 覆盖来源失败不影响其他来源、基线不误报、榜单完整性、
   同日重复执行幂等、作品复用、暂停配置、`--dry-run` 不写 Base、不推进正式
   通知状态，以及部分写入后 guard 阻止盲目重试。
7. **验证再启用。** 集成完成后先跑模拟测试和
   `python scripts/daily_run.py --run-kind local --dry-run`，核对实际计划。
   由原电脑在既有授权范围内执行写入后，全量回读相关表核对记录和关联。
   调试状态仍只允许已核实 owner 接收既定通知，不能发送测试消息或扩大收件人。
   确认实际接入成功后再维护暂停配置和现场 RUNBOOK，继续沿用原每日 10 点任务，
   不额外启动第二个业务写入任务。

同一时刻的 App 画面对照和跨日稳定性尚未验证；当前可以先运行独立只读采集并积累
完整观察。自动化不得启动 App/Android 环境；如用户提供官方榜单画面，可用于人工对照。

## 可转交原电脑 Codex 的任务文本

> 请更新 KAtOReNA7/ebook- 的 main，保留本机私有配置和现场状态。先读取
> AGENTS.md、README.md、docs/DEVELOPMENT.md、docs/FANQIE_RISING_HANDOFF.md，
> 以及本机 RUNBOOK.md 和 config/runtime.json。新加入的
> scripts/collect_fanqie_rising.py 已验证可以直接从番茄官方服务读取出版飙升榜，
> 无需模拟器或签名服务。请先运行全量测试，再执行独立只读采集并核对完整 100 本
> 的真实观察。随后根据原任务口径完成每日锁内编排、榜单样本与详情采集、有效基线
> 比较及现有 Base 同步的剩余集成。只有原电脑作为业务写入端；不要把本次只读
> 采集成功等同于已接通每日流程，不重建历史注册表，不扩大通知、不回放历史事件。
