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

用户接受最近上架列表中8、9、10月的作品，不再因列表内作品不是当天上架而排除。已错过的每日数据不补造。

- `meta.updateTime`仅保存为列表时间。果麦8本及读客4本与详情`onTime`吻合，但微信读书出品的连载存在反例，不能统一当作上架日。
- 真正的详情上架时间仍由`collect_weread.py`读取`reader.bookInfo.onTime`。建立按上架起算的30日窗口前，必须完成这一项核对。
- `newRating`按现有规则换算推荐值，未开分留空；`newRatingCount`保留实际点评人数。
- `meta.todayCount`保留原始值；不能当作累计阅读人数，也不能跨天求和得到去重累计读者。
- 最近列表移除不等于作品下架，不自动触发下架提醒。

## 运行

`scripts/collect_weread_recent.py`只读取来源并保存独立观察，不写飞书、不发通知、不推进比较基线，尚未挂入每日业务入口。

```powershell
python scripts/collect_weread_recent.py --providers-file <私有版权方清单.json> --session-file <当前Windows用户的session.dpapi> --out-dir <新的私有观察目录>
```

清单为JSON数组，每项包含`name`、`user_vid`、`copyright_id`。会话来自用户明确扫码授权，使用当前Windows用户的DPAPI加密，不能复制到另一电脑直接使用。授权会话过期时报告来源错误，不用零条覆盖上一次结果。目前未实现自动续期；不同电脑须另行登录。不要把会话、版权方现场清单或原始数据提交Git。

正式接入仍需完成详情日期校验与30日窗口规则、累计阅读口径、会话续期，以及每日入口和飞书同步；源列表已连通不表示这些步骤已完成。
