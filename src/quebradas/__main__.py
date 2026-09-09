"""Top-level command-line interface for explicitly approved workflows."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from quebradas_limaeste.inventory.candidate_audit import (
    AUDIT_OUTPUT,
    CONSOLIDATED_OUTPUT,
    QUALITY_CONFIG,
    CandidateAuditError,
    execute_candidate_audit,
)
from quebradas_limaeste.inventory.candidate_quality import CandidateQualityError
from quebradas_limaeste.inventory.ingestion import (
    INGESTION_OUTPUT,
    IngestionError,
    execute_ingest_seeds,
    execute_ingest_url,
)
from quebradas_limaeste.inventory.ingestion_models import IngestionConfigError
from quebradas_limaeste.inventory.live_smoke import (
    OUTPUT_FILENAME,
    LiveSmokeConfigError,
    execute_live_smoke,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    network_commands = {"live-smoke", "ingest-url", "ingest-seeds"}
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
    if args.command == "audit-candidates":
        return _run_candidate_audit(args)
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


def _run_ingestion(args: argparse.Namespace) -> int:
    try:
        if args.command == "ingest-url":
            payload = execute_ingest_url(
                Path(args.config),
                args.url,
                allowed_root=Path.cwd(),
            )
        else:
            payload = execute_ingest_seeds(
                Path(args.config),
                Path(args.seeds),
                allowed_root=Path.cwd(),
            )
    except (IngestionConfigError, IngestionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    _print_ingestion_summary(payload)
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
    ingest_seeds = indeci_commands.add_parser(
        "ingest-seeds",
        help="Ingest only explicitly configured official seed documents.",
    )
    ingest_seeds.add_argument("--config", required=True, help="Path to source YAML.")
    ingest_seeds.add_argument("--seeds", required=True, help="Path to seed YAML.")
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
    return parser


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


def _print_ingestion_summary(payload: dict[str, object]) -> None:
    print(f"output={INGESTION_OUTPUT}")
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
