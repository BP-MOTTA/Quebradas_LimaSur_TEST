# INDECI spatial false-negative audit

## Scope

A1.17 audits the fixed 131-document A1.16 output without changing it. The
workflow is offline and performs no discovery, download, OCR, historical
crawling, Google Drive operation, event validation, or ground-truth creation.

Run:

```bash
python -m quebradas indeci audit-spatial-false-negatives
```

The command writes two regenerable, Git-ignored artifacts:

- `metadata/indeci/spatial_false_negative_audit.csv`
- `metadata/indeci/spatial_audit_summary.json`

## Method

For every document and each of the 23 configured core, near, and comparison
locations, the audit searches title, matched terms, structured candidate
fields, evidence snippets, and full extracted text. Matching uses case folding,
accent removal, punctuation and hyphen normalization, whitespace collapse,
configured aliases, complete-term boundaries, and longest-phrase overlap
resolution. It does not use fuzzy matching.

The audit detector is separate from the A1.16 detector. A row is marked
`detector_false_negative` only when normalized source evidence contains the
location but the existing `detected_locations` field does not. A location can
instead be `detected_non_primary`: it is retained in `detected_locations`, but
a more relevant core or near location determines the document's primary
spatial class and review priority.

## Observed explanation

The local corpus contains Jicamarca in 2 documents, Huaycoloro in 2, Quirio in
1, Pedregal in 3, and Cieneguilla in 2. A1.16 detects those mentions. Their
zero primary counts are therefore not detector false negatives. Jicamarca and
Cieneguilla coexist with core or near locations in the same multi-location
reports, which explains why the actual corpus has no primary comparison
document and `P3=0`.

There are 14 documents classified core or near. Six reach P1/P2 because they
have target event evidence. The other eight have an excluded principal topic:
seven fire reports and one vehicular accident. Rainfall relation does not enter
the P1-P4 priority formula, so it blocks none of these records. This is an
operational review result, not a scientific validation.

## Trust boundary

All source text and CSV values are untrusted data. Inputs have exact schemas,
bounded sizes and row counts, matching document ownership, and workspace-only
non-symlink paths. Outputs are written through exclusive temporary files with
`fsync` and atomic replacement. CSV formula prefixes and control characters are
neutralized. The audit never reads or writes raw PDFs.
