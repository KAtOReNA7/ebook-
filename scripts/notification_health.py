"""Read-only notification ledger assessment. Never sends or retries messages."""
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
import json

TZ = timezone(timedelta(hours=8))
WAITING_STATES = {'待发送', '发送中'}


def instant(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError('Health timestamps must include timezone')
    return parsed.astimezone(TZ)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def assess(rows, previous, observed_at, mode='shadow'):
    """Classify actual workflow states without equating them with delivery."""
    current_time = instant(observed_at)
    if previous and previous.get('schema_version') != 'notification.health.v1':
        raise ValueError('Unknown notification health state schema')
    old_events = previous.get('events', {})
    state = {'schema_version': 'notification.health.v1', 'events': {}}
    observations, issues, seen = [], [], set()
    for row in rows:
        key = row.get('事件唯一键')
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError('Notification ledger has missing or duplicate event keys')
        seen.add(key)
        old = old_events.get(key, {})
        first_seen = instant(old.get('first_seen_at', current_time))
        statuses = row.get('通知状态', [])
        if not isinstance(statuses, list):
            statuses = [statuses]
        statuses = [value.get('name') if isinstance(value, dict) else value for value in statuses]
        status = statuses[0] if len(statuses) == 1 else None
        classification, reason = 'attention', 'unrecognized_workflow_state'
        if status in WAITING_STATES:
            try:
                event_date = date.fromisoformat(str(row.get('发生日期', ''))[:10])
            except ValueError:
                event_date = None
            if first_seen > current_time:
                reason = 'first_seen_timestamp_in_future'
            elif event_date is None or event_date > current_time.date():
                reason = 'event_date_invalid_or_future'
            elif event_date < current_time.date() or first_seen.date() < current_time.date():
                reason = 'pending_across_calendar_day'
            elif current_time - first_seen >= timedelta(minutes=15):
                reason = 'pending_at_least_15_minutes'
            else:
                classification, reason = 'waiting', 'within_observation_wait_window'
        elif status in {'发送失败', '结果未知'}:
            reason = 'workflow_failed' if status == '发送失败' else 'workflow_outcome_unknown'
        elif status == '已发送':
            # Even populated receipt text is not independently verified here.
            classification, reason = 'workflow_state_only', 'recipient_delivery_not_verified'
        elif status == '暂不发送':
            classification, reason = 'deferred', 'workflow_deferred'
        observation = {'event_id': key, 'record_id': row.get('record_id'),
                       'workflow_state': status, 'classification': classification,
                       'reason': reason, 'first_seen_at': first_seen.isoformat(),
                       'send_time_present': bool(row.get('发送时间')),
                       'receipt_text_present': bool(row.get('发送回执'))}
        observations.append(observation)
        state['events'][key] = {'first_seen_at': first_seen.isoformat(),
                                'last_seen_at': current_time.isoformat(),
                                'classification': classification, 'workflow_state': status}
        if classification == 'attention':
            issues.append({'event_id': key, 'workflow_state': status, 'reason': reason})
    # A removed unresolved row is not evidence that a message was delivered.
    for key, old in old_events.items():
        if key not in seen and old.get('classification') == 'attention':
            state['events'][key] = old
            issues.append({'event_id': key, 'workflow_state': old.get('workflow_state'),
                           'reason': 'unresolved_event_missing_from_ledger'})
    counts = dict(Counter(item['classification'] for item in observations))
    summary = {'schema_version': 'notification.health.v1', 'checked_at': current_time.isoformat(),
               'status': 'requires_review' if issues else 'waiting' if counts.get('waiting') else 'ok',
               'mode': mode, 'read_only': True, 'events_read': len(rows), 'counts': counts,
               'attention': issues, 'event_observations': observations,
               'issue_fingerprints': sorted(fingerprint({**issue,
                   'reason': 'pending_stalled' if issue['reason'] in {
                       'pending_across_calendar_day', 'pending_at_least_15_minutes'} else issue['reason']})
                   for issue in issues),
               'recipient_delivery_verified': False, 'messages_sent_by_check': 0,
               'automatic_retries': 0}
    return summary, state


def with_changes(summary, previous_check):
    """Repeated issues retain nonzero health while suppressing repeat notices."""
    before = set(previous_check.get('issue_fingerprints', []))
    after = set(summary.get('issue_fingerprints', []))
    # An unreadable ledger cannot prove that earlier issues recovered.
    if summary['status'] == 'read_error':
        after |= before
    recovered = before - after if summary['status'] != 'read_error' else set()
    return {**summary, 'issue_fingerprints': sorted(after), 'new_issue_count': len(after - before),
            'recovered_issue_count': len(recovered),
            'should_notify': bool(after - before or recovered)}
