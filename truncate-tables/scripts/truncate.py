#!/usr/bin/env python3
"""Entrypoint for the truncate-tables action.

Reads an explicit table allowlist from a YAML config, checks each table for
foreign keys and triggers that would make truncation unsafe, then either
logs what it would do (dry-run, the default) or truncates the non-blocked
tables. Sends a Slack notification on completion (success or failure).
"""
from __future__ import annotations

import argparse
import os
import sys

from config_loader import ConfigError, load_config
from db_adapters import DbConfig, build_adapter
from notify import send_slack_notification


def is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "true").strip().lower() != "false"


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
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    dry_run = is_dry_run()

    try:
        config = load_config(args.config)
    except (ConfigError, OSError) as exc:
        print(f"::error title=truncate-tables::Failed to load config: {exc}")
        send_slack_notification(
            slack_webhook_url, "#data-retention-alerts", f":x: truncate-tables failed to load config: {exc}"
        )
        return 1

    results = []
    had_error = False

    for entry in config.tables:
        adapter = build_adapter(engine, db_config, entry.schema)
        label = f"{entry.schema}.{entry.name}" if engine == "postgres" else entry.name
        try:
            if not adapter.table_exists(entry.name):
                print(f"::error title=truncate-tables::Table not found: {label}")
                results.append((label, "ERROR", "table not found"))
                had_error = True
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
    icon = ":white_check_mark:" if success else ":x:"
    send_slack_notification(
        slack_webhook_url,
        config.slack_channel,
        f"{icon} truncate-tables ({'dry-run' if dry_run else 'live'}) finished:\n{summary_text}",
    )

    if errored or (blocked and config.fail_on_block):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
