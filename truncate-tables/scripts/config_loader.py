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

    return Config(
        tables=tables,
        fail_on_block=bool(raw.get("fail_on_block", True)),
    )
