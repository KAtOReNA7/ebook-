"""One single-select label retains every current membership, including overlaps."""
from itertools import combinations

BOARD_LABELS = {
    'rising': '出版飙升', 'newbook': '出版新书',
    'novel_female_rising': '女生飙升', 'newbook_novel_female': '女生新书',
}
OFF_BOARD = '未在榜（继续追踪）'


def all_labels():
    names = list(BOARD_LABELS.values())
    return [OFF_BOARD] + ['＋'.join(c) for n in range(1, 5) for c in combinations(names, n)]


def memberships(snapshot):
    """Never clear membership based on a failed, truncated or duplicate chart."""
    charts = snapshot.get('charts', [])
    if len(charts) != 4 or {c.get('chart_id') for c in charts} != set(BOARD_LABELS):
        return None
    labels = {}
    for cid, label in BOARD_LABELS.items():
        chart = next(c for c in charts if c['chart_id'] == cid)
        rows = chart.get('records', [])
        if (not chart.get('complete_top50') or len(rows) != 50
                or {r.get('rank') for r in rows} != set(range(1, 51))
                or len({str(r.get('book_id')) for r in rows}) != 50):
            return None
        for row in rows:
            labels.setdefault('weread:' + str(row['book_id']), []).append(label)
    return {key: '＋'.join(value) for key, value in labels.items()}
