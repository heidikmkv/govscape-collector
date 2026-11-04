# Pilot: 100 redaction candidates

Collected November 3, 2026 (America/Denver). Download timestamps in the manifest are UTC.

- 100 unique PDFs by document ID and SHA-256; 179,402,322 bytes total.
- All 100 passed PDF header/end-marker checks and Poppler pdfinfo parsing.
- Search allocation: visual redacted documents (40), keyword redacted (20), keyword withheld (20), keyword top secret (20).
- No download failures; stopped at the target of 100.
- Source metadata, observed search matches, checksums and page counts are preserved in manifest.jsonl.
- The local SQLite database preserves complete search responses and restart state.

PDFs remain in data/pilot-100/pdfs/ in the local checkout and are excluded from Git. Open data/pilot-100/review.html for the local review index.

These are unreviewed candidates. No assessment of redaction presence, methodology or quality has been performed. Collection is paused pending review; no 3,000-document run has been started.
