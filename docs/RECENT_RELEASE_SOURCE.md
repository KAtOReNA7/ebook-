# 版权方最近上架列表的采集路径

2026-11-05通过官方客户端安装包静态检查与用户主动扫码登录，已验证只读请求：

```text
GET https://i.weread.qq.com/shelf/opus
userVid=<已核实版权方账号>
count=0
listType=1
listMode=0
album=0
```

`userVid`来自官方作品详情`copyrightInfo.userVid`，与`copyrightInfo.id`版权ID不同。客户端`BaseFriendShelfService.OpusList`声明上述参数；`OpusListViewModel`把类型1显示为“最近上架”或“全部新书”。此为客户端使用的接口，并非有稳定性承诺的公开API。

已用果麦截图验证前8本作品ID及顺序；果麦与读客返回总数也分别匹配截图中的43与29。三家均能返回书目、推荐值、点评人数与列表时间。返回`data[]`和`totalCount`，本次`count=0`取得全部；不能假定所有未来请求都完整，必须逐次核对总数、唯一ID、版权与电子书形式。当前调用链没有游标，读取数量不足时标记不完整，不自行编造分页参数或当作空列表。

## 日期和指标

用户接受最近上架列表作为来源，8、9、10月作品也属于新书口径；随后明确只统计新进入列表的作品，不统计连载更新。首次完整列表建立基线，不把存量书目算作新增。基线之后，首次观察到从未出现过的官方作品ID才建立30日追踪，发现当天为第1天，至第30天（起日加29天）结束。已错过的每日数据不补造。

永久保存已出现ID。同一ID更新章节、改名、排序变化、退出后重返均不算新增、不重置窗口。离开列表后仍继续追踪至窗口结束。来源失败、缺行、重复ID或旧观察不得推进比较基线；现有基线和追踪目标保留。

- `meta.updateTime`仅保存为列表时间。果麦8本及读客4本与详情`onTime`吻合，但微信读书出品的连载存在反例，不能统一当作上架日。
- 详情上架时间仍由`collect_weread.py`读取`reader.bookInfo.onTime`，作为作品资料保留；它和列表更新时间都不用于重置按首次新进入起算的30日窗口。
- `newRating`按现有规则换算推荐值，未开分留空；`newRatingCount`保留实际点评人数。
- `meta.todayCount`保留原始值；不能当作累计阅读人数，也不能跨天求和得到去重累计读者。
- 最近列表移除不等于作品下架，不自动触发下架提醒。

## 运行

`scripts/collect_weread_recent.py`只读取来源并保存独立观察，不写飞书、不发通知、不推进比较基线。每日业务入口由 `scripts/publisher_recent_daily.py` 编排，已经挂入 `daily_run.py`。

```powershell
python scripts/collect_weread_recent.py --providers-file <私有版权方清单.json> --session-file <当前Windows用户的session.dpapi> --out-dir <新的私有观察目录>
```

清单为JSON数组，每项包含`name`、`user_vid`、`copyright_id`。会话来自用户明确扫码授权，使用当前Windows用户的DPAPI加密，不能复制到另一电脑直接使用。授权会话过期时报告来源错误，不用零条覆盖上一次结果。目前未实现自动续期；不同电脑须另行登录。不要把会话、版权方现场清单或原始数据提交Git。

`scripts/recent_entry_tracking.py`实现上述ID差异与30日目标登记，默认预览；`--apply`只持久化本地登记表，使用与日任务相同的操作系统锁。将采集观察传入：

```powershell
python scripts/recent_entry_tracking.py --providers-file <私有版权方清单.json> --observation-dir <采集观察目录> --registry <私有登记表.json> --apply
```

该登记表必须随私有交接包迁移，不能在新电脑重建或清空，否则会把旧书重复当新书。实际检测时间保留原始观察时刻，不反推真实进入列表的精确时刻。

每日入口已连接新进入识别、30日目标、公开详情和明确今日阅读采集、飞书项目与每日数据同步。已有营销或榜单项目按同一官方ID复用，保留原监测范围和原窗口；独立记录首次进入最近上架日和版权方追踪截止日。新项目归类为版权方上新，不冒充营销项目或榜单样本，不新增业务提醒。

累计阅读口径及会话自动续期仍未完成。会话失效会报告来源失败，保持上次基线；既有30日目标继续使用公开来源采样。缺失日数据不补造，首次真实新进入前视图为空属于正常状态。
