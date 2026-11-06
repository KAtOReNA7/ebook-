# Windows常开运行电脑与并行开发

本方案不依赖 Dot 或打开 Codex。Windows任务计划程序每天北京时间10:00
直接启动现有 Python 采集流程。电脑必须保持开机、联网；关机无法执行。
任务支持错过时间后启动，但只能记录实际采样时刻，不能补造过去数据。

## 分工

| 位置 | 工作 | 飞书写入 |
| --- | --- | --- |
| 指定常开电脑 | 固定代码版本、每日采集、同步、私有运行状态 | 唯一活动写入端 |
| 原电脑及其他开发电脑 | 分支开发、模拟测试、必要时只读dry-run | 禁止业务写入 |
| Dot | 继续运行环境验证；验证通过前不接管业务 | 禁止业务写入 |

两台电脑可以同时开发和读取，但不能各自执行带写入的每日任务。操作系统
文件锁只覆盖单机；本项目尚无跨电脑分布式锁和自动故障接管能力。
`runner-host.json` 的设备/用户绑定用于防误操作，不代替切换时停用旧端。
也不能约束人工绕过项目直接调用CLI或在飞书界面修改数据。

## 1. 在目标电脑准备

以将来定时任务的Windows账户完成安装和登录。建议运行目录与开发目录分开，
例如 `D:\ebook-runner` 专门运行，其他目录用于开发，不在运行目录改代码。

```powershell
git clone https://github.com/KAtOReNA7/ebook-.git D:\ebook-runner
Set-Location D:\ebook-runner
python -m unittest discover -s tests -t .
npm install -g @larksuite/cli@1.0.87
lark-cli --version
lark-cli auth --help
```

使用Python3.11或更高版本，记录实际 `python.exe`、`lark-cli.cmd` 路径。
飞书按CLI提示重新授权，确认身份为项目所有者，业务命令使用 `--as user`。
微信读书也重新扫码授权；即使另一台同为Windows，也不能复制DPAPI会话。
需要扫码工具时安装 `requirements-dot.txt`，然后执行：

```powershell
python tools/weread_login.py login --session-file D:\ebook-private\session.dpapi
```

这会生成二维码等待本人确认，不发送测试消息。新设备登录应在正式切换前
安排，并核查是否影响旧会话。目标私有 `config/publisher-newbooks.json`
的 `session_file` 改为此目标路径。无需安卓、模拟器或Chrome。

## 2. 私有状态交接

GitHub只含代码。费用、作品身份、永久注册表、通知去重、原始证据和RUNBOOK
通过私有状态包交接，具体校验/恢复步骤见 [HANDOFF.md](HANDOFF.md)。
包只能私密传输，不传GitHub。目标必须固定到包清单记录的代码提交。

准备期可先交接测试快照；旧端继续运行时，这份快照会过期。正式切换必须
先停止旧调度及写入，再导出最终状态，不能用旧快照覆盖新状态。
状态恢复工具强制关闭通知并将角色设为 `standby`，不会启动定时任务。

**公共政策例外**：导出工具不包含公共政策文件。活动端的
`config/fanqie-ranking.json` 已有用户授权的运行开关，须随私有包另附一份
原字节文件及SHA256。核对后在目标原位恢复，不能使用Git中默认暂停版本。
其他公共政策如有本地差异也先逐项核对，不要批量覆盖。
原通知配置、owner身份及汇总去重必须按最终包保留；仅本人通知，禁止回放。

目标恢复后：

```powershell
python tools/scheduled_runner.py prepare --role standby
python tools/doctor.py
python tools/scheduled_runner.py run --dry-run --cli C:\实际路径\lark-cli.cmd
python scripts/verify_current_base.py
```

`--shadow`会写业务数据，不能代替dry-run。检查所有来源的完整度、新采样
时刻、费用合计、guard、关联和通知健康；同日冻结快照不算目标新采集证明。
目标公网网络、系统代理、飞书和微信会话都要实测。不要为消除错误而删guard。

## 3. 导出并验证原生定时任务

Windows时区设为“(UTC+08:00) 北京，重庆，香港特别行政区，乌鲁木齐”。
工具校验 `China Standard Time`，以下命令只导出**禁用状态**的任务XML，
不会注册、启用任务或触发采集：

```powershell
.\tools\export_windows_task.ps1 `
  -Python C:\实际路径\python.exe `
  -LarkCli C:\实际路径\lark-cli.cmd `
  -Mode DryRun -OutputXml D:\ebook-private\ebook-dry-run.xml
```

在Windows“任务计划程序”中导入XML，使用与登录授权相同的Windows账户。
选择“不管用户是否登录都要运行”，按系统对话框输入该账户密码（不是PIN），
不要把密码发到聊天或写进脚本。不要勾选“不存储密码”，它对应的S4U模式
限制网络和加密文件访问，不能据此保证飞书/DPAPI工作。账户还需批处理登录
权限；若企业策略拒绝，应由管理员配置，不能切换为SYSTEM来绕开用户会话。

先在任务中设置一个临近的一次性触发时间、启用dry-run任务，锁屏并关闭
Codex验证自动执行。最好再测试一次注销后的执行，确认用户凭据可用。
结束后禁用并移除此临时测试任务；正式运行只保留一个每天10点的任务。
如需执行PowerShell导出脚本，只对本次进程使用允许的执行策略，不修改全局策略。

预设设置：错过计划后可执行、网络可用、尝试唤醒、已有实例时忽略重复启动。
不自动重试、不强制杀死正在写入的流程；运行异常先核对guard。唤醒受硬件、
电源策略和供电影响，周末建议接电并关闭自动睡眠，不能只依赖WakeToRun。

## 4. 最后切换为唯一写入端

1. 目标dry-run及全表回读通过；准备好最终代码版本、登录与定时任务。
2. 原端停用Codex每日任务和所有其他调度，确认没有在途采集/写入。
3. 导出最终私有状态及活动政策，目标校验后在新鲜目录恢复；重新绑定目标
   `runner-host.json`，配置路径并确认凭据有效。私有证据不因换路径而改字节。
4. 原端执行 `python tools/scheduled_runner.py prepare --role development`，
   作为防误写措施。若已有active策略，先将其角色明确改为development；
   准备工具不会自动停用active策略。原端以后只模拟测试或dry-run。
5. 目标确认 `runner-host.json` 的设备指纹是本机、`pinned_commit` 是验收
   代码提交，再把 `role` 设为 `active`、`previous_writer_stopped` 和
   `target_dry_run_reviewed` 设为true。这些值必须对应真实完成的切换步骤。
6. 目标 `runtime.json` 恢复原已核实的owner_only及仅本人通知配置，
   `production_approved=false`；不能运行旧激活脚本或恢复双收件人。
7. 用同一导出脚本的 `-Mode Live` 生成正式XML，在任务计划程序导入。
   核对每天10:00、账户、Python、CLI、目录后启用。保留原端停用证据。
8. 首次实际执行后全表回读，检查真实采样与去重；不重放历史outbox。

## 日常查看及发布新代码

每次任务在 `receipts/YYYY-MM-DD/native-runner/<时间-随机号>/` 保存
`result.json` 与 `pipeline.log`；后者是私有业务日志，不能公开上传。
入口返回码0表示流程没有已识别的操作故障，**不代表全部作品都有数据**。
1为流程失败，2通常为待核，3为入口拦截；详细状态看当日
`runs/YYYY-MM-DD/pipeline-summary.json` 和独立全表回读。
任务计划程序的“上次运行时间/结果”还要和这些实际文件时间一起检查。

开发电脑用不同 `codex/…` 分支，模拟测试通过后正常推送、审阅、合并。
运行电脑不在10点任务内自动 `git pull`。升级在无在途任务时备份私有状态，
安全更新到已测试提交、dry-run和回读，通过后更新 `pinned_commit`。
不要用开发电脑的旧runs或旧费用基线覆盖运行电脑。

目标电脑故障时，先停用它并确认没有写入，再带**最新**状态切换到备用端。
不能只因为没收到通知就自动启动另一台电脑。缺少最新状态或存在未知写入时，
先只读对账；不能保证在断电、断网、会话失效等情形下绝不停机。

参考：微软 [任务设置](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasksettingsset)
与 [任务安全上下文](https://learn.microsoft.com/en-us/windows/win32/taskschd/security-contexts-for-running-tasks)。
