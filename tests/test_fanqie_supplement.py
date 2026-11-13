import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.fanqie_supplement import merge
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


if __name__=='__main__':unittest.main()
