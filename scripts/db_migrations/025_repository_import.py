#!/usr/bin/env python3
"""
Migration 025: An import of a repository from a fallback source.

Changes:
- repository_import: one row per accepted import: where it reads from, the
  target repository, the state people see, and the byte/file counters the
  repository page shows while it runs.

A repository whose import did not finish is a partial copy; the row is what
lets the page say so (kohakuhub.api.importer).

Plain SQL, not the models: this must keep meaning what it means today.
"""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
# Add db_migrations to path (for _migration_utils)
sys.path.insert(0, os.path.dirname(__file__))

from kohakuhub.config import cfg
from kohakuhub.db import db
from _migration_utils import (
    check_column_exists,
    check_table_exists,
    should_skip_due_to_future_migrations,
)

MIGRATION_NUMBER = 25


def is_applied(db, cfg):
    """Applied once the schema before it is complete (so earlier migrations
    never skip themselves on its account) and its table exists."""
    if not check_table_exists(db, "lfs_gc_state"):
        return False
    if not check_column_exists(db, cfg, "repository", "main_counted_commit"):
        return False
    if not check_column_exists(db, cfg, "repository", "history_root"):
        return False
    if not check_table_exists(db, "repository_write"):
        return False
    return check_table_exists(db, "repository_import")


def _migrate(timestamp_type: str, serial: str, bigint: str):
    cursor = db.cursor()
    print("Creating repository_import...")
    # Built from parts so the types are plainly visible and the quoting stays
    # ordinary (a single escaped-quote f-string block is hard to read and easy
    # to get subtly wrong).
    columns = [
        ("id", serial, "NOT NULL PRIMARY KEY"),
        ("repository_id", "INTEGER", "NOT NULL"),
        ("repo_type", "VARCHAR(16)", "NOT NULL"),
        ("source_repo_id", "VARCHAR(255)", "NOT NULL"),
        ("source_revision", "VARCHAR(255)", "NOT NULL DEFAULT 'main'"),
        ("source_url", "VARCHAR(255)", "NOT NULL"),
        ("source_name", "VARCHAR(255)", "NOT NULL"),
        ("source_type", "VARCHAR(32)", "NOT NULL"),
        ("source_token", "TEXT", None),
        ("requested_by_id", "INTEGER", None),
        ("state", "VARCHAR(16)", "NOT NULL DEFAULT 'queued'"),
        ("stage", "VARCHAR(255)", None),
        ("files_total", "INTEGER", "NOT NULL DEFAULT 0"),
        ("files_done", "INTEGER", "NOT NULL DEFAULT 0"),
        # The byte counters, and the allowance derived from the quota: 64-bit,
        # because a model's bytes do not fit in an INTEGER on PostgreSQL.
        ("bytes_total", bigint, "NOT NULL DEFAULT 0"),
        ("bytes_done", bigint, "NOT NULL DEFAULT 0"),
        ("allowed_bytes", bigint, None),
        ("error", "TEXT", None),
        ("commit_id", "VARCHAR(64)", None),
        ("created_at", timestamp_type, "NOT NULL"),
        ("updated_at", timestamp_type, "NOT NULL"),
    ]
    definitions = ", ".join(
        " ".join(part for part in (f'"{name}"', type_, qualifiers) if part)
        for name, type_, qualifiers in columns
    )
    cursor.execute(
        f'CREATE TABLE IF NOT EXISTS "repository_import" ('
        f"{definitions}, "
        'FOREIGN KEY ("repository_id") REFERENCES "repository" ("id") ON DELETE CASCADE, '
        'FOREIGN KEY ("requested_by_id") REFERENCES "user" ("id") ON DELETE SET NULL)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS "repositoryimport_repository_id" '
        'ON "repository_import" ("repository_id")'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS "repositoryimport_requested_by_id" '
        'ON "repository_import" ("requested_by_id")'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS "repositoryimport_repository_id_state" '
        'ON "repository_import" ("repository_id", "state")'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS "repositoryimport_state_created_at" '
        'ON "repository_import" ("state", "created_at")'
    )
    print("  ✓ Created repository_import")


def run():
    """Run migration 025.

    Returns:
        True if successful or already applied, False otherwise
    """
    db.connect(reuse_if_open=True)

    try:
        if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Skipped (superseded by future migration)")
            return True

        if is_applied(db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Already applied (repository_import exists)")
            return True

        print("=" * 70)
        print(f"Migration {MIGRATION_NUMBER}: Repository imports")
        print("=" * 70)
        with db.atomic():
            if cfg.app.db_backend == "postgres":
                _migrate("TIMESTAMP", "SERIAL", "BIGINT")
            else:
                _migrate("DATETIME", "INTEGER", "BIGINT")
        print(f"Migration {MIGRATION_NUMBER}: ✓ Completed Successfully")
        return True

    except Exception as e:
        print(f"\n✗ Migration {MIGRATION_NUMBER} failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    run()
