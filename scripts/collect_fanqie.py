"""Read-only Fanqie collector using requests observed in the official share H5.

python scripts/collect_fanqie.py --book-id 6972452475022347301
python scripts/collect_fanqie.py --share-url https://changdunovel.com/t/WVmrGtAqMbo/

No login, signing services, Feishu writes or notifications. Endpoint is an
observed website implementation, not a supported public API. The publisher is
kept separate from the digital-distribution copyright provider. Cumulative
readers come ONLY from a labelled 人读过 value, never reading_count/readCount.
"""
import argparse
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))
LANDING = "https://api.fqnovel.com/novel_ug/share/landing_page"
JS_SOURCE = "https://lf-fe.fqnovelstatic.com/obj/novel-fanqie-fe/growth/incentive-h5-monorepo/apps/novel/static/js/book-share.aa459ec3.js"
REQUEST_SOURCE = "https://lf-fe.fqnovelstatic.com/obj/novel-fanqie-fe/growth/incentive-h5-monorepo/apps/novel/static/js/382.0ffa2f7c.js"
MAPPING = {
    "author": "PC book/info data.author (bibliographic name); H5 author.name retained separately because it can be an account nickname",
    "copyright_provider": "book_data.first_chapters_content copyright footer: 本书由…授权本平台进行数字发行",
    "cumulative_readers": "book_data.reading_count_text ONLY when explicitly labelled 人读过; approximate display value requires interval",
    "reviewer_count": "book_data.score_dcnt_acc; official H5 score section labels 人点评",
    "book_review_count": "book_data.comment_cnt; official H5 热门书评 section count",
    "reading_count_raw": "book_data.reading_count; official H5 labels 人在读, NOT cumulative readers",
    "total_reviewers_raw": "book_data.total_reviewers; not the number rendered as 人点评, unmapped",
}


def now():
    return datetime.now(TZ).isoformat()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def integer(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d+", value):
        return int(value)
    return None


def cumulative_display(raw):
    """Conservative envelope covers rounding OR truncation, neither is assumed.

    For '1.0万人读过', value is 10000 but interval 9000..11000, so it must NOT
    establish a 10000 crossing. Bounds are intentionally inclusive/wider.
    """
    result = {"raw": raw, "value": None, "precision": "unavailable",
              "lower_bound": None, "upper_bound": None, "display_unit": None,
              "rounding_rule": "unknown", "status": "label_unverified"}
    if not isinstance(raw, str):
        return result
    match = re.fullmatch(r"\s*([\d,]+(?:\.\d+)?)\s*([万亿]?)\s*人读过\s*", raw)
    if not match:
        return result
    text, unit = match.groups()
    number = Decimal(text.replace(",", ""))
    multiplier = {"": 1, "万": 10000, "亿": 100000000}[unit]
    value = number * multiplier
    decimals = len(text.split(".")[1]) if "." in text else 0
    step = Decimal(multiplier) / (10 ** decimals)
    if not unit and decimals:
        return result  # A fractional person without a unit is not a valid count.
    result.update(value=int(value), status="observed_labelled_value")
    if not unit:
        result.update(precision="exact", lower_bound=int(value), upper_bound=int(value),
                      display_unit=1, rounding_rule="integer_display")
    else:
        result.update(precision="approximate", lower_bound=max(0, int(value-step)),
                      upper_bound=int(value+step), display_unit=float(step),
                      bound_source="assumed_display_unit_buffer",
                      interval_note="Conservative +/- one display unit; rounding/truncation not verified; not a platform-guaranteed error bound")
    return result


def strict_threshold_state(metric, threshold):
    """Classify a single observation. Crossing requires a prior valid observation."""
    if metric.get("lower_bound") is None or metric.get("upper_bound") is None:
        return "unknown"
    if metric["lower_bound"] > threshold:
        return "above"
    if metric["upper_bound"] <= threshold:
        return "at_or_below"
    return "uncertain_due_to_display_precision"


def plain_text(fragment):
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment))


def copyright_metadata(book):
    names = book.get("item_names") or []
    chapters = book.get("first_chapters_content") or []
    fragments = [part for i, part in enumerate(chapters) if isinstance(part, str)
                 and ("cpFooter" in part or (i < len(names) and "版权" in names[i]))]
    text = "\n".join(plain_text(part) for part in fragments)
    match = re.search(r"本书由\s*(.+?)\s*授权本平台进行数字发行", text)
    publisher = re.search(r"出版社[：:]\s*([^\r\n]+)", text)
    isbn = re.search(r"ISBN[：:]\s*([0-9Xx-]+)", text)
    provider = match.group(1).strip() if match else None
    if provider and re.search(r"[\ue000-\uf8ff]", provider):
        provider = None
    return {"copyright_provider": provider, "publisher": publisher.group(1).strip() if publisher else None,
            "isbn": isbn.group(1) if isbn else None, "copyright_raw": text.strip(),
            "copyright_status": "verified_footer" if provider else "not_present_in_observed_preview"}


class Archive:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=False)
        self.sources = []

    def get(self, url, name):
        stamp = now()
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                  "Referer": "https://changdunovel.com/"})
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                raw, status, final = response.read(), response.status, response.url
        except urllib.error.HTTPError as exc:
            raw, status, final = exc.read(), exc.code, exc.url
        path = self.directory / name
        path.write_bytes(raw)
        # User-supplied share URLs may redirect with user/device tracking params.
        # Keep only book-routing params in the human-readable receipt.
        parsed = urllib.parse.urlsplit(final)
        query = urllib.parse.parse_qs(parsed.query)
        safe_query = {key: values[-1] for key, values in query.items()
                      if key in {"book_id", "bookId", "aid", "share_type", "ui_exp_group", "ver"}}
        safe_final = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                             urllib.parse.urlencode(safe_query), ""))
        evidence = {"url": url, "final_url": safe_final, "http_status": status,
                    "observed_at": stamp, "response_sha256": hashlib.sha256(raw).hexdigest(),
                    "response_bytes": len(raw), "archive_path": str(path.resolve())}
        self.sources.append(evidence)
        write_json(path.with_suffix(path.suffix + ".source.json"), evidence)
        if status != 200:
            raise RuntimeError(f"HTTP {status}; see {path}")
        return raw, evidence


def normalize(book_id, landing, identity, source, identity_source):
    if landing.get("err_no") != 0:
        raise ValueError(f"Landing response err_no={landing.get('err_no')}: {landing.get('err_tips')}")
    book = landing.get("data", {}).get("book_data")
    if not isinstance(book, dict) or not book.get("title"):
        raise ValueError("Missing book_data/title; no observation accepted")
    pc = identity.get("data", {})
    if identity.get("code") != 0 or str(pc.get("bookId")) != book_id or pc.get("bookName") != book["title"]:
        raise ValueError("Book identity/title mismatch against official book/info response")
    cumulative = cumulative_display(book.get("reading_count_text"))
    copyright = copyright_metadata(book)
    result = {"book_id": book_id, "title": book["title"],
              "detail_url": "https://fanqienovel.com/page/" + book_id,
              "author": pc.get("author") or (book.get("author") or {}).get("name"),
              "author_pc_raw": pc.get("author"), "author_h5_raw": (book.get("author") or {}).get("name"),
              "word_count": integer(book.get("word_count")),
              "category": "/".join(book.get("category_tags") or []),
              "reviewer_count": integer(book.get("score_dcnt_acc")),
              "book_review_count": integer(book.get("comment_cnt")),
              "cumulative_readers": cumulative["value"], "cumulative_readers_metric": cumulative,
              "reading_count_raw": book.get("reading_count"),
              "total_reviewers_raw": book.get("total_reviewers"), "score_raw": book.get("score"),
              "score_subtitle_infos_raw": book.get("score_subtitle_infos"),
              "detail_source": source, "identity_source": identity_source,
              "identity_status": "book_id_and_title_verified", **copyright}
    required = ["author", "word_count", "copyright_provider", "reviewer_count", "book_review_count", "cumulative_readers"]
    result["missing_fields"] = [key for key in required if result.get(key) is None or result.get(key) == ""]
    result["status"] = "partial" if result["missing_fields"] else "success"
    result["precision_note"] = ("点评人数、书评数为接口整数；累计阅读只取带“人读过”标签的显示值，"
                                "精度见 cumulative_readers_metric，临界阈值用上下界判断；"
                                "reading_count 与 total_reviewers 原文保留但不替代目标指标。")
    return result


def collect(book_id, archive):
    query = urllib.parse.urlencode({"aid": 1967, "book_id": book_id, "share_type": 11, "ui_exp_group": 3})
    raw, source = archive.get(LANDING + "?" + query, book_id + "-landing.json")
    identity_raw, identity_source = archive.get("https://fanqienovel.com/api/book/info?bookId=" + book_id,
                                                book_id + "-identity.json")
    return normalize(book_id, json.loads(raw), json.loads(identity_raw), source, identity_source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book-id", action="append", default=[])
    parser.add_argument("--book-url", action="append", default=[])
    parser.add_argument("--share-url", action="append", default=[])
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if not (args.book_id or args.book_url or args.share_url):
        parser.error("Supply --book-id, --book-url or an official --share-url")
    started = now()
    output = args.output_dir or ROOT / "snapshots" / "fanqie" / datetime.now(TZ).strftime("%Y-%m-%d_%H%M%S_%f")
    archive = Archive(output)
    ids = list(args.book_id)
    for url in args.book_url:
        match = re.fullmatch(r"https://(?:www\.)?fanqienovel\.com/page/(\d+)/?", url)
        if not match:
            parser.error("--book-url requires an official fanqienovel.com/page/<id> URL")
        ids.append(match.group(1))
    for i, url in enumerate(args.share_url):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {"changdunovel.com", "www.changdunovel.com", "fanqienovel.com"}:
            parser.error("--share-url requires an official HTTPS share link")
        _, source = archive.get(url, f"share-{i}.html")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(source["final_url"]).query)
        bid = query.get("book_id", query.get("bookId", [None]))[-1]
        if not bid:
            raise ValueError("Share did not resolve to a book_id; preserve receipt for manual inspection")
        ids.append(bid)
    if any(not re.fullmatch(r"\d{10,25}", bid) for bid in ids):
        raise ValueError("Invalid book ID; IDs must stay exact decimal strings")
    books, errors = [], []
    for bid in dict.fromkeys(ids):
        try:
            books.append(collect(bid, archive))
        except Exception as exc:
            errors.append({"book_id": bid, "error": type(exc).__name__ + ": " + str(exc), "observed_at": now()})
        time.sleep(0.2)
    report = {"schema_version": "fanqie.public.v1", "started_at": started, "finished_at": now(),
              "collection_date": started[:10], "books": books, "errors": errors,
              "status": "partial" if errors or any(book["status"] != "success" for book in books) else "success",
              "field_mapping": MAPPING, "mapping_evidence": [JS_SOURCE, REQUEST_SOURCE],
              "publication_rising_board": {"status": "source_not_identified", "complete": False,
                                            "note": "Original-fiction PC /rank/1_1 is NOT publication rising; absent board data is not an empty board"},
              "sources": archive.sources}
    write_json(output / "snapshot.json", report)
    print(json.dumps({"snapshot": str((output / "snapshot.json").resolve()), "books": len(books),
                      "errors": len(errors), "status": report["status"]}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
