import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.fanqie_supplement import merge, prepare
from scripts.shared_runtime import capture, restore


def observation(ids, errors=()):
    return {'schema_version': 'fanqie.public.v1', 'collection_date': '2026-11-13',
            'started_at': '2026-11-13T10:00:00+08:00', 'finished_at': '2026-11-13T10:05:00+08:00',
            'books': [{'book_id': bid, 'identity_status': 'book_id_and_title_verified',
                       'detail_source': {'observed_at': '2026-11-13T10:01:00+08:00'},
                       'missing_fields': []} for bid in ids],
            'errors': [{'book_id': bid, 'error': 'transport EOF'} for bid in errors]}


class FanqieSupplementTests(unittest.TestCase):
    def test_only_missing_ids_added_original_time_and_errors_retained(self):
        initial=observation(['1'], ['2']);before=copy.deepcopy(initial)
        extra=observation(['2']);extra['finished_at']='2026-11-13T11:05:00+08:00'
        extra['books'][0]['detail_source']['observed_at']='2026-11-13T11:01:00+08:00'
        extra['books'][0]['missing_fields']=['cumulative_readers']
        result=merge(initial,extra,{'1','2'},'2026-11-13')
        self.assertEqual(initial,before)
        self.assertEqual(result['books'][0],before['books'][0])
        self.assertEqual(result['books'][1],extra['books'][0])
        self.assertEqual(result['errors'],[])
        self.assertEqual(result['resolved_collection_errors'],before['errors'])
        self.assertEqual(result['status'],'partial')
        self.assertNotIn('cumulative_readers',result['books'][1])

    def test_reject_overlap_partial_missing_wrong_day_and_unverified_identity(self):
        initial=observation(['1'], ['2','3'])
        for extra in [observation(['1','2','3']), observation(['2']),
                      observation(['2','3'], ['3'])]:
            with self.assertRaises(ValueError):merge(initial,extra,{'1','2','3'},'2026-11-13')
        extra=observation(['2','3']);extra['collection_date']='2026-11-12'
        with self.assertRaises(ValueError):merge(initial,extra,{'1','2','3'},'2026-11-13')
        extra=observation(['2','3']);extra['books'][0]['identity_status']='pending'
        with self.assertRaises(ValueError):merge(initial,extra,{'1','2','3'},'2026-11-13')

    def test_supplement_archive_is_shared_and_uncommitted_files_quarantined(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);day=root/'runs/2026-11-13';day.mkdir(parents=True)
            extra=root/'receipts/2026-11-13/supplement';extra.mkdir(parents=True)
            (extra/'snapshot.json').write_text('{}')
            (extra/'book.json').write_text('{"official":true}')
            (day/'fanqie-effective.json').write_text(json.dumps({'reviewed_supplements':[
                'receipts/2026-11-13/supplement/snapshot.json']}))
            snapshot=capture(root,'2026-11-13')
            self.assertIn('receipts/2026-11-13/supplement/book.json',snapshot['files'])
            (extra/'uncommitted.json').write_text('{}')
            restore(root,snapshot,'2026-11-13')
            self.assertFalse((extra/'uncommitted.json').exists())
            self.assertTrue((extra/'book.json').exists())

    def test_prepare_rechecks_raw_evidence_and_existing_effective_snapshot(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp); day=root/'runs/2026-11-13'; (day/'fanqie').mkdir(parents=True)
            extra_dir=root/'receipts/supplement'; extra_dir.mkdir(parents=True)
            response=extra_dir/'response.json'; response.write_bytes(b'{}')
            source={'url':'https://fanqienovel.com/api/book/info?book_id=2',
                    'archive_path':str(response),'http_status':200,
                    'observed_at':'2026-11-13T11:01:00+08:00',
                    'response_sha256':hashlib.sha256(b'{}').hexdigest()}
            extra=observation(['2']); extra['sources']=[source]
            extra['books'][0].update(detail_source=source,identity_source=source)
            (day/'fanqie/snapshot.json').write_text(json.dumps(observation(['1'],['2'])))
            extra_path=extra_dir/'snapshot.json'; extra_path.write_text(json.dumps(extra))
            with patch('scripts.collect_fanqie.normalize',return_value=extra['books'][0]):
                target=prepare(root,day,extra_path,{'1','2'},'2026-11-13')
                self.assertEqual(prepare(root,day,extra_path,{'1','2'},'2026-11-13'),target)
                response.write_bytes(b'{"changed":true}')
                with self.assertRaisesRegex(ValueError,'receipt failed'):
                    prepare(root,day,extra_path,{'1','2'},'2026-11-13')
                response.write_bytes(b'{}')
                result=json.loads(target.read_text()); result['books'][0]['book_id']='9'
                target.write_text(json.dumps(result))
                with self.assertRaisesRegex(ValueError,'differs'):
                    prepare(root,day,extra_path,{'1','2'},'2026-11-13')


if __name__=='__main__':unittest.main()
