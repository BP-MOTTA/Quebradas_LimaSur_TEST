"""Top-level command-line interface for explicitly approved workflows."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from quebradas_limaeste.inventory.batch_ingestion import (
    BATCH_RUN_OUTPUT,
    BatchIngestionError,
    execute_ingest_batch,
    format_batch_summary,
    load_batch_manifest,
)
from quebradas_limaeste.inventory.candidate_audit import (
    AUDIT_OUTPUT,
    CANDIDATE_OUTPUT,
    CONSOLIDATED_OUTPUT,
    GOLDEN_CONTROLS_OUTPUT,
    QUALITY_CONFIG,
    CandidateAuditError,
    execute_candidate_audit,
    execute_golden_control_comparison,
)
from quebradas_limaeste.inventory.candidate_quality import CandidateQualityError
from quebradas_limaeste.inventory.discovery import (
    CANDIDATES_OUTPUT as DISCOVERY_CANDIDATES_OUTPUT,
)
from quebradas_limaeste.inventory.discovery import (
    RUN_OUTPUT as DISCOVERY_RUN_OUTPUT,
)
from quebradas_limaeste.inventory.discovery import (
    DiscoveryConfigError,
    execute_discovery,
)
from quebradas_limaeste.inventory.ingestion import (
    INGESTION_OUTPUT,
    IngestionError,
    execute_ingest_file,
    execute_ingest_seeds,
    execute_ingest_url,
)
from quebradas_limaeste.inventory.ingestion_models import IngestionConfigError
from quebradas_limaeste.inventory.live_smoke import (
    OUTPUT_FILENAME,
    LiveSmokeConfigError,
    execute_live_smoke,
)
from quebradas_limaeste.inventory.triage import (
    BATCH_SELECTION_OUTPUT,
    SOURCE_CONFIG,
    BatchPolicyError,
    execute_select_batch,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    network_commands = {
        "live-smoke",
        "discover",
        "ingest-url",
        "ingest-seeds",
        "ingest-batch",
    }
    if (
        args.command in network_commands
        and os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
    ):
        print(
            f"error: INDECI {args.command} is disabled in GitHub Actions",
            file=sys.stderr,
        )
        return 2

    if args.command == "live-smoke":
        return _run_live_smoke(args)
    if args.command == "discover":
        return _run_discovery(args)
    if args.command == "select-batch":
        return _run_select_batch(args)
    if args.command == "ingest-batch":
        return _run_batch_ingestion(args)
    if args.command == "batch-summary":
        return _run_batch_summary(args)
    if args.command == "audit-candidates":
        return _run_candidate_audit(args)
    if args.command == "compare-golden-controls":
        return _run_golden_control_comparison(args)
    return _run_ingestion(args)


def _run_live_smoke(args: argparse.Namespace) -> int:
    try:
        payload = execute_live_smoke(
            Path(args.config),
            output_path=OUTPUT_FILENAME,
            allowed_root=Path.cwd(),
        )
    except (LiveSmokeConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    _print_live_smoke_summary(payload)
    return 1 if payload["errors"] else 0


def _run_discovery(args: argparse.Namespace) -> int:
    try:
        years = _parse_years(args.years)
        payload = execute_discovery(
            Path(args.config),
            years=years,
            candidates_output=Path(args.candidates_output),
            run_output=Path(args.run_output),
            allowed_root=Path.cwd(),
        )
    except (DiscoveryConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    _print_discovery_summary(payload)
    return 0


def _run_select_batch(args: argparse.Namespace) -> int:
    try:
        payload = execute_select_batch(
            Path(args.discovery),
            config_path=Path(args.config),
            output_path=Path(args.output),
            max_documents=args.max_documents,
            allowed_root=Path.cwd(),
        )
    except (BatchPolicyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"batch_id={payload['batch_id']}")
    print(f"documents_available={payload['documents_available']}")
    print(f"documents_selected={payload['documents_selected']}")
    for year, count in payload["selected_by_year"].items():
        print(f"year:{year}={count}")
    for tier, count in payload["selected_by_tier"].items():
        print(f"tier:{tier}={count}")
    print(f"output={payload['selection_output']}")
    return 0


def _run_batch_ingestion(args: argparse.Namespace) -> int:
    try:
        payload = execute_ingest_batch(
            Path(args.selection),
            config_path=Path(args.config),
            allowed_root=Path.cwd(),
            allow_large_batch=args.allow_large_batch,
        )
    except (
        BatchIngestionError,
        BatchPolicyError,
        IngestionConfigError,
        IngestionError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_batch_ingestion_summary(payload)
    return 1 if payload["errors"] else 0


def _run_batch_summary(args: argparse.Namespace) -> int:
    try:
        payload = load_batch_manifest(
            Path(args.run),
            allowed_root=Path.cwd(),
        )
        print(format_batch_summary(payload))
    except (BatchIngestionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def _run_ingestion(args: argparse.Namespace) -> int:
    output_path = Path(args.output)
    common = {
        "output_path": output_path,
        "candidate_output": Path(args.candidate_output),
        "allowed_root": Path.cwd(),
    }
    try:
        if args.command == "ingest-url":
            payload = execute_ingest_url(
                Path(args.config),
                args.url,
                **common,
            )
        elif args.command == "ingest-file":
            payload = execute_ingest_file(
                Path(args.config),
                Path(args.file),
                source_url=args.source_url,
                **common,
            )
        else:
            payload = execute_ingest_seeds(
                Path(args.config),
                Path(args.seeds),
                **common,
            )
    except (IngestionConfigError, IngestionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    _print_ingestion_summary(payload, output_path=output_path)
    return 1 if payload["errors"] else 0


def _run_candidate_audit(args: argparse.Namespace) -> int:
    try:
        payload = execute_candidate_audit(
            Path(args.input),
            config_path=Path(args.quality_config),
            audit_output=Path(args.audit_output),
            consolidated_output=Path(args.consolidated_output),
            allowed_root=Path.cwd(),
        )
    except (CandidateAuditError, CandidateQualityError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_candidate_audit(payload)
    return 0


def _run_golden_control_comparison(args: argparse.Namespace) -> int:
    output_path = Path(args.output)
    try:
        payload = execute_golden_control_comparison(
            Path(args.negative_audit),
            Path(args.positive_audit),
            output_path=output_path,
            allowed_root=Path.cwd(),
        )
    except (CandidateAuditError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"output={output_path}")
    for role in ("negative_control", "positive_control"):
        control = payload[role]
        print(
            f"{role}={control['document_id']},"
            f"strong={control['strong']},"
            f"moderate={control['moderate']},"
            f"weak={control['weak']}"
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m quebradas")
    domains = parser.add_subparsers(dest="domain", required=True)
    indeci = domains.add_parser("indeci", help="INDECI metadata workflows.")
    indeci_commands = indeci.add_subparsers(dest="command", required=True)
    live_smoke = indeci_commands.add_parser(
        "live-smoke",
        help="Run a bounded, read-only metadata check against the public portal.",
    )
    live_smoke.add_argument("--config", required=True, help="Path to live-smoke YAML.")
    discover = indeci_commands.add_parser(
        "discover",
        help="Discover bounded INDECI metadata without downloading documents.",
    )
    discover.add_argument("--config", required=True, help="Path to source YAML.")
    discover.add_argument(
        "--years",
        required=True,
        help="Comma-separated subset of the configured pilot years.",
    )
    discover.add_argument(
        "--dry-run",
        action="store_true",
        required=True,
        help="Required metadata-only mode; no document ingestion is implemented.",
    )
    discover.add_argument(
        "--candidates-output",
        default=str(DISCOVERY_CANDIDATES_OUTPUT),
        help="Discovery candidate CSV.",
    )
    discover.add_argument(
        "--run-output",
        default=str(DISCOVERY_RUN_OUTPUT),
        help="Discovery run manifest JSON.",
    )
    select_batch = indeci_commands.add_parser(
        "select-batch",
        help="Select a deterministic, year-balanced batch without network access.",
    )
    select_batch.add_argument(
        "--discovery",
        required=True,
        help="Discovery candidate CSV.",
    )
    select_batch.add_argument(
        "--max-documents",
        type=int,
        default=25,
        help="Maximum selected documents, bounded by configuration.",
    )
    select_batch.add_argument(
        "--config",
        default=str(SOURCE_CONFIG),
        help="Path to source YAML.",
    )
    select_batch.add_argument(
        "--output",
        default=str(BATCH_SELECTION_OUTPUT),
        help="Batch selection CSV.",
    )
    ingest_batch = indeci_commands.add_parser(
        "ingest-batch",
        help="Download and process only selected rows from one bounded batch.",
    )
    ingest_batch.add_argument(
        "--selection",
        required=True,
        help="Batch selection CSV.",
    )
    ingest_batch.add_argument(
        "--config",
        default=str(SOURCE_CONFIG),
        help="Path to source YAML.",
    )
    ingest_batch.add_argument(
        "--allow-large-batch",
        action="store_true",
        help="Explicitly allow the configured maximum to be exceeded.",
    )
    batch_summary = indeci_commands.add_parser(
        "batch-summary",
        help="Print a local controlled-batch summary without network access.",
    )
    batch_summary.add_argument(
        "--run",
        default=str(BATCH_RUN_OUTPUT),
        help="Batch run manifest JSON.",
    )
    ingest_url = indeci_commands.add_parser(
        "ingest-url",
        help="Ingest one explicitly supplied official PDF URL.",
    )
    ingest_url.add_argument("--config", required=True, help="Path to source YAML.")
    ingest_url.add_argument(
        "--url",
        required=True,
        help="Allowlisted official PDF URL.",
    )
    _add_ingestion_output_arguments(ingest_url)
    ingest_file = indeci_commands.add_parser(
        "ingest-file",
        help="Ingest one explicitly supplied local PDF without copying it.",
    )
    ingest_file.add_argument("--config", required=True, help="Path to source YAML.")
    ingest_file.add_argument("--file", required=True, help="Path to local PDF.")
    ingest_file.add_argument(
        "--source-url",
        help="Optional known official URL; never inferred from the local file.",
    )
    _add_ingestion_output_arguments(ingest_file)
    ingest_seeds = indeci_commands.add_parser(
        "ingest-seeds",
        help="Ingest only explicitly configured official seed documents.",
    )
    ingest_seeds.add_argument("--config", required=True, help="Path to source YAML.")
    ingest_seeds.add_argument("--seeds", required=True, help="Path to seed YAML.")
    _add_ingestion_output_arguments(ingest_seeds)
    audit = indeci_commands.add_parser(
        "audit-candidates",
        help="Audit and consolidate a local candidate CSV without network access.",
    )
    audit.add_argument("--input", required=True, help="Original candidate CSV.")
    audit.add_argument(
        "--quality-config",
        default=str(QUALITY_CONFIG),
        help="Candidate quality policy YAML.",
    )
    audit.add_argument(
        "--audit-output",
        default=str(AUDIT_OUTPUT),
        help="Separate audited candidate CSV.",
    )
    audit.add_argument(
        "--consolidated-output",
        default=str(CONSOLIDATED_OUTPUT),
        help="Consolidated event CSV.",
    )
    compare_controls = indeci_commands.add_parser(
        "compare-golden-controls",
        help="Compare two audited control documents without retaining evidence text.",
    )
    compare_controls.add_argument(
        "--negative-audit",
        required=True,
        help="Audited negative-control CSV.",
    )
    compare_controls.add_argument(
        "--positive-audit",
        required=True,
        help="Audited positive-control CSV.",
    )
    compare_controls.add_argument(
        "--output",
        default=str(GOLDEN_CONTROLS_OUTPUT),
        help="Golden-control JSON summary.",
    )
    return parser


def _add_ingestion_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output",
        default=str(INGESTION_OUTPUT),
        help="Ingestion run JSON.",
    )
    parser.add_argument(
        "--candidate-output",
        default=str(CANDIDATE_OUTPUT),
        help="Original candidate CSV.",
    )


def _print_live_smoke_summary(payload: dict[str, object]) -> None:
    print(f"output={OUTPUT_FILENAME}")
    print(f"pages_requested={payload['pages_requested']}")
    print(f"documents_seen={payload['documents_seen']}")
    print(f"golden_2019_found={str(payload['golden_2019_found']).lower()}")
    print(f"golden_2023_found={str(payload['golden_2023_found']).lower()}")
    for query in payload["queries_attempted"]:
        print(
            "query="
            f"{query['title']!r}, year={query['year']}, "
            f"requests={query['requests_made']}, pages={query['pages_parsed']}"
        )
    for warning in payload["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    for error in payload["errors"]:
        print(f"error: {error}", file=sys.stderr)


def _print_discovery_summary(payload: dict[str, object]) -> None:
    print("metric\tvalue")
    for field in (
        "requests",
        "pages",
        "candidates_raw",
        "candidates_unique",
        "duplicates",
    ):
        print(f"{field}\t{payload[field]}")
    for year, count in payload["candidates_by_year"].items():
        print(f"year:{year}\t{count}")
    for connector, count in payload["candidates_by_connector"].items():
        print(f"connector:{connector}\t{count}")
    for field in (
        "golden_rc630_discovered",
        "golden_ie1496_discovered",
        "golden_ie1496_available_as_seed",
    ):
        print(f"{field}\t{str(payload[field]).lower()}")
    for golden in ("golden_rc630_connectors", "golden_ie1496_connectors"):
        print(f"{golden}\t{','.join(payload[golden]) or 'none'}")
    for attempt in payload["connectors_attempted"]:
        print(f"status:{attempt['connector']}\t{attempt['status']}")
    print(f"candidates_output\t{payload['candidates_output']}")
    print(f"run_output\t{payload['run_output']}")
    for warning in payload["warnings"]:
        print(f"warning: {_terminal_safe(warning)}", file=sys.stderr)
    for error in payload["errors"]:
        print(f"error: {_terminal_safe(error)}", file=sys.stderr)


def _parse_years(value: str) -> tuple[int, ...]:
    try:
        parts = tuple(part.strip() for part in value.split(","))
        if not parts or any(not part for part in parts):
            raise ValueError
        return tuple(int(part) for part in parts)
    except (AttributeError, ValueError) as exc:
        raise DiscoveryConfigError("years must be comma-separated integers") from exc


def _print_ingestion_summary(
    payload: dict[str, object],
    *,
    output_path: Path,
) -> None:
    print(f"output={output_path}")
    for field in (
        "documents_requested",
        "documents_downloaded",
        "duplicates",
        "download_errors",
        "documents_extracted",
        "documents_classified",
        "event_candidates",
        "items_pending_review",
    ):
        print(f"{field}={payload[field]}")
    for warning in payload["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    for error in payload["errors"]:
        print(f"error: {error}", file=sys.stderr)


def _print_batch_ingestion_summary(payload: dict[str, object]) -> None:
    for field in (
        "documents_selected",
        "documents_downloaded",
        "documents_failed",
        "duplicates",
        "ocr_required",
        "documents_relevant",
        "documents_possible",
        "documents_irrelevant",
        "documents_excluded_geography",
        "event_candidates",
        "strong_candidates",
        "moderate_candidates",
        "weak_candidates",
        "event_clusters",
        "items_pending_review",
    ):
        print(f"{field}={payload[field]}")
    for year, count in payload["documents_by_year"].items():
        print(f"year:{year}={count}")
    for warning in payload["warnings"]:
        print(f"warning: {_terminal_safe(warning)}", file=sys.stderr)
    for error in payload["errors"]:
        print(f"error: {_terminal_safe(error)}", file=sys.stderr)


def _print_candidate_audit(payload: dict[str, object]) -> None:
    fields = (
        "candidate_id",
        "source_page",
        "event_date",
        "reported_quebrada",
        "event_type",
        "evidence_snippet",
        "matched_terms",
        "candidate_strength",
        "review_reason",
    )
    for candidate in payload["audited_candidates"]:
        print("candidate:")
        for field in fields:
            value = candidate[field]
            if isinstance(value, list):
                value = "|".join(map(str, value))
            print(f"{field}={_terminal_safe(value)}")
    for field in (
        "candidates_original",
        "candidates_excluded",
        "strong",
        "moderate",
        "weak",
        "clusters_consolidated",
    ):
        print(f"{field}={payload[field]}")
    print(f"pages_audited={','.join(map(str, payload['pages_audited']))}")
    print(f"pages_relevant={','.join(map(str, payload['pages_relevant']))}")
    print(f"strong_cusipata={str(payload['strong_cusipata']).lower()}")
    print(f"audit_output={payload['audit_output']}")
    print(f"consolidated_output={payload['consolidated_output']}")


def _terminal_safe(value: object) -> str:
    text = "" if value is None else str(value)
    escaped = []
    for character in text:
        codepoint = ord(character)
        if character.isprintable():
            escaped.append(character)
        elif codepoint <= 0xFF:
            escaped.append(f"\\x{codepoint:02x}")
        else:
            escaped.append(f"\\u{codepoint:04x}")
    return "".join(escaped)


if __name__ == "__main__":
    raise SystemExit(main())
