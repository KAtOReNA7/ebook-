"""Public WeRead metadata collector; no login, chapters, writes or notifications.

Default: python scripts/collect_weread.py
Only observed website GET endpoints are used; they are not a supported public API.
Details archives contain the extracted book metadata, not chapter content/user state.
"""
import argparse
import hashlib
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
CHARTS = {
    "rising": "微信读书飙升榜Top50",
    "newbook": "微信读书新书榜Top50",
    "novel_female_rising": "女生小说飙升Top50",
    "newbook_novel_female": "女生小说新书Top50",
}
CHART_IDENTITIES = {
    "rising": "飙升·出版",
    "newbook": "新书·出版",
    "novel_female_rising": "飙升·女生小说",
    "newbook_novel_female": "新书·女生小说",
}
MAPPING = {
    "author": "bookInfo.author",
    "word_count": "reader.bookInfo.totalWords",
    "copyright_provider": "reader.bookInfo.copyrightInfo.name",
    "copyright_provider_id": "reader.bookInfo.copyrightInfo.id",
    "publisher": "reader.bookInfo.publisher (not copyright provider)",
    "platform_listing_time": "reader.bookInfo.onTime; Asia/Shanghai; matches App 上架于, not publishTime or proven first-ever listing",
    "category": "bookInfo.category",
    "reviewer_count": "bookInfo.newRatingCount",
    "recommendation_percent": "bookInfo.newRating / 10; nonpositive => unavailable",
    "today_reading_count": "books[].readingCount; official chart/search UI says 人今日阅读; search requires exact bookInfo.bookId",
    "reader_page_reading_count_raw": "reader.readingStat.readingCount; UI only says 阅读, NOT mapped to today",
}


def now():
    return datetime.now(TZ).isoformat()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as res:
        raw = res.read()
        evidence = {"url": url, "http_status": res.status, "observed_at": now(),
                    "response_bytes": len(raw), "response_sha256": hashlib.sha256(raw).hexdigest()}
    return raw.decode("utf-8"), evidence


def decode_state(body):
    marker = "window.__INITIAL_STATE__="
    return json.JSONDecoder().raw_decode(body[body.index(marker) + len(marker):])[0]


def rating(raw):
    return raw / 10 if isinstance(raw, (int, float)) and raw > 0 else None


def listing_time(raw):
    """Normalize the platform's listing timestamp, never the print publication date."""
    if type(raw) is not int or raw <= 0:
        return None
    try:
        return datetime.fromtimestamp(raw, TZ).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def detail_url(info):
    if info.get("readerLink"):
        return info["readerLink"]
    token = urllib.parse.parse_qs(urllib.parse.urlparse(info.get("deepLink", "")).query).get("v", [""])[0]
    return "https://weread.qq.com/web/reader/" + token if token else None


def is_count(value):
    return type(value) is int and value >= 0


def collect_search_today(book, raw_dir, max_pages=10):
    """Match the platform ID exactly; a matching title alone is never sufficient."""
    bid, title = str(book.get("book_id", "")), book.get("title")
    result = {"book_id": bid, "status": "missing", "value": None,
              "source": None, "requests": [], "missing_reason": None}
    if not bid.isdigit() or not isinstance(title, str) or not title.strip():
        result["missing_reason"] = "离榜书缺少有效平台作品ID或书名，未执行搜索"
        return result
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    offset = 0
    for page in range(max_pages):
        url = "https://weread.qq.com/web/search/global?" + urllib.parse.urlencode({
            "keyword": title.strip(), "maxIdx": offset, "fragmentSize": 120, "count": 20})
        evidence = {"url": url, "observed_at": now(), "requested_book_id": bid}
        try:
            body, response_evidence = get(url)
            archive = raw_dir / ("search-today-" + bid + "-" + str(page) + ".json")
            archive.write_text(body, encoding="utf-8")
            evidence.update(response_evidence)
            evidence["archive_path"] = str(archive.resolve())
            result["requests"].append(evidence)
            data = json.loads(body)
            if not isinstance(data, dict) or data.get("errcode") or data.get("errCode") or data.get("error"):
                raise ValueError("official search returned an error or unexpected response")
            rows = data.get("books")
            if not isinstance(rows, list):
                raise ValueError("official search books is missing or not a list")
            evidence.update({"returned_rows": len(rows), "has_more": data.get("hasMore")})
            matches = [r for r in rows if isinstance(r, dict)
                       and isinstance(r.get("bookInfo"), dict)
                       and str(r["bookInfo"].get("bookId", "")) == bid]
            if matches:
                if len(matches) != 1:
                    result["missing_reason"] = "官方搜索返回重复作品ID，未选择冲突记录"
                    return result
                match = matches[0]
                if not is_count(match.get("readingCount")):
                    result["missing_reason"] = "已找到相同作品ID，但今日阅读人数缺失或类型无效"
                    return result
                result.update({"status": "observed_in_search_exact_book_id", "value": match["readingCount"],
                    "source": {"value": match["readingCount"], "observed_at": evidence["observed_at"],
                               "source_url": url, "archive_path": str(archive.resolve()),
                               "response_sha256": evidence.get("response_sha256"),
                               "source_kind": "official_search_exact_book_id", "matched_book_id": bid,
                               "matched_title": match["bookInfo"].get("title"), "search_index": match.get("searchIdx"),
                               "source_field": "books[].readingCount", "source_label": "人今日阅读"}})
                return result
            more = data.get("hasMore")
            if not (more is True or (type(more) is int and more == 1)):
                result["missing_reason"] = "官方搜索已结束，未返回相同作品ID"
                return result
            indexes = [r.get("searchIdx") for r in rows if isinstance(r, dict) and type(r.get("searchIdx")) is int]
            next_offset = max(indexes, default=offset)
            if next_offset <= offset:
                result["missing_reason"] = "官方搜索分页未前进，未返回相同作品ID"
                return result
            offset = next_offset
            time.sleep(0.2)
        except Exception as exc:
            evidence["error"] = str(exc)
            if not result["requests"] or result["requests"][-1] is not evidence:
                result["requests"].append(evidence)
            result["missing_reason"] = "离榜书官方搜索采集失败: " + str(exc)
            return result
    result["missing_reason"] = "达到搜索分页上限，未返回相同作品ID；未继续推测"
    return result


def finalize_today_reading(book, raw_dir):
    observations = [o for o in book["today_reading_observations"] if is_count(o.get("value"))]
    if not observations and not book.get("memberships"):
        search = collect_search_today(book, raw_dir)
        book["today_reading_search"] = search
        if search["source"]:
            observations.append(search["source"])
            book["today_reading_observations"].append(search["source"])
    latest = max(observations, key=lambda o: o["observed_at"]) if observations else None
    book["today_reading_count"] = latest["value"] if latest else None
    book["today_reading_source"] = latest
    if latest:
        book["today_reading_status"] = ("observed_in_search_exact_book_id" if latest.get("source_kind") ==
                                        "official_search_exact_book_id" else "observed_on_chart")
        book["today_reading_missing_reason"] = None
    else:
        book["today_reading_status"] = "unavailable_off_chart" if not book.get("memberships") else "unavailable_on_chart"
        book["today_reading_missing_reason"] = book.get("today_reading_search", {}).get("missing_reason") or "榜单今日阅读人数缺失；不挪用详情页readingStat"
    book["today_reading_conflicting_observations"] = len({o["value"] for o in observations}) > 1


def collect_chart(cid, raw_dir):
    cursor, records, requests, errors = 0, [], [], []
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    identity, identity_valid = None, False
    identity_url = "https://weread.qq.com/web/categoryinfo?" + urllib.parse.urlencode({"rank": 1, "categoryId": cid})
    try:
        body, evidence = get(identity_url)
        archive = raw_dir / (cid + "-info.json")
        archive.write_text(body, encoding="utf-8")
        requests.append({**evidence, "archive_path": str(archive.resolve()), "response_kind": "chart_identity"})
        identity = json.loads(body)
        identity_valid = (isinstance(identity, dict) and not identity.get("errCode") and not identity.get("errcode")
                          and not identity.get("error") and identity.get("CategoryId") == cid
                          and identity.get("title") == CHART_IDENTITIES[cid] and identity.get("ranklist") == 1)
        if not identity_valid:
            errors.append({"url": identity_url, "observed_at": evidence["observed_at"],
                           "error": "Official chart identity absent, changed, or mismatched"})
    except Exception as exc:
        errors.append({"url": identity_url, "observed_at": now(), "error": str(exc)})
    for page_index in range(4 if identity_valid else 0):
        url = "https://weread.qq.com/web/bookListInCategory/" + cid + "?" + urllib.parse.urlencode({"rank": 1, "maxIndex": cursor})
        try:
            body, evidence = get(url)
            archive = raw_dir / (cid + "-" + str(page_index) + ".json")
            archive.write_text(body, encoding="utf-8")
            evidence["archive_path"] = str(archive.resolve())
            data = json.loads(body)
            rows = data.get("books", [])
            requests.append({**evidence, "returned_rows": len(rows), "has_more": data.get("hasMore"),
                             "source_total_count": data.get("totalCount"), "synckey": data.get("synckey")})
            if not isinstance(rows, list) or data.get("errcode"):
                raise ValueError("unexpected response schema")
            if not rows:
                break
            for row in rows:
                rank = row.get("searchIdx")
                info = row.get("bookInfo", {})
                if not isinstance(rank, int) or not 1 <= rank <= 50 or not info.get("bookId"):
                    continue
                records.append({"rank": rank, "book_id": str(info["bookId"]),
                    "title": info.get("title"), "author": info.get("author"), "category": info.get("category"),
                    "copyright_provider_id": info.get("cpid"), "reviewer_count": info.get("newRatingCount"),
                    "recommendation_percent": rating(info.get("newRating")),
                    "recommendation_raw": info.get("newRating"), "today_reading_count": row.get("readingCount"),
                    "platform_publish_time": info.get("publishTime"), "detail_url": detail_url(info),
                    "source_url": url, "observed_at": evidence["observed_at"]})
            next_cursor = max(row.get("searchIdx", 0) for row in rows)
            if next_cursor >= 50 or next_cursor <= cursor or not data.get("hasMore"):
                break
            cursor = next_cursor
            time.sleep(0.3)
        except Exception as exc:
            errors.append({"url": url, "observed_at": now(), "error": str(exc)})
            break
    complete = (identity_valid and not errors and len(records) == 50 and len({r["book_id"] for r in records}) == 50
                and sorted(r["rank"] for r in records) == list(range(1, 51)))
    return {"chart_id": cid, "chart_name": CHARTS[cid], "page_url": "https://weread.qq.com/web/category/" + cid,
            "status": "complete" if complete else "partial", "complete_top50": complete,
            "identity_valid": identity_valid, "identity_info": identity,
            "safe_for_membership_diff": complete, "source_data_date": None,
            "source_date_note": "接口未提供已验证榜单日期；synckey保留原样，不解释为日期。",
            "records": records, "requests": requests, "errors": errors}


def collect_detail(task):
    bid, url, raw_dir = task
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "weread.qq.com" or not parsed.path.startswith("/web/reader/"):
            raise ValueError("Only official WeRead reader metadata URLs are accepted")
        body, evidence = get(url)
        reader = decode_state(body)["reader"]
        info = reader["bookInfo"]
        actual_id = str(info.get("bookId", ""))
        if bid and actual_id != bid:
            raise ValueError("book_id mismatch")
        raw_metadata = {"bookInfo": {k: info.get(k) for k in (
            "bookId", "title", "author", "totalWords", "publisher", "category", "copyrightInfo",
            "newRating", "newRatingCount", "publishTime", "onTime", "cpid", "ranklist")},
            "readingStat": reader.get("readingStat", {})}
        archive = raw_dir / ("detail-" + actual_id + ".json")
        write_json(archive, raw_metadata)
        evidence.update({"archive_path": str(archive.resolve()),
                         "archive_scope": "extracted metadata; full HTML/chapter contents/user state not retained"})
        result = {"book_id": actual_id, "status": "ok", "title": info.get("title"),
            "author": info.get("author"), "word_count": info.get("totalWords"),
            "publisher": info.get("publisher"), "category": info.get("category"),
            "copyright_provider": info.get("copyrightInfo", {}).get("name"),
            "copyright_provider_id": info.get("copyrightInfo", {}).get("id"),
            "reviewer_count": info.get("newRatingCount"), "recommendation_percent": rating(info.get("newRating")),
            "recommendation_raw": info.get("newRating"), "platform_publish_time": info.get("publishTime"),
            "platform_listing_time": listing_time(info.get("onTime")),
            "platform_listing_time_raw": info.get("onTime"),
            "reader_page_reading_count_raw": reader.get("readingStat", {}).get("readingCount"),
            "detail_url": url, "detail_source": evidence}
        time.sleep(0.2)
        return result
    except Exception as exc:
        return {"book_id": bid, "status": "error", "detail_url": url, "error": str(exc), "observed_at": now()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runs" / datetime.now(TZ).date().isoformat() / "weread.json")
    parser.add_argument("--book-urls", type=Path, help="JSON list of official reader URLs to continue tracking off-chart books")
    parser.add_argument("--tracked-json", type=Path, help="JSON list of {book_id, detail_url, ...} for off-chart 30-day tracking")
    parser.add_argument("--skip-details", action="store_true")
    parser.add_argument("--charts", nargs="+", choices=list(CHARTS), help="Collect selected charts only, e.g. a separate supplement")
    parser.add_argument("--no-charts", action="store_true", help="Collect registered books only, retaining separate chart evidence")
    parser.add_argument("--run-kind", choices=["manual_feasibility", "supplement"], default="manual_feasibility")
    args = parser.parse_args()
    started = now()
    raw_dir = args.output.parent / ("raw-weread-" + datetime.now(TZ).strftime("%H%M%S"))
    raw_dir.mkdir(parents=True, exist_ok=True)
    if args.no_charts and args.charts:
        parser.error('--no-charts and --charts are mutually exclusive')
    charts = [] if args.no_charts else [collect_chart(cid, raw_dir) for cid in (args.charts or CHARTS)]
    books = {}
    for chart in charts:
        for row in chart["records"]:
            bid = row["book_id"]
            books.setdefault(bid, {"book_id": bid, "title": row["title"], "detail_url": row["detail_url"],
                                   "author": row["author"], "category": row["category"],
                                   "reviewer_count": row["reviewer_count"],
                                   "recommendation_percent": row["recommendation_percent"],
                                   "copyright_provider_id": row["copyright_provider_id"],
                                   "memberships": [], "today_reading_observations": []})
            books[bid]["memberships"].append({"chart_id": chart["chart_id"], "rank": row["rank"]})
            books[bid]["today_reading_observations"].append({
                "value": row["today_reading_count"], "observed_at": row["observed_at"],
                "source_url": row["source_url"], "chart_id": chart["chart_id"]})
    tasks = [(bid, b["detail_url"], raw_dir) for bid, b in books.items() if b["detail_url"]]
    if args.tracked_json:
        tracked = json.loads(args.tracked_json.read_text(encoding="utf-8-sig"))
        if not isinstance(tracked, list):
            raise ValueError("--tracked-json must contain a JSON list")
        for row in tracked:
            bid, url = str(row["book_id"]), row["detail_url"]
            if bid not in books:
                books[bid] = {**row, "book_id": bid, "detail_url": url,
                              "memberships": [], "today_reading_observations": []}
                tasks.append((bid, url, raw_dir))
    if args.book_urls:
        known_urls = {task[1] for task in tasks}
        tasks += [(None, u, raw_dir) for u in json.loads(args.book_urls.read_text(encoding="utf-8-sig")) if u not in known_urls]
    details = []
    if not args.skip_details:
        with ThreadPoolExecutor(max_workers=2) as pool:
            for i, result in enumerate(pool.map(collect_detail, tasks)):
                details.append(result)
                if (i + 1) % 20 == 0:
                    print("metadata fetched:", i + 1, "/", len(tasks), flush=True)
    for detail in details:
        bid = detail["book_id"]
        if not bid:
            continue
        books.setdefault(bid, {"book_id": bid, "memberships": [], "today_reading_observations": []}).update(detail)
    for book in books.values():
        finalize_today_reading(book, raw_dir)
    data = {"schema_version": "weread.public.v1", "timezone": "Asia/Shanghai", "started_at": started,
        "finished_at": now(), "collection_date": started[:10], "scheduled_for": None,
        "run_kind": args.run_kind, "field_mapping": MAPPING, "charts": charts,
        "books": list(books.values()), "detail_errors": [d for d in details if d["status"] == "error"],
        "gaps": ["快照为实际采集时刻，不是上午10点历史数据；不补造过去30天。",
                 "榜单发布日期未知，采集日期与平台出版时间分别保存。"] + [
                     c["chart_name"] + "采集不完整，不能判断上榜或下榜。" for c in charts if not c["complete_top50"]]
                 + (["部分作品今日阅读缺失；具体原因见各书today_reading_missing_reason。"]
                    if any(b["today_reading_count"] is None for b in books.values()) else [])}
    write_json(args.output, data)
    print(json.dumps({"output": str(args.output.resolve()), "books": len(books), "detail_errors": len(data["detail_errors"]),
        "charts": [{"id": c["chart_id"], "status": c["status"], "count": len(c["records"])} for c in charts]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
