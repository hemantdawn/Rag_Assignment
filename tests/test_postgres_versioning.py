from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.migrate_sqlite import migrate
from app.postgres_db import PostgresDatabase
from app.processing import make_chunks, parse_document
from app.worker import run_once


@pytest.fixture
def pg_tenant():
    tenant = f"test-{uuid4().hex}"
    yield tenant
    if not os.getenv("RAG_TEST_DATABASE_URL"):
        return
    import psycopg

    with psycopg.connect(os.environ["RAG_TEST_DATABASE_URL"]) as conn:
        ids = conn.execute("SELECT id FROM documents WHERE tenant=%s", (tenant,)).fetchall()
        document_ids = [row[0] for row in ids]
        if document_ids:
            conn.execute("DELETE FROM children WHERE document_id=ANY(%s)", (document_ids,))
            conn.execute("DELETE FROM parents WHERE document_id=ANY(%s)", (document_ids,))
            conn.execute(
                "DELETE FROM jobs WHERE version_id IN "
                "(SELECT id FROM document_versions WHERE document_id=ANY(%s))",
                (document_ids,),
            )
            conn.execute(
                "UPDATE documents SET active_version_id=NULL,latest_version_id=NULL WHERE id=ANY(%s)",
                (document_ids,),
            )
            conn.execute("DELETE FROM document_versions WHERE document_id=ANY(%s)", (document_ids,))
            conn.execute("DELETE FROM documents WHERE id=ANY(%s)", (document_ids,))
        conn.execute("DELETE FROM stage_events WHERE tenant=%s", (tenant,))


@pytest.mark.skipif(not os.getenv("RAG_TEST_DATABASE_URL"), reason="Set RAG_TEST_DATABASE_URL to a disposable pgvector database")
def test_postgres_atomic_version_publish(tmp_path, monkeypatch, pg_tenant):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    tenant = pg_tenant
    settings = Settings(
        data_dir=tmp_path,
        api_keys={"owner-key": {"tenant": tenant, "scopes": ["hr"]},
                  "outsider-key": {"tenant": f"other-{tenant}", "scopes": ["hr"]}},
        database_url=os.environ["RAG_TEST_DATABASE_URL"],
        max_attempts=1,
    )
    app = create_app(settings)
    client = TestClient(app)
    owner = {"X-API-Key": "owner-key"}
    outsider = {"X-API-Key": "outsider-key"}

    def upload(path: str, content: bytes):
        return client.post(
            path, headers=owner, data={"scope": "hr"},
            files={"file": ("refunds.md", content)},
        )

    def ask():
        return client.post(
            "/v1/query/sync", headers=owner,
            json={"scope": "hr", "question": "What is the refund period?"},
        ).json()

    first = upload("/v1/documents", b"# Refunds\nCustomers may request a refund within 30 days.")
    assert first.status_code == 202
    document_id = first.json()["id"]
    version_one = first.json()["version_id"]
    assert run_once(app.state.db, app.state.engine.embedder)
    assert client.get(f"/v1/documents/{document_id}", headers=owner).json()["active_version_id"] == version_one
    assert "30 days" in ask()["answer"]
    assert client.get(f"/v1/documents/{document_id}", headers=outsider).status_code == 404
    assert client.post(
        "/v1/query/sync", headers=outsider,
        json={"scope": "hr", "question": "What is the refund period?"},
    ).json()["status"] == "abstained"

    second = upload(
        f"/v1/documents/{document_id}/versions",
        b"# Refunds\nCustomers may request a refund within 45 days.",
    )
    assert second.status_code == 202
    version_two = second.json()["version_id"]
    pending = client.get(f"/v1/documents/{document_id}", headers=owner).json()
    assert pending["status"] == "queued"
    assert pending["active_version_id"] == version_one
    assert "30 days" in ask()["answer"]
    assert run_once(app.state.db, app.state.engine.embedder)
    published = client.get(f"/v1/documents/{document_id}", headers=owner).json()
    assert published["active_version_id"] == version_two
    result = ask()
    assert "45 days" in result["answer"]
    assert "30 days" not in result["answer"]
    assert result["citations"][0]["version_id"] == version_two

    third = upload(
        f"/v1/documents/{document_id}/versions",
        b"# Refunds\nCustomers may request a refund within 60 days.",
    )
    version_three = third.json()["version_id"]

    class WrongDimensionEmbedder:
        def embed(self, texts):
            return [[0.0, 0.0, 1.0] for _ in texts]

    assert run_once(app.state.db, WrongDimensionEmbedder())
    failed = client.get(f"/v1/documents/{document_id}", headers=owner).json()
    assert failed["status"] == "failed"
    assert failed["active_version_id"] == version_two
    assert "45 days" in ask()["answer"]
    with app.state.db.connect() as conn:
        parent_count = conn.execute(
            "SELECT count(*) AS n FROM parents WHERE version_id=%s", (version_three,)
        ).fetchone()["n"]
        child_count = conn.execute(
            "SELECT count(*) AS n FROM children WHERE version_id=%s", (version_three,)
        ).fetchone()["n"]
    assert (parent_count, child_count) == (0, 0)

    history = client.get(f"/v1/documents/{document_id}/versions", headers=owner).json()["versions"]
    assert [item["version_number"] for item in history] == [3, 2, 1]
    assert [item["active"] for item in history] == [False, True, False]
    assert client.get(
        f"/v1/documents/{document_id}/versions", headers=outsider,
    ).status_code == 404

    fourth = upload(
        f"/v1/documents/{document_id}/versions",
        b"# Refunds\nCustomers may request a refund within 60 days.",
    ).json()
    fifth = upload(
        f"/v1/documents/{document_id}/versions",
        b"# Refunds\nCustomers may request a refund within 90 days.",
    ).json()
    older_job = app.state.db.claim_job()
    newer_job = app.state.db.claim_job()
    assert older_job["version_id"] == fourth["version_id"]
    assert newer_job["version_id"] == fifth["version_id"]

    def complete(job):
        markdown = parse_document(Path(job["object_path"]), job["filename"])
        parents, children = make_chunks(markdown, job["filename"])
        vectors = app.state.engine.embedder.embed(
            [child["contextual_text"] for child in children]
        )
        for child, vector in zip(children, vectors, strict=True):
            child["embedding"] = vector
        app.state.db.complete_job(job["version_id"], parents, children)

    # A faster newer job wins even if an older worker finishes afterward.
    complete(newer_job)
    complete(older_job)
    assert client.get(f"/v1/documents/{document_id}", headers=owner).json()["active_version_id"] == fifth["version_id"]
    assert "90 days" in ask()["answer"]
    final_history = client.get(f"/v1/documents/{document_id}/versions", headers=owner).json()["versions"]
    assert final_history[0]["active"] is True
    assert final_history[1]["status"] == "superseded"

    sixth = upload(
        f"/v1/documents/{document_id}/versions",
        b"# Refunds\nCustomers may request a refund within 120 days.",
    ).json()
    last_job = app.state.db.claim_job()
    assert last_job["version_id"] == sixth["version_id"]
    with app.state.db.search_session(tenant, "hr") as search:
        before = search.lexical_search('"refund" OR "period"', tenant, "hr", 10)
        assert before and all(row["version_id"] == fifth["version_id"] for row in before)
        complete(last_job)
        during = search.lexical_search('"refund" OR "period"', tenant, "hr", 10)
        assert during and all(row["version_id"] == fifth["version_id"] for row in during)
        assert search.get_parents([during[0]["parent_id"]], tenant, "hr")
    assert "120 days" in ask()["answer"]


@pytest.mark.skipif(not os.getenv("RAG_TEST_DATABASE_URL"), reason="Set RAG_TEST_DATABASE_URL to a disposable pgvector database")
def test_ready_sqlite_document_migrates_and_reindexes(tmp_path, monkeypatch, pg_tenant):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    tenant = pg_tenant
    keys = {"key": {"tenant": tenant, "scopes": ["policies"]}}
    source_settings = Settings(data_dir=tmp_path / "sqlite", api_keys=keys)
    source_app = create_app(source_settings)
    source_client = TestClient(source_app)
    headers = {"X-API-Key": "key"}
    uploaded = source_client.post(
        "/v1/documents", headers=headers, data={"scope": "policies"},
        files={"file": ("policy.md", b"# Returns\nReturns are accepted within 14 days.")},
    ).json()
    assert run_once(source_app.state.db, source_app.state.engine.embedder)

    target_settings = Settings(
        data_dir=tmp_path / "postgres", api_keys=keys,
        database_url=os.environ["RAG_TEST_DATABASE_URL"],
    )
    target_db = PostgresDatabase(target_settings)
    target_db.initialize()
    assert migrate(source_settings.db_path, target_db)["queued"] == 1
    assert migrate(source_settings.db_path, target_db, dry_run=True)["already_present"] == 1
    target_app = create_app(target_settings)
    assert run_once(target_app.state.db, target_app.state.engine.embedder)
    target_client = TestClient(target_app)
    status = target_client.get(f"/v1/documents/{uploaded['id']}", headers=headers).json()
    assert status["status"] == "ready"
    assert status["active_version_id"]
    result = target_client.post(
        "/v1/query/sync", headers=headers,
        json={"scope": "policies", "question": "When are returns accepted?"},
    ).json()
    assert result["status"] == "answered"
    assert "14 days" in result["answer"]


@pytest.mark.skipif(not os.getenv("RAG_TEST_DATABASE_URL"), reason="Set RAG_TEST_DATABASE_URL to a disposable pgvector database")
def test_failed_latest_version_retries_without_losing_active_content(tmp_path, monkeypatch, pg_tenant):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    tenant = pg_tenant
    app = create_app(Settings(
        data_dir=tmp_path,
        api_keys={"key": {"tenant": tenant, "scopes": ["hr"]}},
        database_url=os.environ["RAG_TEST_DATABASE_URL"], max_attempts=1,
    ))
    client = TestClient(app)
    headers = {"X-API-Key": "key"}

    def upload(path, days):
        return client.post(
            path, headers=headers, data={"scope": "hr"},
            files={"file": ("policy.md", f"# Refunds\nRefunds are allowed within {days} days.".encode())},
        ).json()

    first = upload("/v1/documents", 30)
    assert run_once(app.state.db, app.state.engine.embedder)
    second = upload(f"/v1/documents/{first['id']}/versions", 45)

    class WrongDimensionEmbedder:
        def embed(self, texts):
            return [[1.0] for _ in texts]

    assert run_once(app.state.db, WrongDimensionEmbedder())
    failed = client.get(f"/v1/documents/{first['id']}", headers=headers).json()
    assert failed["status"] == "failed"
    assert failed["active_version_id"] == first["version_id"]

    retry = client.post(f"/v1/documents/{first['id']}/retry", headers=headers)
    assert retry.status_code == 202
    assert run_once(app.state.db, app.state.engine.embedder)
    ready = client.get(f"/v1/documents/{first['id']}", headers=headers).json()
    assert ready["active_version_id"] == second["version_id"]
    answer = client.post(
        "/v1/query/sync", headers=headers,
        json={"scope": "hr", "question": "What is the refund period?"},
    ).json()
    assert "45 days" in answer["answer"]
