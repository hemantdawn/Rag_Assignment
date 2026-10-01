from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.config import Settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.settings.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings.objects_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    object_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    trace_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (tenant, scope, sha256)
                );
                CREATE INDEX IF NOT EXISTS documents_scope_idx
                    ON documents (tenant, scope, status);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL UNIQUE REFERENCES documents(id),
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    lease_until TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS parents (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    ordinal INTEGER NOT NULL,
                    heading TEXT NOT NULL,
                    text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS parents_document_idx ON parents(document_id);
                CREATE TABLE IF NOT EXISTS children (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    parent_id TEXT NOT NULL REFERENCES parents(id),
                    ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    contextual_text TEXT NOT NULL,
                    embedding TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS children_document_idx ON children(document_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS child_fts USING fts5(
                    child_id UNINDEXED, contextual_text, tokenize='porter unicode61'
                );
                CREATE TABLE IF NOT EXISTS stage_events (
                    span_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    parent_span_id TEXT,
                    tenant TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    document_id TEXT,
                    pipeline TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms REAL NOT NULL,
                    self_ms REAL NOT NULL DEFAULT 0,
                    error_type TEXT,
                    details TEXT NOT NULL,
                    started_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS stage_events_scope_time_idx
                    ON stage_events(tenant, scope, started_at);
                CREATE INDEX IF NOT EXISTS stage_events_trace_idx
                    ON stage_events(trace_id, tenant, scope);
                """
            )
            # Existing workspaces predate trace IDs; keep their documents and indexes.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
            if "trace_id" not in columns:
                conn.execute("ALTER TABLE documents ADD COLUMN trace_id TEXT")
            event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(stage_events)")}
            if "self_ms" not in event_columns:
                conn.execute("ALTER TABLE stage_events ADD COLUMN self_ms REAL NOT NULL DEFAULT 0")
            signature = (
                "hash-v1" if self.settings.embedder == "hash"
                else f"sentence-transformers:{self.settings.embedding_model}"
            )
            row = conn.execute("SELECT value FROM metadata WHERE key='embedding_signature'").fetchone()
            if row and row["value"] != signature:
                raise RuntimeError(
                    f"Index uses {row['value']}; configured embedder is {signature}. "
                    "Use a new RAG_DATA_DIR or reindex before switching embedders."
                )
            conn.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('embedding_signature', ?)",
                (signature,),
            )

    def existing_document(self, tenant: str, scope: str, sha256: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE tenant=? AND scope=? AND sha256=?",
                (tenant, scope, sha256),
            ).fetchone()
            return dict(row) if row else None

    def create_document(
        self, document_id: str, tenant: str, scope: str, filename: str,
        sha256: str, object_path: Path, trace_id: str,
    ) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO documents
                (id, tenant, scope, filename, sha256, object_path, status, trace_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
                (document_id, tenant, scope, filename, sha256, str(object_path), trace_id, now, now),
            )
            conn.execute(
                """INSERT INTO jobs(id, document_id, status, available_at, updated_at)
                VALUES (?, ?, 'queued', ?, ?)""",
                (document_id, document_id, now, now),
            )

    def get_document(self, document_id: str, tenant: str, scopes: frozenset[str]) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT d.id, d.scope, d.filename, d.sha256, d.status, d.error,
                          d.trace_id, d.created_at, d.updated_at, j.attempts
                   FROM documents d JOIN jobs j ON j.document_id=d.id
                   WHERE d.id=? AND d.tenant=?""",
                (document_id, tenant),
            ).fetchone()
            return dict(row) if row and row["scope"] in scopes else None

    def retry_document(self, document_id: str, tenant: str, scopes: frozenset[str]) -> bool:
        document = self.get_document(document_id, tenant, scopes)
        if not document or document["status"] != "failed":
            return False
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE documents SET status='queued', error=NULL, updated_at=? WHERE id=?",
                (now, document_id),
            )
            conn.execute(
                """UPDATE jobs SET status='queued', attempts=0, error=NULL,
                   lease_until=NULL, available_at=?, updated_at=? WHERE document_id=?""",
                (now, now, document_id),
            )
        return True

    def claim_job(self) -> dict | None:
        now = utc_now()
        lease = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT j.id, j.document_id, d.filename, d.object_path,
                          d.tenant, d.scope, d.trace_id
                   FROM jobs j JOIN documents d ON d.id=j.document_id
                   WHERE (j.status='queued' AND j.available_at<=?)
                      OR (j.status='running' AND j.lease_until<?)
                   ORDER BY j.available_at LIMIT 1""",
                (now, now),
            ).fetchone()
            if not row:
                return None
            trace_id = row["trace_id"] or uuid4().hex
            if not row["trace_id"]:
                conn.execute("UPDATE documents SET trace_id=? WHERE id=?", (trace_id, row["document_id"]))
            conn.execute(
                """UPDATE jobs SET status='running', attempts=attempts+1,
                   lease_until=?, updated_at=? WHERE id=?""",
                (lease, now, row["id"]),
            )
            conn.execute(
                "UPDATE documents SET status='processing', updated_at=? WHERE id=?",
                (now, row["document_id"]),
            )
            return {**dict(row), "trace_id": trace_id}

    def record_stage(self, event: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO stage_events
                   (span_id, trace_id, parent_span_id, tenant, scope, document_id,
                    pipeline, stage, status, duration_ms, self_ms, error_type, details, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event["span_id"], event["trace_id"], event["parent_span_id"],
                    event["tenant"], event["scope"], event["document_id"],
                    event["pipeline"], event["stage"], event["status"],
                    event["duration_ms"], event["self_ms"], event["error_type"],
                    json.dumps(event["details"], sort_keys=True), event["started_at"],
                ),
            )

    def get_trace(self, trace_id: str, tenant: str, scopes: frozenset[str]) -> list[dict]:
        if not scopes:
            return []
        placeholders = ",".join("?" for _ in scopes)
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT span_id, trace_id, parent_span_id, scope, document_id,
                           pipeline, stage, status, duration_ms, self_ms,
                           error_type, details, started_at
                    FROM stage_events
                    WHERE trace_id=? AND tenant=? AND scope IN ({placeholders})
                    ORDER BY started_at, rowid""",
                (trace_id, tenant, *sorted(scopes)),
            ).fetchall()
            return [{**dict(row), "details": json.loads(row["details"])} for row in rows]

    def stage_summary(self, tenant: str, scope: str, hours: int) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT pipeline, stage, status, duration_ms, self_ms
                   FROM stage_events WHERE tenant=? AND scope=? AND started_at>=?""",
                (tenant, scope, since),
            ).fetchall()
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            groups.setdefault((row["pipeline"], row["stage"]), []).append(row)
        summary = []
        for (pipeline, stage), events in groups.items():
            durations = sorted(row["duration_ms"] for row in events)
            total = sum(durations)
            total_self = sum(row["self_ms"] for row in events)
            summary.append({
                "pipeline": pipeline,
                "stage": stage,
                "count": len(events),
                "failures": sum(row["status"] == "error" for row in events),
                "total_ms": round(total, 3),
                "avg_ms": round(total / len(events), 3),
                "self_total_ms": round(total_self, 3),
                "self_avg_ms": round(total_self / len(events), 3),
                "p95_ms": round(durations[math.ceil(0.95 * len(events)) - 1], 3),
                "max_ms": round(durations[-1], 3),
            })
        return sorted(summary, key=lambda item: item["self_total_ms"], reverse=True)

    def complete_job(self, document_id: str, parents: list[dict], children: list[dict]) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM child_fts WHERE child_id IN (SELECT id FROM children WHERE document_id=?)",
                (document_id,),
            )
            conn.execute("DELETE FROM children WHERE document_id=?", (document_id,))
            conn.execute("DELETE FROM parents WHERE document_id=?", (document_id,))
            conn.executemany(
                "INSERT INTO parents(id, document_id, ordinal, heading, text) VALUES (?, ?, ?, ?, ?)",
                [(p["id"], document_id, p["ordinal"], p["heading"], p["text"]) for p in parents],
            )
            conn.executemany(
                """INSERT INTO children
                   (id, document_id, parent_id, ordinal, text, contextual_text, embedding)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (c["id"], document_id, c["parent_id"], c["ordinal"], c["text"],
                     c["contextual_text"], json.dumps(c["embedding"]))
                    for c in children
                ],
            )
            conn.executemany(
                "INSERT INTO child_fts(child_id, contextual_text) VALUES (?, ?)",
                [(c["id"], c["contextual_text"]) for c in children],
            )
            conn.execute(
                "UPDATE jobs SET status='done', lease_until=NULL, error=NULL, updated_at=? WHERE document_id=?",
                (now, document_id),
            )
            conn.execute(
                "UPDATE documents SET status='ready', error=NULL, updated_at=? WHERE id=?",
                (now, document_id),
            )

    def fail_job(self, document_id: str, error: str) -> None:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with self.connect() as conn:
            row = conn.execute("SELECT attempts FROM jobs WHERE document_id=?", (document_id,)).fetchone()
            attempts = row["attempts"] if row else self.settings.max_attempts
            exhausted = attempts >= self.settings.max_attempts
            status = "failed" if exhausted else "queued"
            available = (now_dt + timedelta(seconds=min(300, 2 ** attempts))).isoformat()
            conn.execute(
                """UPDATE jobs SET status=?, error=?, lease_until=NULL,
                   available_at=?, updated_at=? WHERE document_id=?""",
                (status, error[:1000], available, now, document_id),
            )
            conn.execute(
                "UPDATE documents SET status=?, error=?, updated_at=? WHERE id=?",
                (status, error[:1000], now, document_id),
            )

    def lexical_search(self, query: str, tenant: str, scope: str, limit: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT c.id, c.parent_id, c.document_id, c.text, c.contextual_text,
                          c.embedding,
                          d.filename, bm25(child_fts) AS bm25_score
                   FROM child_fts JOIN children c ON c.id=child_fts.child_id
                   JOIN documents d ON d.id=c.document_id
                   WHERE child_fts MATCH ? AND d.tenant=? AND d.scope=? AND d.status='ready'
                   ORDER BY bm25(child_fts) LIMIT ?""",
                (query, tenant, scope, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def dense_search(self, vector: list[float], tenant: str, scope: str, limit: int) -> list[dict]:
        from app.embedding import cosine

        rows = self.dense_candidates(tenant, scope)
        ranked = sorted(
            ((cosine(vector, json.loads(row["embedding"])), row) for row in rows),
            key=lambda item: item[0], reverse=True,
        )[:limit]
        return [{**row, "similarity": score} for score, row in ranked]

    @contextmanager
    def search_session(self, tenant: str, scope: str):
        # The legacy SQLite backend has immutable documents. PostgreSQL's
        # versioned backend provides a repeatable-read snapshot here.
        yield self

    def dense_candidates(self, tenant: str, scope: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT c.id, c.parent_id, c.document_id, c.text, c.contextual_text,
                          c.embedding, d.filename
                   FROM children c JOIN documents d ON d.id=c.document_id
                   WHERE d.tenant=? AND d.scope=? AND d.status='ready'""",
                (tenant, scope),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_parents(self, ids: list[str], tenant: str, scope: str) -> dict[str, dict]:
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT p.id, p.heading, p.text, p.document_id, d.filename
                    FROM parents p JOIN documents d ON d.id=p.document_id
                    WHERE p.id IN ({placeholders}) AND d.tenant=? AND d.scope=?
                      AND d.status='ready'""",
                (*ids, tenant, scope),
            ).fetchall()
            return {row["id"]: dict(row) for row in rows}
