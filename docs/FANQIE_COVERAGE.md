# 番茄营销作品网页核验

完整作者、分类不代表番茄ID或动态指标接通。以当前 Base 的电子书营销项目为范围，
分别报告已接入、待核、已找到候选、证据确认但尚未写入。

## 可用路径与边界

1. 完整书名及作者检索番茄官方页面。只使用实际观察到的链接，不构造作品ID。
2. 官网作者页、评论站点地图及关键词页可以发现书目；关键词推荐会变动，
   搜索摘要与当前页面不一致时不能沿用摘要当作当前证据。
3. 回看营销服务原始截图，检查改名、副标题、作者和封面。腾讯文档公开图片使用
   正常 `https://docs.qq.com/` 来源请求；403不能当作图片已失效或来源不存在。
4. 用已有 `collect_fanqie.py` 核对官方PC书目和分享页的真实ID、完整标题，保留
   原始响应与时间。相同书名、作者可能有多个版本，不能默认取第一个。

官方搜索接口返回HTTP200但内容为空时，记录为不可用，不解释为搜索无结果。
不启动 App、Android、模拟器或新环境。查不到保留待核，不能猜ID或填零。

## 生成可复核的本地交接

`build_fanqie_coverage.py` 接收以下私有留档：

- `--projects`：标准化项目行列表，包含记录ID、作品唯一键、书名、作品形式、平台作品ID。
- `--candidates`：实际发现的 `source_title / book_id / discovery_sources` 列表。
- `--observations`：已有采集器的候选观察列表，可重复传入，每个ID只保留一份观察。
- `--source-reviews`：`resolve_fanqie_projects.py` 支持的截图人工核验，绑定图片SHA256。
- `--output-dir`：新的私有输出目录。

```powershell
python scripts/build_fanqie_coverage.py --projects research/private/project-rows.json --candidates research/private/candidates.json --observations research/private/observations.json --source-reviews research/private/reviews.json --output-dir research/private/reviewed
```

输出 `coverage.json`、逐本 `REPORT.md` 和共享格式 `identities.json`。只有满足现有
严格匹配规则的作品进入映射；另一个候选没有采集成功时不能静默选择已取得的候选。
同一平台ID对应多个待核行或已有正式项目时转入人工归并，不直接改键。

截图只有改名卡片时，还可人工核对其独特长段简介与官方分享页简介：至少40个
非标点字符，绑定原始图片、官方响应SHA256及请求中的真实book_id，并要求营销
源完整标题与官方书目一致。仅名称相近、通用宣传语或简介不一致都不能通过。
该规则用于作品身份，不将截图读者数或评分作为当前指标。

这一步不写飞书，也不把历史截图数值生成当日样本。所有营销名单、截图、映射和
采集证据保留在忽略目录，不提交公开Git。

## 原电脑接入

保留既有身份映射，在唯一活动写入电脑核对未知 guard，并生成计划：

```powershell
python scripts/link_project_identities.py --identity-map research/private/reviewed/identities.json
```

核对项目记录ID、服务关联及碰撞项后，按既有授权在同一电脑执行 `--apply`；
脚本完成全量回读。将补充映射加入原 `config/tencent-runtime.json` 的 `identity_maps`
列表，保留原有条目；源费用金额、日期和服务形式不变，不当作新交易通知。
之后按真实日期重新采集已匹配ID；过往证据只用于身份核验，不回填为今天的指标。

另一台电脑未停止写入或缺失现场状态时，不在新电脑执行 apply、shadow 或 scheduled。
`--shadow` 仍会写业务数据。不要为了身份补充重建旧系统或替代 Base。
