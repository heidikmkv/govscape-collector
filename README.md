# Govscape collector

A resumable Python collector for public government PDF candidates from [GovScape](https://govscape.net/). It uses the same search API and archived PDF download URLs as the website. No Python package dependencies or language-model calls are needed during collection.

The completed [3,000-document collection](runs/collection-3000/README.md) contains 100 reused pilot records and 2,900 new PDFs, with no duplicate IDs or SHA-256 hashes. Its source metadata and verification results are committed here; the 4.98 GB of PDF binaries remain local. The original [100-document pilot](runs/pilot-100/README.md) is preserved.

## Run the 100-document pilot

Requires Python 3.10+ on macOS or Linux. From this repository:

```sh
python3 -m unittest discover -s tests -v
python3 collector.py --plan plans/pilot.json --output data/pilot-100 --limit 100
```

For stronger PDF validation and page counts, install Poppler and add `--pdfinfo`. The actual pilot was run with this flag. All files are checked for a PDF header and end marker, regardless of this option.

Re-run the same command to resume. Cached search pages and completed downloads are reused, and existing file hashes are verified. Use `--retry-failed` to retry exhausted download failures. Only run one collector per output directory. The collector stops at the explicit limit and never schedules a larger run. Increase `--max-pages` as needed (20 results per page before deduplication). Use a new output directory when changing plans.

The pilot allocates 40 unique PDFs to visual search for `redacted documents`, and 20 each to keyword searches for `redacted`, `withheld`, and `"top secret"`. Queries run in that order, assigning overlapping PDFs to the earliest query. This is a purposive candidate sample, not a representative sample of government records. Matches do not prove that a PDF has redactions; visual search ranks all eligible records, including irrelevant results further down the ranking.

## Expand to 3,000 total unique documents

```sh
python3 collector.py --plan plans/collection-3000.json \
  --output data/collection-3000 --limit 3000 --max-pages 200 \
  --seed-from data/pilot-100 --pdfinfo --workers 8 --download-interval 0.25
```

This includes the 100 pilot PDFs and collects 2,900 additional unique PDFs. The pilot files are copied locally and hash-verified; their document IDs, content hashes, source metadata, and cached search pages are reused without new network transfers. The expanded plan keeps the same search definitions and allocates 1,200 documents to visual search and 600 to each keyword search. The original pilot directory and committed metadata snapshot remain intact.

The seed must have compatible query definitions and fit the new quotas. Seeding is performed once per output directory; subsequent runs resume the expanded collection. Deduplication by both ID and SHA-256 applies across the entire combined collection. The manifest marks reused pilot records with `reused_from_seed`; the report records the seed manifest hash, seed document IDs, and number of new unique documents.

Up to eight PDF transfers may run concurrently, with at least 0.25 seconds between transfer starts to the archive host. Search requests remain sequential with their default one-second spacing. All database writes and PDF acceptance occur on the main thread in search-ranking order. At most the remaining quota is fetched, so concurrency does not overrun the requested document count. Review files and manifests are updated after each search page and at completion; SQLite persists each accepted document.

## Outputs

- `data/pilot-100/pdfs/`: original archived PDFs, named by GovScape document ID.
- `manifest.jsonl`: one row per unique downloaded PDF, with original URL/name/domain, crawl date, archive download URL, SHA-256, byte count, optional PDF page count, download time, raw source metadata, and observed search matches.
- `report.json`: counts, source-domain distribution, collection plan, and stop reason.
- `review.html`: local review index linking to the archived PDFs at matching pages.
- `collection.sqlite3`: complete cached search responses, candidate failures, duplicate mappings, and restart state.

Search matches include query/mode/filters, results page, rank, and matching PDF page. GovScape API PDF page indices are zero-based; the manifest also supplies a one-based page number. Overall rank uses the API's reported page size (falling back to the observed size of 20). Only encountered matches are recorded; this is not an exhaustive cross-query relevance assessment. Raw search responses remain in the local database. Metadata snapshots for the completed pilot are in `runs/`; PDF binaries and the database are excluded from Git.

## Collection behavior

By default, requests run sequentially with at least one second between requests to each host. `--workers` (1–8) and `--download-interval` configure bounded concurrency for archive downloads without increasing search concurrency. Transient HTTP/network errors are retried, respecting `Retry-After`. The collector honors `has_next_page`, stops on empty/repeated result pages, and caps searches at 30 pages per query by default. It does not trust the API total counts, which have been observed as zero on nonempty searches. Download failures are recorded and the collector tries later candidates to fill each quota. If a query cannot fill its quota, the run reports a shortfall and exits with status 2; it does not silently change the sampling plan.

Downloads are bounded at 50 MiB per response. The plan is bound to each output directory, files are written atomically, and both document IDs and PDF content hashes are deduplicated. A stopped process can leave an unregistered PDF, which is validated and reused on restart. Collection dates are UTC; the pilot was initiated on November 3, 2026 in America/Denver.

## Upstream references and limits

- [Search endpoint schema](https://github.com/govscape/govscape/blob/main/govscape/api/endpoints/search.py)
- [Website API client](https://github.com/govscape/govscape/blob/main/interface/src/lib/utils/fetch.js)
- [Website PDF download implementation](https://github.com/govscape/govscape/blob/main/interface/src/lib/components/PDFPreview.svelte)
- [GovScape paper](https://arxiv.org/abs/2511.11010)
- [Public bulk source archive](https://source.coop/govscape/eota-pdf-archive)

These are website-facing endpoints, with no stability guarantee established here. The documented indexed collection covers PDFs of at most 50 pages from the 2020 End of Term crawl; crawl timestamps can fall in 2021. The archived files may differ from versions currently at their original government URLs. Keep original bytes and provenance when studying redaction methodology. This collector does not attempt to recover redacted content or assess redaction quality.
