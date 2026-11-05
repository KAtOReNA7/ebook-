"""Read-only publication rising ranking probe; no Base writes or notifications.

Discovers the ranking cell and algorithm from the current official directory.
Archives both responses and rejects incomplete/ambiguous/changed rankings.
This standalone entry does not enable the paused daily ranking pipeline.
"""
import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

try:
    from .collect_fanqie import Archive, ROOT, TZ, now, write_json
except ImportError:
    from collect_fanqie import Archive, ROOT, TZ, now, write_json

HOST = "https://api5-normal-sinfonlineb.fqnovel.com"
DIRECTORY_PATH = "/reading/bookapi/bookmall/tab/v"
RANK_PATH = "/reading/bookapi/bookmall/cell/change/v1/"
BOARD_NAME = "出版飙升榜"
EXPECTED_ALGORITHM = "197"
# Compatibility parameters, not a device registration or account session.
# manifest/update version fields are necessary for the observed new directory.
CLIENT = {
    "aid": "1967", "app_name": "novelapp", "version_code": "72432",
    "version_name": "7.2.4.32", "manifest_version_code": "72432",
    "update_version_code": "72432", "channel": "xiaomi_1967_64",
    "device_platform": "android", "os": "android",
}
CSV_FIELDS = ["rank", "book_id", "title", "author", "rising_value_display",
              "score_display", "reader_label_raw", "detail_url"]


def route_params(route):
    """Decode only observed nested navigation URLs; never request their hosts."""
    for _ in range(5):
        query = parse_qs(urlsplit(route).query, keep_blank_values=True)
        if "main_algo_name" in query and "main_algo_type" in query:
            if any(len(values) != 1 for values in query.values()):
                raise ValueError("Ambiguous repeated route parameters")
            return {key: values[0] for key, values in query.items()}
        nested = query.get("url") or query.get("surl")
        if not nested or len(nested) != 1:
            break
        route = nested[0]
    raise ValueError("Ranking route parameters absent")


def identify_route(route):
    params = route_params(route)
    names = params["main_algo_name"].split(",")
    algorithms = params["main_algo_type"].split(",")
    if len(names) != len(algorithms) or names.count(BOARD_NAME) != 1:
        raise ValueError("Publication rising name/algorithm mapping absent or ambiguous")
    algo = algorithms[names.index(BOARD_NAME)]
    if algo != EXPECTED_ALGORITHM:
        raise ValueError("Publication rising algorithm changed; review source")
    if not re.fullmatch(r"\d{10,25}", params.get("cell_id", "")):
        raise ValueError("Exact official ranking cell ID absent")
    if params.get("category_id") != "0" or params.get("list_type") != "daily":
        raise ValueError("Unexpected filtered or non-daily ranking scope")
    return params, algo


def discover(payload):
    if type(payload.get("code")) is not int or payload["code"] != 0:
        raise ValueError("Official directory failed")
    found = {}

    def walk(node):
        if not isinstance(node, dict):
            return
        route = node.get("cell_url")
        if isinstance(route, str):
            try:
                params, algo = identify_route(route)
            except ValueError:
                pass
            else:
                if node.get("cell_id_str") != params["cell_id"]:
                    raise ValueError("Directory cell ID disagrees with route")
                found[(params["cell_id"], algo)] = params
        for child in node.get("cell_data", []):
            walk(child)

    for tab in payload.get("data", {}).get("tab_item", []):
        walk(tab)
    if len(found) != 1:
        raise ValueError("Expected one verified publication rising entry; found " + str(len(found)))
    (cell_id, algo), params = next(iter(found.items()))
    for key in ("tab_type", "cell_gender", "genre_type"):
        if not re.fullmatch(r"\d+", params.get(key, "")):
            raise ValueError("Missing official routing parameter: " + key)
    return {"board_name": BOARD_NAME, "algo_type": algo, "cell_id": cell_id,
            "route_params": {key: params[key] for key in
                             ("cell_id", "tab_type", "cell_gender", "genre_type", "category_id", "list_type")}}


def normalize(payload, entry, expected_size=100):
    if type(payload.get("code")) is not int or payload["code"] != 0:
        raise ValueError("Official ranking request failed")
    data = payload.get("data", {})
    if data.get("has_more") is not False:
        raise ValueError("Ranking has more pages or completion flag is missing")
    cell = data.get("cell_view", {})
    params, algo = identify_route(cell.get("cell_url", ""))
    if (cell.get("cell_id_str") != entry["cell_id"] or params["cell_id"] != entry["cell_id"]
            or params.get("algo_type") != entry["algo_type"] or algo != entry["algo_type"]):
        raise ValueError("Ranking response identity/algorithm mismatch")
    books = cell.get("book_data")
    if not isinstance(books, list) or len(books) != expected_size:
        raise ValueError("Ranking size changed or incomplete; review expected size")
    rows, ids = [], set()
    for position, book in enumerate(books, 1):
        bid = book.get("book_id")
        if not isinstance(bid, str) or not re.fullmatch(r"\d{10,25}", bid) or bid in ids:
            raise ValueError("Invalid or repeated official book ID")
        if (str(book.get("is_ebook")) != "1" or str(book.get("genre")) != "6"
                or str(book.get("book_type")) != "0"):
            raise ValueError("Non-publication ebook or unknown medium in ranking")
        evidence_rank = json.loads(book.get("recommend_info", "{}")).get("rank")
        if str(evidence_rank) != str(position):
            raise ValueError("Ranking gap, duplicate or response order mismatch")
        title = book.get("book_name")
        score = book.get("rank_score")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Book title absent")
        if not isinstance(score, str) or not re.fullmatch(r"\d+(?:\.\d+)?[万亿]?飙升值", score):
            raise ValueError("Rising-value label absent or changed")
        ids.add(bid)
        rows.append({"rank": position, "book_id": bid, "title": title,
                     "author": book.get("author"), "rising_value_display": score,
                     "score_display": book.get("score"), "reader_label_raw": book.get("sub_info"),
                     "detail_url": "https://fanqienovel.com/page/" + bid})
    return {"board_name": BOARD_NAME, "algo_type": algo, "cell_id": entry["cell_id"],
            "status": "success", "complete": True, "count": len(rows), "books": rows,
            "completion_basis": "expected size, terminal flag, unique ebook IDs and explicit consecutive ranks",
            "expected_size": expected_size, "has_more": False,
            "rank_version_raw": data.get("rank_version"), "source_updated_at": None,
            "ranking_description": cell.get("cell_alias"),
            "note": "榜单是实际请求时的响应；平台未提供明确更新时间。飙升值不代表累计读者或新增读者。"}


def collect(archive):
    directory_query = {**CLIENT, "tab_type": "2", "offset": "0", "tab_index": "0",
                       "last_tab_index": "0", "classic_tab_style": "v3", "client_req_type": "3"}
    raw, directory_source = archive.get(HOST + DIRECTORY_PATH + "?" + urlencode(directory_query), "directory.json")
    entry = discover(json.loads(raw))
    query = {**CLIENT, **entry["route_params"], "algo_type": entry["algo_type"],
             "change_type": "1", "offset": "0", "limit": "12", "client_req_type": "1", "book_type": "0"}
    raw, ranking_source = archive.get(HOST + RANK_PATH + "?" + urlencode(query), "ranking.json")
    return {**normalize(json.loads(raw), entry), "directory_source": directory_source,
            "ranking_source": ranking_source, "discovery": entry}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    directory = args.output_dir or ROOT / "snapshots" / "fanqie-rising" / datetime.now(TZ).strftime("%Y-%m-%d_%H%M%S_%f")
    archive = Archive(directory)
    started = now()
    try:
        result = collect(archive)
    except Exception as exc:
        result = {"board_name": BOARD_NAME, "status": "failed", "complete": False,
                  "error": type(exc).__name__ + ": " + str(exc)}
    report = {"schema_version": "fanqie.publication-rising.v1", "started_at": started,
              "finished_at": now(), **result, "sources": archive.sources}
    write_json(directory / "snapshot.json", report)
    if result["complete"]:
        with (directory / "ranking.csv").open("w", encoding="utf-8-sig", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(result["books"])
    print(json.dumps({"snapshot": str((directory / "snapshot.json").resolve()),
                      "status": result["status"], "complete": result["complete"],
                      "count": result.get("count"), "error": result.get("error")}, ensure_ascii=False))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
