# INDECI human review workflow

## Scope

A1.15 converts the controlled A1.14 outputs into an offline, document-first
scientific review queue. It performs no network requests, PDF reads, OCR,
historical crawling, Google Drive operations, or automatic human decisions.

The A1.14 `review_queue.csv` contains 77 reason rows: 20 document reasons and 57
candidate reasons. Several reasons can refer to the same candidate. A1.15 joins
those automatic outputs into 23 documents, 29 event clusters, and 29 candidates
without treating duplicate reasons as independent scientific decisions.

## Review order

Documents are ordered by automatic relevance:

1. `relevant`
2. `potentially_relevant` (shown as `possible` in summaries)
3. `not_relevant` (shown as `irrelevant`)

Within each category, the strongest candidate determines order: `strong`, then
`moderate`, then `weak`, then documents without candidates. Candidates and
clusters use the same strength order and then source page.

RC630 and IE1496 retain their configured control role and are exposed as
`golden_control=true`. They remain available for comparison packets but are not
included in new review work or pending counts.

## Commands

Interactive review:

```bash
python -m quebradas indeci review-batch
```

The command requests a reviewer identifier, prints one bounded document packet,
and accepts only explicit values from the documented decision vocabularies.
After every completed document, it atomically replaces the local CSV state.
`skip` leaves a document pending; `quit`, EOF, or an interrupt preserves all
previously completed documents.

Read-only evidence packets and aggregate status:

```bash
python -m quebradas indeci review-batch --summary
python -m quebradas indeci review-summary
```

`review-batch --summary` prints compact packets for relevant and possible
documents, including controls. `review-summary` prints only aggregate counts.
Neither command creates, refreshes, or modifies review CSVs.

## Decision model

Document decisions record:

- `human_site_review`: `confirmed`, `rejected`, or `ambiguous`.
- `human_inventory_decision`: `include`, `exclude`, or `pending`.
- `human_spatial_precision`: `A`, `B`, `C`, `D`, `E`, or `unknown`.
- `human_notes`, `reviewed_at`, and `reviewed_by`.

Every event cluster additionally records:

- `human_event_review`: `confirmed`, `rejected`, or `ambiguous`.
- `human_event_date`: `exact`, `approximate`, `missing`, or
  `not_applicable`.

The workflow never rewrites `relevance_status`, `candidate_strength`, or
`event_date_extracted`. `human_inventory_decision=include` does not create a
`training_label`; labels and ground truth remain outside A1.15.

## Persistence and staleness

Curated local files are:

- `metadata/indeci/document_review.csv`: one row per reviewed document.
- `metadata/indeci/human_review.csv`: one row per reviewed event cluster.

They are ignored by Git because they can contain reviewer identifiers and free
notes. They must not be deleted by regenerable pipeline cleanup and require an
independent research-data backup policy.

Existing document and cluster decisions cannot be submitted again. Each row
stores a SHA-256 fingerprint of its automatic inputs. At the start of an
interactive session, changed or removed automatic evidence sets
`review_stale=true` while preserving every human field. Read-only commands do
not refresh stale markers.

## Trust boundary

All automatic CSV values and evidence snippets are untrusted data. Input paths
must remain inside the workspace, symlinks are rejected, files and row counts
are bounded, headers are exact, identifiers and enumerations are validated, and
candidate-to-cluster ownership cannot cross documents. Terminal rendering
removes control characters. CSV output neutralizes spreadsheet formula prefixes
and uses an exclusive temporary file plus `fsync` and atomic replacement.

The packet is generated only from bounded CSV fields. It never reads a retained
PDF or the full extracted document text.

## A1.16 reduced coauthor package

The separate A1.16 filter may read already extracted local text to derive
review priority, but it does not make human decisions. Its reduced package
contains all filtered rows, including `PX`, while physically copying only PDFs
already present under `data/raw/indeci/`. The package index has blank human
columns and no `training_label`; `PX` is an operational queue status rather
than scientific exclusion. The complete evidence and priority contract is in
`docs/indeci_limaeste_filter.md`.
