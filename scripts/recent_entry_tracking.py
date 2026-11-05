"""Permanent-ID recent-list diff and 30-day cohorts; no Base writes or notifications."""
import argparse
import copy
import json
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    from .daily_run import atomic_json, process_lock, TZ, ROOT
except ImportError:
    from daily_run import atomic_json, process_lock, TZ, ROOT


SCHEMA = 'recent-entry-tracking.v1'


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('Observation requires actual timezone-aware time')
    return result.astimezone(TZ)


def active_targets(state, as_of):
    day = date.fromisoformat(as_of)
    return [copy.deepcopy(track) for track in state['tracks'].values()
            if date.fromisoformat(track['start_date']) <= day <= date.fromisoformat(track['end_date'])]


def observe(previous, observations, as_of):
    """Each pair is (verified provider config, complete normalized observation)."""
    if previous is not None and previous.get('schema') != SCHEMA:
        raise ValueError('Unknown registry schema; reconcile instead of overwriting')
    state = copy.deepcopy(previous) if previous is not None else {
        'schema': SCHEMA, 'providers': {}, 'seen': {}, 'tracks': {}}
    changes = []
    for provider, snapshot in observations:
        key = str(provider['copyright_id'])
        result = {'provider': provider['name'], 'new_entries': []}
        books = snapshot.get('books')
        if snapshot.get('status') != 'complete' or snapshot.get('provider') != provider['name'] or not isinstance(books, list):
            result['status'] = 'held_incomplete'; changes.append(result); continue
        ids = [book.get('book_id') for book in books]
        if (any(not isinstance(bid, str) or not bid.isdigit() for bid in ids)
                or len(set(ids)) != len(ids)
                or snapshot.get('total_count') != len(ids)
                or snapshot.get('returned_rows') != len(ids)
                or snapshot.get('unique_ids') != len(ids)
                or any(book.get('copyright_id') != provider['copyright_id'] for book in books)):
            result['status'] = 'held_invalid'; changes.append(result); continue
        observed = timestamp(snapshot['source']['observed_at'])
        if observed.date() > date.fromisoformat(as_of):
            raise ValueError('Observation is later than the requested evaluation day')
        prior = state['providers'].get(key)
        if prior and observed <= timestamp(prior['last_observed_at']):
            result['status'] = 'ignored_replayed_or_stale'; changes.append(result); continue
        baseline = prior is None
        for book in books:
            bid = book['book_id']
            if bid in state['seen']:
                continue
            state['seen'][bid] = {'first_observed_at': observed.isoformat(),
                                  'provider_key': key, 'baseline': baseline}
            if not baseline:
                track = {'book_id': bid, 'title': book.get('title'), 'author': book.get('author'),
                         'provider': provider['name'], 'copyright_id': provider['copyright_id'],
                         'reader_url': book.get('reader_url'),
                         'first_entry_observed_at': observed.isoformat(),
                         'start_date': observed.date().isoformat(),
                         'end_date': (observed.date()+timedelta(days=29)).isoformat(),
                         'start_basis': 'first observed new ID after complete initial baseline'}
                state['tracks'][bid] = track
                result['new_entries'].append(copy.deepcopy(track))
        state['providers'][key] = {
            'name': provider['name'],
            'baseline_observed_at': prior['baseline_observed_at'] if prior else observed.isoformat(),
            'last_observed_at': observed.isoformat(), 'current_ids': sorted(ids),
            'source_sha256': snapshot['source'].get('response_sha256')}
        result['status'] = 'baseline_created' if baseline else 'compared'
        changes.append(result)
    return state, {'changes': changes, 'active_targets': active_targets(state, as_of),
                   'base_writes': 0, 'notifications': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--providers-file', required=True)
    parser.add_argument('--observation-dir', required=True)
    parser.add_argument('--registry', required=True)
    parser.add_argument('--apply', action='store_true', help='Persist local registry; default is preview only')
    args = parser.parse_args()
    providers = json.loads(Path(args.providers_file).read_text(encoding='utf-8'))
    observations = [(provider, json.loads((Path(args.observation_dir)/f'provider-{i}.json').read_text(encoding='utf-8')))
                    for i, provider in enumerate(providers)]
    registry = Path(args.registry)
    with process_lock(ROOT/'runs'/'.daily.lock'):
        previous = json.loads(registry.read_text(encoding='utf-8')) if registry.exists() else None
        state, report = observe(previous, observations, datetime.now(TZ).date().isoformat())
        if args.apply:
            atomic_json(registry, state)
        report['registry_saved'] = args.apply
        print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
