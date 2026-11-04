# Govscape collector

A resumable Python collector for public government PDF candidates from [GovScape](https://govscape.net/). It uses the same search API and archived PDF download URLs as the website. No Python package dependencies or language-model calls are needed during collection.

## Run the 100-document pilot

Requires Python 3.10+ on macOS or Linux. From this repository:

```sh
python3 -m unittest discover -s tests -v
python3 collector.py --plan plans/pilot.json --output data/pilot-100 --limit 100
```

For stronger PDF validation and page counts, install Poppler and add `--pdfinfo`. The actual pilot was run with this flag. All files are checked for a PDF header and end marker, regardless of this option.

Re-run the same command to resume. Cached search pages and completed downloads are reused, and existing file hashes are verified. Use `--retry-failed` to retry exhausted download failures. Only run one collector per output directory. The collector stops at 100 unique PDF hashes; it never schedules a larger run. Scaling requires an explicitly edited plan with larger quotas and an explicit limit. Increase `--max-pages` as needed (20 results per page before deduplication). Use a new output directory when changing plans.

The pilot allocates 40 unique PDFs to visual search for `redacted documents`, and 20 each to keyword searches for `redacted`, `withheld`, and `"top secret"`. Queries run in that order, assigning overlapping PDFs to the earliest query. This is a purposive candidate sample, not a representative sample of government records. Matches do not prove that a PDF has redactions; visual search ranks all eligible records, including irrelevant results further down the ranking.

## Outputs

- `data/pilot-100/pdfs/`: original archived PDFs, named by GovScape document ID.
- `manifest.jsonl`: one row per unique downloaded PDF, with original URL/name/domain, crawl date, archive download URL, SHA-256, byte count, optional PDF page count, download time, raw source metadata, and observed search matches.
- `report.json`: counts, source-domain distribution, collection plan, and stop reason.
- `review.html`: local review index linking to the archived PDFs at matching pages.
- `collection.sqlite3`: complete cached search responses, candidate failures, duplicate mappings, and restart state.

Search matches include query/mode/filters, results page, rank, and matching PDF page. GovScape API PDF page indices are zero-based; the manifest also supplies a one-based page number. Overall rank uses the API's reported page size (falling back to the observed size of 20). Only encountered matches are recorded; this is not an exhaustive cross-query relevance assessment. Raw search responses remain in the local database. Metadata snapshots for the completed pilot are in `runs/`; PDF binaries and the database are excluded from Git.

## Collection behavior

Requests run sequentially, with at least one second between requests to each host. Transient HTTP/network errors are retried, respecting `Retry-After`. The collector honors `has_next_page`, stops on empty/repeated result pages, and caps searches at 30 pages per query by default. It does not trust the API total counts, which have been observed as zero on nonempty searches. Download failures are recorded and the collector tries later candidates to fill each quota. If a query cannot fill its quota, the run reports a shortfall and exits with status 2; it does not silently change the sampling plan.

Downloads are bounded at 50 MiB per response. The plan is bound to each output directory, files are written atomically, and both document IDs and PDF content hashes are deduplicated. A stopped process can leave an unregistered PDF, which is validated and reused on restart. Collection dates are UTC; the pilot was initiated on November 3, 2026 in America/Denver.

## Upstream references and limits

- [Search endpoint schema](https://github.com/govscape/govscape/blob/main/govscape/api/endpoints/search.py)
- [Website API client](https://github.com/govscape/govscape/blob/main/interface/src/lib/utils/fetch.js)
- [Website PDF download implementation](https://github.com/govscape/govscape/blob/main/interface/src/lib/components/PDFPreview.svelte)
- [GovScape paper](https://arxiv.org/abs/2511.11010)
- [Public bulk source archive](https://source.coop/govscape/eota-pdf-archive)

These are website-facing endpoints, with no stability guarantee established here. The documented indexed collection covers PDFs of at most 50 pages from the 2020 End of Term crawl; crawl timestamps can fall in 2021. The archived files may differ from versions currently at their original government URLs. Keep original bytes and provenance when studying redaction methodology. This collector does not attempt to recover redacted content or assess redaction quality.
