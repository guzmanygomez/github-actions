import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from config_loader import ConfigError, load_config  # noqa: E402


def write_config(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".yml")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def test_load_valid_config():
    path = write_config(
        """
        fail_on_block: true
        tables:
          - database: analytics
            schema: public
            name: example_audit_log
        """
    )
    config = load_config(path)
    assert config.fail_on_block is True
    assert len(config.tables) == 1
    assert config.tables[0].database == "analytics"
    assert config.tables[0].name == "example_audit_log"


def test_lock_defaults():
    path = write_config(
        """
        tables:
          - database: analytics
            name: example_audit_log
        """
    )
    config = load_config(path)
    assert config.lock_max_attempts == 10
    assert config.lock_base_delay_seconds == 1
    assert config.lock_max_delay_seconds == 30
    assert config.lock_attempt_timeout_seconds == 3


def test_lock_overrides():
    path = write_config(
        """
        lock:
          max_attempts: 5
          base_delay_seconds: 0.5
          max_delay_seconds: 10
          attempt_timeout_seconds: 2
        tables:
          - database: analytics
            name: example_audit_log
        """
    )
    config = load_config(path)
    assert config.lock_max_attempts == 5
    assert config.lock_base_delay_seconds == 0.5
    assert config.lock_max_delay_seconds == 10
    assert config.lock_attempt_timeout_seconds == 2


def test_rejects_invalid_lock_max_attempts():
    path = write_config(
        """
        lock:
          max_attempts: 0
        tables:
          - database: analytics
            name: example_audit_log
        """
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_supports_multiple_databases():
    path = write_config(
        """
        tables:
          - database: analytics
            name: example_audit_log
          - database: reporting
            schema: staging
            name: example_audit_log
        """
    )
    config = load_config(path)
    assert [t.database for t in config.tables] == ["analytics", "reporting"]


def test_rejects_empty_table_list():
    path = write_config("tables: []")
    with pytest.raises(ConfigError):
        load_config(path)


def test_rejects_missing_table_list():
    path = write_config("fail_on_block: true")
    with pytest.raises(ConfigError):
        load_config(path)


def test_rejects_duplicate_tables():
    path = write_config(
        """
        tables:
          - database: analytics
            schema: public
            name: dupe
          - database: analytics
            schema: public
            name: dupe
        """
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_rejects_entry_without_name():
    path = write_config(
        """
        tables:
          - database: analytics
            schema: public
        """
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_rejects_entry_without_database():
    path = write_config(
        """
        tables:
          - schema: public
            name: example_audit_log
        """
    )
    with pytest.raises(ConfigError):
        load_config(path)
