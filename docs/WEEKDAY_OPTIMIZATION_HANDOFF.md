# 周中电脑：获取日志并执行优化任务

适用于已经配置过本项目、在本机独立登录的周中运行电脑。此次交接针对2026-11-15运行问题；日期按实际执行时的北京时间判断，不把“明天”固定为某一天。

## 先安全更新

在该电脑**现有项目仓库目录**打开PowerShell，使用已安装的Git和项目Python。无需改成另一台电脑的目录，无需复制其代理、凭据或运行包。不要另建业务基线。

```powershell
$ErrorActionPreference = 'Stop'
git status --short
if ($LASTEXITCODE -ne 0) { throw '当前目录不是可用Git仓库' }
$ebookBranch = (git branch --show-current).Trim()
if ($ebookBranch -ne 'main') { throw '当前不是main，请先核查分支与本地修改，不要强行切换' }
git fetch origin main
if ($LASTEXITCODE -ne 0) { throw '更新失败，停止后续操作' }
git merge --ff-only origin/main
if ($LASTEXITCODE -ne 0) { throw '无法安全快进，保留现状并交给Codex处理；不要reset/clean或强推' }
Get-Content -Encoding UTF8 docs/RUN_REVIEW_20261115.md
Get-Content -Encoding UTF8 docs/DAILY_RUN_OPTIMIZATION_TASK.md
```

命令保留本地修改；有冲突或分支分叉时停止，不自动stash。不要把后面的生产运行命令拼进这个更新脚本。

## 给该电脑Codex的开发指令

```text
继续 ebook 项目，执行 docs/DAILY_RUN_OPTIMIZATION_TASK.md。
先读 AGENTS.md、README.md、docs/DEVELOPMENT.md、本机私有 RUNBOOK.md、
config/runtime.json、docs/SHARED_RUNTIME.md、docs/RUN_REVIEW_20261115.md
和 docs/WEEKDAY_OPTIMIZATION_HANDOFF.md。
检查工作区并安全更新main，保留私有配置、凭据及本地修改；禁止reset、clean、强推、恢复旧stash。
先用现有证据和合成测试量化瓶颈，优先减少共享检查点全表扫描、重复保存和失败后的全流程重走。
核查实际代码：11月15日部分运行修复尚未提交，不能把文档描述当作当前代码能力。
完成必要修复、模拟测试和性能对比，提交并推送不含私有数据的代码、说明与回退办法。
共享进度、全局互斥、未知写入对账、五榜完整性、发布前独立回读与日报去重必须保留。
不重跑11月15日，不发测试或历史通知，不为性能验收自动发生产日报。
正常日目标60分钟内；来源齐备后到发布及共享提交目标15分钟内。
报告实际改进和剩余瓶颈；未经真实日运行验证，不宣称性能已达标。
```

## 正常每日任务入口（与优化开发分开）

当天正常任务已获授权、代码部署和模拟验证通过后，用目标电脑自身的项目Python和独立授权环境执行：

```powershell
python tools/check_shared_runner.py --online
if ($LASTEXITCODE -ne 0) { throw '在线检查未通过，先只读诊断，禁止盲重试' }
python scripts/daily_run.py --run-kind scheduled
```

在线检查只读，不恢复状态或获取锁。每日主入口负责恢复共享状态、获取互斥锁及全部发布检查；另一实例运行时安全跳过。未知锁或未完成保护先只读对账，不删除文件或抢锁。同日已完成模块和日报不得重复写入/发送。

不要把11月15日的补采参数、旧日期、旧锁、一次性恢复脚本或指定历史榜单固定到日程。代码改动的来源适配先做对应dry-run；不要为了检查准备状态而无条件将一次完整采集dry-run与一次完整正式采集串联。

完成后独立核对来源覆盖、五榜、费用、比较、两条日报精确收件人及工作流状态、共享提交和锁释放。保留实际采样与完成时间；手动执行不记为10点自动触发验收。

公开仓库提供代码与脱敏说明；私有业务进度从飞书共享检查点恢复。原始回执和凭据不会随Git克隆，也不应上传GitHub。
