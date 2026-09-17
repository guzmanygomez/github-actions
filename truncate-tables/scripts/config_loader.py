"""Loads and validates the truncate-tables allowlist config."""
from __future__ import annotations

from dataclasses import dataclass

import yaml


class ConfigError(ValueError):
    pass


@dataclass
class TableEntry:
    database: str
    schema: str
    name: str


@dataclass
class Config:
    tables: list[TableEntry]
    fail_on_block: bool
    lock_max_attempts: int
    lock_base_delay_seconds: float
    lock_max_delay_seconds: float
    lock_attempt_timeout_seconds: float


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    tables_raw = raw.get("tables")
    if not tables_raw or not isinstance(tables_raw, list):
        raise ConfigError(
            "Config must define a non-empty 'tables' list (explicit allowlist, no wildcards)"
        )

    tables: list[TableEntry] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in tables_raw:
        if not isinstance(entry, dict) or "name" not in entry or "database" not in entry:
            raise ConfigError(
                f"Invalid table entry: {entry!r} (must be a mapping with 'database' and 'name' keys)"
            )
        database = entry["database"]
        schema = entry.get("schema", "public")
        name = entry["name"]
        key = (database, schema, name)
        if key in seen:
            raise ConfigError(f"Duplicate table entry in allowlist: {key}")
        seen.add(key)
        tables.append(TableEntry(database=database, schema=schema, name=name))

    lock_raw = raw.get("lock") or {}
    if not isinstance(lock_raw, dict):
        raise ConfigError("'lock' must be a mapping if present")

    try:
        lock_max_attempts = int(lock_raw.get("max_attempts", 10))
        lock_base_delay_seconds = float(lock_raw.get("base_delay_seconds", 1))
        lock_max_delay_seconds = float(lock_raw.get("max_delay_seconds", 30))
        lock_attempt_timeout_seconds = float(lock_raw.get("attempt_timeout_seconds", 3))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid 'lock' config: {exc}") from exc

    if lock_max_attempts < 1:
        raise ConfigError("'lock.max_attempts' must be >= 1")
    if lock_base_delay_seconds < 0 or lock_max_delay_seconds < 0 or lock_attempt_timeout_seconds <= 0:
        raise ConfigError("'lock' delay/timeout values must be >= 0 (attempt_timeout_seconds > 0)")

    return Config(
        tables=tables,
        fail_on_block=bool(raw.get("fail_on_block", True)),
        lock_max_attempts=lock_max_attempts,
        lock_base_delay_seconds=lock_base_delay_seconds,
        lock_max_delay_seconds=lock_max_delay_seconds,
        lock_attempt_timeout_seconds=lock_attempt_timeout_seconds,
    )
