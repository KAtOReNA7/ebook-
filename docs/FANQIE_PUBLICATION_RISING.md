# 番茄出版飙升榜：只读采集验证

2026-11-05 用户明确要求继续研究出版飙升榜。本机已从官方服务取得榜单，
新增 `scripts/collect_fanqie_rising.py`，仅保存本地观察和 CSV。
现有每日任务的暂停配置保持原状；本入口不连接飞书、不更新通知或追踪注册表。
原运行电脑的更新步骤和剩余业务接入要求见 [原电脑接续说明](FANQIE_RISING_HANDOFF.md)。

## 已验证路径

1. 获取官方书城目录：
   `GET https://api5-normal-sinfonlineb.fqnovel.com/reading/bookapi/bookmall/tab/v`
2. 从返回的 `cell_url` 逐层解析 `url`、`surl` 导航参数，找到
   `main_algo_name` 中的“出版飙升榜”，按同一位置对应 `main_algo_type`。
   本次对应值为 `197`，目录返回的榜单 cell ID 也随观察留档，不猜 ID。
3. 使用该 cell ID 和算法请求：
   `GET https://api5-normal-sinfonlineb.fqnovel.com/reading/bookapi/bookmall/cell/change/v1/`
4. 从 `data.cell_view.book_data` 读取结果。名次来自每本书
   `recommend_info` 中的 `rank`，并与返回顺序交叉核对。

官方依据还包括：

- [官网加载的脚本](https://lf-fe.fqnovelstatic.com/obj/novel-fanqie-fe/toutiao/muye/js/muye_812aa168.js)
  包含 `PublishRankListRaise=197`。
- [官方 H5 榜单页面](https://reading.snssdk.com/reading_offline/drweb/page/rank-list.html)
  加载的共享脚本中，模块 76687 使用上述 `cell/change/v1/` GET 接口。
- 当前目录实际返回的中文榜名和算法对应关系、榜单响应中的“飙升值”标签，
  共同确认目标身份；仅有一个数字枚举不够。

第三方开源代码仅帮助定位请求路径；榜单条目与身份判断均来自本机请求的官方响应。
未安装或启动 App、模拟器、WSL、签名服务；请求无需账号、Cookie、设备注册 ID。
这是已观察到的内部实现，不是有稳定性承诺的公开 API。

## 兼容性与完整性

请求携带代码中公开列出的客户端版本兼容参数。本次发现仅有 `version_code`
和 `version_name` 会取得不包含出版榜的旧目录；补充 `manifest_version_code`
和 `update_version_code` 的组合后取得包含目标榜的目录。尚未单独判定两者各自作用。
不需要复制设备型号、设备 ID 或账号身份。

本次请求的 `limit=12` 没有限制返回条数：服务实际一次给出 100 本，
`has_more=false`，名次 1–100 连续，100 个作品 ID 唯一，全部为出版电子书。
采集器不假定请求 limit 就是实际榜长，也不凭空构造分页参数。

目前成功判定要求：成功响应、目录与返回算法一致、未过滤的日榜、100 本、
终止标记明确、名次连续、作品 ID 唯一且为字符串、电子书标记与出版类型明确、
每本具有“飙升值”标签。任一变化都失败并保留原始响应，不能当作空榜或触发下榜事件。
100 是本轮核实的预期大小；若平台改变榜长，需要先核实再修改。

`rank_score` 原样保存为飙升值，`sub_info` 原样保存为读者标签；不把飙升值解释为
累计人数或新增人数。平台没有给出明确榜单更新时间，`source_updated_at` 保持空值，
记录实际请求时间。本轮尚未与同一时刻的官方 App 画面逐名对照，也未验证跨日稳定性。

## 运行和输出

```powershell
.venv\Scripts\python.exe scripts/collect_fanqie_rising.py
```

默认写入新的 `snapshots/fanqie-rising/<实际时间>/`：

- `directory.json`、`ranking.json`：官方原始响应。
- `*.source.json`：请求 URL、实际采样时间、HTTP 状态、响应 SHA256。
- `snapshot.json`：经过校验的榜单与来源；失败则明确 `complete=false`。
- `ranking.csv`：成功时生成，包含名次、作品 ID、书名、作者、飙升值等。

观察与研究留档由已有 `.gitignore` 排除，不提交公开仓库。
CSV 中的长作品 ID 应按文本导入，避免电子表格软件截断数字。
接入每日流水线、上下榜比较和飞书写入属于后续工作，需要现有业务状态核对。
