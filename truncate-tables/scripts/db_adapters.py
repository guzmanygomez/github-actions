"""Thin per-engine DB adapters used by truncate.py.

Each adapter exposes the same interface so truncate.py can stay engine-agnostic:
  - connect()
  - table_exists(table) -> bool
  - row_count(table) -> int
  - referencing_foreign_keys(table) -> list[str]   # human-readable descriptions
  - triggers(table) -> list[str]                    # trigger names on the table
  - truncate(table) -> None
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DbConfig:
    host: str
    port: int
    name: str
    username: str
    password: str


class MySQLAdapter:
    def __init__(self, config: DbConfig):
        import pymysql

        self._config = config
        self._conn = pymysql.connect(
            host=config.host,
            port=int(config.port),
            user=config.username,
            password=config.password,
            database=config.name,
            cursorclass=pymysql.cursors.Cursor,
        )

    def close(self) -> None:
        self._conn.close()

    def table_exists(self, table: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s",
                (self._config.name, table),
            )
            (count,) = cur.fetchone()
            return count > 0

    def row_count(self, table: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            (count,) = cur.fetchone()
            return count

    def referencing_foreign_keys(self, table: str) -> list[str]:
        # Any FK where this table is either the parent (referenced) or the
        # child (has the FK column) counts as a reason truncation is unsafe.
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, constraint_name, referenced_table_name
                FROM information_schema.key_column_usage
                WHERE table_schema = %s
                  AND referenced_table_name IS NOT NULL
                  AND (table_name = %s OR referenced_table_name = %s)
                """,
                (self._config.name, table, table),
            )
            return [
                f"{child}.{constraint} -> {parent}"
                for child, constraint, parent in cur.fetchall()
            ]

    def triggers(self, table: str) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT trigger_name FROM information_schema.triggers "
                "WHERE event_object_schema = %s AND event_object_table = %s",
                (self._config.name, table),
            )
            return [name for (name,) in cur.fetchall()]

    def truncate(self, table: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE `{table}`")
        self._conn.commit()


class PostgresAdapter:
    def __init__(self, config: DbConfig, schema: str):
        import psycopg2

        self._config = config
        self._schema = schema
        self._conn = psycopg2.connect(
            host=config.host,
            port=int(config.port),
            user=config.username,
            password=config.password,
            dbname=config.name,
        )

    def close(self) -> None:
        self._conn.close()

    def table_exists(self, table: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s",
                (self._schema, table),
            )
            (count,) = cur.fetchone()
            return count > 0

    def row_count(self, table: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM "{self._schema}"."{table}"')
            (count,) = cur.fetchone()
            return count

    def referencing_foreign_keys(self, table: str) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    tc.table_name AS child_table,
                    tc.constraint_name,
                    ccu.table_name AS parent_table
                FROM information_schema.table_constraints tc
                JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                    AND tc.table_schema = ccu.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                    AND tc.table_schema = %s
                    AND (tc.table_name = %s OR ccu.table_name = %s)
                """,
                (self._schema, table, table),
            )
            return [
                f"{child}.{constraint} -> {parent}"
                for child, constraint, parent in cur.fetchall()
            ]

    def triggers(self, table: str) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT trigger_name FROM information_schema.triggers "
                "WHERE event_object_schema = %s AND event_object_table = %s",
                (self._schema, table),
            )
            return [name for (name,) in cur.fetchall()]

    def truncate(self, table: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(f'TRUNCATE TABLE "{self._schema}"."{table}"')
        self._conn.commit()


def build_adapter(engine: str, config: DbConfig, schema: str):
    if engine == "mysql":
        return MySQLAdapter(config)
    if engine == "postgres":
        return PostgresAdapter(config, schema)
    raise ValueError(f"Unsupported db_engine: {engine!r} (expected 'mysql' or 'postgres')")
