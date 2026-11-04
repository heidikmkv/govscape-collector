import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, call
import urllib.error

import collector as c


def pdf(label):
    return b"%PDF-1.4\n" + label.encode() + b"\n%%EOF\n"


def result(doc_id):
    return {"pdf": doc_id, "page": "0", "crawl_url": f"https://agency.gov/{doc_id}.pdf",
            "crawl_date": "2021-01-01", "sub_domain": "agency.gov"}


class FakeClient:
    def __init__(self, pages, files):
        self.pages, self.files, self.calls = pages, files, []

    def search(self, query, page):
        self.calls.append(("search", query["id"], page))
        ids, has_next = self.pages[(query["id"], page)]
        return {"query": query["query"], "page": page}, {
            "results": [result(i) for i in ids],
            "pagination": {"has_next_page": has_next, "total_count": 0, "page_size": 20}}

    def request(self, url):
        doc_id = url.rsplit("/", 1)[-1][:-4]
        self.calls.append(("download", doc_id))
        return self.files[doc_id]


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name)
        (self.output / "pdfs").mkdir()
        self.plan = {"queries": [{"id": "q", "mode": "keyword", "query": "redacted", "quota": 3}]}
        self.db = c.open_database(self.output / "collection.sqlite3", self.plan)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_content_deduplication_backfills_and_preserves_metadata(self):
        client = FakeClient({("q", 1): (["A", "B", "A", "C", "D"], False)},
                            {"A": pdf("same"), "B": pdf("same"), "C": pdf("C"), "D": pdf("D")})
        report = c.collect(self.db, client, self.plan, self.output, 3)
        self.assertEqual(report["downloaded_unique"], 3)
        self.assertEqual(report["candidate_statuses"]["duplicate"], 1)
        rows = [json.loads(s) for s in (self.output / "manifest.jsonl").read_text().splitlines()]
        self.assertEqual([r["id"] for r in rows], ["A", "C", "D"])
        self.assertEqual(len(rows[0]["matches"]), 2)
        self.assertEqual(rows[0]["matches"][0]["pdf_page_number"], 1)
        self.assertEqual(rows[0]["source_url"], "https://agency.gov/A.pdf")

    def test_resume_uses_cached_pages_and_existing_downloads(self):
        client = FakeClient({("q", 1): (["A", "B", "C"], False)}, {i: pdf(i) for i in "ABC"})
        c.collect(self.db, client, self.plan, self.output, 1)
        client.calls.clear()
        report = c.collect(self.db, client, self.plan, self.output, 3)
        self.assertEqual(client.calls, [("download", "B"), ("download", "C")])
        self.assertEqual(report["downloaded_unique"], 3)
        client.calls.clear()
        c.collect(self.db, client, self.plan, self.output, 3)
        self.assertEqual(client.calls, [])

    def test_repeated_pages_stop_even_with_has_next_and_zero_total(self):
        client = FakeClient({("q", 1): (["A"], True), ("q", 2): (["A"], True)}, {"A": pdf("A")})
        report = c.collect(self.db, client, self.plan, self.output, 3)
        self.assertEqual(report["downloaded_unique"], 1)
        self.assertEqual(report["search_pages_cached"], 2)
        self.assertNotEqual(report["stop_reason"], "target_reached")

    def test_seed_reuses_pilot_and_excludes_same_content_under_new_id(self):
        initial = FakeClient({("q", 1): (["A", "B", "C"], True)}, {i: pdf(i) for i in "ABC"})
        c.collect(self.db, initial, self.plan, self.output, 3)
        expanded = {"queries": [dict(self.plan["queries"][0], quota=5)]}
        target = self.output / "expanded"
        (target / "pdfs").mkdir(parents=True)
        db = c.open_database(target / "collection.sqlite3", expanded)
        try:
            c.seed_collection(db, target, self.output, expanded)
            c.seed_collection(db, target, self.output, expanded)
            client = FakeClient({("q", 2): (["D", "E", "F"], False)},
                                {"D": pdf("A"), "E": pdf("E"), "F": pdf("F")})
            report = c.collect(db, client, expanded, target, 5, workers=3)
            self.assertEqual(report["downloaded_unique"], 5)
            self.assertEqual(report["new_unique_documents"], 2)
            self.assertEqual(report["candidate_statuses"]["duplicate"], 1)
            self.assertEqual(report["seed"]["document_count"], 3)
            self.assertNotIn(("search", "q", 1), client.calls)
            self.assertEqual({x[1] for x in client.calls if x[0] == "download"}, {"D", "E", "F"})
            self.assertEqual((target / "pdfs/A.pdf").read_bytes(), (self.output / "pdfs/A.pdf").read_bytes())
        finally:
            db.close()

    def test_concurrent_transfers_do_not_overfetch_past_target(self):
        client = FakeClient({("q", 1): (["A", "B", "C"], False)}, {i: pdf(i) for i in "ABC"})
        report = c.collect(self.db, client, self.plan, self.output, 2, workers=4)
        self.assertEqual(report["downloaded_unique"], 2)
        self.assertEqual({x[1] for x in client.calls if x[0] == "download"}, {"A", "B"})

    def test_seed_rejects_different_queries(self):
        changed = {"queries": [dict(self.plan["queries"][0], query="different")]}
        target = self.output / "different"
        (target / "pdfs").mkdir(parents=True)
        db = c.open_database(target / "collection.sqlite3", changed)
        try:
            with self.assertRaisesRegex(ValueError, "definitions"):
                c.seed_collection(db, target, self.output, changed)
        finally:
            db.close()

    def test_bad_pdf_is_recorded_and_replaced_by_next_candidate(self):
        client = FakeClient({("q", 1): (["A", "B"], False)}, {"A": b"<html>error</html>", "B": pdf("B")})
        report = c.collect(self.db, client, self.plan, self.output, 1)
        self.assertEqual(report["candidate_statuses"]["failed"], 1)
        self.assertEqual(report["downloaded_unique"], 1)
        self.assertFalse((self.output / "pdfs/A.pdf").exists())

    def test_changed_local_file_fails_before_network(self):
        client = FakeClient({("q", 1): (["A"], False)}, {"A": pdf("A")})
        c.collect(self.db, client, self.plan, self.output, 1)
        (self.output / "pdfs/A.pdf").write_bytes(pdf("changed"))
        client.calls.clear()
        with self.assertRaisesRegex(ValueError, "missing or changed"):
            c.collect(self.db, client, self.plan, self.output, 1)
        self.assertEqual(client.calls, [])

    def test_parser_failure_keeps_rejected_file_out_of_pdf_set(self):
        client = FakeClient({("q", 1): (["A", "B"], False)}, {"A": pdf("A"), "B": pdf("B")})
        with patch("collector.pdf_pages", side_effect=[ValueError("parse failed"), 2]):
            report = c.collect(self.db, client, self.plan, self.output, 1, parser="pdfinfo")
        self.assertEqual(report["downloaded_unique"], 1)
        self.assertEqual([p.name for p in (self.output / "pdfs").glob("*.pdf")], ["B.pdf"])

    def test_rejects_path_traversal_and_changed_plan(self):
        with self.assertRaisesRegex(ValueError, "identifier"):
            c.record_matches(self.db, self.plan["queries"][0], 1, [result("../bad")])
        with self.assertRaisesRegex(ValueError, "different plan"):
            c.open_database(self.output / "collection.sqlite3", {"queries": []})

    def test_http_retries_respect_retry_after(self):
        error = urllib.error.HTTPError(c.API, 429, "slow down", {"Retry-After": "3"}, io.BytesIO())
        response = io.BytesIO(b"ok")
        response.headers = {"Content-Length": "2"}
        with patch("urllib.request.urlopen", side_effect=[error, response]) as request, patch("time.sleep") as sleep:
            self.assertEqual(c.Client(interval=0).request(c.API), b"ok")
            self.assertEqual(request.call_count, 2)
            self.assertIn(call(3.0), sleep.call_args_list)

    def test_oversized_response_is_rejected(self):
        response = io.BytesIO(b"too large")
        response.headers = {}
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "size limit"):
                c.Client(interval=0, max_bytes=3).request(c.API)


if __name__ == "__main__":
    unittest.main()
