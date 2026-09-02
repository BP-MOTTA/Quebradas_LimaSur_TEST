"""CLI entrypoint for the documentary inventory collector."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from quebradas_limaeste.inventory.io import read_source_text, write_inventory_outputs
from quebradas_limaeste.inventory.models import (
    InventoryManifest,
    InventoryValidationError,
)
from quebradas_limaeste.inventory.parser import parse_inventory_html

BLOCKED_COMMANDS = frozenset({"historical", "live-smoke", "upload-drive"})


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command in BLOCKED_COMMANDS:
            return _blocked_mode(args.command)
        if args.command == "inventory":
            return _run_inventory(args)
    except InventoryValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except UnicodeDecodeError as exc:
        print(f"error: source is not valid UTF-8: {exc}", file=sys.stderr)
        return 2
    except SystemExit as exc:
        return int(exc.code)

    print(f"error: unsupported command {args.command}", file=sys.stderr)
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quebradas-inventory",
        description="Offline documentary inventory collector.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser(
        "inventory",
        help="Parse a local synthetic HTML/text source without network access.",
    )
    inventory.add_argument("--source", required=True, help="Local HTML/text file.")
    inventory.add_argument(
        "--source-name",
        required=True,
        help="Traceable source name.",
    )
    inventory.add_argument("--source-url", required=True, help="Public source URL.")
    inventory.add_argument(
        "--output-dir",
        required=True,
        help="Directory where JSON, CSV, and manifest outputs will be written.",
    )

    for command in sorted(BLOCKED_COMMANDS):
        subparsers.add_parser(
            command,
            help="Blocked until separate approval is granted.",
        )

    return parser


def _run_inventory(args: argparse.Namespace) -> int:
    source_text = read_source_text(Path(args.source))
    result = parse_inventory_html(
        source_text,
        source_name=args.source_name,
        source_url=args.source_url,
    )
    manifest = InventoryManifest.create(
        source_name=args.source_name,
        source_url=args.source_url,
        records=result.records,
        warnings=result.warnings,
    )
    paths = write_inventory_outputs(
        Path(args.output_dir),
        records=result.records,
        manifest=manifest,
    )
    print(f"Wrote {len(result.records)} records to {paths.inventory_json}")
    if result.warnings:
        print("; ".join(result.warnings), file=sys.stderr)
    return 0


def _blocked_mode(command: str) -> int:
    print(
        f"{command} requires separate approval before implementation or execution.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
