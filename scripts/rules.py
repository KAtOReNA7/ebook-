"""Pure monitoring rules. No network, file writes, message sends, or implicit state.

All dates are Shanghai calendar dates (YYYY-MM-DD), supplied by the caller.
Store emitted event IDs durably and pass them back as ``emitted`` on retries.
Observations require platform/book_id/date/valid; never pass another metric as
cumulative_readers. ``readers_exact`` and ``readers_metric`` are collector attestations.
"""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json

RULE_VERSION = "1"
WEREAD_BOARDS = frozenset({
    "weread_rising", "weread_new", "weread_female_rising", "weread_female_new"
})


def _result(events=None, pending=None, baseline=False, emitted=()):
    seen = set(emitted)
    unique = {}
    for event in events or []:
        if event["event_id"] not in seen:
            unique[event["event_id"]] = event
    return {"events": list(unique.values()), "pending": pending or [], "baseline": baseline}


def _day(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _identity(record):
    if not isinstance(record, dict):
        return None
    platform, book_id = record.get("platform"), record.get("book_id")
    return (platform, str(book_id)) if platform and book_id else None


def _event(kind, identity, key_parts, **details):
    stable = [RULE_VERSION, kind, *identity, *key_parts]
    digest = hashlib.sha256(json.dumps(stable, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return {"event_id": digest, "rule_version": RULE_VERSION, "kind": kind,
            "platform": identity[0], "book_id": identity[1], **details}


def _observation_error(current, platform):
    if not isinstance(current, dict) or current.get("valid") is not True:
        return "observation_invalid_or_missing"
    if not _identity(current) or current.get("platform") != platform:
        return "platform_or_book_identity_invalid"
    if _day(current.get("date")) is None:
        return "observation_date_invalid"
    return None


def _pair_error(previous, current, platform):
    error = _observation_error(previous, platform)
    if error:
        return error
    if _identity(previous) != _identity(current):
        return "different_platform_book_identity"
    if _day(previous["date"]) >= _day(current["date"]):
        return "previous_date_not_before_current"
    return None


def _percentage(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and 0 <= number <= 100 else None
    except InvalidOperation:
        return None


def recommendation_events(previous, current, emitted=()):
    """Displayed recommendation: <90 -> >=90 and >=90 -> <90.

    Input: recommendation (numeric displayed percentage), plus observation fields.
    A missing first observation establishes a baseline, never a crossing.
    """
    error = _observation_error(current, "weread")
    if error:
        return _result(pending=[error])
    now = _percentage(current.get("recommendation"))
    if now is None:
        return _result(pending=["recommendation_unavailable"])
    if previous is None:
        return _result(baseline=True)
    error = _pair_error(previous, current, "weread")
    if error:
        return _result(pending=[error])
    before = _percentage(previous.get("recommendation"))
    if before is None:
        return _result(pending=["previous_recommendation_unavailable"])
    up, down = before < 90 <= now, now < 90 <= before
    if not (up or down):
        return _result()
    kind = "recommendation_up" if up else "recommendation_down"
    event = _event(kind, _identity(current), [previous["date"], current["date"]],
                   previous_date=previous["date"], observed_date=current["date"],
                   previous_value=float(before), current_value=float(now), threshold=90,
                   observation_gap_days=(_day(current["date"]) - _day(previous["date"])).days)
    return _result([event], emitted=emitted)


def _board_error(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("valid") is not True or snapshot.get("complete") is not True:
        return "board_incomplete_or_invalid"
    if snapshot.get("platform") != "weread" or snapshot.get("board_id") not in WEREAD_BOARDS:
        return "board_identity_invalid"
    if _day(snapshot.get("date")) is None:
        return "board_date_invalid"
    entries = snapshot.get("entries")
    if not isinstance(entries, list) or len(entries) != 50:
        return "board_top50_coverage_invalid"
    if any(not isinstance(entry, dict) or not entry.get("book_id") for entry in entries):
        return "board_book_identity_missing"
    if any(type(entry.get("rank")) is not int for entry in entries):
        return "board_rank_invalid"
    if {entry["rank"] for entry in entries} != set(range(1, 51)):
        return "board_rank_coverage_invalid"
    if len({str(entry["book_id"]) for entry in entries}) != 50:
        return "board_duplicate_book_identity"
    return None


def board_events(previous, current, projects=(), emitted=()):
    """Diff two complete 1..50 snapshots, scoped to projects or verified Motie.

    Each entry: book_id/rank; Motie requires copyright_verified=True AND
    copyright_group='磨铁'. Project scope uses platform+book_id, never title.
    """
    error = _board_error(current)
    if error:
        return _result(pending=[error])
    if previous is None:
        return _result(baseline=True)
    error = _board_error(previous)
    if error:
        return _result(pending=["previous_" + error])
    if previous["board_id"] != current["board_id"]:
        return _result(pending=["different_board_identity"])
    if _day(previous["date"]) >= _day(current["date"]):
        return _result(pending=["previous_date_not_before_current"])
    project_ids = {_identity(project) for project in projects}
    old = {str(entry["book_id"]): entry for entry in previous["entries"]}
    new = {str(entry["book_id"]): entry for entry in current["entries"]}
    events = []
    for kind, keys, entries in (("board_enter", new.keys() - old.keys(), new),
                                ("board_exit", old.keys() - new.keys(), old)):
        for book_id in sorted(keys):
            entry = entries[book_id]
            reasons = []
            if ("weread", book_id) in project_ids:
                reasons.append("project")
            if entry.get("copyright_verified") is True and entry.get("copyright_group") == "磨铁":
                reasons.append("verified_motie")
            if not reasons:
                continue
            events.append(_event(kind, ("weread", book_id),
                                 [current["board_id"], previous["date"], current["date"]],
                                 board_id=current["board_id"], reasons=reasons,
                                 previous_date=previous["date"], observed_date=current["date"],
                                 previous_rank=old.get(book_id, {}).get("rank"),
                                 current_rank=new.get(book_id, {}).get("rank"),
                                 observation_gap_days=(_day(current["date"]) - _day(previous["date"])).days))
    return _result(events, emitted=emitted)


def _readers(record):
    value = record.get("cumulative_readers")
    if (record.get("readers_exact") is not True or
            record.get("readers_metric") != "cumulative_readers" or
            type(value) is not int or value < 0):
        return None
    return value


def fanqie_events(previous, current, registered_date, emitted=(), known_reached=()):
    """Strict >10k (no deadline), >20k/50k/100k within D0..6/13/29.

    Counts must be exact integers with readers_exact=True and
    readers_metric='cumulative_readers'. First observed overshoots are baselines.
    Milestone IDs are stable across recrossings. Persist/pass emitted IDs AND
    reached_thresholds as known_reached, scoped to platform/book/registered_date.
    Initial overshoots enter that state without producing notifications.
    """
    error = _observation_error(current, "fanqie")
    if error:
        return _result(pending=[error])
    start = _day(registered_date)
    if start is None or _day(current["date"]) < start:
        return _result(pending=["registration_date_invalid"])
    now = _readers(current)
    if now is None:
        return _result(pending=["exact_cumulative_readers_unavailable"])
    milestones = ((10000, None), (20000, 7), (50000, 14), (100000, 30))
    if any(type(value) is not int or value not in {10000, 20000, 50000, 100000}
           for value in known_reached):
        return _result(pending=["known_milestone_state_invalid"])
    known = set(known_reached)

    def finish(result):
        result["reached_thresholds"] = sorted(known | {threshold for threshold, _ in milestones if now > threshold})
        result["milestone_state_scope"] = {"platform": current["platform"],
                                            "book_id": str(current["book_id"]),
                                            "registered_date": registered_date}
        return result

    if previous is None:
        return finish(_result(baseline=True))
    error = _pair_error(previous, current, "fanqie")
    if error:
        return finish(_result(pending=[error]))
    if _day(previous["date"]) < start:
        return finish(_result(pending=["previous_observation_before_registration"]))
    before = _readers(previous)
    if before is None:
        return finish(_result(pending=["previous_exact_cumulative_readers_unavailable"]))
    known.update(threshold for threshold, _ in milestones if before > threshold)
    elapsed = (_day(current["date"]) - start).days
    events = []
    for threshold, days in milestones:
        if threshold not in known and before <= threshold < now and (days is None or elapsed < days):
            events.append(_event("fanqie_milestone", _identity(current), [registered_date, threshold],
                                 registered_date=registered_date, threshold=threshold,
                                 window_days=days, previous_date=previous["date"],
                                 observed_date=current["date"], previous_value=before, current_value=now,
                                 observation_gap_days=(_day(current["date"]) - _day(previous["date"])).days))
    return finish(_result(events, emitted=emitted))


def tracking_due(enrollments, on_date, currently_ranked=()):
    """Return active identities from the union of inclusive 30-day windows.

    Enrollment: platform/book_id/start_date/source. Remaining on a target board
    also makes an identity due. No inferred backfill or date reset occurs here.
    """
    today = _day(on_date)
    if today is None:
        raise ValueError("on_date must be an ISO Shanghai calendar date")
    due = {}
    pending = []
    for enrollment in enrollments:
        identity = _identity(enrollment)
        start = _day(enrollment.get("start_date")) if isinstance(enrollment, dict) else None
        if identity is None or start is None:
            pending.append("invalid_enrollment")
            continue
        end = start + timedelta(days=29)
        if start <= today <= end:
            record = due.setdefault(identity, {"platform": identity[0], "book_id": identity[1],
                                               "reasons": [], "minimum_end_date": end.isoformat()})
            reason = enrollment.get("source", "enrollment")
            if reason not in record["reasons"]:
                record["reasons"].append(reason)
            record["minimum_end_date"] = max(record["minimum_end_date"], end.isoformat())
    for record in currently_ranked:
        identity = _identity(record)
        if identity is None:
            pending.append("invalid_ranked_identity")
            continue
        item = due.setdefault(identity, {"platform": identity[0], "book_id": identity[1],
                                         "reasons": [], "minimum_end_date": None})
        if "currently_ranked" not in item["reasons"]:
            item["reasons"].append("currently_ranked")
    return {"due": [due[key] for key in sorted(due)], "pending": pending}


def evaluate(payload):
    """JSON-friendly dispatcher; all state must be supplied by caller."""
    kind = payload.get("rule")
    if kind == "recommendation":
        return recommendation_events(payload.get("previous"), payload.get("current"), payload.get("emitted", ()))
    if kind == "board":
        return board_events(payload.get("previous"), payload.get("current"),
                            payload.get("projects", ()), payload.get("emitted", ()))
    if kind == "fanqie":
        return fanqie_events(payload.get("previous"), payload.get("current"),
                             payload.get("registered_date"), payload.get("emitted", ()),
                             payload.get("known_reached", ()))
    if kind == "tracking":
        return tracking_due(payload.get("enrollments", ()), payload.get("on_date"),
                             payload.get("currently_ranked", ()))
    return _result(pending=["unknown_rule"])


if __name__ == "__main__":
    import sys
    json.dump(evaluate(json.load(sys.stdin)), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
