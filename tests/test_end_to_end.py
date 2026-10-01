from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.worker import run_once


def test_upload_worker_query_and_dedup(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = Settings(
        data_dir=tmp_path,
        api_keys={
            "alpha-key": {"tenant": "alpha", "scopes": ["hr"]},
            "beta-key": {"tenant": "beta", "scopes": ["hr"]},
        },
    )
    app = create_app(settings)
    client = TestClient(app)
    alpha = {"X-API-Key": "alpha-key"}
    beta = {"X-API-Key": "beta-key"}

    assert client.get("/v1/documents/missing").status_code == 401
    assert client.post(
        "/v1/documents", headers=alpha, data={"scope": "other"},
        files={"file": ("rules.md", b"# Refunds\nRefunds are allowed within 30 days.")},
    ).status_code == 403

    uploaded = client.post(
        "/v1/documents", headers=alpha, data={"scope": "hr"},
        files={"file": ("rules.md", b"# Refunds\nRefunds are allowed within 30 days.")},
    )
    assert uploaded.status_code == 202
    document_id = uploaded.json()["id"]
    ingestion_trace_id = uploaded.json()["trace_id"]
    assert uploaded.headers["X-Trace-ID"] == ingestion_trace_id
    assert uploaded.json()["status"] == "queued"
    assert client.get(f"/v1/documents/{document_id}", headers=beta).status_code == 404

    duplicate = client.post(
        "/v1/documents", headers=alpha, data={"scope": "hr"},
        files={"file": ("copy.md", b"# Refunds\nRefunds are allowed within 30 days.")},
    )
    assert duplicate.json()["id"] == document_id
    assert duplicate.json()["deduplicated"] is True

    assert run_once(app.state.db, app.state.engine.embedder)
    document = client.get(f"/v1/documents/{document_id}", headers=alpha).json()
    assert document["status"] == "ready"
    assert document["trace_id"] == ingestion_trace_id
    ingestion_spans = client.get(
        f"/v1/observability/traces/{ingestion_trace_id}", headers=alpha,
    ).json()["spans"]
    assert {"store_upload", "deduplicate_upload", "parse_document", "chunk_document",
            "embed_children", "index_document", "ingestion_total"} <= {
                span["stage"] for span in ingestion_spans
            }
    assert all(span["duration_ms"] >= 0 for span in ingestion_spans)
    assert all(0 <= span["self_ms"] <= span["duration_ms"] + 0.001
               for span in ingestion_spans)
    assert client.get(
        f"/v1/observability/traces/{ingestion_trace_id}", headers=beta,
    ).status_code == 404

    answer_response = client.post(
        "/v1/query/sync", headers=alpha,
        json={"scope": "hr", "question": "What is the refund period?"},
    )
    answer = answer_response.json()
    assert answer_response.headers["X-Trace-ID"] == answer["trace_id"]
    assert answer["status"] == "answered"
    assert "30 days" in answer["answer"]
    assert "[1]" in answer["answer"]
    assert answer["citations"][0]["document_id"] == document_id
    assert "30 days" in answer["citations"][0]["excerpt"]
    assert app.state.engine._grounded(
        "Refunds are allowed within 60 days [1].",
        [{"citation": 1, "parent_text": "Refunds are allowed within 30 days."}],
    ) is False
    query_spans = client.get(
        f"/v1/observability/traces/{answer['trace_id']}", headers=alpha,
    ).json()["spans"]
    assert {"lexical_search", "dense_search", "rrf_fusion", "rerank", "relevance_gate",
            "parent_expansion", "evidence_sufficiency", "citation_binding", "query_total"} <= {
                span["stage"] for span in query_spans
            }
    assert any(span["parent_span_id"] for span in query_spans)
    summary = client.get("/v1/observability/stages?scope=hr", headers=alpha).json()["stages"]
    assert any(stage["stage"] == "parse_document" and stage["count"] == 1 for stage in summary)
    assert any(stage["stage"] == "query_total" for stage in summary)
    assert all(stage["self_total_ms"] <= stage["total_ms"] + 0.001 for stage in summary)
    assert client.get("/v1/observability/stages?scope=other", headers=alpha).status_code == 403

    isolated = client.post(
        "/v1/query/sync", headers=beta,
        json={"scope": "hr", "question": "What is the refund period?"},
    ).json()
    assert isolated["status"] == "abstained"
    unknown = client.post(
        "/v1/query/sync", headers=alpha,
        json={"scope": "hr", "question": "What is the lunar launch schedule?"},
    ).json()
    assert unknown["status"] == "abstained"

    stream = client.post(
        "/v1/query", headers=alpha,
        json={"scope": "hr", "question": "What is the refund period?"},
    )
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: result" in stream.text
    assert "event: done" in stream.text


def test_worker_failure_is_visible_in_trace_and_summary(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = create_app(Settings(
        data_dir=tmp_path,
        api_keys={"key": {"tenant": "alpha", "scopes": ["hr"]}},
        max_attempts=1,
    ))
    client = TestClient(app)
    headers = {"X-API-Key": "key"}
    upload = client.post(
        "/v1/documents", headers=headers, data={"scope": "hr"},
        files={"file": ("broken.txt", b"\xff\xfe\xfa")},
    ).json()
    assert run_once(app.state.db, app.state.engine.embedder)
    status = client.get(f"/v1/documents/{upload['id']}", headers=headers).json()
    assert status["status"] == "failed"
    spans = client.get(
        f"/v1/observability/traces/{upload['trace_id']}", headers=headers,
    ).json()["spans"]
    assert any(span["stage"] == "parse_document" and span["status"] == "error"
               and span["error_type"] == "UnicodeDecodeError" for span in spans)
    assert any(span["stage"] == "ingestion_total" and span["status"] == "error"
               for span in spans)
    summary = client.get("/v1/observability/stages?scope=hr", headers=headers).json()["stages"]
    assert any(stage["stage"] == "parse_document" and stage["failures"] == 1
               for stage in summary)
