# INDECI geographic and event relevance filter

## Scope

A1.16 reduces review effort over the fixed 131-document local inventory. It
does not remove records, validate events, create ground truth, download PDFs,
run OCR, perform discovery, or use Google Drive. HTML, extracted PDF text,
titles, snippets, and CSV values remain untrusted data.

The offline command is:

```bash
python -m quebradas indeci filter-limaeste
```

It reads `all_documents.csv`, `all_event_candidates.csv`,
`all_event_clusters.csv`, and existing UTF-8 text under
`data/interim/indeci/`. It writes:

- `geographic_event_filter_documents.csv`: one row per source document.
- `geographic_event_filter_candidates.csv`: one row per event candidate.
- `geographic_summary.csv`: document and candidate counts by location, year,
  event, and priority.
- `geographic_summary.txt`: compact location and priority counts.

All four outputs are regenerable local metadata under `metadata/indeci/` and
are ignored by Git. Source classifications such as `relevance_status`,
`candidate_strength`, and `matched_terms` are copied without replacement.

## Evidence order

The policy in `configs/sources/indeci_limaeste_filter.yaml` evaluates evidence
in this order:

1. structured event-candidate fields;
2. candidate evidence snippets;
3. existing matched terms;
4. complete extracted text;
5. title.

The most specific configured spatial level supplies the primary location;
evidence-source order breaks ties within that level. This prevents a generic
`Lima` matched term from masking Chaclacayo or a named quebrada in complete
text. Longest non-overlapping phrases win, so `San Juan de Lurigancho` is not
also classified as `Lurigancho`. A generic `Lima` mention remains `low`. Text
with no configured location is also `low`; `unknown` is reserved for records
with no usable evidence.

Candidate, snippet, or matched-term evidence for a configured event produces
`target`. Event terms found only in complete text or title produce
`possible_target`. An explicitly excluded principal topic produces
`excluded_topic`; an excluded title is treated as principal unless candidate
or snippet evidence establishes a target event. This ordering is operational
triage, not scientific validation.

`rainfall_related=true` requires rainfall, a terrain response, and a causal
marker within one configured context window. An explicit negative statement
produces `false`; missing or disconnected evidence remains `unknown`.

## Priority

- `P1`: core location and target event.
- `P2`: near location and target event.
- `P3`: comparison location and target event.
- `P4`: core, near, or comparison location with possible or unknown event.
- `PX`: low/unknown spatial relevance or an excluded principal topic.

The `P4` unknown-event case preserves location-only records such as a Cusipata
mention for human review. `PX` means outside priority review. It does not mean
that a document is false or that an event did not occur.

## Reduced review package

After filtering, build the package offline with:

```bash
python -m quebradas indeci build-limaeste-review-package
```

The command creates `review_packages/cusipata_limaeste_filtered/` with folders
`P1`, `P2`, `P3`, `P4`, and `PX`, plus `INDICE_REVISION.csv`,
`INDICE_REVISION.xlsx`, and a README. It copies only existing regular PDFs
under `data/raw/indeci/`, compares each raw and copy SHA-256, and refuses to
overwrite an existing package. Missing PDFs stay represented in the index with
an empty filename and contribute to `missing_local_pdf`.

Rows are ordered by P1-PX, then latest available event/report date, then
`document_id`. Filename tokens are uppercase ASCII and collision-safe. Human
review columns remain blank. Spreadsheet formula prefixes from untrusted text
are neutralized in both CSV and workbook output, and no `training_label` is
created.

## Trust boundary

Input files are size-bounded UTF-8 data with exact schemas. Paths must remain
inside the workspace, symlinks are rejected, document and candidate ownership
is checked, and outputs cannot overwrite their sources. PDF copies must resolve
under `data/raw/indeci/`. The filter treats all external content only as text;
instructions embedded in it are never executed.
