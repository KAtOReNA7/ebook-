# 跨电脑接续现有项目

## 只开发代码

克隆仓库、安装 Python、运行单元测试即可。GitHub 不含现场私有数据，也没有自动部署或日常采集工作流。

## 连同现有运行状态迁移

必须同时保留源行证据、真实平台 ID、首次入表时间、当前有效观察、未完成写入 guard、里程碑与通知注册表。仅复制代码和 Base URL 会丢失这些边界，可能产生重复或历史提醒。

1. 在原电脑没有采集/同步运行时导出。工具使用与每日入口相同的两把操作系统锁，拒绝未完成写入 guard；不会停用现有定时任务。

   ```powershell
   python tools/handoff.py export --output backups/ebook-private-state.zip
   python tools/handoff.py inspect --archive backups/ebook-private-state.zip
   ```

   返回的 SHA256 用于核对传输。包包含私有费用与业务证据，**不得上传 GitHub、公开网盘或粘贴到聊天**。通过你选择的私密文件传输方式移交，并在另一端核对摘要。包不加密；传输与保存位置需由所有者控制。工具不读取外部凭据目录，不导出登录信息、环境文件、模拟器、安装包或依赖环境。

2. 在新电脑克隆同一代码版本，恢复到全新的克隆目录：

   ```powershell
   python tools/handoff.py restore --archive D:/private/ebook-private-state.zip --destination D:/project/ebook-
   ```

   恢复先核对全部文件的路径、长度和 SHA256，拒绝路径越界、重复条目与冲突文件。保留证据原始字节；`workspace-origin.json` 将旧工作目录映射到当前目录，不通过改写证据来适配路径。历史审计文件仅供查询，绝不能回放 outbox 或执行旧系统。

   恢复后强制 `mode=shadow`、`notifications_enabled=false`、`production_approved=false`，不会创建定时任务或发送消息。`--shadow` 本身仍会写业务数据，所以此时继续使用 `--dry-run`。

3. 在新电脑安装/登录飞书 CLI，然后运行：

   ```powershell
   python -m unittest discover -s tests -t .
   python tools/doctor.py
   python scripts/daily_run.py --run-kind local --dry-run
   python scripts/verify_current_base.py
   ```

   检查三来源状态、完整榜单、真实采样时间、费用合计、待核覆盖与通知健康。`doctor` 只检查本地依赖，不代表已登录、数据全覆盖或可以启用另一台写入电脑。

4. **切换唯一运行电脑时，先停止旧电脑的每日任务，再制作最终状态包。** 测试用包可能已落后于旧电脑的新采样；最终接续必须在新鲜克隆中恢复最终包，核对注册表和线上数据，不能用旧包覆盖后来状态。操作系统文件锁只能保护本机，不能协调两台电脑。

5. 由所有者选择新电脑作为唯一运行端之后，才配置每天北京时间 10:00 的任务。当前项目仍未正式验收，恢复调试通知也只能是已核实 owner；不得扩大收件人或回放历史事件。先检查飞书原生工作流的真实收件人，直接新增项目工作流继续关闭。GitHub 仓库不会自动更改这些开关。

## 故障与限制

- 不删除锁文件或 guard 来“恢复”。无法确认上次写入结果时，先全量只读对账。
- 恢复不是原地覆盖/升级工具。冲突时保留现有目录，换一个新鲜克隆；不要 `reset --hard`、`clean` 或强推。
- 私有包提供运行数据/证据，不包含账号登录，也不自动授予新电脑访问飞书的权限。
- 当前番茄出版飙升榜仍暂停。身份待核作品不能因迁移被写成已采集；未接通平台指标保持空并显示原因。
