"""Bounded retries for a single idempotent GET, never a mutation or a pipeline."""
import http.client
import time
import urllib.error


def read_get(operation, failures=None):
    failures = failures if failures is not None else []
    for attempt in range(3):
        try:
            return operation()
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            status = getattr(exc, 'code', None)
            transient = status is None or status in {408, 429, 500, 502, 503, 504}
            failures.append({'attempt': attempt+1, 'error_type': type(exc).__name__, 'http_status': status})
            if not transient or attempt == 2:
                raise
            time.sleep(attempt+1)
