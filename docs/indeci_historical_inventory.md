# INDECI historical inventory 2010-2026

## Scope

A1.19 extends the approved documentary workflow to every year from 2010 through
2026. The versioned policy is
`configs/sources/indeci_historical_cusipata.yaml`. It references the frozen A1.16
spatial and event policy by path and SHA-256, so a vocabulary change cannot be
accepted silently.

The workflow does not perform OCR, use Google Drive, create ground truth, create
training labels, or validate an event automatically. HTML, titles, links, PDF
bytes, and extracted text are untrusted data. Network boundaries retain the
existing HTTPS host, path, timeout, retry, response-size, PDF-signature, and
no-redirect checks.

## Discovery

Run the metadata stage explicitly:

```bash
make indeci-historical-discovery
```

The two validated archive connectors and known official seeds are attempted for
each year. Queries remain sequential and request at most one page per term and
year. Completed years are stored under
`metadata/indeci/historical/checkpoints/`; a later invocation reuses matching
checkpoints. A complete output with the same configuration is reused, while a
different configuration cannot overwrite it.

`discovery_documents.csv` keeps source provenance, report metadata, conservative
deduplication signals, and title-only preliminary ordering. Canonical PDF URL is
the primary discovery identity, complete report identity is secondary, and
normalized title is only a conservative signal. Conflicting URLs are retained
as ambiguous. Years with no rows still appear in `discovery_run.json`.

The prefilter does not exclude documents. In particular, a regional title such
as `LLUVIAS INTENSAS EN LIMA` remains PX at title-only resolution but is eligible
for the fixed PX control sample.

## Controlled run

After reviewing discovery and configuration, run:

```bash
make indeci-historical-run
```

P1, P2, P3, and P4 records with a PDF URL are processed first. PX records remain
in the inventory and only a deterministic sample configured with a fixed seed is
downloaded. If sampled PX text changes the frozen filter result to P1-P4, the
remaining PX expansion stops and the manifest reports the suspected false
negative.

The document path reuses download, SHA-256, text extraction, classification,
event-candidate extraction, quality audit, and event clustering. Exact SHA-256
duplicates are retained as metadata rows but share a canonical document and
document family. Compatible candidates within a family can share one event
cluster, preventing a family of reports from being counted automatically as
multiple physical events. Ambiguous evidence remains separate.

All candidates and clusters have `validation_status=pending_review`; documents
have `human_validation_status=pending_review`. Report date and event date are
separate fields. An event date is populated only from extracted evidence.

## Outputs

The ignored local directory `metadata/indeci/historical/` contains:

- `discovery_documents.csv` and `discovery_run.json`.
- `all_documents.csv`.
- `event_candidates.csv` and `event_clusters.csv`.
- `year_summary.csv` and `location_summary.csv`.
- `historical_run.json`.
- Discovery and run checkpoints used for resumption.

Existing files under `data/raw/indeci/` are never replaced or renamed. New PDFs
are stored by the existing safe downloader and remain ignored by Git. Text under
`data/interim/indeci/` is regenerable and ignored.

## Limitations

- INDECI archive indices and title search are not known to be exhaustive.
- One requested page per query intentionally limits portal load and coverage.
- Zero discovered documents does not imply zero events.
- Report dates may differ from event dates.
- Image-only or insufficient-text PDFs are marked for future OCR; OCR is not run.
- Documentary families and event clusters remain hypotheses requiring human
  review.
- P1-P4 order review work; PX means outside current priority, not a negative
  event.

## Observed controlled run

The approved run completed on 2026-09-11 with 408 metadata requests and one
parsed page per request. It recorded 2,438 raw observations, 1,991 unique
documents, and 447 discovery duplicates. Years 2010 and 2011 returned no
documents; this is an index result and is not interpreted as absence of events.

The controlled selection processed 58 PDFs with zero final download failures
and zero OCR-required documents. Final documentary priorities were P1=6, P2=11,
P3=0, P4=33, and PX=1,941. P3 remained zero because no processed record combined
`comparison` spatial relevance with `target` event evidence. The fixed control
sample inspected 10 of 1,728 PX rows with a PDF URL and found zero false
negatives.

The review-only event inventory contains 11 candidates and 11 clusters: one
strong, zero moderate, and ten weak. Location summaries include Cusipata=3,
San Bartolomé=1, Chaclacayo=6, Lurigancho-Chosica=22, Huascarán=2,
Huaycoloro=6, Jicamarca=4, and Cieneguilla=1 documents. Quirio and Pedregal have
zero documents in this bounded result. These counts remain pending human review
and must not be interpreted as physical-event counts or labels.
