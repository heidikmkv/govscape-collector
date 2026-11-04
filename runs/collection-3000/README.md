# Collection: 3,000 redaction candidates

Completed November 4, 2026 (America/Denver). Download and verification timestamps are UTC.

- 3,000 PDFs unique by document ID and SHA-256.
- 100 pilot records reused locally, with 2,900 additional unique PDFs and no new hashes overlapping the pilot.
- 4,981,622,958 bytes, 67,128 PDF pages, and 271 source domains.
- All PDFs passed header/end-marker checks and Poppler pdfinfo parsing; every saved file hash was verified after collection.
- Original pilot file hashes, download timestamps, source metadata, source URLs, and crawl dates are unchanged.
- Search allocation: visual redacted documents (1,200), keyword redacted (600), keyword withheld (600), keyword top secret (600).
- 1 unusable candidate rejected and recorded in rejected-candidates.jsonl; later results filled the quota.
- Collection stopped at exactly 3,000. A completed-run resume made zero network requests.

Source metadata and observed search matches are in manifest.jsonl. The report records the seed manifest checksum and IDs. Detailed validation is in verification.json. The local SQLite database retains full search responses, candidate state, and restart metadata.

PDFs are local at data/collection-3000/pdfs/ and are excluded from Git. Open data/collection-3000/review.html to browse them; the Batch column distinguishes reused pilot records from new additions.

These are unreviewed candidates. Redaction presence, methodology, and quality have not been assessed.
