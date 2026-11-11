# 开发与运行

2026-11-10用户明确批准现有项目正式运行。当前运行端使用v5共享检查点中的
私有runtime与已核实收件人；通知生成时实时读取，不能在模块导入时缓存旧名单。
新开发部署仍默认shadow，不从示例配置推定生产授权。正式切换先执行
`tools/activate_production.py --authorization <private-json>`生成只读预览，
经核对后添加`--execute`；该入口只用于明确授权的当日切换，不作为每日任务。
它保持共享锁、工作流全量回读、未知写入保护和独立一次性通知键。
日常继续使用原入口，不执行历史activate_verified_rules.py。

2026-11-09起，现有业务checkout默认采用[共享运行状态](SHARED_RUNTIME.md)。
每日入口自动从私有飞书恢复状态并取得全局锁，不再绑定一台固定运行电脑；
下面的历史“唯一活动电脑”应理解为同一时刻唯一持锁实例。开发通常运行模拟测试或dry-run；
经更新、登录的电脑均可通过共享每日入口执行。不得直接运行同步脚本绕过共享入口。
`gh`登录与仓库写权限用于执行互斥，业务数据不上传Git。

## 配置与凭据

现有 Windows 采集不依赖第三方 Python 包。Linux/Dot 的可选加密会话及扫码工具使用 `python -m pip install -r requirements-dot.txt` 安装依赖；CI 同时测试该适配。外部读写使用独立安装的 `@larksuite/cli`，始终以 `--as user` 操作。可以先按已验证版本安装：

```powershell
npm install -g @larksuite/cli@1.0.87
lark-cli --version
lark-cli auth --help
```

按 CLI 自带帮助完成本机用户登录。不要复制旧电脑的 token、浏览器 Cookie、系统凭据库或整个 `.codex` 目录。如果程序找不到 CLI，把 `EBOOK_LARK_CLI` 设为实际可执行文件的绝对路径；变量中不能添加命令参数。

真实 `config/local-system.json` 保存源表 URL、已核实 owner 的 open ID 和姓名。真实 `config/base-manifest.json` 保存现有表、字段、视图 ID。示例文件故意不可用于线上运行；恢复私有状态包后再执行 `python tools/doctor.py`。

版权方会话默认保留 `.dpapi`；目标 Linux 可独立授权成 `.aesgcm`，通过
`EBOOK_WEREAD_KEY_FILE` 指向仓库外的私人密钥文件，详见 [Dot 交接](DOT_HANDOFF.md)。
不得直接复制旧设备登录。模拟测试使用合成凭据，不读取真实会话。

版本控制仅包含公开政策和示例配置。真实费用、原始留档、个人身份、源行确认、平台身份映射和去重注册表全部被 `.gitignore` 排除。提交前查看 `git diff --cached`；不要使用 `git add -f` 把这些数据提交到公开仓库。

## 日常入口

```powershell
# 本地准备、依赖与证据摘要；不联网、不写飞书
python tools/doctor.py

# 已恢复现场状态且已本机登录后：读取来源和飞书，生成本地计划
python scripts/daily_run.py --run-kind local --dry-run

# 仅由唯一活动运行电脑的每日10点任务执行
python scripts/daily_run.py --run-kind scheduled

# 只读全量回读当前Base，另存本地审计收据
python scripts/verify_current_base.py
```

`--dry-run` 会进行真实读取并生成本地留档，但不变更多维表格。`--shadow` 仅抑制业务通知，**仍会写业务数据**，不能作为只读检查。完成的同日来源可能复用冻结观察；需要补采时另存新观察并组合有效观察，不能覆盖原始证据或伪造 10 点时间。

执行顺序：手工新项目初始化 → 腾讯完整源表 → 微信四榜和作品 → 番茄网页 → 番茄出版飙升榜（按本机授权配置） → 版权方最近上架 → 新项目事件去重 → 营销费用变化核对 → 业务比较 → 当日独立Base完整回读 → 两条日报发布检查与投递 → 通知健康检查 → 共享检查点提交及释放锁。独立来源失败不抹去其他来源已取得的数据。进程成功不等于所有待核作品都已覆盖。

`scripts/report_publication.py`必须确认全部要求的模块与嵌套比较成功，实际观察数量一致、费用与关联核对通过、五个榜单完整且版权核实。`run_report_validation`拒绝旧回读收据；`daily_reports.flush`在创建任何通知事件前再次检查。任一失败，两条均零投递，仅保留私有失败原因，向负责人报告后处理，不能用“未核实”正文代替阻断。已知未开分不编造数值；尚未核实ID的书目不冒充已采集。

2026-11-11负责人要求暂停公司日报。私有共享runtime的`report_publication_hold.active=true`具有最高优先级，底层通知写入也执行该限制；原生工作流同时停用。采集继续，验证通过、日期变化、恢复网络都不构成恢复通知的授权。恢复必须有负责人明确指令，先更新所有参与运行端并核对共享政策、事件和工作流；不补发历史或再次执行正式上线重发。见[事故分析](REPORT_PUBLICATION_INCIDENT_20261111.md)。

版权方模块为 `scripts/publisher_recent_daily.py`，沿用统一锁；只读试跑使用 `--dry-run`。`config/publisher-newbooks.json` 指定私有版权方清单、DPAPI会话与永久ID注册表。首次部署必须恢复既有注册表，不能静默重新建基线。未知写入保护使用 `publisher-recent-state.json`；Base回读完成后才提交注册表，重复执行当天已完成模块不写入。认证失败仍继续既有目标的公开指标采集，源比较基线冻结。当前累计阅读定义未核实，不用今日阅读累计代替。

## 修改入口

### 事件队列与日报

活动端的私有 `config/notification-grouping.json` 启用后，各来源只把当前新事件写入当日 `notification-digest-queue.json`。现行投递由`config/daily-notification-format.json`和`daily_reports.py`生成两条日报，不再分别发送上榜、下榜、营销、90%等旧分类通知。即使无变化，也只有全部发布检查通过且未暂停时才发送两条。使用现有原生工作流和共享已核实收件人限制，无直接IM。

通知事件表每条汇总保留全部关联项目、成员事件唯一键和原判定证据，正文保留逐项变化及链接。日期+类别唯一键禁止复跑重复发送；既有单条通知不回放。某类已发送后又出现补采事件时保留队列并报告待核，不自动再发第二条。超长正文不截断、不拆条，报告待核。跨日未发送队列及未知写入必须先对账，不自动补发。单独运行来源脚本只入队，投递由统一每日入口完成。调试/只读预览不消费队列；`notification-digest.apply-state.json`保护未知投递结果。

| 目的 | 文件 |
| --- | --- |
| 全流程、锁、冻结观察、隔离与通知时机 | `scripts/daily_run.py` |
| 腾讯解析、准入与源行核对 | `collect_tencent.py`、`parse_tencent.py`、`prepare_tencent_rows.py`、`sync_marketing.py`、`tencent_daily.py` |
| 微信 / 番茄采集与同步 | `collect_weread.py`、`sync_weread.py` / `collect_fanqie.py`、`sync_fanqie.py`、`fanqie_daily.py` |
| 全量飞书读写、分页与回执 | `scripts/feishu_io.py` |
| 标题、标签、摘要展示 | `scripts/presentation.py` |
| 通知安全边界、费用通知与健康 | `notification_policy.py`、`marketing_expense_notifications.py`、`notification_health.py` |
| 历史绝对路径跨电脑解析 | `scripts/local_settings.py` |
| 受控身份接入与归并 | `link_project_identities.py`、`merge_project_aliases.py` |

上表省略目录的文件均位于 `scripts/`。受控身份/归并脚本须先生成与核对计划；默认不运行修复、清理或历史迁移。未知写入与 `requires_review` guard 必须完整只读对账，不能删除 guard 后重试。

## 验证

```powershell
python -m unittest discover -s tests -t .
git diff --check
```

测试不能证明线上全部覆盖。授权变更之后，还需读回相关全表核对记录 ID、关联、日期、服务费及去重键。标签按字段 ID 比较，版权/分类原文与展示简称分开保留。更改字段名时同步维护 `presentation.py`，不要只手动修改飞书表头。

CI 在 Windows 和 Linux 上运行模拟测试，既不恢复私有数据，也不获取飞书登录或设置定时采集。GitHub 上传、开发测试和生产验收是不同步骤。
