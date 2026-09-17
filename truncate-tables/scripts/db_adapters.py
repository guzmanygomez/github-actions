"""Thin per-engine DB adapters used by truncate.py.

Each adapter exposes the same interface so truncate.py can stay engine-agnostic:
  - close()
  - table_exists(table) -> bool
  - row_count(table) -> int
  - referencing_foreign_keys(table) -> list[str]   # human-readable descriptions
  - triggers(table) -> list[str]                    # trigger names on the table
  - try_lock(table, timeout_seconds) -> None         # single attempt; raises on failure/timeout
  - unlock() -> None                                 # releases whatever try_lock acquired
  - capture_sequence_state(table) -> Any             # opaque, pass straight to restore_sequence_state
  - restore_sequence_state(table, state) -> None
  - truncate(table) -> None                          # does not commit - caller controls that
  - commit() -> None
  - rollback() -> None

Locking + sequence preservation exist to avoid a primary key clash with the
datalake: a table's PK sequence/auto-increment "next value" is captured right
after the exclusive lock is acquired (so no in-flight transaction can still be
consuming it), the table is truncated, and the captured value is restored -
all before the lock is released - so the next row inserted after truncation
continues from where it left off instead of restarting from 1.
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
            autocommit=False,
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

    def try_lock(self, table: str, timeout_seconds: float) -> None:
        # LOCK TABLES is session-scoped (not part of the transaction) and
        # either fully succeeds or fails outright - no partial lock state to
        # clean up on failure. lock_wait_timeout bounds how long this single
        # attempt waits; the caller layers its own retry/backoff on top.
        timeout = max(1, int(round(timeout_seconds)))
        with self._conn.cursor() as cur:
            cur.execute(f"SET SESSION lock_wait_timeout = {timeout}")
            cur.execute(f"LOCK TABLES `{table}` WRITE")

    def unlock(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute("UNLOCK TABLES")

    def capture_sequence_state(self, table: str):
        # The "next" AUTO_INCREMENT value MySQL will hand out - exactly what
        # we want to restore after truncate (which always resets it to 1).
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT AUTO_INCREMENT FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s",
                (self._config.name, table),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def restore_sequence_state(self, table: str, state) -> None:
        if state is None:
            return  # table has no AUTO_INCREMENT column - nothing to restore
        with self._conn.cursor() as cur:
            cur.execute(f"ALTER TABLE `{table}` AUTO_INCREMENT = %s", (state,))

    def truncate(self, table: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE `{table}`")

    def commit(self) -> None:
        # TRUNCATE/ALTER are DDL and auto-commit in MySQL regardless; this is
        # a no-op safety net for symmetry with the Postgres adapter.
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


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
        self._conn.autocommit = False

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

    def try_lock(self, table: str, timeout_seconds: float) -> None:
        # ACCESS EXCLUSIVE is what TRUNCATE itself takes anyway; taking it
        # explicitly first, with a short lock_timeout, lets us control
        # retry/backoff ourselves instead of blocking on Postgres's queue.
        # Runs inside the transaction that also does the truncate/restore/
        # commit, so the lock is held until that commit (or a rollback).
        timeout_ms = max(1, int(round(timeout_seconds * 1000)))
        with self._conn.cursor() as cur:
            cur.execute(f"SET LOCAL lock_timeout = '{timeout_ms}ms'")
            cur.execute(f'LOCK TABLE "{self._schema}"."{table}" IN ACCESS EXCLUSIVE MODE')

    def unlock(self) -> None:
        # Nothing to do: the lock is released by commit()/rollback().
        pass

    def capture_sequence_state(self, table: str):
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT pg_get_serial_sequence(%s, a.attname)
                FROM pg_attribute a
                JOIN pg_class c ON a.attrelid = c.oid
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE c.relname = %s AND n.nspname = %s
                  AND a.attnum > 0 AND NOT a.attisdropped
                """,
                (f"{self._schema}.{table}", table, self._schema),
            )
            sequence_names = [row[0] for row in cur.fetchall() if row[0]]

            state = []
            for seq in sequence_names:
                cur.execute(f"SELECT last_value, is_called FROM {seq}")
                last_value, is_called = cur.fetchone()
                state.append((seq, last_value, is_called))
            return state or None

    def restore_sequence_state(self, table: str, state) -> None:
        if not state:
            return  # table has no serial/identity column - nothing to restore
        with self._conn.cursor() as cur:
            for seq, last_value, is_called in state:
                cur.execute("SELECT setval(%s, %s, %s)", (seq, last_value, is_called))

    def truncate(self, table: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(f'TRUNCATE TABLE "{self._schema}"."{table}"')

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


def build_adapter(engine: str, config: DbConfig, schema: str):
    if engine == "mysql":
        return MySQLAdapter(config)
    if engine == "postgres":
        return PostgresAdapter(config, schema)
    raise ValueError(f"Unsupported db_engine: {engine!r} (expected 'mysql' or 'postgres')")
