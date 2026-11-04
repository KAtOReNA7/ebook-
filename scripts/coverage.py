"""Separate unavailable identities from failures of an expected daily source."""

REQUIRED_BOARDS = ('rising', 'newbook', 'novel_female_rising', 'newbook_novel_female')


def weread_coverage(snapshot, tracked, pending):
    charts = {row['chart_id']: row for row in snapshot.get('charts', [])}
    incomplete = [cid for cid in REQUIRED_BOARDS
                  if not charts.get(cid, {}).get('complete_top50')]
    expected = {str(row['book_id']) for row in tracked}
    for chart in charts.values():
        expected.update(str(row['book_id']) for row in chart.get('records', []))
    books = {str(row['book_id']): row for row in snapshot.get('books', [])}
    missing = sorted(expected - set(books))
    failed = sorted(bid for bid in expected & set(books) if books[bid].get('status') != 'ok')
    missing_metrics = {bid: [key for key in ('reviewer_count', 'recommendation_percent', 'today_reading_count')
                             if books[bid].get(key) is None]
                       for bid in expected & set(books) if books[bid].get('status') == 'ok'}
    missing_metrics = {bid: keys for bid, keys in missing_metrics.items() if keys}
    attention = bool(incomplete or missing or failed)
    return {'status': 'requires_review' if attention else 'partial' if pending or missing_metrics else 'complete',
            'requested_books': len(expected), 'successful_books': len(expected) - len(missing) - len(failed),
            'incomplete_boards': incomplete, 'missing_book_ids': missing, 'failed_book_ids': failed,
            'unresolved_project_count': len(pending), 'missing_metric_fields': missing_metrics,
            'meaning': '运行完成不等于全部营销作品和字段已覆盖；未知值保持为空。'}
