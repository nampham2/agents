#!/usr/bin/env python3
"""Validate an agentic workspace project without mutating it."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from workspace_lib import validate_project


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_directory", type=Path)
    parser.add_argument("--close", action="store_true", help="Enforce completion invariants")
    parser.add_argument(
        "--check-index",
        action="store_true",
        help="Require the parent workspace INDEX.md to match canonical project state",
    )
    parser.add_argument(
        "--allow-legacy-close",
        action="store_true",
        help="Acknowledge the limited guarantees of closing an unmigrated schema-v1 project",
    )
    args = parser.parse_args()

    report = validate_project(
        args.project_directory,
        close=args.close,
        check_index=args.check_index,
        allow_legacy_close=args.allow_legacy_close,
    )
    for warning in report.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    if report.errors:
        for error in report.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"Workspace valid: {args.project_directory.expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
