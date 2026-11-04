#!/usr/bin/env python3
"""Collect public GovScape search results and archived PDFs; Python 3.10+."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import html
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://govscape.net/api/search/"
PDF_BASE = "https://bcgl-public-bucket.s3.amazonaws.com/archive/2020/PDFs/"
RETRYABLE = {408, 429, 500, 502, 503, 504}


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def atomic_write(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(data)
    temporary.replace(path)


def retry_delay(header, attempt):
    if header:
        try:
            return max(0, float(header))
        except ValueError:
            try:
                return max(0, parsedate_to_datetime(header).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    return min(60, 2 ** attempt)


class Client:
    def __init__(self, interval=1.0, retries=4, timeout=60, max_bytes=50 * 1024**2):
        self.interval, self.retries = interval, retries
        self.timeout, self.max_bytes = timeout, max_bytes
        self.last = {}
        self.lock = threading.Lock()

    def request(self, url, payload=None):
        host = urllib.parse.urlsplit(url).netloc
        for attempt in range(self.retries + 1):
            with self.lock:
                time.sleep(max(0, self.last.get(host, 0) + self.interval - time.monotonic()))
                self.last[host] = time.monotonic()
            headers = {"User-Agent": "GovscapeCollector/1.0 (+https://github.com/heidikmkv/govscape-collector)"}
            data = None
            if payload is not None:
                headers["Content-Type"] = "application/json"
                data = dump(payload).encode()
            try:
                request = urllib.request.Request(url, data=data, headers=headers)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read(self.max_bytes + 1)
                    if len(body) > self.max_bytes:
                        raise ValueError("Response exceeds configured size limit")
                    length = response.headers.get("Content-Length")
                    if length and len(body) != int(length):
                        raise OSError("Incomplete response body")
                    return body
            except urllib.error.HTTPError as error:
                if error.code not in RETRYABLE or attempt == self.retries:
                    raise
                delay = retry_delay(error.headers.get("Retry-After"), attempt)
                error.close()
            except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead):
                if attempt == self.retries:
                    raise
                delay = retry_delay(None, attempt)
            time.sleep(delay)

    def search(self, query, page):
        payload = {"query": query["query"], "search_type": query["mode"],
                   "page": page, "filters": query.get("filters", {})}
        result = json.loads(self.request(API, payload))
        if not isinstance(result.get("results"), list) or not isinstance(result.get("pagination"), dict):
            raise ValueError("Unexpected search response schema")
        return payload, result


def validate_pdf(data):
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-4096:]:
        raise ValueError("Missing PDF header or end marker")


def pdf_pages(path, executable):
    if not executable:
        return None
    result = subprocess.run([executable, str(path)], capture_output=True, text=True, timeout=30)
    match = re.search(r"^Pages:\s+(\d+)", result.stdout, re.MULTILINE)
    if result.returncode or not match:
        raise ValueError("pdfinfo could not parse the PDF")
    return int(match[1])


def open_database(path, plan):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pages (
            query_id TEXT, page INTEGER, payload TEXT, response TEXT, fetched_at TEXT,
            PRIMARY KEY (query_id, page));
        CREATE TABLE IF NOT EXISTS candidates (
            id TEXT PRIMARY KEY, metadata TEXT NOT NULL, status TEXT DEFAULT 'pending',
            error TEXT, canonical_id TEXT);
        CREATE TABLE IF NOT EXISTS matches (
            id TEXT, query_id TEXT, page INTEGER, rank INTEGER, metadata TEXT,
            PRIMARY KEY (query_id, page, rank));
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, bytes INTEGER,
            filename TEXT, query_id TEXT, downloaded_at TEXT, page_count INTEGER);
        CREATE INDEX IF NOT EXISTS matches_by_id ON matches(id);
    """)
    saved = db.execute("SELECT value FROM settings WHERE key='plan'").fetchone()
    if saved and saved[0] != dump(plan):
        db.close()
        raise ValueError("This output directory belongs to a different plan; use a new directory")
    db.execute("INSERT OR IGNORE INTO settings VALUES ('plan', ?)", (dump(plan),))
    db.commit()
    return db


def seed_collection(db, output, seed, plan):
    """Reuse a prior run locally, retaining its cached searches and provenance."""
    if db.execute("SELECT 1 FROM settings WHERE key='seed'").fetchone():
        return
    if db.execute("SELECT count(*) FROM documents").fetchone()[0]:
        raise ValueError("Seeding requires an empty collection")
    source_path = (seed / "collection.sqlite3").resolve()
    if source_path == (output / "collection.sqlite3").resolve():
        raise ValueError("Seed and output must be different directories")
    source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        old_plan = json.loads(source.execute("SELECT value FROM settings WHERE key='plan'").fetchone()[0])
        new_queries = {q["id"]: q for q in plan["queries"]}
        for q in old_plan["queries"]:
            new = new_queries.get(q["id"], {})
            if any(q.get(k, {}) != new.get(k, {}) for k in ("query", "mode", "filters")):
                raise ValueError("Seed search definitions must match the new plan")
        documents = source.execute("SELECT * FROM documents ORDER BY rowid").fetchall()
        for qid, count in Counter(r["query_id"] for r in documents).items():
            if count > new_queries[qid]["quota"]:
                raise ValueError("Seed document count exceeds a new query quota")
        for row in documents:
            original = seed / row["filename"]
            data = original.read_bytes()
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError(f"Seed PDF hash mismatch: {original}")
            atomic_write(output / row["filename"], data)
        with db:
            for table in ("pages", "candidates", "matches", "documents"):
                rows = source.execute(f"SELECT * FROM {table}").fetchall()
                if rows:
                    placeholders = ",".join("?" for _ in rows[0])
                    db.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
            info = {"plan_name": old_plan.get("name"), "document_count": len(documents),
                    "document_ids": [r["id"] for r in documents],
                    "manifest_sha256": hashlib.sha256((seed / "manifest.jsonl").read_bytes()).hexdigest()}
            db.execute("INSERT INTO settings VALUES ('seed', ?)", (dump(info),))
    finally:
        source.close()


def validate_plan(plan):
    queries = plan.get("queries", [])
    if not queries or len({q["id"] for q in queries}) != len(queries):
        raise ValueError("Plan needs queries with unique IDs")
    for q in queries:
        if q["mode"] not in {"keyword", "visual", "textual"} or not q["query"].strip():
            raise ValueError("Invalid search mode or empty query")
        if not isinstance(q["quota"], int) or q["quota"] <= 0:
            raise ValueError("Query quotas must be positive integers")
    return queries


def cached_page(db, client, query, page):
    saved = db.execute("SELECT * FROM pages WHERE query_id=? AND page=?", (query["id"], page)).fetchone()
    if saved:
        return json.loads(saved["response"])
    payload, response = client.search(query, page)
    db.execute("INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
               (query["id"], page, dump(payload), dump(response), now()))
    db.commit()
    return response


def record_matches(db, query, page, results):
    ids = []
    for rank, result in enumerate(results, 1):
        doc_id = result.get("pdf", "")
        if not isinstance(doc_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", doc_id):
            raise ValueError("Unsafe or unsupported PDF identifier in search response")
        ids.append(doc_id)
        db.execute("INSERT OR IGNORE INTO candidates(id,metadata) VALUES (?, ?)", (doc_id, dump(result)))
        db.execute("INSERT OR IGNORE INTO matches VALUES (?, ?, ?, ?, ?)",
                   (doc_id, query["id"], page, rank, dump(result)))
    db.commit()
    return ids


def fetch_pdf(client, doc_id, output):
    path = output / "pdfs" / (doc_id + ".pdf")
    return path.read_bytes() if path.exists() else client.request(PDF_BASE + doc_id + ".pdf")


def download(db, client, doc_id, query_id, output, parser=None, prefetched=None):
    candidate = db.execute("SELECT status FROM candidates WHERE id=?", (doc_id,)).fetchone()
    if candidate[0] in {"downloaded", "duplicate", "failed"}:
        return False
    path = output / "pdfs" / (doc_id + ".pdf")
    try:
        if isinstance(prefetched, Exception):
            raise prefetched
        data = fetch_pdf(client, doc_id, output) if prefetched is None else prefetched
        validate_pdf(data)
        sha = hashlib.sha256(data).hexdigest()
        duplicate = db.execute("SELECT id FROM documents WHERE sha256=?", (sha,)).fetchone()
        if duplicate:
            db.execute("UPDATE candidates SET status='duplicate',canonical_id=? WHERE id=?", (duplicate[0], doc_id))
            db.commit()
            return False
        staging = path.with_suffix(".pdf.part")
        atomic_write(staging, data)
        count = pdf_pages(staging, parser)
        staging.replace(path)
        db.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (doc_id, sha, len(data), "pdfs/" + path.name, query_id, now(), count))
        db.execute("UPDATE candidates SET status='downloaded',error=NULL WHERE id=?", (doc_id,))
        db.commit()
        return True
    except (OSError, ValueError, http.client.IncompleteRead, subprocess.SubprocessError) as error:
        db.execute("UPDATE candidates SET status='failed',error=? WHERE id=?", (str(error), doc_id))
        db.commit()
        print(f"Download failed: {doc_id}: {error}", file=sys.stderr, flush=True)
        return False


def export(db, output, plan, limit, reason):
    queries = {q["id"]: q for q in plan["queries"]}
    seed = db.execute("SELECT value FROM settings WHERE key='seed'").fetchone()
    seed = json.loads(seed[0]) if seed else None
    seed_ids = set(seed["document_ids"]) if seed else set()
    page_sizes = {(r["query_id"], r["page"]): json.loads(r["response"])["pagination"].get("page_size", 20)
                  for r in db.execute("SELECT query_id,page,response FROM pages")}
    records = []
    for row in db.execute("SELECT d.*,c.metadata FROM documents d JOIN candidates c USING(id) ORDER BY d.rowid"):
        r = dict(row)
        raw = json.loads(r.pop("metadata"))
        matches = []
        for match in db.execute("SELECT * FROM matches WHERE id=? ORDER BY query_id,page,rank", (r["id"],)):
            q = queries[match["query_id"]]
            metadata = json.loads(match["metadata"])
            raw_page = metadata.get("page")
            page_size = page_sizes[(match["query_id"], match["page"])]
            matches.append({"query_id": q["id"], "query": q["query"], "mode": q["mode"],
                            "filters": q.get("filters", {}), "results_page": match["page"],
                            "rank_on_page": match["rank"], "rank_overall": (match["page"] - 1) * page_size + match["rank"],
                            "pdf_page_index": raw_page,
                            "pdf_page_number": int(raw_page) + 1 if str(raw_page).isdigit() else None,
                            "raw_result": metadata})
        original = raw.get("crawl_url", "")
        r.update({"source_url": original, "source_filename": urllib.parse.unquote(urllib.parse.urlsplit(original).path.rsplit("/", 1)[-1]),
                  "crawl_date": raw.get("crawl_date"), "source_domain": raw.get("sub_domain"),
                  "download_url": PDF_BASE + r["id"] + ".pdf", "raw_source_metadata": raw,
                  "matches": matches, "reused_from_seed": r["id"] in seed_ids,
                  "redaction_review_status": "unreviewed"})
        records.append(r)
    atomic_write(output / "manifest.jsonl", ("".join(dump(r) + "\n" for r in records)).encode())
    statuses = dict(db.execute("SELECT status,count(*) FROM candidates GROUP BY status").fetchall())
    report = {"updated_at": now(), "target": limit, "downloaded_unique": len(records),
              "stop_reason": reason, "total_bytes": sum(r["bytes"] for r in records),
              "source_domains": dict(Counter(r["source_domain"] for r in records)),
              "selected_by_query": dict(Counter(r["query_id"] for r in records)),
              "candidate_statuses": statuses, "search_pages_cached": db.execute("SELECT count(*) FROM pages").fetchone()[0],
              "scope": "Candidate collection only; redaction presence and quality are unreviewed.",
              "plan": plan}
    if seed:
        report["seed"] = seed
        report["new_unique_documents"] = len(records) - len(seed_ids)
    atomic_write(output / "report.json", (json.dumps(report, indent=2) + "\n").encode())
    write_review(output, records, report)
    return report


def write_review(output, records, report):
    """A local file index; no remote scripts, images, or requests."""
    rows = []
    for number, r in enumerate(records, 1):
        name = html.escape(r["source_filename"] or r["id"])
        domain = html.escape(r["source_domain"] or "unknown")
        source = r["source_url"]
        source_link = (f'<a href="{html.escape(source, quote=True)}">Original source</a>'
                       if urllib.parse.urlsplit(source).scheme in {"http", "https"} else "")
        query = html.escape(r["query_id"])
        batch = "Reused" if r.get("reused_from_seed") else "New"
        pages = r["page_count"] if r["page_count"] is not None else "—"
        match = next((m for m in r["matches"] if m["query_id"] == r["query_id"]), {})
        page = match.get("pdf_page_number") or 1
        link = html.escape(r["filename"], quote=True) + f"#page={page}"
        rows.append(f'<tr><td>{number}</td><td><a href="{link}">{name}</a><br><small>{html.escape(r["id"])}</small></td>'
                    f'<td>{domain}<br>{source_link}</td><td>{query}</td><td>{batch}</td><td>{pages}</td><td>{page}</td></tr>')
    body = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>GovScape collection review</title>
<style>body{font:16px system-ui;max-width:1300px;margin:40px auto;padding:0 24px;color:#172634}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:12px;border-bottom:1px solid #dde3e8;vertical-align:top}
th{background:#eff4f7}small{color:#526575;font-size:11px;overflow-wrap:anywhere}a{color:#126095}td:nth-child(2){max-width:420px;overflow-wrap:anywhere}</style>
<h1>GovScape collection review</h1><p>COUNT unique PDFs collected. Redaction presence and quality are unreviewed.</p>
<p>PDF links open at the matching page from the search that selected the document. Original source links may serve a different version from the archived PDF.</p>
<p><a href="manifest.jsonl">Source metadata</a> · <a href="report.json">Collection report</a></p>
<table><thead><tr><th>#</th><th>Archived PDF</th><th>Source</th><th>Selected by</th><th>Batch</th><th>Pages</th><th>Match page</th></tr></thead><tbody>ROWS</tbody></table></html>"""
    body = body.replace("COUNT", str(report["downloaded_unique"])).replace("ROWS", "\n".join(rows))
    atomic_write(output / "review.html", body.encode())


def collect(db, client, plan, output, limit, max_pages=30, parser=None,
            workers=1, download_client=None):
    queries = validate_plan(plan)
    for row in db.execute("SELECT * FROM documents"):
        path = output / row["filename"]
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError(f"Saved file missing or changed: {path}; restore before resuming")
    download_client = download_client or client
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for query in queries:
            previous = set()
            selected = db.execute("SELECT count(*) FROM documents WHERE query_id=?", (query["id"],)).fetchone()[0]
            for page in range(1, max_pages + 1):
                total = db.execute("SELECT count(*) FROM documents").fetchone()[0]
                if total >= limit or selected >= query["quota"]:
                    break
                response = cached_page(db, client, query, page)
                ids = record_matches(db, query, page, response["results"])
                if not ids or not set(ids) - previous:
                    break
                previous.update(ids)
                print(f"Search {query['id']} page {page}: {len(ids)} results", flush=True)
                pending = [i for i in dict.fromkeys(ids) if db.execute(
                    "SELECT status FROM candidates WHERE id=?", (i,)).fetchone()[0] == "pending"]
                while pending and total < limit and selected < query["quota"]:
                    # Queue at most one search page, keeping the bounded pool busy
                    # without fetching beyond either remaining quota.
                    count = min(len(pending), limit - total, query["quota"] - selected)
                    chunk, pending = pending[:count], pending[count:]
                    futures = [(i, pool.submit(fetch_pdf, download_client, i, output)) for i in chunk]
                    # Accept in ranking order, while transfers run concurrently.
                    for doc_id, future in futures:
                        try:
                            data = future.result()
                        except Exception as error:
                            data = error
                        if download(db, download_client, doc_id, query["id"], output, parser, data):
                            selected += 1
                            total += 1
                            if total % 25 == 0:
                                print(f"Saved {total}/{limit} unique PDFs", flush=True)
                export(db, output, plan, limit, "in_progress")
                if not response["pagination"].get("has_next_page", False):
                    break
    total = db.execute("SELECT count(*) FROM documents").fetchone()[0]
    return export(db, output, plan, limit, "target_reached" if total >= limit else "searches_exhausted_or_page_cap")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan", type=Path, default=Path("plans/pilot.json"))
    ap.add_argument("--output", type=Path, default=Path("data/pilot-100"))
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--max-pages", type=int, default=30, help="Maximum pages per query")
    ap.add_argument("--interval", type=float, default=1.0, help="Minimum seconds between requests to each host")
    ap.add_argument("--pdfinfo", action="store_true", help="Require pdfinfo parsing and record page counts")
    ap.add_argument("--retry-failed", action="store_true", help="Retry candidates previously marked failed")
    ap.add_argument("--seed-from", type=Path, help="Reuse a prior collection without downloading its documents again")
    ap.add_argument("--workers", type=int, default=1, help="Concurrent PDF transfers (1-8); search requests remain sequential")
    ap.add_argument("--download-interval", type=float, default=1.0, help="Minimum seconds between PDF transfer starts per host")
    args = ap.parse_args()
    plan = json.loads(args.plan.read_text())
    queries = validate_plan(plan)
    if args.limit <= 0 or args.limit > sum(q["quota"] for q in queries) or args.max_pages < 1 or args.interval < 0:
        ap.error("Limit must be positive and within plan quotas; max-pages >= 1 and interval >= 0")
    if not 1 <= args.workers <= 8 or args.download_interval < 0:
        ap.error("Workers must be between 1 and 8; download-interval must be nonnegative")
    parser = shutil.which("pdfinfo") if args.pdfinfo else None
    if args.pdfinfo and not parser:
        ap.error("pdfinfo is required with --pdfinfo (install Poppler)")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "pdfs").mkdir(exist_ok=True)
    # Lock the run, preventing concurrent processes from overwriting its files.
    import fcntl
    with (args.output / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            ap.error("Another collector is using this output directory")
        db = open_database(args.output / "collection.sqlite3", plan)
        if args.seed_from:
            seed_collection(db, args.output, args.seed_from, plan)
        if args.retry_failed:
            db.execute("UPDATE candidates SET status='pending',error=NULL WHERE status='failed'")
            db.commit()
        try:
            report = collect(db, Client(interval=args.interval), plan, args.output, args.limit, args.max_pages, parser,
                             args.workers, Client(interval=args.download_interval))
            print(json.dumps({k: report[k] for k in ("downloaded_unique", "target", "stop_reason", "total_bytes")}), flush=True)
            return 0 if report["downloaded_unique"] == args.limit else 2
        except KeyboardInterrupt:
            export(db, args.output, plan, args.limit, "interrupted")
            return 130
        except Exception:
            export(db, args.output, plan, args.limit, "error")
            raise
        finally:
            db.close()


if __name__ == "__main__":
    sys.exit(main())
