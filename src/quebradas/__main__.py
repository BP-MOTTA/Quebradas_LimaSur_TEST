"""Top-level command-line interface for explicitly approved workflows."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

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

    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        print("error: INDECI live-smoke is disabled in GitHub Actions", file=sys.stderr)
        return 2

    try:
        payload = execute_live_smoke(
            Path(args.config),
            output_path=OUTPUT_FILENAME,
            allowed_root=Path.cwd(),
        )
    except (LiveSmokeConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    _print_summary(payload)
    return 1 if payload["errors"] else 0


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
    return parser


def _print_summary(payload: dict[str, object]) -> None:
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


if __name__ == "__main__":
    raise SystemExit(main())
