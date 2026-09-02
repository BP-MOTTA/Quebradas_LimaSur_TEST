# Implementation Plan: A1 INDECI/COEN Documentary Inventory

## Overview

Implement the approved offline subset of the A1 INDECI/COEN documentary
inventory collector. The project starts from documentation only, so the work is
split into small commits: specification, Python project base, validated models,
offline parser, and CLI.

## Architecture Decisions

- Use standard-library runtime code first. This avoids unneeded supply-chain
  risk while the real external-source requirement is still unavailable.
- Keep live crawling, historical crawling, PDF download, and Google Drive upload
  fail-closed. They need explicit future approval.
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

### Phase 5: Deferred Live Integrations

No implementation in this approved run.

Future approval is required before adding real crawling, live smoke checks, PDF
downloads, Google Drive upload, or any real INDECI/COEN source adapter.

## Risks and Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Missing complete requirement | High | Keep real source adapters out of scope and preserve open questions. |
| Prompt injection in external text | High | Treat parsed HTML/PDF/text as data; never execute embedded instructions. |
| Accidental real PDF/raw commit | High | `.gitignore`, pre-commit scan, and no real downloads in tests. |
| Scientific overclaiming | High | Candidate records require human review and no operational conclusions. |
| Dependency supply-chain risk | Medium | Use standard library runtime code first; dev dependencies are limited to Pytest/Ruff. |

## Open Questions

- What exact INDECI/COEN pages, date ranges, and source fields are authoritative?
- What is the retention policy for real PDFs if downloads are later approved?
- What Google Drive folder and credential model should be used if upload is later
  approved?
- What constitutes a reviewed strong event label for A1?
