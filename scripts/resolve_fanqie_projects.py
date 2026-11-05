"""Resolve marketing titles to verified official Fanqie IDs, preserving ambiguity.

Read-only. Discovery candidates must come from observed official page links.
Original source_book_id values are retained but NEVER used as Fanqie IDs.
Only confirmed mappings enter snapshot.json; all candidates and reasons remain
in identity-map.json. Screenshot evidence contributes identity, never today's
metrics. A unique full title plus verified 磨铁数盟 footer is an accepted mapping
rule; an absent source author is explicitly disclosed.
"""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import urlsplit, parse_qs

try:
    from .collect_fanqie import Archive, collect, now, write_json, MAPPING, JS_SOURCE, REQUEST_SOURCE
except ImportError:
    from collect_fanqie import Archive, collect, now, write_json, MAPPING, JS_SOURCE, REQUEST_SOURCE


def title_key(text):
    # Cosmetic punctuation is ignored, but version words/numbers are preserved.
    text = unicodedata.normalize("NFKC", text or "")
    return "".join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith("P"))


def author_key(text):
    return re.sub(r"\s+", "", text or "").casefold()


def validate_review(review):
    if review.get("review_method") != "visual_inspection":
        raise ValueError("Unsupported identity review method")
    for path_key, hash_key in [("source_image_path", "source_image_sha256"),
                               ("official_cover_path", "official_cover_sha256")]:
        if path_key == "official_cover_path" and not review.get("cover_visual_match"):
            continue
        if not review.get(path_key) or hashlib.sha256(Path(review[path_key]).read_bytes()).hexdigest() != review.get(hash_key):
            raise ValueError("Identity review evidence missing or hash changed: " + path_key)
    if review.get('synopsis_visual_match'):
        path = Path(review['official_text_path'])
        raw = path.read_bytes()
        receipt = json.loads(path.with_name(path.name+'.source.json').read_text(encoding='utf-8'))
        url = urlsplit(receipt['url'])
        digest = hashlib.sha256(raw).hexdigest()
        if (url.scheme != 'https' or url.netloc != 'api.fqnovel.com'
                or url.path != '/novel_ug/share/landing_page'
                or parse_qs(url.query).get('book_id') != [review.get('quote_match_book_id')]
                or receipt.get('http_status') != 200
                or digest != receipt.get('response_sha256') or digest != review.get('official_text_sha256')):
            raise ValueError('Official synopsis evidence identity/hash mismatch')
        book = json.loads(raw)['data']['book_data']
        quote = title_key(review.get('source_text_quote'))
        if (len(quote) < 40 or quote not in title_key(book.get('intro'))
                or book.get('title') != review.get('official_text_title')):
            raise ValueError('Reviewed screenshot quote is too short or absent from official synopsis')


def choose(source_title, candidates, source_review=None, copyright_aliases=None):
    review = source_review or {}
    aliases = copyright_aliases or {"磨铁数盟"}
    allowed_titles = {title_key(source_title)}
    if review.get("review_method") == "visual_inspection" and review.get("source_image_sha256"):
        allowed_titles.add(title_key(review.get("observed_title")))
    source_author = review.get("observed_author")
    author_verified = review.get("review_method") == "visual_inspection" and review.get("source_image_sha256") and source_author
    same_title_count = sum(title_key(c.get('title')) == title_key(source_title) for c in candidates)
    eligible, assessments = [], []
    for candidate in candidates:
        reason = []
        # This collector verifies ebook detail pages. An audio service screenshot
        # cannot establish that the corresponding electronic edition is the target.
        if review.get("observed_medium") == "audio":
            reason.append("source_audio_edition_not_verified_by_ebook_collector")
        cover_verified = (review.get("review_method") == "visual_inspection" and review.get("source_image_sha256")
                          and review.get("cover_match_book_id") == candidate.get("book_id")
                          and review.get("official_cover_sha256") and review.get("cover_visual_match") is True
                          and title_key(review.get("observed_title")) == title_key(candidate.get("title")))
        synopsis_verified = (review.get('review_method') == 'visual_inspection'
                             and review.get('source_image_sha256') and review.get('official_text_sha256')
                             and review.get('synopsis_visual_match') is True
                             and review.get('quote_match_book_id') == candidate.get('book_id')
                             and review.get('official_text_title') == candidate.get('title')
                             and title_key(source_title) == title_key(candidate.get('title'))
                             and same_title_count == 1
                             and len(title_key(review.get('source_text_quote'))) >= 40)
        if candidate.get("identity_status") != "book_id_and_title_verified":
            reason.append("official_identity_not_verified")
        if title_key(candidate.get("title")) not in allowed_titles:
            reason.append("full_title_or_version_mismatch")
        if author_verified and author_key(candidate.get("author")) != author_key(source_author):
            reason.append("source_author_mismatch")
        if not author_verified and not cover_verified and not synopsis_verified and candidate.get("copyright_provider") not in aliases:
            reason.append("no_source_author_and_no_verified_motie_copyright")
        assessments.append({"book_id": candidate.get("book_id"), "title": candidate.get("title"),
                            "author": candidate.get("author"), "copyright_provider": candidate.get("copyright_provider"),
                            "detail_url": candidate.get("detail_url"), "rejection_reasons": reason,
                            "detail_source": candidate.get("detail_source")})
        if not reason:
            eligible.append(candidate)
    # Same title and multiple authors/versions never silently collapse.
    if len(eligible) == 1:
        chosen = eligible[0]
        basis = ("source_screenshot_title_and_author" if author_verified else
                 "source_screenshot_full_title_and_same_official_cover" if review.get("cover_match_book_id") == chosen["book_id"]
                 else 'source_screenshot_distinctive_synopsis_and_full_source_title' if review.get('quote_match_book_id') == chosen['book_id']
                 else "unique_full_title_and_verified_motie_copyright")
        return {"status": "confirmed", "book_id": chosen["book_id"], "match_basis": basis,
                "source_author_unavailable": not bool(author_verified), "candidates": assessments}
    return {"status": "ambiguous" if len(eligible) > 1 else "unresolved", "book_id": None,
            "match_basis": None, "source_author_unavailable": not bool(author_verified), "candidates": assessments,
            "reason": "multiple_eligible_platform_versions" if len(eligible) > 1 else
                      "official_candidate_not_found" if not candidates else "no_candidate_meets_identity_rule"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True,
                        help="JSON list: source_title, book_id, discovery_sources (observed URLs)")
    parser.add_argument("--source-reviews", type=Path)
    parser.add_argument("--copyright-aliases", type=Path, default=Path("config/copyright-aliases.json"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--candidate-observations", type=Path,
                        help="Reuse today's archived verified candidate observations; do not refetch")
    args = parser.parse_args()
    started = now()
    source_raw = args.source.read_bytes()
    rows = [row for row in json.loads(source_raw)["rows"] if row["normalized"].get("platform") == "fanqie"]
    groups = defaultdict(list)
    for row in rows:
        groups[row["normalized"]["title"]].append(row)
    candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
    reviews = json.loads(args.source_reviews.read_text(encoding="utf-8")) if args.source_reviews else []
    for review in reviews:
        validate_review(review)
    review_by_title = {entry["source_title"]: entry for entry in reviews}
    aliases = set(json.loads(args.copyright_aliases.read_text(encoding="utf-8"))["exact_aliases"])
    archive = Archive(args.output_dir)
    observations, errors = {}, []
    if args.candidate_observations:
        cached = json.loads(args.candidate_observations.read_text(encoding="utf-8"))
        for book in cached:
            if book.get("detail_source", {}).get("observed_at", "")[:10] != started[:10]:
                raise ValueError("Refusing stale cached candidate metrics")
            observations[book["book_id"]] = book
    by_source = defaultdict(list)
    for entry in candidates:
        bid = entry["book_id"]
        if not isinstance(bid, str) or not re.fullmatch(r"\d{10,25}", bid):
            raise ValueError("Candidate Fanqie ID must be an exact long decimal string")
        if not entry.get("discovery_sources"):
            raise ValueError("Candidate must have observed discovery evidence; guessed ID prohibited")
        by_source[entry["source_title"]].append(entry)
        if bid not in observations:
            try:
                observations[bid] = collect(bid, archive)
            except Exception as exc:
                errors.append({"book_id": bid, "error": type(exc).__name__ + ": " + str(exc), "observed_at": now()})
            time.sleep(0.2)
    mappings, confirmed_books = [], {}
    for title, group in groups.items():
        source_candidates = by_source[title]
        available = [observations[entry["book_id"]] for entry in source_candidates if entry["book_id"] in observations]
        available = list({book["book_id"]: book for book in available}.values())
        mapping = choose(title, available, review_by_title.get(title), aliases)
        mapping.update(source_title=title, source_row_keys=[row["source_coordinate_key"] for row in group],
                       source_book_ids_raw=list(dict.fromkeys(row["normalized"].get("source_book_id") for row in group)),
                       source_identity_review=review_by_title.get(title), discovery=source_candidates)
        mappings.append(mapping)
        if mapping["status"] == "confirmed":
            bid = mapping["book_id"]
            book = confirmed_books.setdefault(bid, {**observations[bid], "marketing_source_titles": [], "marketing_source_row_keys": []})
            book["marketing_source_titles"].append(title)
            book["marketing_source_row_keys"].extend(mapping["source_row_keys"])
    summary = {"source_rows": len(rows), "source_titles": len(groups), "confirmed_titles": sum(m["status"] == "confirmed" for m in mappings),
               "confirmed_platform_books": len(confirmed_books), "ambiguous_titles": sum(m["status"] == "ambiguous" for m in mappings),
               "unresolved_titles": sum(m["status"] == "unresolved" for m in mappings)}
    identity = {"schema_version": "fanqie.marketing-identity.v1", "started_at": started, "finished_at": now(),
                "source": str(args.source.resolve()), "source_sha256": hashlib.sha256(source_raw).hexdigest(),
                "summary": summary, "mappings": mappings, "errors": errors,
                "policy": "source screenshot author+title OR full visible title+same official cover OR reviewed distinctive screenshot synopsis+full source title OR unique full title+verified exact Motie copyright; audio editions are separate; keep ambiguity; short internal source IDs never identify Fanqie books"}
    write_json(args.output_dir / "candidate-observations.json", list(observations.values()))
    write_json(args.output_dir / "identity-map.json", identity)
    write_json(args.output_dir / "snapshot.json", {"schema_version": "fanqie.public.v1", "started_at": started,
               "finished_at": now(), "collection_date": started[:10], "books": list(confirmed_books.values()), "errors": errors,
               "status": "partial" if errors or summary["unresolved_titles"] or summary["ambiguous_titles"] else "success",
               "identity_map": str((args.output_dir / "identity-map.json").resolve()), "identity_summary": summary,
               "field_mapping": MAPPING, "mapping_evidence": [JS_SOURCE, REQUEST_SOURCE],
               "publication_rising_board": {"status": "source_not_identified", "complete": False}, "sources": archive.sources})
    print(json.dumps({**summary, "output_dir": str(args.output_dir.resolve()), "errors": len(errors)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
