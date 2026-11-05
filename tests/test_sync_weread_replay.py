from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import sync_weread


class SyncWeReadReplayTests(unittest.TestCase):
    def sync_fixture(self, missing_score=False):
        captured = {}
        day = '2026-11-05'
        class FakeBase:
            changes = 0
            manifest = {'base': {'url': 'https://example.invalid/base'}}

            def __init__(self, *_): pass

            def _fields(self, table): return {}

            def read(self, table, *_):
                if table == '项目总表':
                    return [{'作品唯一键': 'weread:123', 'record_id': 'project',
                             '作品形式': '电子书', '监测范围': ['仅榜单样本']}]
                if table == '每日数据':
                    return [{'日数据唯一键': day + ':weread:123', '采集时间': day + 'T10:34:00+08:00',
                             '缺失字段与原因': None}]
                return []

            def ensure(self, table, key, rows, *_args, **_kwargs):
                captured[table] = rows
                return {'weread:123': {'record_id': 'project'}} if table == '项目总表' else {}

        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'snapshot.json'
            source.write_text(json.dumps({'schema_version': 'weread.public.v1',
                'collection_date': day, 'started_at': day + 'T10:34:00+08:00',
                'finished_at': day + 'T10:38:00+08:00', 'field_mapping': {}, 'charts': [],
                'books': [{'book_id': '123', 'title': '测试作品', 'author': '测试作者',
                    'word_count': 100, 'copyright_provider': '测试版权', 'category': '小说/悬疑',
                    'reviewer_count': 10, 'recommendation_percent': None if missing_score else 95,
                    'today_reading_count': 20, 'detail_url': 'https://example.invalid/book/123',
                    'detail_source': {'observed_at': day + 'T10:34:00+08:00'}}]}, ensure_ascii=False), encoding='utf-8')
            with patch.object(sync_weread, 'ROOT', root), patch.object(sync_weread, 'Base', FakeBase), \
                    patch('sys.argv', ['sync_weread.py', '--source', str(source)]), redirect_stdout(io.StringIO()):
                sync_weread.main()
        return captured['每日数据'][0]

    def test_complete_frozen_observation_replays_null_reason_not_empty_string(self):
        row = self.sync_fixture()
        self.assertIsNone(row['缺失字段与原因'])
        self.assertEqual(row['采集时间'], '2026-11-05 10:34')
        self.assertEqual(row['今日阅读人数'], 20)

    def test_unrated_observation_keeps_explanation_and_null_score(self):
        row = self.sync_fixture(missing_score=True)
        self.assertEqual(row['缺失字段与原因'], '推荐值：未开分')
        self.assertIsNone(row['推荐值'])


if __name__ == '__main__':
    unittest.main()
