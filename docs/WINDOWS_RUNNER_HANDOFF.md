# Windows常开电脑与并行开发

现行方案使用全局互斥锁和私有飞书共享检查点，不固定某一台电脑。
两台已更新、已授权的电脑都可触发每日入口；同一时刻只允许一个写入者。
每日进度自动恢复，不要求搬运状态包。详见[SHARED_RUNTIME.md](SHARED_RUNTIME.md)。

## 1. 首次配置

使用运行定时任务的Windows账户，在独立运行目录准备Python3.11以上、飞书CLI、
GitHub CLI及仓库访问权限。账号登录不能由另一台电脑的DPAPI密文代替。
Base清单和本地账号身份是首次配置；业务状态以后从共享检查点恢复。

```powershell
git clone https://github.com/KAtOReNA7/ebook-.git D:\ebook-runner
Set-Location D:\ebook-runner
python -m unittest discover -s tests -t .
npm install -g @larksuite/cli@1.0.87
lark-cli auth --help
gh auth login
```

飞书使用 `--as user`，确认本人身份及项目Base权限。微信读书需本机扫码，
扫码工具依赖见requirements-dot.txt，运行：

```powershell
python tools/weread_login.py login --session-file D:\ebook-private\session.dpapi
```

本机config/publisher-newbooks.json的session_file设为上述本地路径。
共享上新业务配置不会覆盖这个路径。登录是否挤掉旧会话须实测；会话失效不能被视为空列表。
无需安装安卓、模拟器或Chrome。凭据、RUNBOOK和业务收据不得进入Git。

## 2. 验证自动接续

```powershell
python tools/check_shared_runner.py --online
python tools/scheduled_runner.py run --dry-run --cli C:\实际路径\lark-cli.cmd
python scripts/verify_current_base.py
```

只读诊断检查依赖、共享锁、检查点完整性和未知写入保护，不取得锁、不写Base。
诊断通过不代表所有来源会话有效；daily dry-run才验证采集和计划。
同日已有冻结样本不算目标设备新采集证明。首次部署还要核对真实来源网络访问。

仅 `--dry-run` 防止Base写入，`--shadow`仍会写入。共享模式无需将runner-host.json
切换为active。番茄榜单开关、上新业务政策、通知格式、版权核验、追踪及通知去重状态
均从共享检查点恢复。HANDOFF.md离线包仅用于首次建机/审计，不是每日前提。
不支持共享锁的旧程序必须停用；v1/v2共享客户端遇到v3检查点会拒绝业务执行，须更新代码。

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

## 4. 启用并验收

1. 使用支持共享检查点v3的已测试版本，完成独立登录、只读诊断和dry-run。
2. 按上节验证Windows调度在关闭Codex、锁屏后的实际运行，必要时验证注销状态。
3. 使用导出脚本的 `-Mode Live` 生成正式XML，核对账户、路径与每日10:00后启用。
4. 每台电脑只配置一个每日调度，避免同机Codex和OS重复触发；跨机竞争由全局锁处理。
5. 首次真实执行核对共享HEAD提交、锁释放、各模块完整度和飞书回读。

这不是固定主机切换，不需要每日导出最终状态。调试状态保持production_approved=false，
通知仅本人；不能恢复旧双收件人配置或回放历史outbox。

## 日常运行与代码升级

操作系统任务必须保持电脑开机联网；关机不能执行。另一台可接续，不补造错过的历史数据。
日志在receipts/YYYY-MM-DD/native-runner/，返回码0不代表所有作品覆盖完整。
结合pipeline-summary.json、共享HEAD与实际飞书回读判断完成程度。

开发用独立分支或目录；运行目录不得在采集中修改。每日入口不自动git pull。
升级前确认无在途进程、保留私有文件和本地差异，安全更新到已测试版本，再进行dry-run。
不reset、clean、强推或恢复旧stash，也不以旧runs覆盖共享业务进度。

正常跨机续接无需人工交接。若进程在写入中断电、状态不明，全局锁会保留，
需要依据共享状态和实际飞书记录对账后恢复；不能按超时抢锁或把无通知当成可重发。
GitHub、飞书不可达或账号失效也会阻断相应阶段，不保证任何故障下绝不停机。
