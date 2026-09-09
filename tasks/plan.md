# Implementation Plan: A1 INDECI/COEN Documentary Inventory

## Overview

Implement the approved offline subset of the A1 INDECI/COEN documentary
inventory collector. The project starts from documentation only, so the work is
split into small commits: specification, Python project base, validated models,
offline parser, and CLI.

## Architecture Decisions

- Keep standard-library runtime code except where an approved source format
  requires a bounded dependency. A1.10 uses PyMuPDF only for PDF extraction.
- Keep broad and historical crawling, unlisted PDF download, and Google Drive
  upload fail-closed. They need explicit future approval.
- Use synthetic fixtures only. They are not scientific observations and cannot
  be used as labels or evidence.
- Emit CSV, JSON, and manifest files for traceability, matching repository rules
  for tabular outputs and JSON manifests.

## Task List

### Phase 0: Specification

- Create `docs/SPEC-a1-indeci-inventory.md`.
- Record this incremental implementation plan in `tasks/plan.md`.

Acceptance:

- Scope boundaries are explicit.
- Open questions caused by the missing attached requirement are preserved.
- No executable behavior is introduced.

Verification:

- `git diff --check`
- Diff review
- Secret/PDF scan

### Phase 1: Python Project Base

- Add `pyproject.toml`.
- Add `Makefile` with expected commands.
- Add package skeleton under `src/`.
- Add `.gitignore` for virtualenvs, caches, raw downloads, real PDFs, and
  secrets.
- Add a minimal import test.

Acceptance:

- Project has a reproducible Python layout.
- `make setup`, `make lint`, and `make test` exist.
- No runtime dependency is added.

Verification:

- `make setup`
- `make lint`
- `make test`
- Diff review
- Secret/PDF scan

### Phase 2: Inventory Models

- Add validated inventory record and manifest models.
- Add deterministic record IDs.
- Add date, URL, precision, and text validation.

Acceptance:

- Invalid URLs, invalid precision labels, empty required fields, and oversized
  text are rejected.
- Records default to `requires_human_review = true`.
- No coordinates or labels are inferred.

Verification:

- Focused model tests
- Full `make lint`
- Full `make test`
- Diff review
- Secret/PDF scan

### Phase 3: Offline Parser

- Add an HTML/text parser for local content.
- Extract only candidate document records from explicit fixture markup.
- Normalize relative links against a validated source URL.
- Ignore script/style content and never follow links.

Acceptance:

- Synthetic HTML fixture produces deterministic candidate records.
- Malicious or unsupported links are skipped with warnings.
- Missing dates remain missing with warnings; dates are not invented.

Verification:

- Focused parser tests
- Full `make lint`
- Full `make test`
- Diff review
- Secret/PDF scan

### Phase 4: CLI Offline Inventory

- Add CLI command for offline inventory generation.
- Wire `make inventory` to the CLI against synthetic fixtures.
- Block historical, live-smoke, and upload-drive modes.
- Document the command in `README.md`.

Acceptance:

- CLI writes JSON, CSV, and manifest outputs.
- CLI tests run without network.
- Blocked modes return non-zero and explain that separate approval is required.

Verification:

- Focused CLI tests
- `make inventory`
- Full `make lint`
- Full `make test`
- Diff review
- Secret/PDF scan

### Phase 5: A1.8 Read-only INDECI live smoke

- Document the observed public portal form, cards, pagination, and empty state.
- Add a pure parser covered by synthetic offline fixtures.
- Add a bounded, sequential HTTP client with HTTPS/host/path allowlists,
  timeout, request delay, retry/backoff, and response-size limits.
- Add the explicit `python -m quebradas indeci live-smoke` command and
  `make indeci-live-smoke` wrapper.
- Record runtime metadata only; never request card, detail, or PDF links.

Acceptance:

- Offline tests cover parsing, golden matching, error paths, and CI blocking.
- One approved live run makes at most four configured single-page queries.
- Both golden flags and all runtime errors/warnings are explicit.
- Historical crawling, downloads, and Drive remain disabled.

### Phase 6: A1.9 Emergency archive golden-case smoke

- Allowlist the official `/informe/informe-de-emergencia/` archive alongside
  the existing reports archive.
- Reuse the pure parser and bounded transport without following card links.
- Add a dedicated configuration for one `1496`/`2023` single-page query.
- Add an explicit Make target and document the observed archive structure.

Acceptance:

- Offline tests prove that both approved archives work and all other archive
  paths remain rejected.
- One approved live run makes one narrow metadata request, subject only to the
  existing bounded retry policy.
- The result reports the 2023 golden flag and any limitation explicitly.
- Historical crawling, downloads, and Drive remain disabled.

### Phase 7: A1.10 Controlled official PDF ingestion

- Keep discovery separate from direct ingestion of known official URLs.
- Validate explicit seed identities and allowlisted upload URLs.
- Download through a bounded, retrying transport to `.part`, verify HTTP 200,
  PDF signature, size, final URL, and SHA-256, then publish without overwrite.
- Extract page text with PyMuPDF without OCR; classify with transparent term
  rules and generate only explicit-date candidates with page evidence.
- Persist ignored runtime provenance, deduplication state, and run counters.
- Add explicit `ingest-url`, `ingest-seeds`, and Make entrypoints, blocked in CI.

Acceptance:

- Offline fake-HTTP tests cover downloader failures, deduplication, extraction,
  classification, evidence, orchestration, and CLI dispatch.
- One approved seed-only live run downloads only the configured IE1496 URL.
- Every classification and candidate remains subject to human review.
- No real PDF, extracted text, runtime registry, or secret is Git-tracked.
- Historical crawling, OCR, Drive, push, and merge remain disabled.

### Phase 8: A1.11 Candidate quality audit and consolidation

- Export original ingestion candidates to a separate ignored CSV without
  rewriting the ingestion manifest.
- Audit minimal site, event, and date evidence using configurable geographic
  exclusions and sentence/paragraph/window proximity.
- Classify each retained candidate as strong, moderate, or weak while keeping
  `pending_review` mandatory.
- Exclude Cusipata references tied to Cusco/Quispicanchi and downgrade content,
  bibliography, header, list, or uncertain contexts.
- Consolidate only compatible document/quebrada/date/type evidence, preserving
  every candidate ID, source page, and supporting fragment.
- Add the offline `audit-candidates` command and document all runtime outputs.

Acceptance:

- Synthetic offline tests cover the seven requested scientific cases and CSV
  safety boundaries.
- The 16 original IE1496 candidates can be audited without modifying them.
- A consolidated CSV is generated and no row becomes validated automatically.
- The golden Cusipata check reports the observed result without hardcoding it.
- Historical crawling, OCR, Drive, push, and merge remain disabled.

### Phase 9: A1.12 Real positive event control

- Add content-based fixtures for positive, list-only, missing-site, date
  separation, and URL/local-file equivalence cases.
- Route explicit local PDFs and allowlisted URL downloads through one internal
  document model and one downstream processing function.
- Preserve local source provenance without copying the PDF into `data/raw/`.
- Keep positive candidate, audit, and consolidation outputs separate from the
  IE1496 negative control.
- Write an ignored golden-control summary containing only document IDs and
  strong/moderate/weak counts.
- Validate the real RC630 through its previously observed official URL.

Acceptance:

- Offline tests remain network-free and all review states remain
  `pending_review`.
- RC630 is extracted, audited, consolidated, and compared with IE1496 without a
  report-number-specific rule.
- Report date and event date remain distinct provenance fields.
- No real PDF, extracted text, runtime output, credential, or token is tracked.
- Historical crawling, OCR, Drive, push, and merge remain disabled.

## Risks and Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Portal structure changes | High | Fail visibly on unrecognized HTML and keep synthetic contract tests. |
| Prompt injection in external text | High | Treat parsed HTML/PDF/text as data; never execute embedded instructions. |
| Accidental real PDF/raw commit | High | `.gitignore`, pre-commit scan, and no real downloads in tests. |
| Scientific overclaiming | High | Candidate records require human review and no operational conclusions. |
| Dependency supply-chain risk | Medium | Use standard library runtime code first; dev dependencies are limited to Pytest/Ruff. |
| PyMuPDF license incompatibility | High | Keep this phase local and require an AGPL/commercial compatibility decision before distribution. |

## Open Questions

- What is the retention policy for real PDFs if downloads are later approved?
- What Google Drive folder and credential model should be used if upload is later
  approved?
- What constitutes a reviewed strong event label for A1?
