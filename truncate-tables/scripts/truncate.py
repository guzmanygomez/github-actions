#!/usr/bin/env python3
"""Entrypoint for the truncate-tables action.

Reads an explicit table allowlist from a YAML config - each entry names its
own database/schema, so a single run can target multiple databases/schemas
on the same DB server/credentials. For each table:

  1. Checks for foreign keys and triggers that would make truncation unsafe;
     blocks the table if either is found.
  2. In dry-run mode (the default), logs what it would do and stops there.
  3. Otherwise, takes an exclusive lock on the table (retrying with backoff
     and jitter if it's contended - see locking.py), captures its PK
     sequence/AUTO_INCREMENT "next value", truncates it, restores that value,
     then releases the lock. This guards against a truncated table's PK
     sequence restarting and later clashing with rows already moved to the
     datalake.

Writes `status` and `summary` to $GITHUB_OUTPUT so the calling workflow can
post its own Slack notification (see README).
"""
from __future__ import annotations

import argparse
import os
import sys

from config_loader import ConfigError, load_config
from db_adapters import DbConfig, build_adapter
from locking import LockAcquisitionError, acquire_lock_with_retry


def is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "true").strip().lower() != "false"


def write_outputs(status: str, summary: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as f:
        f.write(f"status={status}\n")
        f.write(f"summary<<TRUNCATE_TABLES_SUMMARY_EOF\n{summary}\nTRUNCATE_TABLES_SUMMARY_EOF\n")


def truncate_with_lock(adapter, entry, label: str, config) -> tuple[str, str]:
    """Locks (with retry), preserves the PK sequence, truncates, and unlocks.

    Returns (status, detail). Never raises - all failure modes are reported
    as a status string so the caller can keep processing other tables.
    """

    def on_retry(attempt, max_attempts, delay, exc):
        print(
            f"::warning title=truncate-tables::{label}: lock attempt {attempt}/{max_attempts} "
            f"failed ({exc}); retrying in {delay:.1f}s"
        )

    try:
        attempts = acquire_lock_with_retry(
            lambda: adapter.try_lock(entry.name, config.lock_attempt_timeout_seconds),
            max_attempts=config.lock_max_attempts,
            base_delay_seconds=config.lock_base_delay_seconds,
            max_delay_seconds=config.lock_max_delay_seconds,
            on_retry=on_retry,
        )
    except LockAcquisitionError as exc:
        print(f"::error title=truncate-tables::{label}: {exc}")
        return "LOCK_FAILED", str(exc)

    truncated = False
    try:
        seq_state = adapter.capture_sequence_state(entry.name)
        adapter.truncate(entry.name)
        truncated = True
        adapter.restore_sequence_state(entry.name, seq_state)
        adapter.commit()
        detail = f"lock acquired on attempt {attempts}, PK sequence preserved"
        print(f"::notice title=truncate-tables::Truncated {label} ({detail})")
        return "TRUNCATED", detail
    except Exception as exc:  # noqa: BLE001 - surfaced via status, not re-raised
        adapter.rollback()
        if truncated:
            # MySQL's TRUNCATE is DDL and already committed - the sequence
            # restore failing afterwards needs manual follow-up, it can't be
            # rolled back.
            print(
                f"::error title=truncate-tables::{label}: truncated but failed to restore "
                f"PK sequence: {exc}"
            )
            return "TRUNCATED_SEQUENCE_RESTORE_FAILED", str(exc)
        print(f"::error title=truncate-tables::{label}: failed before truncation: {exc}")
        return "ERROR", str(exc)
    finally:
        adapter.unlock()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to the allowlist YAML config")
    args = parser.parse_args()

    engine = os.environ["DB_ENGINE"]
    db_host = os.environ["DB_HOST"]
    db_port = os.environ["DB_PORT"]
    db_username = os.environ["DB_USERNAME"]
    db_password = os.environ["DB_PASSWORD"]
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
        db_config = DbConfig(
            host=db_host,
            port=db_port,
            name=entry.database,
            username=db_username,
            password=db_password,
        )
        adapter = build_adapter(engine, db_config, entry.schema)
        label = (
            f"{entry.database}.{entry.schema}.{entry.name}"
            if engine == "postgres"
            else f"{entry.database}.{entry.name}"
        )
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
                print(
                    f"::notice title=truncate-tables::[DRY RUN] would lock {label} "
                    f"(up to {config.lock_max_attempts} attempts), preserve its PK sequence/"
                    f"auto-increment value, truncate ({count} rows), then restore it"
                )
                results.append((label, "DRY_RUN", f"{count} rows"))
                continue

            status, detail = truncate_with_lock(adapter, entry, label, config)
            results.append((label, status, detail))
        finally:
            adapter.close()

    failing_statuses = {"ERROR", "LOCK_FAILED", "TRUNCATED_SEQUENCE_RESTORE_FAILED"}
    blocked = [r for r in results if r[1] == "BLOCKED"]
    failed = [r for r in results if r[1] in failing_statuses]

    summary_lines = [f"{label}: {status} ({detail})" for label, status, detail in results]
    summary_text = "\n".join(summary_lines)
    print("::group::truncate-tables summary")
    print(summary_text)
    print("::endgroup::")

    success = not failed and not (blocked and config.fail_on_block)
    write_outputs("success" if success else "failure", summary_text)

    if failed or (blocked and config.fail_on_block):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
