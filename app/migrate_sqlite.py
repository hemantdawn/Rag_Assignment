"""Queue existing ready SQLite documents for reindexing in PostgreSQL.

The original SQLite database and object files are read only. Run the
PostgreSQL worker afterward to parse, embed, and atomically publish them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from app.config import load_settings
from app.postgres_db import PostgresDatabase


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def migrate(sqlite_path: Path, target: PostgresDatabase, dry_run: bool = False) -> dict:
    if not sqlite_path.is_file():
        raise FileNotFoundError(sqlite_path)
    summary = {"queued": 0, "already_present": 0, "missing_object": 0,
               "checksum_mismatch": 0, "not_ready": 0}
    uri = sqlite_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as source:
        source.row_factory = sqlite3.Row
        columns = {row["name"] for row in source.execute("PRAGMA table_info(documents)")}
        trace_column = "trace_id" if "trace_id" in columns else "NULL AS trace_id"
        rows = source.execute(
            f"""SELECT id,tenant,scope,filename,sha256,object_path,status,{trace_column}
                FROM documents ORDER BY created_at"""
        ).fetchall()
    for row in rows:
        if row["status"] != "ready":
            summary["not_ready"] += 1
            continue
        object_path = Path(row["object_path"])
        if not object_path.is_file():
            summary["missing_object"] += 1
            continue
        if file_sha256(object_path) != row["sha256"]:
            summary["checksum_mismatch"] += 1
            continue
        if target.get_document(row["id"], row["tenant"], frozenset([row["scope"]])):
            summary["already_present"] += 1
            continue
        if not dry_run:
            created = target.create_document(
                row["id"], row["tenant"], row["scope"], row["filename"],
                row["sha256"], object_path, row["trace_id"] or uuid4().hex,
            )
            if created["deduplicated"]:
                summary["already_present"] += 1
                continue
        summary["queued"] += 1
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-db", type=Path, help="Source SQLite database (default: RAG_DATA_DIR/metadata.sqlite3)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    if not settings.database_url:
        parser.error("Set RAG_DATABASE_URL to the target PostgreSQL database")
    target = PostgresDatabase(settings)
    target.initialize()
    summary = migrate(args.sqlite_db or settings.db_path, target, args.dry_run)
    print(json.dumps({"dry_run": args.dry_run, **summary}, indent=2))


if __name__ == "__main__":
    main()
