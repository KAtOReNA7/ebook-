import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, Mock

from scripts.shared_runtime import Mutex, Store, capture, restore, digest, safe_path, TABLE, commit_checkpoint


class FakeGit:
    def __init__(self):
        self.commits = {'root': {'message': json.dumps({'schema': 'ebook-mutex.v1', 'phase': 'idle'}),
                                'tree': {'sha': 'empty'}, 'parents': []}}
        self.current = 'root'

    def __call__(self, repo, endpoint, payload=None):
        if endpoint.startswith('ref/'): return {'object': {'sha': self.current}}
        if endpoint.startswith('commits/'): return self.commits[endpoint.split('/')[-1]]
        if endpoint == 'commits':
            sha = str(len(self.commits))
            self.commits[sha] = {**payload, 'tree': {'sha': payload['tree']}}
            return {'sha': sha}
        if endpoint.startswith('refs/heads/'):
            assert payload['force'] is False
            if self.commits[payload['sha']]['parents'] != [self.current]:
                raise RuntimeError('not fast forward')
            self.current = payload['sha']
            return {'object': {'sha': self.current}}
        raise AssertionError(endpoint)


class MemoryBase:
    def __init__(self): self.rows = {}; self.fail_chunks = False
    def read(self, _): return copy.deepcopy(list(self.rows.values()))
    def ensure(self, table, key, rows, update=False):
        if self.fail_chunks and any(r[key] != 'HEAD' for r in rows): raise RuntimeError('interrupted')
        for row in rows: self.rows[row[key]] = {**row, 'record_id': row[key]}


class SharedRuntimeTests(unittest.TestCase):
    def test_reviewed_tencent_replacement_keeps_both_frozen_sources(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);day=root/'runs/2026-11-13'
            original=day/'tencent-source';reviewed=day/'tencent-source-reviewed'
            original.mkdir(parents=True);reviewed.mkdir()
            (original/'raw.jsonp').write_text('original frozen response',encoding='utf-8')
            (reviewed/'raw.jsonp').write_text('reviewed frozen response',encoding='utf-8')
            (reviewed/'filtered-rows.json').write_text('{}',encoding='utf-8')
            (day/'tencent-daily-import-plan.json').write_text(json.dumps({
                'source_file':'runs/2026-11-13/tencent-source-reviewed/filtered-rows.json'}),encoding='utf-8')
            snapshot=capture(root,'2026-11-13')
            self.assertEqual(snapshot['files']['runs/2026-11-13/tencent-source/raw.jsonp']['text'],'original frozen response')
            self.assertEqual(snapshot['files']['runs/2026-11-13/tencent-source-reviewed/raw.jsonp']['text'],'reviewed frozen response')
            (reviewed/'uncommitted.json').write_text('{}',encoding='utf-8')
            restore(root,snapshot,'2026-11-13')
            self.assertFalse((reviewed/'uncommitted.json').exists())
            self.assertEqual((reviewed/'raw.jsonp').read_text('utf-8'),'reviewed frozen response')

    def test_failed_ownership_read_retains_exact_acquisition_for_reconciliation(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'acquire.json';git=FakeGit()
            mutex=Mutex('test/repo',git,receipt_path=path)
            with patch.object(mutex,'assert_owned',side_effect=PermissionError('stale ownership response')):
                with self.assertRaises(PermissionError):mutex.acquire()
            receipt=json.loads(path.read_text(encoding='utf-8'))
            actual=json.loads(git.commits[git.current]['message'])
            self.assertEqual(receipt['phase'],'requires_review')
            self.assertEqual(receipt['token'],actual['token'])
            self.assertEqual(receipt['parent_sha'],'root')
            self.assertEqual(receipt['acquired_sha'],git.current)
            self.assertEqual(actual['phase'],'busy')
            mutex.assert_owned();mutex.release()
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['phase'],'finished')

    def test_uncommitted_local_completed_cache_is_quarantined_not_reused(self):
        with TemporaryDirectory() as d:
            root=Path(d);directory=root/'runs/2026-11-11';directory.mkdir(parents=True)
            marker=directory/'daily-state.json';marker.write_text('{"phase":"finished"}')
            local=directory/'weread.json';local.write_text('{"books":[]}')
            staging=directory/'weread.collecting.json';staging.write_text('{"books":[]}')
            old=root/'runs/2026-10-01';old.mkdir();(old/'weread.json').write_text('{}')
            (old/'notification-digest-queue.json').write_text('{"events":{"stale":{}}}')
            checkpoint={'schema':'ebook-shared-checkpoint.v6','source_root':str(root),'day':'2026-11-10','files':{}}
            restore(root,checkpoint,'2026-11-11')
            self.assertFalse(marker.exists());self.assertFalse(local.exists())
            self.assertFalse(staging.exists())
            saved=list((root/'backups').glob('shared-cache-*/uncommitted/runs/2026-11-11/daily-state.json'))
            self.assertEqual(json.loads(saved[0].read_text())['phase'],'finished')
            self.assertTrue((old/'weread.json').exists())
            self.assertFalse((old/'notification-digest-queue.json').exists())
            self.assertNotIn('runs/2026-10-01/weread.json',capture(root,'2026-11-11')['files'])

    def test_unpublished_compared_day_keeps_evidence_beyond_two_days(self):
        with TemporaryDirectory() as d:
            root=Path(d);day=root/'runs/2026-11-03';day.mkdir(parents=True)
            (day/'notification-comparison.json').write_text('{"status":"completed"}')
            (day/'notification-digest-queue.json').write_text('{"events":{"unpublished":{}}}')
            (day/'notification-digest.apply-state.json').write_text('{"phase":"finished","consumed_ids":[]}')
            (day/'weread.json').write_text('{"books":[]}')
            previous=root/'runs/2026-11-02';previous.mkdir();(previous/'weread.json').write_text('{}')
            snapshot=capture(root,'2026-11-11')
            self.assertIn('runs/2026-11-03/weread.json',snapshot['files'])
            self.assertIn('runs/2026-11-02/weread.json',snapshot['files'])

    def test_new_checkpoint_version_and_backward_read(self):
        with TemporaryDirectory() as directory:
            value = capture(Path(directory), '2026-11-09')
            self.assertEqual(value['schema'], 'ebook-shared-checkpoint.v8')
            store = Store(MemoryBase())
            for schema in ('ebook-shared-checkpoint.v1', 'ebook-shared-checkpoint.v2','ebook-shared-checkpoint.v3','ebook-shared-checkpoint.v4','ebook-shared-checkpoint.v5','ebook-shared-checkpoint.v6','ebook-shared-checkpoint.v7','ebook-shared-checkpoint.v8'):
                store.publish({**value, 'schema': schema})
                self.assertEqual(store.read()[0]['schema'], schema)
            with self.assertRaisesRegex(RuntimeError, 'schema'):
                store.publish({**value, 'schema': 'ebook-shared-checkpoint.v9'})

    def test_pre_publication_gate_client_rejects_new_checkpoint(self):
        store=Store(MemoryBase())
        store.publish({'schema':'ebook-shared-checkpoint.v8','files':{}})
        with patch('scripts.shared_runtime.SCHEMA','ebook-shared-checkpoint.v7'):
            with self.assertRaisesRegex(RuntimeError,'schema'):store.read()

    def test_provider_policy_portable_but_session_stays_on_target(self):
        with TemporaryDirectory() as a, TemporaryDirectory() as b:
            a, b = Path(a), Path(b)
            for root in (a, b): (root/'config').mkdir()
            source = {'enabled': True, 'providers_file': 'research/providers.json',
                      'session_file': 'E:/private/source-session.dpapi', 'tracking_days': 30}
            (a/'config/publisher-newbooks.json').write_text(json.dumps(source))
            target = {'enabled': False, 'session_file': 'D:/private/target-session.dpapi'}
            (b/'config/publisher-newbooks.json').write_text(json.dumps(target))
            evidence = a/'runs/2026-11-09/publisher-recent-a/lists/provider.json'
            evidence.parent.mkdir(parents=True)
            evidence.write_bytes(b'{\r\n"complete":true\r\n}')
            checkpoint = capture(a, '2026-11-09')
            self.assertNotIn('source-session', json.dumps(checkpoint))
            restore(b, checkpoint)
            result = json.loads((b/'config/publisher-newbooks.json').read_text())
            self.assertEqual(result, {**source, 'session_file': target['session_file']})
            self.assertEqual((b/evidence.relative_to(a)).read_bytes(), evidence.read_bytes())

    def test_notification_unknown_guards_block_on_either_computer(self):
        from scripts.initialize_manual_projects import unknown_guard_paths
        with TemporaryDirectory() as directory:
            root = Path(directory); (root/'runs').mkdir()
            for name in ('manual-project-events-state.json', 'marketing-expense-notification-state.json'):
                path = root/'runs'/name
                path.write_text('{"phase":"requires_review"}')
                self.assertIn(str(path), unknown_guard_paths(root))
                checkpoint = capture(root, '2026-11-09')
                self.assertIn(str(path), checkpoint['unresolved_guards'])
                path.write_text('{"phase":"finished"}')
            self.assertEqual(unknown_guard_paths(root), [])

    def test_finished_run_health_does_not_churn_checkpoint(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); day = root/'runs/2026-11-09'; day.mkdir(parents=True)
            (day/'tencent-daily-import-plan.json').write_text('{"services": []}')
            before = capture(root, '2026-11-09')
            (day/'pipeline-summary.json').write_text('{"attempt": 2}')
            (root/'runs/marketing-expense-notification-state.json').write_text('{"phase":"finished"}')
            self.assertEqual(before, capture(root, '2026-11-09'))
            self.assertIn('runs/2026-11-09/tencent-daily-import-plan.json', before['files'])
            (root/'runs/marketing-expense-notification-state.json').write_text('{"phase":"requires_review"}')
            self.assertTrue(capture(root, '2026-11-09')['unresolved_guards'])

    def test_checkpoint_lost_ack_is_resolved_without_second_write(self):
        store, mutex = Mock(), Mock(spec=Mutex)
        store.publish.side_effect = RuntimeError('lost response')
        store.read.return_value = ({'files':{}}, {'generation':'saved'})
        self.assertEqual(commit_checkpoint(store, {'files':{}}, mutex), {'generation':'saved'})
        store.publish.assert_called_once(); mutex.assert_owned.assert_called_once()

    def test_checkpoint_unknown_mutation_never_retries(self):
        with TemporaryDirectory() as directory:
            store, mutex = Mock(), Mock(spec=Mutex)
            store.api.receipt_dir = Path(directory)
            (Path(directory)/'request-PENDING.json').write_text('{}')
            store.publish.side_effect = RuntimeError('lost response')
            store.read.return_value = ({'old':True}, {})
            with self.assertRaisesRegex(RuntimeError, 'unknown'):
                commit_checkpoint(store, {'new':True}, mutex)
            store.publish.assert_called_once()

    def test_checkpoint_known_old_state_allows_metadata_only_retry(self):
        with TemporaryDirectory() as directory:
            store, mutex = Mock(), Mock(spec=Mutex)
            store.api.receipt_dir = Path(directory)
            store.publish.side_effect = [RuntimeError('read interrupted'), {'generation':'saved'}]
            store.read.return_value = ({'old':True}, {})
            self.assertEqual(commit_checkpoint(store, {'new':True}, mutex), {'generation':'saved'})
            self.assertEqual(store.publish.call_count, 2)

    def test_competing_stale_read_cannot_acquire(self):
        git = FakeGit()
        a, b = Mutex('test/repo', git), Mutex('test/repo', git)
        stale = b.head()
        a.acquire()
        with self.assertRaisesRegex(RuntimeError, 'not fast forward'):
            b.transition(stale[0], stale[1], {'schema': 'ebook-mutex.v1', 'phase': 'busy', 'token': 'other'})
        a.assert_owned()
        with self.assertRaises(RuntimeError): b.acquire()
        a.release()
        b.acquire(); b.assert_owned(); b.release()

    def test_lost_ownership_blocks_write_and_release(self):
        git = FakeGit(); a = Mutex('test/repo', git); a.acquire()
        a.token = 'wrong'
        with self.assertRaises(PermissionError): a.assert_owned()
        with self.assertRaises(PermissionError): a.release()

    def test_incomplete_checkpoint_does_not_replace_committed_head(self):
        api = MemoryBase(); store = Store(api)
        first = {'schema': 'ebook-shared-checkpoint.v1', 'files': {}, 'day': '2026-11-09'}
        store.publish(first)
        api.fail_chunks = True
        with self.assertRaises(RuntimeError): store.publish({**first, 'day': '2026-11-10'})
        self.assertEqual(store.read()[0], first)

    def test_corrupt_remote_chunk_is_rejected(self):
        api = MemoryBase(); store = Store(api)
        store.publish({'schema': 'ebook-shared-checkpoint.v1', 'files': {}})
        for key, row in api.rows.items():
            if key != 'HEAD': row['内容'] = 'broken'
        with self.assertRaises(Exception): store.read()

    def test_rotating_slots_keep_state_rows_bounded(self):
        api = MemoryBase(); store = Store(api)
        for day in range(5):
            value = {'schema':'ebook-shared-checkpoint.v1','files':{},'sequence':day}
            meta = store.publish(value)
            self.assertEqual(store.read()[0], value)
            self.assertEqual(meta['slot'], day % 2)
        self.assertEqual(len(api.rows), 3)  # HEAD plus two small slots, no unbounded history.
        self.assertEqual(api.write_batch_size, 20)
        self.assertEqual(api.read_page_size, 100)

    def test_text_heavy_state_reads_use_bounded_pages(self):
        from scripts.feishu_io import Base
        with TemporaryDirectory() as directory:
            root = Path(directory)
            api = object.__new__(Base)
            api.receipt_dir = root; api.presentation_enabled = False
            Store(api)
            calls = []
            def call(command, table, **flags):
                calls.append((flags['offset'], flags['limit']))
                count = 100 if flags['offset'] == 0 else 5
                rows = [{'record_id': str(flags['offset']+i)} for i in range(count)]
                (root/flags['output']).write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
                return {'has_more': count == 100, 'records_count': count}
            api.call = call
            with patch('scripts.feishu_io.ROOT', root):
                self.assertEqual(len(api.read(TABLE)), 105)
            self.assertEqual(calls, [(0,100), (100,100)])

    def test_text_heavy_state_writes_split_below_changeset_limit(self):
        from scripts.feishu_io import Base
        api = object.__new__(Base); api.presentation_enabled = False
        api.manifest = {'tables': {TABLE: {'field_readback': {'fields': [
            {'name':'状态键','type':'text'},{'name':'内容','type':'text'}]}}}}
        Store(api)
        stored, sizes = {}, []
        api.read = lambda *_: copy.deepcopy(list(stored.values()))
        def call(command, table, payload):
            rows = payload['create_records']; sizes.append(len(rows))
            for row in rows: stored[row['状态键']] = {**row,'record_id':row['状态键']}
        api.call = call
        api.ensure(TABLE,'状态键',[{'状态键':str(i),'内容':'x'*24000} for i in range(45)])
        self.assertEqual(sizes,[20,20,5])

    def test_second_computer_restores_exact_bytes_and_preserves_credentials(self):
        with TemporaryDirectory() as a, TemporaryDirectory() as b:
            a, b = Path(a), Path(b)
            (a/'runs').mkdir(); (b/'runs').mkdir(); (b/'config').mkdir()
            raw = b'{\r\n "tracks": {"42": {"start_date": "2026-11-07"}}\r\n}'
            (a/'runs/publisher-recent-entry-registry.json').write_bytes(raw)
            (b/'runs/publisher-recent-entry-registry.json').write_text('{"stale":true}')
            (b/'config/runner-host.json').write_text('{"role":"development"}')
            checkpoint = capture(a, '2026-11-09')
            store = Store(MemoryBase()); store.publish(checkpoint)
            restore(b, store.read()[0])
            self.assertEqual((b/'runs/publisher-recent-entry-registry.json').read_bytes(), raw)
            self.assertEqual(json.loads((b/'config/runner-host.json').read_text())['role'], 'development')
            self.assertEqual(digest(raw), checkpoint['files']['runs/publisher-recent-entry-registry.json']['sha256'])

    def test_untrusted_paths_and_host_settings_rejected_before_restore(self):
        with TemporaryDirectory() as root:
            for name in ('../outside.json', 'config/runner-host.json', 'config/local-system.json',
                         'research/session.json', 'C:/secret.json', 'runs/../../outside.json'):
                with self.subTest(name=name), self.assertRaises(ValueError): safe_path(root, name)


if __name__ == '__main__': unittest.main()
