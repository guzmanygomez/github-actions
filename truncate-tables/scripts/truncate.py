#!/usr/bin/env python3
"""Entrypoint for the truncate-tables action.

Reads an explicit table allowlist from a YAML config, checks each table for
foreign keys and triggers that would make truncation unsafe, then either
logs what it would do (dry-run, the default) or truncates the non-blocked
tables. Writes `status` and `summary` to $GITHUB_OUTPUT so the calling
workflow can post its own Slack notification (see README).
"""
from __future__ import annotations

import argparse
import os
import sys

from config_loader import ConfigError, load_config
from db_adapters import DbConfig, build_adapter


def is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "true").strip().lower() != "false"


def write_outputs(status: str, summary: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as f:
        f.write(f"status={status}\n")
        f.write(f"summary<<TRUNCATE_TABLES_SUMMARY_EOF\n{summary}\nTRUNCATE_TABLES_SUMMARY_EOF\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to the allowlist YAML config")
    args = parser.parse_args()

    engine = os.environ["DB_ENGINE"]
    db_config = DbConfig(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        name=os.environ["DB_NAME"],
        username=os.environ["DB_USERNAME"],
        password=os.environ["DB_PASSWORD"],
    )
    dry_run = is_dry_run()

    try:
        config = load_config(args.config)
    except (ConfigError, OSError) as exc:
        message = f"Failed to load config: {exc}"
        print(f"::error title=truncate-tables::{message}")
        write_outputs("failure", message)
        return 1

    results = []

    for entry in config.tables:
        adapter = build_adapter(engine, db_config, entry.schema)
        label = f"{entry.schema}.{entry.name}" if engine == "postgres" else entry.name
        try:
            if not adapter.table_exists(entry.name):
                print(f"::error title=truncate-tables::Table not found: {label}")
                results.append((label, "ERROR", "table not found"))
                continue

            fks = adapter.referencing_foreign_keys(entry.name)
            trigs = adapter.triggers(entry.name)
            count = adapter.row_count(entry.name)

            if fks or trigs:
                reason = []
                if fks:
                    reason.append(f"foreign keys: {', '.join(fks)}")
                if trigs:
                    reason.append(f"triggers: {', '.join(trigs)}")
                msg = f"BLOCKED {label} ({count} rows) - " + "; ".join(reason)
                print(f"::warning title=truncate-tables::{msg}")
                results.append((label, "BLOCKED", "; ".join(reason)))
                continue

            if dry_run:
                print(f"::notice title=truncate-tables::[DRY RUN] would truncate {label} ({count} rows)")
                results.append((label, "DRY_RUN", f"{count} rows"))
            else:
                adapter.truncate(entry.name)
                print(f"::notice title=truncate-tables::Truncated {label} ({count} rows)")
                results.append((label, "TRUNCATED", f"{count} rows"))
        finally:
            adapter.close()

    blocked = [r for r in results if r[1] == "BLOCKED"]
    errored = [r for r in results if r[1] == "ERROR"]

    summary_lines = [f"{label}: {status} ({detail})" for label, status, detail in results]
    summary_text = "\n".join(summary_lines)
    print("::group::truncate-tables summary")
    print(summary_text)
    print("::endgroup::")

    success = not errored and not (blocked and config.fail_on_block)
    write_outputs("success" if success else "failure", summary_text)

    if errored or (blocked and config.fail_on_block):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
