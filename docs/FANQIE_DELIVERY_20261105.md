# 2026-11-05 番茄核验交付

供原电脑在 2026-11-06 接续。代码已交付，业务数据尚未由本机写回；原电脑仍是唯一业务写入端。

## 已确定的结果

| 项目 | 本轮状态 |
| --- | --- |
| 线上营销电子书 | 98 本，其中 34 本已有番茄 ID，64 本待核 |
| 新确认身份 | 2 本，映射与证据已留在私有包，尚未写回 |
| 本地剩余待核 | 62 本，含新发现但未完成身份确认的 1 本候选 |
| 原始截图核验 | 已视觉查看 56 张，完成 24 条补充检索；OCR 全文未逐字人工确认 |
| 网页搜索 | 匿名接口 HTTP 200 空体，浏览器搜索未成功；不能解释为零结果 |
| 同名多版本 | 详细核验尚未完成，不自动选择版本 |
| 出版飙升榜 | 只读采集器和每日模块已入库，仍暂停，通知关闭 |

新确认的两本缺少明确累计阅读标签，保留缺失，不用在读量代替。新候选不进入已确认
映射，不减少待核数量。完整名单、作品 ID、截图和响应只在私有研究包中。

## 原电脑更新代码

在现有项目目录执行；工作区有未提交修改时先核对，不能 reset、clean 或强制覆盖：

```powershell
git status --short
git fetch origin
git pull --ff-only origin main
python -m unittest discover -s tests -t .
python tools/check_public_tree.py
python tools/doctor.py
```

保留原电脑的 RUNBOOK、真实配置、锁、未知写入 guard、身份映射及通知注册表。
doctor 发现现场依赖缺失时先补齐，不以示例配置替代。本次交接电脑缺少现场状态，
未执行线上部署或每日任务。

## 接收私有研究包

另行私下复制 `fanqie-research-handoff-20261105.zip` 及同名 `.zip.sha256` 文件。
它不在 Git 中：仅拉取仓库能取得工具，不能取得新增两本的私有身份映射。

1. 用 `Get-FileHash <包路径> -Algorithm SHA256` 与随包校验文件比对。
2. 解压到新的临时目录。包内 `research-handoff-manifest.json` 记录每个文件的 SHA256，
   可用随包 `verify_research_bundle.py` 在解压目录运行校验；它不联网、不写 Base。
3. 将包内 `research/fanqie-coverage-20261105` 复制到原项目相同相对位置。
   如果已有同名目录，先比较文件哈希，不覆盖不同内容。
4. 阅读其中 `HANDOFF.md`，使用 **reviewed-coverage-v2/identities.json**（两本版本）。
   包是研究补充，不能当作完整运行状态包交给 `tools/handoff.py restore`。
5. 如项目根路径不同，在原 `config/workspace-origin.json` 的 `roots` 数组追加
   `D:/porject/ebook`，保留已有条目。项目的历史路径解析器会定位相同相对文件；
   不批量修改证据正文或重算原始来源哈希。

## 接入顺序

确认原电脑是唯一写入端，先只读核对所有 unknown / incomplete / requires_review guard。
然后生成身份链接计划：

```powershell
python scripts/link_project_identities.py --identity-map research/fanqie-coverage-20261105/reviewed-coverage-v2/identities.json
```

核对两本映射的来源、项目记录、服务关联与 ID 碰撞，按既有授权追加 `--apply` 执行并
完整回读。将补充映射路径追加到原 `config/tencent-runtime.json.identity_maps`，保留旧条目。
确认回读成功后才能更新覆盖数字；不要提前写成线上 36/98。

随后按 [开发说明](DEVELOPMENT.md) 使用 `daily_run.py --run-kind local --dry-run`
检查每日计划，沿用原电脑唯一的北京时间 10 点调度。身份补充不产生新营销交易，
不修改历史金额、日期，不重放 outbox，不发送测试消息。旧截图和今晚的观察仅作证据，
明日指标按实际采样时间重新取得。

出版飙升榜继续暂停，不修改配置解除暂停，不另建调度，不启动 App、Android 或模拟器。
其代码接续方法见 [榜单交接](FANQIE_RISING_HANDOFF.md)。

## 可直接交给原电脑的任务文本

> 拉取 main，阅读 docs/FANQIE_DELIVERY_20261105.md 和私有研究目录 HANDOFF.md。
> 保留现场全部配置与注册表；验证研究包哈希及两本 v2 映射的依赖证据。先核对未知 guard，
> 再生成默认只读身份链接计划；核对无冲突后按既有授权链接并完整回读，追加映射配置，
> 用 dry-run 检查下一次每日计划。沿用现有唯一 10 点任务，出版飙升榜保持暂停。
> 继续核验剩余 62 本时，从包内视觉笔记和新增候选开始，不把候选当成已确认身份。
