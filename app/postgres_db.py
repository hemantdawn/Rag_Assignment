from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.config import Settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PostgresSearchSession:
    """One MVCC snapshot for retrieval and citation expansion."""

    def __init__(self, conn):
        self.conn = conn

    def lexical_search(self, query: str, tenant: str, scope: str, limit: int) -> list[dict]:
        rows = self.conn.execute(
            """SELECT c.id, c.parent_id, c.document_id, c.version_id, c.text,
                      c.contextual_text, c.embedding::text AS embedding,
                      v.filename, ts_rank_cd(c.search_vector,
                      websearch_to_tsquery('english', %s)) AS lexical_score
               FROM children c
               JOIN document_versions v ON v.id=c.version_id
               JOIN documents d ON d.id=c.document_id AND d.active_version_id=c.version_id
               WHERE d.tenant=%s AND d.scope=%s
                 AND c.search_vector @@ websearch_to_tsquery('english', %s)
               ORDER BY lexical_score DESC LIMIT %s""",
            (query, tenant, scope, query, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def dense_search(self, vector: list[float], tenant: str, scope: str, limit: int) -> list[dict]:
        encoded = json.dumps(vector)
        rows = self.conn.execute(
            """SELECT c.id, c.parent_id, c.document_id, c.version_id, c.text,
                      c.contextual_text, c.embedding::text AS embedding,
                      v.filename, 1 - (c.embedding <=> %s::vector) AS similarity
               FROM children c
               JOIN document_versions v ON v.id=c.version_id
               JOIN documents d ON d.id=c.document_id AND d.active_version_id=c.version_id
               WHERE d.tenant=%s AND d.scope=%s
               ORDER BY c.embedding <=> %s::vector LIMIT %s""",
            (encoded, tenant, scope, encoded, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_parents(self, ids: list[str], tenant: str, scope: str) -> dict[str, dict]:
        if not ids:
            return {}
        rows = self.conn.execute(
            """SELECT p.id, p.heading, p.text, p.document_id, p.version_id,
                      v.filename
               FROM parents p
               JOIN document_versions v ON v.id=p.version_id
               JOIN documents d ON d.id=p.document_id AND d.active_version_id=p.version_id
               WHERE p.id = ANY(%s) AND d.tenant=%s AND d.scope=%s""",
            (ids, tenant, scope),
        ).fetchall()
        return {row["id"]: dict(row) for row in rows}


class PostgresDatabase:
    def __init__(self, settings: Settings):
        self.settings = settings
        if not settings.database_url:
            raise ValueError("RAG_DATABASE_URL is required for PostgreSQL")

    @contextmanager
    def connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL needs: pip install -e '.[postgres]'") from exc
        conn = psycopg.connect(self.settings.database_url, row_factory=dict_row)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        dimensions = self.settings.embedding_dimensions
        if not 1 <= dimensions <= 2000:
            raise ValueError("RAG_EMBEDDING_DIMENSIONS must be between 1 and 2000")
        self.settings.objects_dir.mkdir(parents=True, exist_ok=True)
        signature = (
            "hash-v1" if self.settings.embedder == "hash"
            else f"sentence-transformers:{self.settings.embedding_model}"
        )
        with self.connect() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute("CREATE TABLE IF NOT EXISTS app_metadata(key text PRIMARY KEY, value text NOT NULL)")
            expected = f"{signature}:{dimensions}"
            row = conn.execute(
                """INSERT INTO app_metadata(key,value)
                   VALUES ('embedding_signature',%s)
                   ON CONFLICT (key) DO UPDATE SET value=app_metadata.value
                   WHERE app_metadata.value=EXCLUDED.value
                   RETURNING value""",
                (expected,),
            ).fetchone()
            if not row:
                actual = conn.execute(
                    "SELECT value FROM app_metadata WHERE key='embedding_signature'"
                ).fetchone()["value"]
                raise RuntimeError(
                    f"PostgreSQL index uses {actual}; configured model is {expected}. "
                    "Re-embed the corpus before changing the model or dimensions."
                )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS documents (
                    id text PRIMARY KEY,
                    tenant text NOT NULL,
                    scope text NOT NULL,
                    active_version_id text,
                    latest_version_id text,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS documents_scope_idx ON documents(tenant,scope)")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS document_versions (
                    id text PRIMARY KEY,
                    document_id text NOT NULL REFERENCES documents(id),
                    version_number integer NOT NULL,
                    filename text NOT NULL,
                    sha256 text NOT NULL,
                    object_path text NOT NULL,
                    status text NOT NULL CHECK(status IN
                        ('queued','processing','ready','failed','superseded')),
                    error text,
                    trace_id text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    published_at timestamptz,
                    UNIQUE(document_id,version_number),
                    UNIQUE(document_id,id)
                )"""
            )
            conn.execute(
                """DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='documents_active_version_fk') THEN
                        ALTER TABLE documents ADD CONSTRAINT documents_active_version_fk
                        FOREIGN KEY(id,active_version_id)
                        REFERENCES document_versions(document_id,id) DEFERRABLE INITIALLY DEFERRED;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='documents_latest_version_fk') THEN
                        ALTER TABLE documents ADD CONSTRAINT documents_latest_version_fk
                        FOREIGN KEY(id,latest_version_id)
                        REFERENCES document_versions(document_id,id) DEFERRABLE INITIALLY DEFERRED;
                    END IF;
                END $$"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    id text PRIMARY KEY,
                    version_id text NOT NULL UNIQUE REFERENCES document_versions(id),
                    status text NOT NULL,
                    attempts integer NOT NULL DEFAULT 0,
                    available_at timestamptz NOT NULL DEFAULT now(),
                    lease_until timestamptz,
                    error text,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs(status,available_at)")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS parents (
                    id text PRIMARY KEY,
                    document_id text NOT NULL REFERENCES documents(id),
                    version_id text NOT NULL REFERENCES document_versions(id),
                    ordinal integer NOT NULL,
                    heading text NOT NULL,
                    text text NOT NULL
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS parents_version_idx ON parents(version_id)")
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS children (
                    id text PRIMARY KEY,
                    document_id text NOT NULL REFERENCES documents(id),
                    version_id text NOT NULL REFERENCES document_versions(id),
                    parent_id text NOT NULL REFERENCES parents(id),
                    ordinal integer NOT NULL,
                    text text NOT NULL,
                    contextual_text text NOT NULL,
                    embedding vector({dimensions}) NOT NULL,
                    search_vector tsvector GENERATED ALWAYS AS
                        (to_tsvector('english'::regconfig, contextual_text)) STORED
                )"""
            )
            conn.execute(
                """DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='parents_doc_version_fk') THEN
                        ALTER TABLE parents ADD CONSTRAINT parents_doc_version_fk
                        FOREIGN KEY(document_id,version_id)
                        REFERENCES document_versions(document_id,id);
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='parents_id_version_key') THEN
                        ALTER TABLE parents ADD CONSTRAINT parents_id_version_key UNIQUE(id,version_id);
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='children_doc_version_fk') THEN
                        ALTER TABLE children ADD CONSTRAINT children_doc_version_fk
                        FOREIGN KEY(document_id,version_id)
                        REFERENCES document_versions(document_id,id);
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='children_parent_version_fk') THEN
                        ALTER TABLE children ADD CONSTRAINT children_parent_version_fk
                        FOREIGN KEY(parent_id,version_id) REFERENCES parents(id,version_id);
                    END IF;
                END $$"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS children_version_idx ON children(version_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS children_fts_idx ON children USING gin(search_vector)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS children_embedding_idx ON children USING hnsw(embedding vector_cosine_ops)"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS stage_events (
                    span_id text PRIMARY KEY,
                    trace_id text NOT NULL,
                    parent_span_id text,
                    tenant text NOT NULL,
                    scope text NOT NULL,
                    document_id text,
                    pipeline text NOT NULL,
                    stage text NOT NULL,
                    status text NOT NULL,
                    duration_ms double precision NOT NULL,
                    self_ms double precision NOT NULL,
                    error_type text,
                    details jsonb NOT NULL,
                    started_at timestamptz NOT NULL
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS stage_events_scope_time_idx ON stage_events(tenant,scope,started_at)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS stage_events_trace_idx ON stage_events(trace_id,tenant,scope)")

    def _existing_document(self, conn, tenant: str, scope: str, sha256: str) -> dict | None:
        row = conn.execute(
            """SELECT d.id, v.id AS version_id, v.status, v.version_number
               FROM documents d JOIN document_versions v
                 ON v.id IN (d.active_version_id,d.latest_version_id)
               WHERE d.tenant=%s AND d.scope=%s AND v.sha256=%s
                 AND v.status IN ('queued','processing','ready')
               ORDER BY v.created_at DESC LIMIT 1""",
            (tenant, scope, sha256),
        ).fetchone()
        return dict(row) if row else None

    def existing_document(self, tenant: str, scope: str, sha256: str) -> dict | None:
        with self.connect() as conn:
            return self._existing_document(conn, tenant, scope, sha256)

    def create_document(
        self, document_id: str, tenant: str, scope: str, filename: str,
        sha256: str, object_path: Path, trace_id: str,
    ) -> dict:
        version_id = uuid4().hex
        with self.connect() as conn:
            # Serializes simultaneous uploads of the same content in one scope.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (json.dumps([tenant, scope, sha256]),),
            )
            existing = self._existing_document(conn, tenant, scope, sha256)
            if existing:
                return {**existing, "deduplicated": True}
            conn.execute(
                "INSERT INTO documents(id,tenant,scope) VALUES (%s,%s,%s)",
                (document_id, tenant, scope),
            )
            conn.execute(
                """INSERT INTO document_versions
                   (id,document_id,version_number,filename,sha256,object_path,status,trace_id)
                   VALUES (%s,%s,1,%s,%s,%s,'queued',%s)""",
                (version_id, document_id, filename, sha256, str(object_path), trace_id),
            )
            conn.execute("UPDATE documents SET latest_version_id=%s WHERE id=%s", (version_id, document_id))
            conn.execute("INSERT INTO jobs(id,version_id,status) VALUES (%s,%s,'queued')", (version_id, version_id))
            return {"id": document_id, "version_id": version_id, "version_number": 1,
                    "status": "queued", "deduplicated": False}

    def create_version(
        self, document_id: str, tenant: str, scope: str, filename: str,
        sha256: str, object_path: Path, trace_id: str,
    ) -> dict | None:
        version_id = uuid4().hex
        with self.connect() as conn:
            document = conn.execute(
                "SELECT * FROM documents WHERE id=%s AND tenant=%s AND scope=%s FOR UPDATE",
                (document_id, tenant, scope),
            ).fetchone()
            if not document:
                return None
            latest = conn.execute(
                "SELECT id,version_number,sha256,status FROM document_versions WHERE id=%s",
                (document["latest_version_id"],),
            ).fetchone()
            if latest and latest["sha256"] == sha256 and latest["status"] != "failed":
                return {"id": document_id, "version_id": latest["id"],
                        "version_number": latest["version_number"], "status": latest["status"],
                        "deduplicated": True}
            version_number = (latest["version_number"] if latest else 0) + 1
            conn.execute(
                """INSERT INTO document_versions
                   (id,document_id,version_number,filename,sha256,object_path,status,trace_id)
                   VALUES (%s,%s,%s,%s,%s,%s,'queued',%s)""",
                (version_id, document_id, version_number, filename, sha256,
                 str(object_path), trace_id),
            )
            conn.execute(
                "UPDATE documents SET latest_version_id=%s,updated_at=now() WHERE id=%s",
                (version_id, document_id),
            )
            conn.execute("INSERT INTO jobs(id,version_id,status) VALUES (%s,%s,'queued')", (version_id, version_id))
            return {"id": document_id, "version_id": version_id,
                    "version_number": version_number, "status": "queued", "deduplicated": False}

    def get_document(self, document_id: str, tenant: str, scopes: frozenset[str]) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT d.id,d.scope,d.active_version_id,d.latest_version_id,
                          v.filename,v.sha256,v.status,v.error,v.trace_id,v.version_number,
                          d.created_at,d.updated_at,j.attempts
                   FROM documents d JOIN document_versions v ON v.id=d.latest_version_id
                   JOIN jobs j ON j.version_id=v.id
                   WHERE d.id=%s AND d.tenant=%s""",
                (document_id, tenant),
            ).fetchone()
            if not row or row["scope"] not in scopes:
                return None
            return self._serialize(row)

    def get_version_history(self, document_id: str, tenant: str,
                            scopes: frozenset[str]) -> list[dict] | None:
        with self.connect() as conn:
            document = conn.execute(
                "SELECT id,scope,active_version_id FROM documents WHERE id=%s AND tenant=%s",
                (document_id, tenant),
            ).fetchone()
            if not document or document["scope"] not in scopes:
                return None
            rows = conn.execute(
                """SELECT id AS version_id,version_number,filename,sha256,status,error,
                          trace_id,created_at,published_at
                   FROM document_versions WHERE document_id=%s ORDER BY version_number DESC""",
                (document_id,),
            ).fetchall()
            return [{**self._serialize(row), "active": row["version_id"] == document["active_version_id"]}
                    for row in rows]

    def retry_document(self, document_id: str, tenant: str, scopes: frozenset[str]) -> bool:
        with self.connect() as conn:
            document = conn.execute(
                "SELECT scope,latest_version_id FROM documents WHERE id=%s AND tenant=%s",
                (document_id, tenant),
            ).fetchone()
            if not document or document["scope"] not in scopes:
                return False
            version_id = document["latest_version_id"]
            conn.execute("SELECT id FROM jobs WHERE version_id=%s FOR UPDATE", (version_id,))
            version = conn.execute(
                "SELECT status FROM document_versions WHERE id=%s FOR UPDATE",
                (version_id,),
            ).fetchone()
            current = conn.execute(
                "SELECT latest_version_id FROM documents WHERE id=%s FOR UPDATE", (document_id,)
            ).fetchone()
            if not version or version["status"] != "failed" or current["latest_version_id"] != version_id:
                return False
            conn.execute(
                """UPDATE document_versions SET status='queued',error=NULL,updated_at=now()
                   WHERE id=%s AND status='failed'""",
                (version_id,),
            )
            conn.execute(
                """UPDATE jobs SET status='queued',attempts=0,error=NULL,
                   lease_until=NULL,available_at=now(),updated_at=now()
                   WHERE version_id=%s""",
                (version_id,),
            )
            return True

    def claim_job(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT j.id,j.version_id,v.document_id,v.filename,v.object_path,
                          v.trace_id,d.tenant,d.scope
                   FROM jobs j JOIN document_versions v ON v.id=j.version_id
                   JOIN documents d ON d.id=v.document_id
                   WHERE (j.status='queued' AND j.available_at<=now())
                      OR (j.status='running' AND j.lease_until<now())
                   ORDER BY j.available_at
                   FOR UPDATE OF j SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """UPDATE jobs SET status='running',attempts=attempts+1,
                   lease_until=now()+interval '30 minutes',updated_at=now() WHERE id=%s""",
                (row["id"],),
            )
            conn.execute(
                "UPDATE document_versions SET status='processing',updated_at=now() WHERE id=%s",
                (row["version_id"],),
            )
            return dict(row)

    def complete_job(self, version_id: str, parents: list[dict], children: list[dict]) -> None:
        if not children:
            raise ValueError("Cannot publish a version without child chunks")
        with self.connect() as conn:
            conn.execute("SELECT id FROM jobs WHERE version_id=%s FOR UPDATE", (version_id,))
            version = conn.execute(
                """SELECT v.document_id,v.status,d.latest_version_id
                   FROM document_versions v JOIN documents d ON d.id=v.document_id
                   WHERE v.id=%s FOR UPDATE OF v,d""",
                (version_id,),
            ).fetchone()
            if not version:
                raise ValueError("Version not found")
            if version["status"] in ("ready", "superseded"):
                return
            document_id = version["document_id"]
            conn.execute("DELETE FROM children WHERE version_id=%s", (version_id,))
            conn.execute("DELETE FROM parents WHERE version_id=%s", (version_id,))
            with conn.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO parents(id,document_id,version_id,ordinal,heading,text)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    [(p["id"], document_id, version_id, p["ordinal"], p["heading"], p["text"])
                     for p in parents],
                )
                cursor.executemany(
                    """INSERT INTO children(id,document_id,version_id,parent_id,ordinal,
                       text,contextual_text,embedding)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector)""",
                    [(c["id"], document_id, version_id, c["parent_id"], c["ordinal"],
                      c["text"], c["contextual_text"], json.dumps(c["embedding"]))
                     for c in children],
                )
            publish = version["latest_version_id"] == version_id
            conn.execute(
                """UPDATE document_versions SET status=%s,error=NULL,
                   published_at=CASE WHEN %s THEN now() ELSE NULL END,updated_at=now()
                   WHERE id=%s""",
                ("ready" if publish else "superseded", publish, version_id),
            )
            if publish:
                conn.execute(
                    "UPDATE documents SET active_version_id=%s,updated_at=now() WHERE id=%s",
                    (version_id, document_id),
                )
            conn.execute(
                "UPDATE jobs SET status='done',lease_until=NULL,error=NULL,updated_at=now() WHERE version_id=%s",
                (version_id,),
            )

    def fail_job(self, version_id: str, error: str) -> None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT j.attempts,v.status AS version_status
                   FROM jobs j JOIN document_versions v ON v.id=j.version_id
                   WHERE j.version_id=%s FOR UPDATE OF j,v""",
                (version_id,),
            ).fetchone()
            if row and row["version_status"] in ("ready", "superseded"):
                return
            attempts = row["attempts"] if row else self.settings.max_attempts
            exhausted = attempts >= self.settings.max_attempts
            status = "failed" if exhausted else "queued"
            conn.execute(
                """UPDATE jobs SET status=%s,error=%s,lease_until=NULL,
                   available_at=now()+(%s * interval '1 second'),updated_at=now()
                   WHERE version_id=%s""",
                (status, error[:1000], min(300, 2 ** attempts), version_id),
            )
            conn.execute(
                "UPDATE document_versions SET status=%s,error=%s,updated_at=now() WHERE id=%s",
                (status, error[:1000], version_id),
            )

    @contextmanager
    def search_session(self, tenant: str, scope: str):
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            # Filtered ANN search must continue scanning past other tenants.
            conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            yield PostgresSearchSession(conn)

    def record_stage(self, event: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO stage_events
                   (span_id,trace_id,parent_span_id,tenant,scope,document_id,pipeline,
                    stage,status,duration_ms,self_ms,error_type,details,started_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                (event["span_id"], event["trace_id"], event["parent_span_id"],
                 event["tenant"], event["scope"], event["document_id"], event["pipeline"],
                 event["stage"], event["status"], event["duration_ms"], event["self_ms"],
                 event["error_type"], json.dumps(event["details"]), event["started_at"]),
            )

    def get_trace(self, trace_id: str, tenant: str, scopes: frozenset[str]) -> list[dict]:
        if not scopes:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT span_id,trace_id,parent_span_id,scope,document_id,pipeline,
                          stage,status,duration_ms,self_ms,error_type,details,started_at
                   FROM stage_events WHERE trace_id=%s AND tenant=%s AND scope=ANY(%s)
                   ORDER BY started_at,span_id""",
                (trace_id, tenant, sorted(scopes)),
            ).fetchall()
            return [self._serialize(row) for row in rows]

    def stage_summary(self, tenant: str, scope: str, hours: int) -> list[dict]:
        since = _now() - timedelta(hours=hours)
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT pipeline,stage,count(*) AS count,
                          count(*) FILTER (WHERE status='error') AS failures,
                          sum(duration_ms) AS total_ms,avg(duration_ms) AS avg_ms,
                          sum(self_ms) AS self_total_ms,avg(self_ms) AS self_avg_ms,
                          percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms,
                          max(duration_ms) AS max_ms
                   FROM stage_events WHERE tenant=%s AND scope=%s AND started_at>=%s
                   GROUP BY pipeline,stage ORDER BY self_total_ms DESC""",
                (tenant, scope, since),
            ).fetchall()
            return [
                {key: round(value, 3) if key.endswith("_ms") else value
                 for key, value in row.items()}
                for row in rows
            ]

    @staticmethod
    def _serialize(row: dict) -> dict:
        return {key: value.isoformat() if isinstance(value, datetime) else value
                for key, value in dict(row).items()}
