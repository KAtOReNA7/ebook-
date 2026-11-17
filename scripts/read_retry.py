"""Bounded retries for a single idempotent GET, never a mutation or a pipeline."""
import http.client
import time
import urllib.error


def read_get(operation, failures=None, record_kind='source_http'):
    failures = failures if failures is not None else []
    for attempt in range(3):
        started = time.monotonic()
        facts = {'attempt': attempt+1, 'outcome': 'failed', 'body_bytes': 0}
        retry = False
        try:
            result = operation()
            values = result if isinstance(result, tuple) else (result,)
            facts.update(outcome='completed', body_bytes=sum(len(v) for v in values if isinstance(v, bytes)))
            return result
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            status = getattr(exc, 'code', None)
            transient = status is None or status in {408, 429, 500, 502, 503, 504}
            failures.append({'attempt': attempt+1, 'error_type': type(exc).__name__, 'http_status': status})
            facts.update(error_type=type(exc).__name__, http_status=status)
            if not transient or attempt == 2:
                raise
            retry = True
        finally:
            if record_kind:
                try:
                    from .run_performance import event
                except ImportError:
                    from run_performance import event
                event(record_kind, started, **facts, backoff_seconds=(attempt+1 if retry else 0))
        if retry:
            time.sleep(attempt+1)
