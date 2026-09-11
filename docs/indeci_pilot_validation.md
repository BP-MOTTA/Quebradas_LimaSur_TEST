# INDECI pilot human validation

## Decision scope

A1.18 freezes the researcher's explicit documentary-inventory decision over
the 131-document pilot:

- P1: include.
- P2: include.
- PX: exclude.

The approved mapping is recorded in
`configs/sources/indeci_pilot_human_validation.yaml`. The code applies that
mapping; it does not infer or recreate the human decision. An unexpected P3 or
P4 record is rejected because no human decision was approved for it.

A1.16 remains the automatic spatial and event filter. A1.17 independently
searched 23 configured locations in all 131 records and found zero spatial
false negatives. The human freeze does not overwrite `review_priority`,
`spatial_relevance`, `event_relevance`, `relevance_status`,
`candidate_strength`, or `matched_terms`.

## Commands and outputs

Freeze the approved decision offline:

```bash
python -m quebradas indeci freeze-pilot-validation
```

This creates local, Git-ignored files under `metadata/indeci/`:

- `pilot_human_decisions.csv`: one reviewed human decision per document.
- `pilot_retained_documents.csv`: only P1 and P2 documents.
- `pilot_excluded_documents.csv`: all PX documents and their exclusion reason.
- `pilot_validation_summary.json`: reconciled totals and retained-event counts.

PX reasons are derived only from the preserved automatic fields:
`outside_study_area`, `excluded_event_type`,
`outside_area_and_event_type`, or conservative fallback `out_of_scope`.
Rejected records, PDFs, manifests, and prior review packages are not removed.

Build the final retained package offline:

```bash
python -m quebradas indeci build-final-pilot-package
```

`review_packages/cusipata_limaeste_final/` contains only `P1/`, `P2/`,
`INDICE_FINAL.csv`, `INDICE_FINAL.xlsx`, and `README.txt`. Raw-to-copy SHA-256
is verified for every available retained PDF. The complete prior package at
`review_packages/cusipata_limaeste_filtered/` remains unchanged for audit.

## Scientific meaning

`human_inventory_decision=include` means only that the document remains in the
working documentary inventory. It does not set `event_confirmed`,
`training_label`, `hazard_label`, ground truth, or any ML target. PX means out
of the approved pilot scope, not that an event is false or did not occur.

## Trust and persistence

All automatic CSV content is untrusted input. The workflow validates exact
schemas, IDs, cross-file consistency, row and file bounds, enumerations,
workspace paths, and PDF hashes. Formula prefixes and illegal spreadsheet
control characters are neutralized. Human outputs and the final package are
created once and not overwritten automatically, preserving the original UTC
review timestamp and preventing silent changes to the frozen decision.
