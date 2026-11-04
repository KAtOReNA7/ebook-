"""Keep one latest actual observation per book/day; never backdate or regress it."""
from datetime import datetime


def select_daily_observations(incoming, current):
    index={r['日数据唯一键']:r for r in current}
    result=[]
    def observed(value):
        # Sync payloads use local minute timestamps; API readback includes +08:00.
        parsed=datetime.fromisoformat(value)
        return parsed.replace(tzinfo=None)
    for row in incoming:
        old=index.get(row['日数据唯一键'])
        if old and observed(row['采集时间']) < observed(old['采集时间']):
            continue
        # Explicit nulls clear metrics that became unavailable on a newer sample.
        result.append(row if old else {k:v for k,v in row.items() if v is not None and v!=''})
    return result
